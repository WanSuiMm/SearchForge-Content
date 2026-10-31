"""Optional cache boundary. The core application does not require a cache."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CacheSnapshot:
    """A value and namespace generation observed as one cache read."""

    version: str
    value: object | None


class CachePort(Protocol):
    def get(self, namespace: str, key: str) -> object | None: ...

    def set(self, namespace: str, key: str, value: object, *, ttl_seconds: float) -> None: ...

    def invalidate(self, namespace: str) -> None: ...


class VersionedCachePort(CachePort, Protocol):
    """Cache operations that prevent a stale fill crossing invalidation."""

    def get_snapshot(self, namespace: str, key: str) -> CacheSnapshot: ...

    def set_if_current(
        self,
        namespace: str,
        key: str,
        value: object,
        *,
        ttl_seconds: float,
        expected_version: str,
    ) -> bool: ...
