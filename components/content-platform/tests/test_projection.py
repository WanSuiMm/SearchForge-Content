"""CPU checks for the independent public-content projection event source."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from content_platform.application import create_application  # noqa: E402
from content_platform.errors import ValidationError  # noqa: E402
from content_platform.jobs.worker import OutboxWorker  # noqa: E402
from content_platform.projection import ProjectionEventSource  # noqa: E402
from content_platform.storage.database import Database  # noqa: E402


class QueryCountingDatabase:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.statements: list[str] = []

    @contextmanager
    def connect(self):
        with self.database.connect() as connection:
            owner = self

            class ConnectionProxy:
                def execute(self, statement, parameters=()):
                    owner.statements.append(statement)
                    return connection.execute(statement, parameters)

            yield ConnectionProxy()


class ProjectionEventSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=PROJECT_ROOT)
        self.addCleanup(self.tempdir.cleanup)
        self.database_path = Path(self.tempdir.name) / "projection.sqlite3"
        self.app = create_application(self.database_path, initialize=True)
        self.app.identity.create_user("projection-author", "Projection Author")
        self.app.identity.create_user("projection-reader", "Projection Reader")
        self.app.identity.follow("projection-reader", "projection-author")
        self.source = ProjectionEventSource(self.app.database)

    def scalar(self, statement: str, parameters: tuple[object, ...] = ()) -> int:
        with self.app.database.connect() as connection:
            return int(connection.execute(statement, parameters).fetchone()[0])

    def test_publish_appends_one_versioned_public_event_and_replay_is_idempotent(self) -> None:
        created = self.app.content.publish(
            "projection-author", "public projection body", "projection-idempotency"
        )
        replay = self.app.content.publish(
            "projection-author", "public projection body", "projection-idempotency"
        )

        self.assertFalse(created.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM projection_events"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox"), 1)
        events = self.source.pending("searchforge-content-v1")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(
            set(event),
            {
                "schema_version", "event_id", "event_type", "content_id", "version",
                "visibility", "author_id", "body", "published_at", "occurred_at",
                "source_uri",
            },
        )
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["event_type"], "content.published")
        self.assertEqual(event["content_id"], created.content.content_id)
        self.assertEqual(event["version"], 1)
        self.assertEqual(event["visibility"], "public")
        self.assertEqual(event["author_id"], "projection-author")
        self.assertEqual(event["body"], "public projection body")
        self.assertEqual(event["source_uri"], f"/v1/content/{created.content.content_id}")
        self.assertEqual(event["occurred_at"], event["published_at"])
        self.assertEqual(
            self.source.get_public(created.content.content_id),
            {
                "content_id": created.content.content_id,
                "author_id": "projection-author",
                "body": "public projection body",
                "published_at": event["published_at"],
                "version": 1,
                "visibility": "public",
                "source_uri": f"/v1/content/{created.content.content_id}",
            },
        )
        self.assertIsNone(self.source.get_public("missing-content"))

    def test_projection_insert_failure_rolls_back_publication_and_notification_outbox(self) -> None:
        with self.app.database.connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_projection_insert
                BEFORE INSERT ON projection_events
                BEGIN
                    SELECT RAISE(ABORT, 'forced projection insert failure');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.app.content.publish(
                "projection-author", "must roll back", "projection-rollback"
            )

        self.assertEqual(self.scalar("SELECT COUNT(*) FROM contents"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM feed_entries"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM projection_events"), 0)

    def test_notification_outbox_drain_does_not_consume_projection_event(self) -> None:
        published = self.app.content.publish(
            "projection-author", "independent durable event", "projection-outbox"
        )
        worker = OutboxWorker(
            self.app.outbox,
            self.app.consumer,
            worker_id="projection-test-worker",
        )

        self.assertEqual(worker.run_once(), 1)
        with self.app.database.connect() as connection:
            outbox_event_id = connection.execute(
                "SELECT event_id FROM outbox WHERE event_type = 'content.published'"
            ).fetchone()[0]
        self.assertEqual(self.app.outbox.status(outbox_event_id), "delivered")
        self.assertEqual(len(self.app.notifications.list_for_user("projection-reader")), 1)
        events = self.source.pending("searchforge-content-v1")
        self.assertEqual([event["content_id"] for event in events], [published.content.content_id])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM projection_event_receipts"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM consumer_receipts"), 1)

    def test_receipts_are_per_consumer_idempotent_and_persist_across_source_restart(self) -> None:
        published = self.app.content.publish(
            "projection-author", "receipt isolation", "projection-receipts"
        )
        first_source = self.source.pending("consumer-a")
        second_source = self.source.pending("consumer-b")
        event_id = str(first_source[0]["event_id"])
        self.assertEqual(second_source[0]["event_id"], event_id)

        self.assertTrue(self.source.acknowledge("consumer-a", event_id))
        self.assertTrue(self.source.acknowledge("consumer-a", event_id))
        self.assertFalse(self.source.acknowledge("consumer-a", "unknown-event"))
        self.assertEqual(self.source.pending("consumer-a"), [])
        self.assertEqual(len(self.source.pending("consumer-b")), 1)

        restarted_source = ProjectionEventSource(Database(self.database_path))
        self.assertEqual(restarted_source.pending("consumer-a"), [])
        self.assertEqual(
            restarted_source.pending("consumer-b")[0]["content_id"],
            published.content.content_id,
        )

    def test_bulk_public_lookup_preserves_requested_order_and_omits_missing_ids(self) -> None:
        first = self.app.content.publish(
            "projection-author", "first bulk item", "projection-bulk-first"
        )
        second = self.app.content.publish(
            "projection-author", "second bulk item", "projection-bulk-second"
        )

        items = self.source.get_public_many(
            [second.content.content_id, "missing-content", first.content.content_id]
        )
        self.assertEqual(
            [item["content_id"] for item in items],
            [second.content.content_id, first.content.content_id],
        )
        self.assertEqual([item["body"] for item in items], ["second bulk item", "first bulk item"])
        self.assertEqual(self.source.get_public_many([]), [])

        counting_database = QueryCountingDatabase(self.app.database)
        counting_source = ProjectionEventSource(counting_database)  # type: ignore[arg-type]
        counting_source.get_public_many([first.content.content_id, second.content.content_id])
        content_selects = [sql for sql in counting_database.statements if "FROM contents" in sql]
        self.assertEqual(len(content_selects), 1)

    def test_bulk_public_lookup_rejects_duplicate_or_oversized_id_lists(self) -> None:
        with self.assertRaises(ValidationError):
            self.source.get_public_many(["same-id", "same-id"])
        with self.assertRaises(ValidationError):
            self.source.get_public_many([f"content-{index}" for index in range(21)])
        with self.assertRaises(ValidationError):
            self.source.get_public_many(["valid-id", 17])  # type: ignore[list-item]

    def test_projection_events_are_immutable_and_pending_limit_is_bounded(self) -> None:
        self.app.content.publish("projection-author", "immutable source row", "projection-immutable")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.app.database.transaction() as connection:
                connection.execute(
                    "UPDATE projection_events SET body = ?",
                    ("rewritten body",),
                )
        with self.assertRaises(ValidationError):
            self.source.pending("searchforge-content-v1", limit=101)
        with self.assertRaises(ValidationError):
            self.source.pending("searchforge-content-v1", limit=True)


if __name__ == "__main__":
    unittest.main()
