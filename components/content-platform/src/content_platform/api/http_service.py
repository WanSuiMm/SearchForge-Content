"""Bounded stdlib HTTP transport for the ContentPlatform application."""

from __future__ import annotations

from collections.abc import Callable
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from ..application import ContentPlatformApplication
from ..errors import (
    AlreadyExists,
    AuthenticationFailed,
    CacheUnavailable,
    IdempotencyConflict,
    InvalidCursor,
    NotFound,
    ValidationError,
)
from ..jobs.worker import OutboxWorker
from ..projection import (
    DEFAULT_PROJECTION_CONSUMER_ID,
    ProjectionEventSource,
)
from ..storage.repositories import CacheEpochRepository
from ..telemetry.http_metrics import RequestMetrics

MAX_REQUEST_BYTES = 64 * 1024
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def read_static_asset(
    static_root: str | Path | None, request_path: str
) -> tuple[str, bytes] | None:
    """Read only the three bundled browser assets; never map arbitrary paths."""
    asset = STATIC_ASSETS.get(request_path)
    if static_root is None or asset is None:
        return None
    filename, content_type = asset
    try:
        payload = (Path(static_root) / filename).read_bytes()
    except OSError:
        return None
    return content_type, payload


class HTTPProblem(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class TokenBucketLimiter:
    """Process-local per-client token buckets with bounded key retention."""

    def __init__(
        self,
        *,
        capacity: float = 120,
        refill_per_second: float = 60,
        max_clients: int = 4_096,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0 or max_clients < 1:
            raise ValueError("rate limit settings must be positive")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._clients: dict[str, tuple[float, float, float]] = {}

    def take(self, client: str, *, now: float | None = None) -> tuple[bool, float]:
        current = time.monotonic() if now is None else now
        with self._lock:
            previous = self._clients.get(client)
            if previous is None:
                tokens = self.capacity
                updated = current
            else:
                old_tokens, old_updated, _ = previous
                tokens = min(
                    self.capacity,
                    old_tokens + max(0.0, current - old_updated) * self.refill_per_second,
                )
                updated = current
            if tokens >= 1.0:
                tokens -= 1.0
                allowed, retry_after = True, 0.0
            else:
                allowed = False
                retry_after = (1.0 - tokens) / self.refill_per_second
            self._clients[client] = (tokens, updated, current)
            if len(self._clients) > self.max_clients:
                oldest = min(self._clients, key=lambda key: self._clients[key][2])
                del self._clients[oldest]
            return allowed, retry_after


class ContentPlatformHTTPServer(ThreadingHTTPServer):
    """HTTP server with explicit request concurrency and worker lifecycle."""

    daemon_threads = False
    block_on_close = True
    request_queue_size = 64

    def __init__(
        self,
        server_address: tuple[str, int],
        application: ContentPlatformApplication,
        *,
        max_concurrent_requests: int = 32,
        rate_capacity: float = 120,
        rate_refill_per_second: float = 60,
        cache_ttl_seconds: float = 2.0,
        worker_poll_seconds: float = 0.1,
        static_root: str | Path | None = None,
        start_outbox_worker: bool = True,
        projection_token: str | None = None,
        projection_consumer_id: str = DEFAULT_PROJECTION_CONSUMER_ID,
    ) -> None:
        if not 1 <= max_concurrent_requests <= 256:
            raise ValueError("max_concurrent_requests must be from 1 to 256")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        if projection_token is not None and not projection_token:
            raise ValueError("projection_token must be non-empty when enabled")
        ProjectionEventSource.validate_consumer_id(projection_consumer_id)
        self.application = application
        self.cache_epochs = CacheEpochRepository(application.database)
        self.projection_events = ProjectionEventSource(application.database)
        self.projection_token = projection_token
        self.projection_consumer_id = projection_consumer_id
        self.metrics = RequestMetrics()
        self.rate_limiter = TokenBucketLimiter(
            capacity=rate_capacity,
            refill_per_second=rate_refill_per_second,
        )
        self.cache_ttl_seconds = cache_ttl_seconds
        self.static_root = Path(static_root).resolve() if static_root is not None else None
        self.start_outbox_worker = start_outbox_worker
        self._cache_guard = threading.RLock()
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self.worker = OutboxWorker(
            application.outbox,
            application.consumer,
            poll_interval_seconds=worker_poll_seconds,
        )
        super().__init__(server_address, ContentPlatformRequestHandler)

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            status = self._reject_over_capacity(request)
            if status == 503:
                self.metrics.increment("concurrency_rejections")
            self.metrics.record(status, 0.0)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    @staticmethod
    def _reject_over_capacity(request) -> int:
        """Drain a bounded HTTP request before closing so TCP does not reset 503."""
        response_status = 503
        body = b'{"error":{"code":"concurrency_limit","message":"server is at capacity"}}'
        request.settimeout(0.25)
        incoming = bytearray()
        marker = b"\r\n\r\n"
        try:
            while marker not in incoming and len(incoming) <= 16 * 1024:
                chunk = request.recv(2048)
                if not chunk:
                    break
                incoming.extend(chunk)
            header_end = incoming.find(marker)
            if header_end < 0 or header_end > 16 * 1024:
                response_status = 400
                body = b'{"error":{"code":"invalid_request","message":"request headers were not accepted"}}'
            else:
                header_lines = bytes(incoming[:header_end]).split(b"\r\n")[1:]
                headers: dict[bytes, bytes] = {}
                for line in header_lines:
                    name, separator, value = line.partition(b":")
                    if separator:
                        headers[name.strip().lower()] = value.strip()
                if b"transfer-encoding" in headers:
                    response_status = 400
                    body = b'{"error":{"code":"invalid_request","message":"transfer encoding is not supported"}}'
                else:
                    try:
                        content_length = int(headers.get(b"content-length", b"0"))
                    except ValueError:
                        content_length = -1
                    if content_length < 0:
                        response_status = 400
                        body = b'{"error":{"code":"invalid_request","message":"Content-Length is invalid"}}'
                    elif content_length > MAX_REQUEST_BYTES:
                        response_status = 413
                        body = b'{"error":{"code":"payload_too_large","message":"request body exceeds the configured limit"}}'
                    else:
                        received = max(0, len(incoming) - header_end - len(marker))
                        while received < content_length:
                            chunk = request.recv(min(4096, content_length - received))
                            if not chunk:
                                break
                            received += len(chunk)
            reasons = {
                400: b"Bad Request",
                413: b"Payload Too Large",
                503: b"Service Unavailable",
            }
            response = (
                b"HTTP/1.1 " + str(response_status).encode("ascii") + b" "
                + reasons[response_status] + b"\r\n"
                + b"Content-Type: application/json; charset=utf-8\r\n"
                + b"Cache-Control: no-store\r\n"
                + b"Connection: close\r\n"
                + b"Retry-After: 1\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            request.sendall(response)
        except OSError:
            return response_status
        return response_status

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if self.start_outbox_worker:
            self.worker.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            if self.start_outbox_worker:
                self.worker.stop()

    def server_close(self) -> None:
        try:
            if self.start_outbox_worker:
                self.worker.stop()
        finally:
            super().server_close()

    def handle_error(self, request, client_address) -> None:
        # Default traceback output may include request or database payload details.
        self.metrics.increment("handler_transport_errors")


class ContentPlatformRequestHandler(BaseHTTPRequestHandler):
    server: ContentPlatformHTTPServer
    protocol_version = "HTTP/1.0"
    server_version = "ContentPlatform/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # Authorization headers and request bodies must never enter access logs.
        return

    def do_GET(self) -> None:
        self._execute(self._get)

    def do_POST(self) -> None:
        self._execute(self._post)

    def do_DELETE(self) -> None:
        self._execute(self._delete)

    def do_PUT(self) -> None:
        self._execute(self._unsupported)

    def do_PATCH(self) -> None:
        self._execute(self._unsupported)

    def do_HEAD(self) -> None:
        self._execute(self._unsupported)

    def _execute(self, operation: Callable[[], None]) -> None:
        started = time.perf_counter()
        self._response_status: int | None = None
        try:
            try:
                path = urlsplit(self.path).path
            except ValueError as exc:
                raise HTTPProblem(400, "invalid_request", "request target was not accepted") from exc
            if path.startswith("/v1/"):
                allowed, retry_after = self.server.rate_limiter.take(
                    self.client_address[0]
                )
                if not allowed:
                    self._response_status = 429
                    self.send_response(429)
                    self.send_header("Retry-After", str(max(1, math.ceil(retry_after))))
                    payload = json.dumps(
                        {"error": {"code": "rate_limited", "message": "request rate limit exceeded"}},
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            operation()
        except HTTPProblem as exc:
            self._send_problem(exc.status, exc.code, exc.message)
        except AuthenticationFailed:
            self._send_problem(401, "unauthorized", "authentication was not accepted")
        except AlreadyExists:
            self._send_problem(409, "already_exists", "resource already exists")
        except IdempotencyConflict:
            self._send_problem(409, "idempotency_conflict", "idempotency key conflicts with an earlier request")
        except NotFound:
            self._send_problem(404, "not_found", "resource was not found")
        except (InvalidCursor, ValidationError):
            self._send_problem(400, "invalid_request", "request parameters were not accepted")
        except sqlite3.IntegrityError:
            self._send_problem(409, "conflict", "request conflicts with stored data")
        except sqlite3.OperationalError:
            self._send_problem(503, "storage_unavailable", "storage is temporarily unavailable")
        except (BrokenPipeError, ConnectionResetError):
            self._response_status = 499
        except Exception as exc:
            database = self.server.application.database
            if database.is_integrity_error(exc):
                self._send_problem(409, "conflict", "request conflicts with stored data")
            elif database.is_operational_error(exc):
                self._send_problem(503, "storage_unavailable", "storage is temporarily unavailable")
            else:
                self.server.metrics.increment("internal_errors")
                self._send_problem(500, "internal_error", "request could not be completed")
        finally:
            status = self._response_status or 500
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.server.metrics.record(status, duration_ms)

    def _get(self) -> None:
        path, query = self._path_query()
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/metrics":
            self._send_json(200, self._metrics_snapshot())
            return
        if path == "/internal/v1/content-projection" or path.startswith(
            "/internal/v1/content-projection/"
        ):
            self._get_projection(path, query)
            return
        if path == "/" or path.startswith("/assets/"):
            asset = read_static_asset(self.server.static_root, path)
            if asset is None:
                raise HTTPProblem(404, "not_found", "static resource was not found")
            content_type, payload = asset
            self._send_static(content_type, payload)
            return
        user_id = self._authenticated_user()
        if path == "/v1/me":
            self._require_no_query_fields(query)
            self._send_json(200, {"user_id": user_id})
            return
        if path == "/v1/feed":
            self._require_query_fields(query, {"limit", "cursor"})
            limit = self._query_limit(query, default=20)
            cursor = self._query_value(query, "cursor")
            key = self._cache_key_for("feed", [user_id, limit, cursor])
            response = self._cached("feed", key, lambda: self.server.application.api.get_feed(
                user_id, limit=limit, cursor=cursor
            ))
            self._send_json(200, response)
            return
        if path == "/v1/search":
            self._require_query_fields(query, {"q", "limit"})
            search_query = self._query_value(query, "q")
            if search_query is None:
                raise ValidationError("query is required")
            limit = self._query_limit(query, default=20)
            key = self._cache_key_for("search", [search_query, limit])
            response = self._cached(
                "search", key,
                lambda: self.server.application.api.search_content(search_query, limit=limit),
            )
            self._send_json(200, response)
            return
        if path == "/v1/notifications":
            self._require_query_fields(query, {"limit"})
            limit = self._query_limit(query, default=20)
            self._send_json(
                200,
                self.server.application.api.get_notifications(user_id, limit=limit),
            )
            return
        raise HTTPProblem(404, "not_found", "route was not found")

    def _post(self) -> None:
        path, query = self._path_query()
        if path == "/internal/v1/content-projection" or path.startswith(
            "/internal/v1/content-projection/"
        ):
            self._post_projection(path, query)
            return
        if path == "/v1/users":
            body = self._read_json()
            self._require_fields(body, {"user_id", "display_name", "password"})
            response = self.server.application.auth.register(
                body["user_id"], body["display_name"], body["password"]
            )
            self._send_json(201, response, no_store=True)
            return
        if path == "/v1/sessions":
            body = self._read_json()
            self._require_fields(body, {"user_id", "password"})
            response = self.server.application.auth.login(body["user_id"], body["password"])
            self._send_json(200, response, no_store=True)
            return

        user_id = self._authenticated_user()
        if path == "/v1/content":
            body = self._read_json()
            self._require_fields(body, {"body"})
            key = self._single_header("Idempotency-Key")
            if not key:
                raise ValidationError("Idempotency-Key is required")
            if self.server.application.cache is None:
                response = self.server.application.api.publish(user_id, body["body"], key)
            else:
                with self.server._cache_guard:
                    response = self.server.application.api.publish(user_id, body["body"], key)
                    if not response["replayed"]:
                        self._invalidate("feed")
                        self._invalidate("search")
            self._send_json(200 if response["replayed"] else 201, response)
            return
        if path == "/v1/follows":
            body = self._read_json()
            self._require_fields(body, {"followee_id"})
            if self.server.application.cache is None:
                response = self.server.application.api.follow(user_id, body["followee_id"])
            else:
                with self.server._cache_guard:
                    response = self.server.application.api.follow(user_id, body["followee_id"])
                    if response["created"]:
                        self._invalidate("feed")
            self._send_json(200, response)
            return
        like_match = self._like_path(path)
        if like_match is not None:
            self._require_empty_body()
            if self.server.application.cache is None:
                response = self.server.application.api.like(user_id, like_match)
            else:
                with self.server._cache_guard:
                    response = self.server.application.api.like(user_id, like_match)
                    if response["liked"]:
                        self._invalidate("feed")
                        self._invalidate("search")
            self._send_json(200, response)
            return
        raise HTTPProblem(404, "not_found", "route was not found")

    def _get_projection(self, path: str, query: dict[str, list[str]]) -> None:
        self._authorize_projection_source()
        if path == "/internal/v1/content-projection/events":
            self._require_query_fields(query, {"consumer_id", "limit"})
            consumer_id = self._query_value(query, "consumer_id")
            if consumer_id is None:
                raise ValidationError("consumer_id is required")
            self._require_projection_consumer(consumer_id)
            limit = self._query_limit(query, default=100)
            events = self.server.projection_events.pending(consumer_id, limit=limit)
            self._send_json(200, {"events": events}, no_store=True)
            return
        prefix = "/internal/v1/content-projection/content/"
        if path.startswith(prefix):
            self._require_no_query_fields(query)
            encoded_content_id = path[len(prefix):]
            if not encoded_content_id or "/" in encoded_content_id:
                raise HTTPProblem(404, "not_found", "resource was not found")
            content_id = unquote(encoded_content_id)
            if "/" in content_id:
                raise ValidationError("content_id is invalid")
            content = self.server.projection_events.get_public(content_id)
            if content is None:
                raise HTTPProblem(404, "not_found", "resource was not found")
            self._send_json(200, content, no_store=True)
            return
        raise HTTPProblem(404, "not_found", "route was not found")

    def _post_projection(self, path: str, query: dict[str, list[str]]) -> None:
        self._authorize_projection_source()
        if path == "/internal/v1/content-projection/resolve":
            self._require_no_query_fields(query)
            body = self._read_json()
            self._require_fields(body, {"content_ids"})
            content_ids = body["content_ids"]
            if not isinstance(content_ids, list):
                raise ValidationError("content_ids must be a list")
            items = self.server.projection_events.get_public_many(content_ids)
            self._send_json(200, {"items": items}, no_store=True)
            return
        if path != "/internal/v1/content-projection/ack":
            raise HTTPProblem(404, "not_found", "route was not found")
        self._require_no_query_fields(query)
        body = self._read_json()
        self._require_fields(body, {"consumer_id", "event_id"})
        consumer_id = body["consumer_id"]
        event_id = body["event_id"]
        if not isinstance(consumer_id, str):
            raise ValidationError("consumer_id is invalid")
        if not isinstance(event_id, str):
            raise ValidationError("event_id is invalid")
        self._require_projection_consumer(consumer_id)
        acknowledged = self.server.projection_events.acknowledge(consumer_id, event_id)
        self._send_json(200, {"acknowledged": acknowledged}, no_store=True)

    def _authorize_projection_source(self) -> None:
        expected = self.server.projection_token
        if expected is None:
            # The route does not exist until an operator opts in with a token.
            raise HTTPProblem(404, "not_found", "route was not found")
        supplied = self._bearer_token()
        if not compare_digest(supplied, expected):
            raise HTTPProblem(401, "unauthorized", "service authentication was not accepted")

    def _require_projection_consumer(self, consumer_id: str) -> None:
        ProjectionEventSource.validate_consumer_id(consumer_id)
        if consumer_id != self.server.projection_consumer_id:
            raise HTTPProblem(403, "forbidden", "consumer is not enabled on this service")

    def _delete(self) -> None:
        path, _query = self._path_query()
        if path != "/v1/sessions/current":
            raise HTTPProblem(404, "not_found", "route was not found")
        token = self._bearer_token()
        self.server.application.auth.authenticate(token)
        revoked = self.server.application.auth.revoke(token)
        self._send_json(200, {"revoked": revoked}, no_store=True)

    def _unsupported(self) -> None:
        raise HTTPProblem(405, "method_not_allowed", "HTTP method is not supported")

    def _authenticated_user(self) -> str:
        return self.server.application.auth.authenticate(self._bearer_token())

    def _bearer_token(self) -> str:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            raise AuthenticationFailed("bearer session was not accepted")
        value = values[0]
        scheme, separator, token = value.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not token
            or token != token.strip()
            or any(character.isspace() for character in token)
        ):
            raise AuthenticationFailed("bearer session was not accepted")
        return token

    def _path_query(self) -> tuple[str, dict[str, list[str]]]:
        try:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
        except ValueError as exc:
            raise HTTPProblem(400, "invalid_request", "request target was not accepted") from exc
        return parsed.path, query

    def _read_json(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding"):
            raise HTTPProblem(400, "invalid_request", "transfer encoding is not supported")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
            raise HTTPProblem(415, "unsupported_media_type", "application/json is required")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise HTTPProblem(411, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HTTPProblem(400, "invalid_request", "Content-Length is invalid") from exc
        if length < 0:
            raise HTTPProblem(400, "invalid_request", "Content-Length is invalid")
        if length > MAX_REQUEST_BYTES:
            raise HTTPProblem(413, "payload_too_large", "request body exceeds the configured limit")
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise HTTPProblem(400, "invalid_request", "request body was incomplete")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPProblem(400, "invalid_json", "request body must be a JSON object") from exc
        if not isinstance(value, dict):
            raise HTTPProblem(400, "invalid_json", "request body must be a JSON object")
        return value

    def _require_empty_body(self) -> None:
        if self.headers.get("Transfer-Encoding"):
            raise HTTPProblem(400, "invalid_request", "transfer encoding is not supported")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HTTPProblem(400, "invalid_request", "Content-Length is invalid") from exc
        if length != 0:
            raise HTTPProblem(400, "invalid_request", "request body must be empty")

    @staticmethod
    def _require_fields(body: dict[str, object], fields: set[str]) -> None:
        if set(body) != fields:
            raise ValidationError("request fields do not match the route contract")

    @staticmethod
    def _require_no_query_fields(query: dict[str, list[str]]) -> None:
        if query:
            raise ValidationError("query parameters are not supported on this route")

    @staticmethod
    def _require_query_fields(query: dict[str, list[str]], allowed: set[str]) -> None:
        if set(query) - allowed:
            raise ValidationError("unknown query parameter")
        if any(len(values) != 1 for values in query.values()):
            raise ValidationError("duplicate query parameter")

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str) -> str | None:
        values = query.get(key)
        return values[0] if values else None

    def _query_limit(self, query: dict[str, list[str]], *, default: int) -> int:
        value = self._query_value(query, "limit")
        if value is None:
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ValidationError("limit must be an integer") from exc

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise ValidationError(f"{name} must be supplied once")
        return values[0].strip() if values else None

    @staticmethod
    def _like_path(path: str) -> str | None:
        prefix, suffix = "/v1/content/", "/likes"
        if path.startswith(prefix) and path.endswith(suffix):
            encoded = path[len(prefix):-len(suffix)]
            if encoded and "/" not in encoded:
                return unquote(encoded)
        return None

    @staticmethod
    def _cache_key(parts: list[object]) -> str:
        encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
        import hashlib

        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _cache_key_for(self, namespace: str, parts: list[object]) -> str:
        """Include the committed SQLite epoch so failed cache invalidation is safe."""
        if self.server.application.cache is None:
            return self._cache_key(parts)
        epoch = self.server.cache_epochs.version(namespace)
        return self._cache_key([epoch, *parts])

    def _cached(
        self, namespace: str, key: str, loader: Callable[[], dict[str, object]]
    ) -> dict[str, object]:
        cache = self.server.application.cache
        if cache is None:
            self.server.metrics.increment("cache_misses")
            return loader()
        snapshot_get = getattr(cache, "get_snapshot", None)
        conditional_set = getattr(cache, "set_if_current", None)
        if callable(snapshot_get) and callable(conditional_set):
            try:
                snapshot = snapshot_get(namespace, key)
            except CacheUnavailable:
                self.server.metrics.increment("cache_errors")
                self.server.metrics.increment("cache_bypasses")
                self.server.metrics.increment("cache_misses")
                return loader()
            if isinstance(snapshot.value, dict):
                self.server.metrics.increment("cache_hits")
                return snapshot.value
            self.server.metrics.increment("cache_misses")
            result = loader()
            try:
                accepted = conditional_set(
                    namespace,
                    key,
                    result,
                    ttl_seconds=self.server.cache_ttl_seconds,
                    expected_version=snapshot.version,
                )
            except CacheUnavailable:
                self.server.metrics.increment("cache_errors")
                self.server.metrics.increment("cache_bypasses")
            else:
                if not accepted:
                    self.server.metrics.increment("cache_stale_fill_discarded")
            return result
        # Legacy cache implementations do not expose generation-checked fills,
        # so keep their load and invalidation serialized within this server.
        with self.server._cache_guard:
            try:
                value = cache.get(namespace, key)
            except CacheUnavailable:
                self.server.metrics.increment("cache_errors")
                self.server.metrics.increment("cache_bypasses")
                self.server.metrics.increment("cache_misses")
                return loader()
            if isinstance(value, dict):
                self.server.metrics.increment("cache_hits")
                return value
            self.server.metrics.increment("cache_misses")
            result = loader()
            try:
                cache.set(namespace, key, result, ttl_seconds=self.server.cache_ttl_seconds)
            except CacheUnavailable:
                self.server.metrics.increment("cache_errors")
                self.server.metrics.increment("cache_bypasses")
            return result

    def _invalidate(self, namespace: str) -> None:
        cache = self.server.application.cache
        if cache is not None:
            try:
                cache.invalidate(namespace)
            except CacheUnavailable:
                self.server.metrics.increment("cache_errors")
                self.server.metrics.increment("cache_invalidation_failures")
            else:
                self.server.metrics.increment("cache_invalidations")

    def _metrics_snapshot(self) -> dict[str, object]:
        snapshot = self.server.metrics.snapshot()
        snapshot["outbox_worker"] = {
            "mode": "in_process" if self.server.start_outbox_worker else "external_kafka",
            "alive": self.server.worker.is_alive,
            "processed": self.server.worker.processed_count,
            "failed": self.server.worker.failed_count,
        }
        return snapshot

    def _send_problem(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_static(self, content_type: str, payload: bytes) -> None:
        self._response_status = 200
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, value: object, *, no_store: bool = False) -> None:
        self._send_json_body(status, value, no_store=no_store)

    def _send_json_body(self, status: int, value: object, *, no_store: bool = False) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._response_status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store" if no_store else "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)
