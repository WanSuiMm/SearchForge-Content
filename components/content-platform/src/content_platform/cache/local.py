"""Process-local versioned TTL cache; entries are not shared across workers."""

from collections import OrderedDict
import threading
import time

from .port import CacheSnapshot


class LocalVersionedTTLCache:
    def __init__(
        self,
        *,
        max_entries: int = 1024,
        clock=time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self.clock = clock
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}
        self._entries: OrderedDict[tuple[str, int, str], tuple[float, object]] = OrderedDict()

    def get(self, namespace: str, key: str) -> object | None:
        return self.get_snapshot(namespace, key).value

    def get_snapshot(self, namespace: str, key: str) -> CacheSnapshot:
        with self._lock:
            version = self._versions.get(namespace, 0)
            entry_key = (namespace, version, key)
            entry = self._entries.get(entry_key)
            if entry is None:
                return CacheSnapshot(str(version), None)
            expires_at, value = entry
            if expires_at <= self.clock():
                del self._entries[entry_key]
                return CacheSnapshot(str(version), None)
            self._entries.move_to_end(entry_key)
            return CacheSnapshot(str(version), value)

    def set(
        self,
        namespace: str,
        key: str,
        value: object,
        *,
        ttl_seconds: float,
    ) -> None:
        with self._lock:
            self._set_locked(namespace, key, value, ttl_seconds=ttl_seconds)

    def set_if_current(
        self,
        namespace: str,
        key: str,
        value: object,
        *,
        ttl_seconds: float,
        expected_version: str,
    ) -> bool:
        with self._lock:
            current_version = self._versions.get(namespace, 0)
            if str(current_version) != expected_version:
                return False
            self._set_locked(namespace, key, value, ttl_seconds=ttl_seconds)
            return True

    def _set_locked(
        self,
        namespace: str,
        key: str,
        value: object,
        *,
        ttl_seconds: float,
    ) -> None:
        if not namespace or not key:
            raise ValueError("namespace and key are required")
        if value is None:
            raise ValueError("None is reserved to mean a cache miss")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        version = self._versions.get(namespace, 0)
        entry_key = (namespace, version, key)
        self._entries[entry_key] = (self.clock() + ttl_seconds, value)
        self._entries.move_to_end(entry_key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, namespace: str) -> None:
        with self._lock:
            self._versions[namespace] = self._versions.get(namespace, 0) + 1
            stale_keys = [entry_key for entry_key in self._entries if entry_key[0] == namespace]
            for entry_key in stale_keys:
                del self._entries[entry_key]
