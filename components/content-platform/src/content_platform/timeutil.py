"""Small time helpers. Timestamps are UTC and lexicographically sortable."""

from datetime import datetime, timezone


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
