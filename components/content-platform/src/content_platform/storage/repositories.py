"""SQLite repositories. Business validation and request hashing live in services."""

import json
import re
import sqlite3
import time
from typing import Iterable
from uuid import uuid4

from ..contracts import ContentRecord, LikeResult, PublishedContent
from ..errors import AlreadyExists, NotFound, ValidationError
from ..projection import ProjectionEventSource
from ..timeutil import utc_timestamp
from .database import Database

CACHE_EPOCH_NAMESPACES = frozenset({"feed", "search"})


def _bump_cache_epochs(connection: sqlite3.Connection, *namespaces: str) -> None:
    """Advance cache epochs atomically with the data mutation they describe."""
    for namespace in sorted(set(namespaces)):
        if namespace not in CACHE_EPOCH_NAMESPACES:
            raise ValueError(f"unsupported cache epoch namespace: {namespace}")
        cursor = connection.execute(
            "UPDATE cache_epochs SET version = ? WHERE namespace = ?",
            (uuid4().hex, namespace),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"cache epoch row is missing: {namespace}")


class CacheEpochRepository:
    """Reads durable versions used to make cache keys safe across recovery."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def version(self, namespace: str) -> str:
        if namespace not in CACHE_EPOCH_NAMESPACES:
            raise ValueError(f"unsupported cache epoch namespace: {namespace}")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT version FROM cache_epochs WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"cache epoch row is missing: {namespace}")
        return str(row["version"])


def _content_record(row: sqlite3.Row) -> ContentRecord:
    return ContentRecord(
        content_id=row["content_id"],
        author_id=row["author_id"],
        author_name=row["author_name"],
        body=row["body"],
        published_at=row["published_at"],
        like_count=int(row["like_count"]),
    )


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_user(self, user_id: str, display_name: str, created_at: str) -> None:
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO users(user_id, display_name, created_at) VALUES (?, ?, ?)",
                    (user_id, display_name, created_at),
                )
        except Exception as exc:
            if self.database.is_user_id_conflict(exc):
                raise AlreadyExists(f"user already exists: {user_id}") from exc
            raise

    def follow(self, follower_id: str, followee_id: str, created_at: str) -> bool:
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (follower_id,)
            ).fetchone():
                raise NotFound(f"user not found: {follower_id}")
            followee_query = "SELECT 1 FROM users WHERE user_id = ?"
            if self.database.dialect == "postgresql":
                # Publishers lock this same author row before reading followers,
                # preventing a follow/publish race from omitting a feed entry.
                followee_query += " FOR UPDATE"
            if not connection.execute(followee_query, (followee_id,)).fetchone():
                raise NotFound(f"user not found: {followee_id}")
            if follower_id == followee_id:
                raise ValidationError("a user cannot follow themselves")
            cursor = connection.execute(
                """
                INSERT INTO follows(follower_id, followee_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(follower_id, followee_id) DO NOTHING
                """,
                (follower_id, followee_id, created_at),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    INSERT INTO feed_entries(user_id, content_id, published_at)
                    SELECT ?, content_id, published_at
                    FROM contents
                    WHERE author_id = ?
                    ON CONFLICT(user_id, content_id) DO NOTHING
                    """,
                    (follower_id, followee_id),
                )
                _bump_cache_epochs(connection, "feed")
            return cursor.rowcount == 1


class ContentRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.projection_events = ProjectionEventSource(database)

    def publish_with_outbox(
        self,
        *,
        author_id: str,
        body: str,
        published_at: str,
        idempotency_key: str,
        request_hash: str,
    ) -> PublishedContent:
        with self.database.transaction() as connection:
            author_query = "SELECT 1 FROM users WHERE user_id = ?"
            if self.database.dialect == "postgresql":
                author_query += " FOR UPDATE"
            if not connection.execute(author_query, (author_id,)).fetchone():
                raise NotFound(f"user not found: {author_id}")
            existing = connection.execute(
                """
                SELECT c.content_id, c.author_id, u.display_name AS author_name,
                       c.body, c.published_at, c.like_count, c.request_hash
                FROM contents AS c
                JOIN users AS u ON u.user_id = c.author_id
                WHERE c.author_id = ? AND c.idempotency_key = ?
                """,
                (author_id, idempotency_key),
            ).fetchone()
            if existing:
                from ..errors import IdempotencyConflict

                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        "idempotency key was already used with different content"
                    )
                return PublishedContent(_content_record(existing), replayed=True)

            content_id = str(uuid4())
            inserted = connection.execute(
                """
                INSERT INTO contents(
                    content_id, author_id, body, published_at,
                    idempotency_key, request_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(author_id, idempotency_key) DO NOTHING
                """,
                (content_id, author_id, body, published_at, idempotency_key, request_hash),
            )
            if inserted.rowcount != 1:
                # PostgreSQL can race two transactions past the earlier read.
                # The targeted conflict clause waits for the winner, after which
                # this statement snapshot can read the committed idempotent row.
                existing = connection.execute(
                    """
                    SELECT c.content_id, c.author_id, u.display_name AS author_name,
                           c.body, c.published_at, c.like_count, c.request_hash
                    FROM contents AS c
                    JOIN users AS u ON u.user_id = c.author_id
                    WHERE c.author_id = ? AND c.idempotency_key = ?
                    """,
                    (author_id, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("idempotency conflict row disappeared")
                from ..errors import IdempotencyConflict

                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(
                        "idempotency key was already used with different content"
                    )
                return PublishedContent(_content_record(existing), replayed=True)
            follower_ids = [
                row["follower_id"]
                for row in connection.execute(
                    "SELECT follower_id FROM follows WHERE followee_id = ? ORDER BY follower_id",
                    (author_id,),
                )
            ]
            connection.executemany(
                """
                INSERT INTO feed_entries(user_id, content_id, published_at)
                VALUES (?, ?, ?)
                """,
                [(user_id, content_id, published_at) for user_id in [author_id, *follower_ids]],
            )
            event_id = str(uuid4())
            payload = {
                "content_id": content_id,
                "author_id": author_id,
                "recipient_ids": follower_ids,
            }
            connection.execute(
                """
                INSERT INTO outbox(
                    event_id, event_type, payload_json, created_at, available_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    "content.published",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    published_at,
                    time.time(),
                ),
            )
            self.projection_events.append_published(
                connection,
                content_id=content_id,
                author_id=author_id,
                body=body,
                published_at=published_at,
            )
            _bump_cache_epochs(connection, "feed", "search")
            row = connection.execute(
                """
                SELECT c.content_id, c.author_id, u.display_name AS author_name,
                       c.body, c.published_at, c.like_count
                FROM contents AS c
                JOIN users AS u ON u.user_id = c.author_id
                WHERE c.content_id = ?
                """,
                (content_id,),
            ).fetchone()
            return PublishedContent(_content_record(row), replayed=False)


class FeedRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def page(
        self,
        *,
        user_id: str,
        limit: int,
        after: tuple[str, str] | None,
    ) -> list[ContentRecord]:
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone():
                raise NotFound(f"user not found: {user_id}")
            sql = """
                SELECT c.content_id, c.author_id, u.display_name AS author_name,
                       c.body, f.published_at, c.like_count
                FROM feed_entries AS f
                JOIN contents AS c ON c.content_id = f.content_id
                JOIN users AS u ON u.user_id = c.author_id
                WHERE f.user_id = ?
            """
            parameters: list[object] = [user_id]
            if after is not None:
                published_at, content_id = after
                sql += """
                    AND (f.published_at < ? OR
                         (f.published_at = ? AND f.content_id < ?))
                """
                parameters.extend((published_at, published_at, content_id))
            sql += " ORDER BY f.published_at DESC, f.content_id DESC LIMIT ?"
            parameters.append(limit)
            rows = connection.execute(sql, parameters).fetchall()
            return [_content_record(row) for row in rows]


class InteractionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def like(self, user_id: str, content_id: str, created_at: str) -> LikeResult:
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone():
                raise NotFound(f"user not found: {user_id}")
            if not connection.execute(
                "SELECT 1 FROM contents WHERE content_id = ?", (content_id,)
            ).fetchone():
                raise NotFound(f"content not found: {content_id}")
            cursor = connection.execute(
                """
                INSERT INTO likes(user_id, content_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, content_id) DO NOTHING
                """,
                (user_id, content_id, created_at),
            )
            if cursor.rowcount:
                _bump_cache_epochs(connection, "feed", "search")
            row = connection.execute(
                "SELECT like_count FROM contents WHERE content_id = ?", (content_id,)
            ).fetchone()
            return LikeResult(liked=cursor.rowcount == 1, like_count=int(row["like_count"]))

    def remove_like(self, user_id: str, content_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM likes WHERE user_id = ? AND content_id = ?",
                (user_id, content_id),
            )
            if cursor.rowcount:
                _bump_cache_epochs(connection, "feed", "search")
            return cursor.rowcount == 1

    def notification_count(self, recipient_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM notifications WHERE recipient_id = ?",
                (recipient_id,),
            ).fetchone()
            return int(row["count"])


class SearchRepository:
    """Full-text search backed by SQLite FTS5 or PostgreSQL tsvector."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def search(self, match_expression: str, *, limit: int) -> list[ContentRecord]:
        with self.database.connect() as connection:
            if self.database.dialect == "postgresql":
                terms = re.findall(r'"([^\"]+)"', match_expression)
                if not terms:
                    return []
                query = " ".join(terms)
                rows = connection.execute(
                    """
                    WITH search_query AS (
                        SELECT plainto_tsquery('simple', ?) AS tsquery
                    )
                    SELECT c.content_id, c.author_id, u.display_name AS author_name,
                           c.body, c.published_at, c.like_count,
                           ts_rank(c.search_vector, search_query.tsquery) AS relevance
                    FROM contents AS c
                    JOIN users AS u ON u.user_id = c.author_id
                    CROSS JOIN search_query
                    WHERE c.search_vector @@ search_query.tsquery
                    ORDER BY relevance DESC, c.published_at DESC, c.content_id DESC
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
                return [_content_record(row) for row in rows]

            rows = connection.execute(
                """
                SELECT c.content_id, c.author_id, u.display_name AS author_name,
                       c.body, c.published_at, c.like_count
                FROM content_search AS fts
                JOIN contents AS c ON c.rowid = fts.rowid
                JOIN users AS u ON u.user_id = c.author_id
                WHERE content_search MATCH ?
                ORDER BY bm25(content_search), c.published_at DESC, c.content_id DESC
                LIMIT ?
                """,
                (match_expression, limit),
            ).fetchall()
            return [_content_record(row) for row in rows]


class NotificationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_for_user(self, user_id: str, *, limit: int) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT n.event_id, n.content_id, n.created_at,
                       c.author_id, u.display_name AS author_name, c.body
                FROM notifications AS n
                JOIN contents AS c ON c.content_id = n.content_id
                JOIN users AS u ON u.user_id = c.author_id
                WHERE n.recipient_id = ?
                ORDER BY n.created_at DESC, n.event_id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [
                {
                    "event_id": row["event_id"],
                    "content_id": row["content_id"],
                    "created_at": row["created_at"],
                    "author_id": row["author_id"],
                    "author_name": row["author_name"],
                    "body": row["body"],
                }
                for row in rows
            ]


def rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, object]]:
    """Small helper for tests and diagnostics without exposing SQLite row objects."""
    return [dict(row) for row in rows]
