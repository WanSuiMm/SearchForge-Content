"""Independent, durable content events for read-only downstream projections."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .errors import ValidationError
from .storage.database import Database
from .timeutil import utc_timestamp

DEFAULT_PROJECTION_CONSUMER_ID = "searchforge-content-v1"
MAX_PROJECTION_PENDING_LIMIT = 100
MAX_PROJECTION_RESOLVE_IDS = 20


class ProjectionEventSource:
    """Expose committed public content and per-consumer projection receipts.

    Projection events use their own append-only table and receipt namespace.
    They do not share state with the notification Outbox or its consumers.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def validate_consumer_id(consumer_id: object) -> str:
        if (
            not isinstance(consumer_id, str)
            or not consumer_id
            or consumer_id != consumer_id.strip()
            or len(consumer_id) > 200
        ):
            raise ValidationError("consumer_id must contain 1 to 200 non-whitespace characters")
        return consumer_id

    def append_published(
        self,
        connection: Any,
        *,
        content_id: str,
        author_id: str,
        body: str,
        published_at: str,
        occurred_at: str | None = None,
    ) -> str:
        """Append version 1 on the caller's publication transaction."""
        if not all(isinstance(value, str) and value for value in (
            content_id,
            author_id,
            body,
            published_at,
        )):
            raise ValidationError("published projection fields must be non-empty strings")
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO projection_events(
                event_id, schema_version, event_type, content_id, version,
                visibility, author_id, body, published_at, occurred_at
            ) VALUES (?, 1, 'content.published', ?, 1, 'public', ?, ?, ?, ?)
            """,
            (
                event_id,
                content_id,
                author_id,
                body,
                published_at,
                occurred_at or published_at,
            ),
        )
        return event_id

    def pending(self, consumer_id: str, limit: int = 100) -> list[dict[str, object]]:
        consumer_id = self.validate_consumer_id(consumer_id)
        if type(limit) is not int or not 1 <= limit <= MAX_PROJECTION_PENDING_LIMIT:
            raise ValidationError("limit must be an integer from 1 to 100")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.schema_version, e.event_id, e.event_type, e.content_id,
                       e.version, e.visibility, e.author_id, e.body,
                       e.published_at, e.occurred_at
                FROM projection_events AS e
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM projection_event_receipts AS r
                    WHERE r.consumer_id = ? AND r.event_id = e.event_id
                )
                ORDER BY e.occurred_at ASC, e.event_id ASC
                LIMIT ?
                """,
                (consumer_id, limit),
            ).fetchall()
        return [self._event_json(row) for row in rows]

    def acknowledge(self, consumer_id: str, event_id: str) -> bool:
        consumer_id = self.validate_consumer_id(consumer_id)
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id != event_id.strip()
            or len(event_id) > 200
        ):
            raise ValidationError("event_id must contain 1 to 200 non-whitespace characters")
        with self.database.transaction() as connection:
            known = connection.execute(
                "SELECT 1 FROM projection_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if known is None:
                return False
            connection.execute(
                """
                INSERT INTO projection_event_receipts(consumer_id, event_id, processed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(consumer_id, event_id) DO NOTHING
                """,
                (consumer_id, event_id, utc_timestamp()),
            )
        # A repeat ack is successful and idempotent for a known event.
        return True

    def get_public(self, content_id: str) -> dict[str, object] | None:
        content_id = self._validate_content_id(content_id)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT content_id, author_id, body, published_at
                FROM contents
                WHERE content_id = ?
                """,
                (content_id,),
            ).fetchone()
        if row is None:
            return None
        return self._public_json(row)

    def get_public_many(self, content_ids: list[str]) -> list[dict[str, object]]:
        if not isinstance(content_ids, list):
            raise ValidationError("content_ids must be a list")
        if len(content_ids) > MAX_PROJECTION_RESOLVE_IDS:
            raise ValidationError("content_ids may contain at most 20 entries")
        validated_ids = [self._validate_content_id(content_id) for content_id in content_ids]
        if len(set(validated_ids)) != len(validated_ids):
            raise ValidationError("content_ids must not contain duplicates")
        if not validated_ids:
            return []

        placeholders = ", ".join("?" for _ in validated_ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT content_id, author_id, body, published_at
                FROM contents
                WHERE content_id IN ({placeholders})
                """,
                tuple(validated_ids),
            ).fetchall()
        rows_by_id = {row["content_id"]: row for row in rows}
        return [
            self._public_json(rows_by_id[content_id])
            for content_id in validated_ids
            if content_id in rows_by_id
        ]

    @staticmethod
    def _validate_content_id(content_id: object) -> str:
        if (
            not isinstance(content_id, str)
            or not content_id
            or content_id != content_id.strip()
            or "/" in content_id
        ):
            raise ValidationError("content_id is invalid")
        return content_id

    @staticmethod
    def _public_json(row: Any) -> dict[str, object]:
        return {
            "content_id": row["content_id"],
            "author_id": row["author_id"],
            "body": row["body"],
            "published_at": row["published_at"],
            "version": 1,
            "visibility": "public",
            "source_uri": f"/v1/content/{row['content_id']}",
        }

    @staticmethod
    def _event_json(row: Any) -> dict[str, object]:
        content_id = row["content_id"]
        return {
            "schema_version": int(row["schema_version"]),
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "content_id": content_id,
            "version": int(row["version"]),
            "visibility": row["visibility"],
            "author_id": row["author_id"],
            "body": row["body"],
            "published_at": row["published_at"],
            "occurred_at": row["occurred_at"],
            "source_uri": f"/v1/content/{content_id}",
        }
