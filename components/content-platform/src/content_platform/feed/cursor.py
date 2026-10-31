"""Opaque deterministic keyset cursor encoding for feed pages."""

import base64
import json

from ..errors import InvalidCursor


def encode_cursor(published_at: str, content_id: str) -> str:
    raw = json.dumps(
        {"published_at": published_at, "content_id": content_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
        raise InvalidCursor("cursor is empty or too long")
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        published_at = value["published_at"]
        content_id = value["content_id"]
        if not isinstance(published_at, str) or not isinstance(content_id, str):
            raise ValueError("cursor fields must be strings")
        if not published_at or not content_id:
            raise ValueError("cursor fields must not be empty")
        return published_at, content_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidCursor("cursor is malformed") from exc
