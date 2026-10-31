"""Transactional outbox claim, acknowledgement and bounded retry operations."""

import json
import time
from uuid import uuid4

from ..contracts import OutboxEvent
from ..errors import ValidationError
from ..storage.database import Database
from ..timeutil import utc_timestamp


class OutboxRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_attempts: int = 5,
        max_retry_delay_seconds: float = 300.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_retry_delay_seconds < 0:
            raise ValueError("max_retry_delay_seconds must be non-negative")
        self.database = database
        self.max_attempts = max_attempts
        self.max_retry_delay_seconds = max_retry_delay_seconds

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: float = 30.0,
        now: float | None = None,
    ) -> list[OutboxEvent]:
        if not worker_id or len(worker_id) > 100:
            raise ValidationError("worker_id must contain 1 to 100 characters")
        if not 1 <= limit <= 100:
            raise ValidationError("limit must be from 1 to 100")
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        current = time.time() if now is None else now
        events: list[OutboxEvent] = []
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET status = 'dead', lease_owner = NULL, lease_until = NULL,
                    last_error = COALESCE(last_error, 'retry limit reached after lease expiry')
                WHERE status = 'claimed' AND lease_until <= ? AND attempts >= ?
                """,
                (current, self.max_attempts),
            )
            claim_sql = """
                SELECT event_id
                FROM outbox
                WHERE available_at <= ?
                  AND attempts < ?
                  AND (
                      status = 'pending'
                      OR (status = 'claimed' AND lease_until <= ?)
                  )
                ORDER BY created_at, event_id
                LIMIT ?
                """
            if self.database.dialect == "postgresql":
                # Lock only rows selected by this claimant; competing relays skip
                # these rows instead of racing to overwrite each other's leases.
                claim_sql += " FOR UPDATE SKIP LOCKED"
            rows = connection.execute(
                claim_sql,
                (current, self.max_attempts, current, limit),
            ).fetchall()
            lease_until = current + lease_seconds
            for row in rows:
                connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'claimed', attempts = attempts + 1,
                        lease_owner = ?, lease_until = ?
                    WHERE event_id = ?
                    """,
                    (worker_id, lease_until, row["event_id"]),
                )
                event_row = connection.execute(
                    """
                    SELECT event_id, event_type, payload_json, created_at, attempts,
                           lease_owner, lease_until
                    FROM outbox WHERE event_id = ?
                    """,
                    (row["event_id"],),
                ).fetchone()
                events.append(
                    OutboxEvent(
                        event_id=event_row["event_id"],
                        event_type=event_row["event_type"],
                        payload=json.loads(event_row["payload_json"]),
                        created_at=event_row["created_at"],
                        attempts=int(event_row["attempts"]),
                        lease_owner=event_row["lease_owner"],
                        lease_until=float(event_row["lease_until"]),
                    )
                )
        return events

    def ack(self, event_id: str, worker_id: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "delivered":
                return True
            cursor = connection.execute(
                """
                UPDATE outbox
                SET status = 'delivered', delivered_at = ?,
                    lease_owner = NULL, lease_until = NULL
                WHERE event_id = ? AND status = 'claimed'
                  AND lease_owner = ? AND lease_until > ?
                """,
                (utc_timestamp(), event_id, worker_id, current),
            )
            return cursor.rowcount == 1

    def nack(
        self,
        event_id: str,
        worker_id: str,
        error: str,
        *,
        retry_delay_seconds: float = 0.0,
        now: float | None = None,
    ) -> bool:
        if not isinstance(error, str) or not error.strip():
            raise ValidationError("error must contain a short description")
        if retry_delay_seconds < 0:
            raise ValidationError("retry_delay_seconds must be non-negative")
        current = time.time() if now is None else now
        delay = min(retry_delay_seconds, self.max_retry_delay_seconds)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            status = "dead" if attempts >= self.max_attempts else "pending"
            cursor = connection.execute(
                """
                UPDATE outbox
                SET status = ?, available_at = ?, last_error = ?,
                    lease_owner = NULL, lease_until = NULL
                WHERE event_id = ? AND status = 'claimed'
                  AND lease_owner = ? AND lease_until > ?
                """,
                (
                    status,
                    current + delay,
                    error.strip()[:500],
                    event_id,
                    worker_id,
                    current,
                ),
            )
            return cursor.rowcount == 1

    def status(self, event_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            return row["status"] if row else None

    def count(self, *, status: str | None = None) -> int:
        with self.database.connect() as connection:
            if status is None:
                row = connection.execute("SELECT COUNT(*) AS count FROM outbox").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM outbox WHERE status = ?", (status,)
                ).fetchone()
            return int(row["count"])
