"""Small RESP2 Redis adapter for the versioned cache boundary.

The adapter uses short-lived connections and only Redis' stable string commands.
It has no local-cache fallback: transport and server failures raise
``CacheUnavailable`` for the HTTP layer to handle explicitly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import json
import math
import random
import re
import socket
import ssl
from typing import TypeAlias, TypeVar

from ..errors import CacheUnavailable
from .port import CacheSnapshot

_RESPLine: TypeAlias = bytes
_MAX_LINE_BYTES = 1_048_576
_MAX_BULK_BYTES = 8 * 1024 * 1024
_MAX_ARRAY_ITEMS = 1_024
_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]{1,80}\Z")
_Result = TypeVar("_Result")


class _RedisServerError(Exception):
    """A Redis error response, intentionally kept free of server text."""


class _RedisProtocolError(Exception):
    """An invalid or unsupported RESP2 response."""


class _RedisSession:
    def __init__(self, connection: socket.socket, reader) -> None:
        self.connection = connection
        self.reader = reader

    def command(self, *parts: str | bytes | int) -> object:
        self.connection.sendall(_encode_command(parts))
        return _read_response(self.reader)


def _encode_command(parts: tuple[str | bytes | int, ...]) -> bytes:
    encoded: list[bytes] = []
    for part in parts:
        if isinstance(part, bytes):
            value = part
        else:
            value = str(part).encode("utf-8")
        encoded.append(value)
    chunks = [f"*{len(encoded)}\r\n".encode("ascii")]
    for value in encoded:
        chunks.extend((f"${len(value)}\r\n".encode("ascii"), value, b"\r\n"))
    return b"".join(chunks)


def _read_line(reader) -> _RESPLine:
    line = reader.readline(_MAX_LINE_BYTES + 1)
    if len(line) > _MAX_LINE_BYTES or not line.endswith(b"\r\n"):
        raise _RedisProtocolError("malformed RESP line")
    return line[:-2]


def _read_response(reader, *, depth: int = 0) -> object:
    if depth > 8:
        raise _RedisProtocolError("RESP nesting limit exceeded")
    prefix = reader.read(1)
    if prefix == b"":
        raise _RedisProtocolError("Redis closed the connection")
    payload = _read_line(reader)
    if prefix == b"+":
        return payload
    if prefix == b"-":
        raise _RedisServerError()
    if prefix == b":":
        try:
            return int(payload)
        except ValueError as exc:
            raise _RedisProtocolError("invalid RESP integer") from exc
    if prefix == b"$":
        try:
            length = int(payload)
        except ValueError as exc:
            raise _RedisProtocolError("invalid RESP bulk length") from exc
        if length == -1:
            return None
        if length < 0 or length > _MAX_BULK_BYTES:
            raise _RedisProtocolError("RESP bulk length is outside the accepted limit")
        value = reader.read(length + 2)
        if len(value) != length + 2 or not value.endswith(b"\r\n"):
            raise _RedisProtocolError("truncated RESP bulk value")
        return value[:-2]
    if prefix == b"*":
        try:
            count = int(payload)
        except ValueError as exc:
            raise _RedisProtocolError("invalid RESP array length") from exc
        if count == -1:
            return None
        if count < 0 or count > _MAX_ARRAY_ITEMS:
            raise _RedisProtocolError("RESP array length is outside the accepted limit")
        return [_read_response(reader, depth=depth + 1) for _ in range(count)]
    raise _RedisProtocolError("unsupported RESP response type")


class RedisVersionedTTLCache:
    """Redis-backed CachePort with shared namespace generations and TTL jitter.

    A cache key includes the generation observed before its database read. If a
    write invalidates the namespace while that read is in flight, a subsequent
    fill remains under the old generation and is invisible to later readers.
    The adapter targets one Redis endpoint; it does not implement Cluster MOVED
    handling or silently substitute a process-local cache.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        *,
        database: int = 0,
        username: str | None = None,
        password: str | None = None,
        key_prefix: str = "content-platform",
        connect_timeout_seconds: float = 0.5,
        socket_timeout_seconds: float = 0.5,
        ttl_jitter_fraction: float = 0.1,
        random_source: Callable[[], float] = random.random,
        tls: bool = False,
    ) -> None:
        if not isinstance(host, str) or not host.strip() or "\x00" in host:
            raise ValueError("Redis host must be a non-empty hostname")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
            raise ValueError("Redis port must be an integer from 1 to 65535")
        if not isinstance(database, int) or isinstance(database, bool) or database < 0:
            raise ValueError("Redis database index must be a non-negative integer")
        if not _PREFIX_PATTERN.fullmatch(key_prefix):
            raise ValueError("Redis key prefix must use 1 to 80 letters, digits, colon, underscore, or hyphen")
        if not math.isfinite(connect_timeout_seconds) or connect_timeout_seconds <= 0:
            raise ValueError("Redis connect timeout must be positive and finite")
        if not math.isfinite(socket_timeout_seconds) or socket_timeout_seconds <= 0:
            raise ValueError("Redis socket timeout must be positive and finite")
        if not math.isfinite(ttl_jitter_fraction) or not 0 <= ttl_jitter_fraction < 1:
            raise ValueError("TTL jitter fraction must be from 0 inclusive to 1 exclusive")
        if username is not None and not password:
            raise ValueError("Redis ACL username requires a password")
        if password is not None and not password:
            raise ValueError("Redis password cannot be empty")

        self.host = host.strip()
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.key_prefix = key_prefix
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.socket_timeout_seconds = float(socket_timeout_seconds)
        self.ttl_jitter_fraction = float(ttl_jitter_fraction)
        self.random_source = random_source
        self._tls_context = ssl.create_default_context() if tls else None

    def ping(self) -> None:
        response = self._run(lambda session: session.command("PING"))
        if response != b"PONG":
            raise CacheUnavailable("Redis cache did not accept the health check")

    def get(self, namespace: str, key: str) -> object | None:
        return self.get_snapshot(namespace, key).value

    def get_snapshot(self, namespace: str, key: str) -> CacheSnapshot:
        self._validate_identity(namespace, key)
        generation_key = self._generation_key(namespace)

        def read(session: _RedisSession) -> CacheSnapshot:
            version = self._read_version(session, generation_key)
            redis_key = self._data_key(namespace, key, version)
            encoded_value = session.command("GET", redis_key)
            current_version = self._read_version(session, generation_key)
            if current_version != version:
                return CacheSnapshot(current_version, None)
            if encoded_value is None:
                return CacheSnapshot(version, None)
            if not isinstance(encoded_value, bytes):
                raise _RedisProtocolError("Redis returned a non-bulk cache value")
            try:
                value = json.loads(encoded_value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                session.command("DEL", redis_key)
                return CacheSnapshot(version, None)
            if value is None:
                session.command("DEL", redis_key)
                return CacheSnapshot(version, None)
            return CacheSnapshot(version, value)

        return self._run(read)

    def set(
        self,
        namespace: str,
        key: str,
        value: object,
        *,
        ttl_seconds: float,
    ) -> None:
        self._validate_value(namespace, key, value, ttl_seconds)
        generation_key = self._generation_key(namespace)
        version = self._run(lambda session: self._read_version(session, generation_key))
        self.set_if_current(
            namespace,
            key,
            value,
            ttl_seconds=ttl_seconds,
            expected_version=version,
        )

    def set_if_current(
        self,
        namespace: str,
        key: str,
        value: object,
        *,
        ttl_seconds: float,
        expected_version: str,
    ) -> bool:
        self._validate_value(namespace, key, value, ttl_seconds)
        if not isinstance(expected_version, str) or not expected_version.isdecimal():
            raise ValueError("expected cache version must be a decimal string")
        encoded_value = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        ttl_milliseconds = self._jittered_ttl_milliseconds(ttl_seconds)
        generation_key = self._generation_key(namespace)
        redis_key = self._data_key(namespace, key, expected_version)

        def write(session: _RedisSession) -> bool:
            current_version = self._read_version(session, generation_key)
            if current_version != expected_version:
                return False
            response = session.command("SET", redis_key, encoded_value, "PX", ttl_milliseconds)
            if response != b"OK":
                raise _RedisProtocolError("Redis did not accept the cache write")
            return True

        return self._run(write)

    def invalidate(self, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("cache namespace is required")
        response = self._run(lambda session: session.command("INCR", self._generation_key(namespace)))
        if not isinstance(response, int) or response < 1:
            raise CacheUnavailable("Redis cache returned an invalid namespace generation")

    def _jittered_ttl_milliseconds(self, ttl_seconds: float) -> int:
        if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool):
            raise ValueError("cache TTL must be a positive finite number")
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("cache TTL must be a positive finite number")
        sample = self.random_source()
        if not math.isfinite(sample) or not 0 <= sample <= 1:
            raise ValueError("random source must return a value from 0 to 1")
        factor = 1 + self.ttl_jitter_fraction * (2 * sample - 1)
        return max(1, math.ceil(ttl_seconds * factor * 1_000))

    @staticmethod
    def _validate_identity(namespace: str, key: str) -> None:
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("cache namespace is required")
        if not isinstance(key, str) or not key:
            raise ValueError("cache key is required")

    def _validate_value(self, namespace: str, key: str, value: object, ttl_seconds: float) -> None:
        self._validate_identity(namespace, key)
        if value is None:
            raise ValueError("None is reserved to mean a cache miss")
        if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool):
            raise ValueError("cache TTL must be a positive finite number")
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("cache TTL must be a positive finite number")

    def _generation_key(self, namespace: str) -> str:
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        return f"{self.key_prefix}:namespace:{digest}:version"

    def _data_key(self, namespace: str, key: str, version: str) -> str:
        namespace_digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{self.key_prefix}:namespace:{namespace_digest}:v:{version}:key:{key_digest}"

    @staticmethod
    def _read_version(session: _RedisSession, key: str) -> str:
        response = session.command("GET", key)
        if response is None:
            return "0"
        if not isinstance(response, bytes):
            raise _RedisProtocolError("Redis returned a non-bulk namespace generation")
        try:
            version = response.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _RedisProtocolError("Redis namespace generation is not ASCII") from exc
        if not version.isdecimal():
            raise _RedisProtocolError("Redis namespace generation is not a decimal integer")
        return version

    def _run(self, operation: Callable[[_RedisSession], _Result]) -> _Result:
        try:
            with self._session() as session:
                return operation(session)
        except (OSError, _RedisServerError, _RedisProtocolError):
            raise CacheUnavailable("Redis cache operation failed") from None

    @contextmanager
    def _session(self) -> Iterator[_RedisSession]:
        connection = socket.create_connection(
            (self.host, self.port), timeout=self.connect_timeout_seconds
        )
        try:
            if self._tls_context is not None:
                connection = self._tls_context.wrap_socket(
                    connection, server_hostname=self.host
                )
            connection.settimeout(self.socket_timeout_seconds)
            with connection.makefile("rb") as reader:
                session = _RedisSession(connection, reader)
                if self.password is not None:
                    if self.username is None:
                        response = session.command("AUTH", self.password)
                    else:
                        response = session.command("AUTH", self.username, self.password)
                    if response != b"OK":
                        raise _RedisProtocolError("Redis did not accept authentication")
                if self.database:
                    response = session.command("SELECT", self.database)
                    if response != b"OK":
                        raise _RedisProtocolError("Redis did not accept the database selection")
                yield session
        finally:
            try:
                connection.close()
            except OSError:
                pass
