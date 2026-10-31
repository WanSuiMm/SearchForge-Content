"""A durable, single-node public-content projection backed by SQLite FTS5.

The projection is updated from versioned events. Event receipts, version
conflict guards, the current document row, and its FTS row are committed in a
single SQLite transaction so retries and tombstones remain safe across restarts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Any


_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "event_type",
        "content_id",
        "version",
        "visibility",
        "author_id",
        "body",
        "published_at",
        "occurred_at",
        "source_uri",
    }
)
_MAX_EVENT_ID = 256
_MAX_CONTENT_ID = 256
_MAX_AUTHOR_ID = 256
_MAX_BODY_CHARS = 50_000
_MAX_TIMESTAMP_CHARS = 128
_MAX_QUERY_CHARS = 512
_MAX_QUERY_TOKENS = 16
_MAX_SEARCH_LIMIT = 100


class EventConflictError(ValueError):
    """Raised when an event ID or content version is reused with new data."""


def _required_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must contain between 1 and {maximum} characters")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid Unicode text") from exc
    return value


def _validate_event(event: object) -> tuple[dict[str, Any], str, str]:
    if not isinstance(event, dict):
        raise ValueError("event must be a dictionary")
    missing = _EVENT_KEYS - event.keys()
    extra = event.keys() - _EVENT_KEYS
    if missing or extra:
        raise ValueError(
            f"event keys mismatch (missing={sorted(missing)}, extra={sorted(extra)})"
        )

    schema_version = event["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise ValueError("schema_version must be 1")

    normalized = dict(event)
    normalized["event_id"] = _required_text(
        event["event_id"], "event_id", _MAX_EVENT_ID
    )
    normalized["content_id"] = _required_text(
        event["content_id"], "content_id", _MAX_CONTENT_ID
    )
    normalized["author_id"] = _required_text(
        event["author_id"], "author_id", _MAX_AUTHOR_ID
    )
    normalized["body"] = event["body"]
    if not isinstance(normalized["body"], str):
        raise ValueError("body must be a string")
    if len(normalized["body"]) > _MAX_BODY_CHARS or "\x00" in normalized["body"]:
        raise ValueError(f"body must be at most {_MAX_BODY_CHARS} characters and contain no NUL")
    try:
        normalized["body"].encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("body must be valid Unicode text") from exc

    event_type = event["event_type"]
    visibility = event["visibility"]
    if (event_type, visibility) not in {
        ("content.published", "public"),
        ("content.withdrawn", "withdrawn"),
    }:
        raise ValueError("event_type and visibility must describe publish or withdrawal")
    if event_type == "content.withdrawn" and normalized["body"]:
        raise ValueError("withdrawal events must have an empty body")

    version = event["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be an integer greater than or equal to 1")

    for name in ("published_at", "occurred_at"):
        normalized[name] = _required_text(
            event[name], name, _MAX_TIMESTAMP_CHARS
        )
    content_id = normalized["content_id"]
    expected_uri = f"/v1/content/{content_id}"
    source_uri = _required_text(event["source_uri"], "source_uri", 512)
    if source_uri != expected_uri:
        raise ValueError("source_uri must be /v1/content/{content_id}")
    normalized["source_uri"] = source_uri

    payload_json = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return normalized, payload_json, payload_hash


def _query_tokens(query: object) -> list[str]:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if len(query) > _MAX_QUERY_CHARS:
        raise ValueError(f"query must be at most {_MAX_QUERY_CHARS} characters")

    tokens: list[str] = []
    current: list[str] = []
    for character in query:
        if unicodedata.category(character)[0] in {"L", "N"}:
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current.clear()
    if current:
        tokens.append("".join(current))
    if len(tokens) > _MAX_QUERY_TOKENS:
        raise ValueError(f"query must contain at most {_MAX_QUERY_TOKENS} terms")
    return tokens


class CommunityIndex:
    """Versioned public-content projection using a persistent SQLite FTS5 index.

    Constructing an instance is side-effect free. Call :meth:`initialize`
    explicitly before using it. Each instance serializes its own connection;
    SQLite's write lock coordinates separate instances pointed at the same file.
    """

    def __init__(self, path: str | Path):
        self.path = ":memory:" if str(path) == ":memory:" else str(Path(path).expanduser().resolve())
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        """Create the schema and FTS5 table if absent; safe to call repeatedly."""
        with self._lock:
            if self._connection is not None:
                return
            if self.path != ":memory:":
                Path(self.path).expanduser().resolve().parent.mkdir(
                    parents=True, exist_ok=True
                )
            connection = sqlite3.connect(
                self.path,
                timeout=10.0,
                isolation_level=None,
                check_same_thread=False,
            )
            try:
                connection.row_factory = sqlite3.Row

                # Ownership is checked using read-only catalog queries before
                # WAL or any schema-writing operation can touch an existing DB.
                objects = connection.execute(
                    """
                    SELECT type, name FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
                meta_table = any(
                    row["type"] == "table" and row["name"] == "community_meta"
                    for row in objects
                )
                if objects and not meta_table:
                    raise ValueError(
                        "refusing to initialize a nonempty database without the "
                        "community ownership marker"
                    )
                if meta_table:
                    meta_columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(community_meta)"
                        ).fetchall()
                    }
                    if not {"key", "value"}.issubset(meta_columns):
                        raise ValueError(
                            "existing community_meta table has an invalid ownership marker schema"
                        )
                    marker_rows = connection.execute(
                        "SELECT value FROM community_meta WHERE key = 'schema_version'"
                    ).fetchall()
                    if len(marker_rows) != 1 or marker_rows[0]["value"] != "1":
                        raise ValueError(
                            "existing community database is missing a valid schema_version=1 ownership marker"
                        )

                connection.execute("PRAGMA busy_timeout = 10000")
                if self.path != ":memory:":
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA synchronous = FULL")

                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS community_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                if not meta_table:
                    connection.execute(
                        "INSERT INTO community_meta(key, value) VALUES ('schema_version', '1')"
                    )
                existing = connection.execute(
                    "SELECT value FROM community_meta WHERE key = 'schema_version'"
                ).fetchone()
                if existing is None or existing["value"] != "1":
                    raise RuntimeError(
                        "community schema ownership marker disappeared during initialization"
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS community_documents (
                        content_id TEXT PRIMARY KEY,
                        version INTEGER NOT NULL CHECK (version >= 1),
                        visibility TEXT NOT NULL CHECK (visibility IN ('public', 'withdrawn')),
                        author_id TEXT NOT NULL,
                        body TEXT NOT NULL,
                        source_uri TEXT NOT NULL,
                        published_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS community_versions (
                        content_id TEXT NOT NULL,
                        version INTEGER NOT NULL CHECK (version >= 1),
                        payload_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        first_event_id TEXT NOT NULL,
                        PRIMARY KEY (content_id, version)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS community_receipts (
                        event_id TEXT PRIMARY KEY,
                        payload_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        content_id TEXT NOT NULL,
                        version INTEGER NOT NULL CHECK (version >= 1),
                        outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'stale', 'duplicate'))
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS community_fts USING fts5(
                        content_id UNINDEXED,
                        body,
                        tokenize = 'unicode61 remove_diacritics 2'
                    )
                    """
                )
                connection.commit()
            except BaseException:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                except sqlite3.Error:
                    pass
                finally:
                    connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        """Close the local SQLite connection; a later initialize reopens it."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("CommunityIndex.initialize() must be called first")
        return self._connection

    def apply(self, event: dict[str, Any]) -> dict[str, Any]:
        """Apply one versioned event atomically and return its durable outcome."""
        normalized, payload_json, payload_hash = _validate_event(event)
        event_id = normalized["event_id"]
        content_id = normalized["content_id"]
        version = normalized["version"]

        with self._lock:
            connection = self._conn()
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior_event = connection.execute(
                    "SELECT payload_sha256, payload_json FROM community_receipts WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if prior_event is not None:
                    if prior_event["payload_sha256"] != payload_hash or prior_event["payload_json"] != payload_json:
                        raise EventConflictError(
                            f"event_id {event_id!r} was already used with a different payload"
                        )
                    connection.commit()
                    return {
                        "event_id": event_id,
                        "content_id": content_id,
                        "version": version,
                        "outcome": "duplicate",
                    }

                prior_version = connection.execute(
                    """
                    SELECT payload_sha256, payload_json
                    FROM community_versions WHERE content_id = ? AND version = ?
                    """,
                    (content_id, version),
                ).fetchone()
                if prior_version is not None:
                    if prior_version["payload_sha256"] != payload_hash or prior_version["payload_json"] != payload_json:
                        raise EventConflictError(
                            f"content {content_id!r} version {version} was already used with a different payload"
                        )
                    connection.execute(
                        """
                        INSERT INTO community_receipts
                            (event_id, payload_sha256, payload_json, content_id, version, outcome)
                        VALUES (?, ?, ?, ?, ?, 'duplicate')
                        """,
                        (event_id, payload_hash, payload_json, content_id, version),
                    )
                    connection.commit()
                    return {
                        "event_id": event_id,
                        "content_id": content_id,
                        "version": version,
                        "outcome": "duplicate",
                    }

                connection.execute(
                    """
                    INSERT INTO community_versions
                        (content_id, version, payload_sha256, payload_json, first_event_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (content_id, version, payload_hash, payload_json, event_id),
                )
                current = connection.execute(
                    "SELECT version FROM community_documents WHERE content_id = ?",
                    (content_id,),
                ).fetchone()
                if current is not None and version < current["version"]:
                    outcome = "stale"
                else:
                    outcome = "applied"
                    connection.execute(
                        """
                        INSERT INTO community_documents
                            (content_id, version, visibility, author_id, body, source_uri, published_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(content_id) DO UPDATE SET
                            version = excluded.version,
                            visibility = excluded.visibility,
                            author_id = excluded.author_id,
                            body = excluded.body,
                            source_uri = excluded.source_uri,
                            published_at = excluded.published_at
                        """,
                        (
                            content_id,
                            version,
                            normalized["visibility"],
                            normalized["author_id"],
                            normalized["body"],
                            normalized["source_uri"],
                            normalized["published_at"],
                        ),
                    )
                    connection.execute(
                        "DELETE FROM community_fts WHERE content_id = ?", (content_id,)
                    )
                    if normalized["visibility"] == "public":
                        connection.execute(
                            "INSERT INTO community_fts(content_id, body) VALUES (?, ?)",
                            (content_id, normalized["body"]),
                        )
                connection.execute(
                    """
                    INSERT INTO community_receipts
                        (event_id, payload_sha256, payload_json, content_id, version, outcome)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, payload_hash, payload_json, content_id, version, outcome),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

        return {
            "event_id": event_id,
            "content_id": content_id,
            "version": version,
            "outcome": outcome,
        }

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search public text using safely quoted FTS5 terms and BM25 ranking."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be an integer from 1 to {_MAX_SEARCH_LIMIT}")
        tokens = _query_tokens(query)
        if not tokens:
            return []
        match_expression = " AND ".join(
            '"' + token.replace('"', '""') + '"' for token in tokens
        )
        with self._lock:
            rows = self._conn().execute(
                """
                SELECT d.content_id, d.author_id, d.version, d.body, d.source_uri,
                       d.published_at, bm25(community_fts) AS rank
                FROM community_fts
                JOIN community_documents AS d ON d.content_id = community_fts.content_id
                WHERE community_fts MATCH ? AND d.visibility = 'public'
                ORDER BY rank ASC, d.content_id ASC
                LIMIT ?
                """,
                (match_expression, limit),
            ).fetchall()
        return [
            {
                "content_id": row["content_id"],
                "author_id": row["author_id"],
                "version": row["version"],
                "body": row["body"],
                "source_uri": row["source_uri"],
                "published_at": row["published_at"],
                "score": -float(row["rank"]),
            }
            for row in rows
        ]

    def get(self, content_id: str) -> dict[str, Any] | None:
        """Return the current public projection row, excluding tombstones."""
        content_id = _required_text(content_id, "content_id", _MAX_CONTENT_ID)
        with self._lock:
            row = self._conn().execute(
                """
                SELECT content_id, author_id, version, body, source_uri, published_at
                FROM community_documents
                WHERE content_id = ? AND visibility = 'public'
                """,
                (content_id,),
            ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def get_receipt(self, event_id: str) -> dict[str, Any] | None:
        """Return the original durable receipt for an event ID, if present."""
        event_id = _required_text(event_id, "event_id", _MAX_EVENT_ID)
        with self._lock:
            row = self._conn().execute(
                """
                SELECT event_id, content_id, version, outcome
                FROM community_receipts WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        return None if row is None else {key: row[key] for key in row.keys()}

    def stats(self) -> dict[str, int]:
        """Return compact counts for the projection and its idempotency ledger."""
        with self._lock:
            connection = self._conn()
            documents = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN visibility = 'public' THEN 1 ELSE 0 END), 0) AS public_count,
                    COALESCE(SUM(CASE WHEN visibility = 'withdrawn' THEN 1 ELSE 0 END), 0) AS withdrawn_count
                FROM community_documents
                """
            ).fetchone()
            versions = connection.execute(
                "SELECT COUNT(*) AS count FROM community_versions"
            ).fetchone()["count"]
            receipts = connection.execute(
                "SELECT COUNT(*) AS count FROM community_receipts"
            ).fetchone()["count"]
        return {
            "public_documents": int(documents["public_count"]),
            "withdrawn_documents": int(documents["withdrawn_count"]),
            "versions": int(versions),
            "receipts": int(receipts),
        }
