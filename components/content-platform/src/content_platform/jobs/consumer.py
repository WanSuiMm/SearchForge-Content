"""Example idempotent notification consumer for content.published events."""

from ..contracts import EventEnvelope
from ..errors import ValidationError
from ..storage.database import Database
from ..timeutil import utc_timestamp


class NotificationConsumer:
    def __init__(self, database: Database, *, consumer_name: str = "notifications-v1") -> None:
        if not consumer_name or len(consumer_name) > 100:
            raise ValueError("consumer_name must contain 1 to 100 characters")
        self.database = database
        self.consumer_name = consumer_name

    def consume(self, event: EventEnvelope) -> bool:
        """Apply one event once; return False when its receipt already exists."""
        if event.event_type != "content.published":
            raise ValidationError(f"unsupported event type: {event.event_type}")
        content_id = event.payload.get("content_id")
        recipient_ids = event.payload.get("recipient_ids")
        if not isinstance(content_id, str) or not isinstance(recipient_ids, list):
            raise ValidationError("content.published payload is malformed")
        if any(not isinstance(recipient_id, str) for recipient_id in recipient_ids):
            raise ValidationError("recipient_ids must contain strings")

        with self.database.transaction() as connection:
            created_at = utc_timestamp()
            receipt = connection.execute(
                """
                INSERT INTO consumer_receipts(consumer_name, event_id, processed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(consumer_name, event_id) DO NOTHING
                """,
                (self.consumer_name, event.event_id, created_at),
            )
            if receipt.rowcount == 0:
                return False
            connection.executemany(
                """
                INSERT INTO notifications(event_id, recipient_id, content_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id, recipient_id) DO NOTHING
                """,
                [
                    (event.event_id, recipient_id, content_id, created_at)
                    for recipient_id in recipient_ids
                ],
            )
        return True
