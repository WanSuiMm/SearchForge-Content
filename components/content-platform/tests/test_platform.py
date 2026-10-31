"""Focused unit and integration checks for the offline framework."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from content_platform.application import create_application  # noqa: E402
from content_platform.cache.local import LocalVersionedTTLCache  # noqa: E402
from content_platform.errors import IdempotencyConflict, InvalidCursor  # noqa: E402
from content_platform.jobs.outbox import OutboxRepository  # noqa: E402
from content_platform.jobs.worker import OutboxWorker  # noqa: E402
from content_platform.storage.migrations import MIGRATIONS  # noqa: E402
from content_platform.storage.database import Database  # noqa: E402


class ContentPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=PROJECT_ROOT)
        self.addCleanup(self.tempdir.cleanup)
        self.database_path = Path(self.tempdir.name) / "platform.sqlite3"
        self.app = create_application(self.database_path, initialize=True)
        self.app.identity.create_user("author", "Author")
        self.app.identity.create_user("reader", "Reader")
        self.app.identity.create_user("reader-2", "Second Reader")
        self.app.identity.follow("reader", "author")

    def publish(self, body: str = "first post", key: str = "publish-1"):
        return self.app.content.publish("author", body, key)

    def scalar(self, statement: str, parameters: tuple[object, ...] = ()) -> int:
        with self.app.database.connect() as connection:
            return int(connection.execute(statement, parameters).fetchone()[0])

    def test_database_initialization_is_explicit_and_migration_is_recorded(self) -> None:
        untouched = Path(self.tempdir.name) / "not-created.sqlite3"
        Database(untouched)
        self.assertFalse(untouched.exists())
        create_application(untouched)
        self.assertFalse(untouched.exists())
        self.app.database.initialize()
        with self.app.database.connect() as connection:
            versions = [
                int(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations")
            ]
        self.assertEqual(versions, [1, 2, 3, 4])

    def test_publish_and_outbox_rollback_as_one_transaction(self) -> None:
        with self.app.database.connect() as connection:
            before_epochs = {
                row["namespace"]: row["version"]
                for row in connection.execute("SELECT namespace, version FROM cache_epochs")
            }
        with self.app.database.connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_outbox_insert
                BEFORE INSERT ON outbox
                BEGIN
                    SELECT RAISE(ABORT, 'forced outbox failure');
                END
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.publish()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM contents"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM feed_entries"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox"), 0)
        with self.app.database.connect() as connection:
            after_epochs = {
                row["namespace"]: row["version"]
                for row in connection.execute("SELECT namespace, version FROM cache_epochs")
            }
        self.assertEqual(after_epochs, before_epochs)

    def test_publish_is_idempotent_per_user_and_conflicting_body_is_rejected(self) -> None:
        first = self.publish()
        replay = self.publish()
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.content.content_id, replay.content.content_id)
        with self.assertRaises(IdempotencyConflict):
            self.publish(body="different body")
        other = self.app.content.publish("reader", "same key, other user", "publish-1")
        self.assertNotEqual(first.content.content_id, other.content.content_id)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM contents"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox"), 2)

    def test_durable_cache_epochs_track_only_committed_feed_and_search_changes(self) -> None:
        def epochs() -> dict[str, str]:
            with self.app.database.connect() as connection:
                return {
                    row["namespace"]: row["version"]
                    for row in connection.execute("SELECT namespace, version FROM cache_epochs")
                }

        initial = epochs()
        published = self.publish(body="epoch indexed phrase", key="epoch-1")
        after_publish = epochs()
        self.assertNotEqual(after_publish["feed"], initial["feed"])
        self.assertNotEqual(after_publish["search"], initial["search"])

        replay = self.publish(body="epoch indexed phrase", key="epoch-1")
        self.assertTrue(replay.replayed)
        self.assertEqual(epochs(), after_publish)

        liked = self.app.interaction.like("reader", published.content.content_id)
        self.assertTrue(liked.liked)
        after_like = epochs()
        self.assertNotEqual(after_like["feed"], after_publish["feed"])
        self.assertNotEqual(after_like["search"], after_publish["search"])
        duplicate = self.app.interaction.like("reader", published.content.content_id)
        self.assertFalse(duplicate.liked)
        self.assertEqual(epochs(), after_like)

        self.assertTrue(
            self.app.interaction.repository.remove_like("reader", published.content.content_id)
        )
        after_unlike = epochs()
        self.assertNotEqual(after_unlike["feed"], after_like["feed"])
        self.assertNotEqual(after_unlike["search"], after_like["search"])

        self.app.identity.create_user("late-reader", "Late Reader")
        self.app.identity.follow("late-reader", "author")
        after_follow = epochs()
        self.assertNotEqual(after_follow["feed"], after_unlike["feed"])
        self.assertEqual(after_follow["search"], after_unlike["search"])

    def test_follow_backfill_and_keyset_feed_pages_do_not_repeat_items(self) -> None:
        self.publish(body="one", key="one")
        self.publish(body="two", key="two")
        self.publish(body="three", key="three")
        first_page = self.app.feed.get_feed("reader", limit=2)
        self.assertEqual(len(first_page.items), 2)
        self.assertIsNotNone(first_page.next_cursor)
        second_page = self.app.feed.get_feed(
            "reader", limit=2, cursor=first_page.next_cursor
        )
        ids = [item.content_id for item in first_page.items + second_page.items]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertIsNone(second_page.next_cursor)
        with self.assertRaises(InvalidCursor):
            self.app.feed.get_feed("reader", cursor="not-a-cursor")

    def test_follow_adds_existing_author_content_to_feed(self) -> None:
        self.app.identity.create_user("late-reader", "Late Reader")
        published = self.publish()
        self.app.identity.follow("late-reader", "author")
        page = self.app.feed.get_feed("late-reader")
        self.assertEqual([item.content_id for item in page.items], [published.content.content_id])

    def test_like_rows_are_unique_and_trigger_keeps_counts_consistent(self) -> None:
        published = self.publish()
        first = self.app.interaction.like("reader", published.content.content_id)
        duplicate = self.app.interaction.like("reader", published.content.content_id)
        second_user = self.app.interaction.like("reader-2", published.content.content_id)
        self.assertTrue(first.liked)
        self.assertFalse(duplicate.liked)
        self.assertEqual(second_user.like_count, 2)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM likes WHERE content_id = ?",
                (published.content.content_id,),
            ),
            2,
        )
        self.assertEqual(
            self.scalar(
                "SELECT like_count FROM contents WHERE content_id = ?",
                (published.content.content_id,),
            ),
            2,
        )

    def test_expired_outbox_lease_recovers_and_consumer_replay_is_idempotent(self) -> None:
        self.publish()
        outbox = self.app.outbox
        now = time.time()
        first_claim = outbox.claim("worker-a", lease_seconds=5, now=now)
        self.assertEqual(len(first_claim), 1)
        self.assertEqual(outbox.claim("worker-b", now=now + 4), [])
        recovered = outbox.claim("worker-b", lease_seconds=5, now=now + 5)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].attempts, 2)
        self.assertFalse(outbox.ack(recovered[0].event_id, "worker-a", now=now + 5))
        self.assertTrue(self.app.consumer.consume(recovered[0]))
        self.assertFalse(self.app.consumer.consume(recovered[0]))
        self.assertTrue(outbox.ack(recovered[0].event_id, "worker-b", now=now + 5))
        self.assertTrue(outbox.ack(recovered[0].event_id, "worker-b", now=now + 6))
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM notifications WHERE recipient_id = 'reader'"
            ),
            1,
        )
        self.assertEqual(outbox.status(recovered[0].event_id), "delivered")
        self.assertEqual(outbox.claim("worker-c", now=now + 10), [])

    def test_outbox_retry_limit_moves_event_to_dead_letter_state(self) -> None:
        self.publish()
        outbox = OutboxRepository(self.app.database, max_attempts=2, max_retry_delay_seconds=10)
        now = time.time()
        first = outbox.claim("worker-a", now=now)[0]
        self.assertTrue(outbox.nack(first.event_id, "worker-a", "temporary", now=now))
        second = outbox.claim("worker-b", now=now)[0]
        self.assertEqual(second.attempts, 2)
        self.assertTrue(outbox.nack(second.event_id, "worker-b", "still failing", now=now))
        self.assertEqual(outbox.status(second.event_id), "dead")
        self.assertEqual(outbox.claim("worker-c", now=now + 100), [])

    def test_local_cache_ttl_and_namespace_version_invalidation(self) -> None:
        current = [10.0]
        cache = LocalVersionedTTLCache(max_entries=2, clock=lambda: current[0])
        cache.set("feed:reader", "page:1", {"ids": ["a"]}, ttl_seconds=5)
        self.assertEqual(cache.get("feed:reader", "page:1"), {"ids": ["a"]})
        stale_snapshot = cache.get_snapshot("feed:reader", "page:2")
        cache.invalidate("feed:reader")
        self.assertIsNone(cache.get("feed:reader", "page:1"))
        self.assertFalse(
            cache.set_if_current(
                "feed:reader",
                "page:2",
                {"ids": ["stale"]},
                ttl_seconds=5,
                expected_version=stale_snapshot.version,
            )
        )
        self.assertIsNone(cache.get("feed:reader", "page:2"))
        cache.set("feed:reader", "page:2", ["b"], ttl_seconds=5)
        current[0] = 15.0
        self.assertIsNone(cache.get("feed:reader", "page:2"))

    def test_api_facade_returns_serializable_shapes(self) -> None:
        response = self.app.api.publish("author", "api post", "api-key")
        self.assertFalse(response["replayed"])
        feed = self.app.api.get_feed("reader", limit=5)
        self.assertEqual(feed["items"][0]["content_id"], response["content"]["content_id"])
        self.assertEqual(self.app.api.like("reader", response["content"]["content_id"]), {
            "liked": True,
            "like_count": 1,
        })

    def test_fts5_search_returns_indexed_content(self) -> None:
        self.publish(body="SQLite FTS indexed retrieval transaction", key="search-1")
        results = self.app.search.search("indexed retrieval")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].body, "SQLite FTS indexed retrieval transaction")
        self.assertEqual(self.app.search.search("no-match-token"), [])

    def test_schema_v2_migration_backfills_search_for_existing_content(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy-v1.sqlite3"
        legacy = Database(legacy_path)
        with legacy.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                for statement in MIGRATIONS[1]:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO users(user_id, display_name, created_at) VALUES (?, ?, ?)",
                    ("legacy-author", "Legacy Author", "2026-01-01T00:00:00Z"),
                )
                connection.execute(
                    """INSERT INTO contents(content_id, author_id, body, published_at,
                       idempotency_key, request_hash) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "legacy-content", "legacy-author", "backfilled search phrase",
                        "2026-01-01T00:00:00Z", "legacy-key", "legacy-hash",
                    ),
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                    ("2026-01-01T00:00:00Z",),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        legacy.initialize()
        migrated_app = create_application(legacy_path)
        results = migrated_app.search.search("backfilled phrase")
        self.assertEqual([row.content_id for row in results], ["legacy-content"])

    def test_hashed_auth_sessions_and_background_worker_start_stop(self) -> None:
        issued = self.app.auth.register("secure-user", "Secure User", "correct-horse-battery")
        token = str(issued["access_token"])
        self.assertEqual(self.app.auth.authenticate(token), "secure-user")
        with self.app.database.connect() as connection:
            stored_hash = connection.execute(
                "SELECT token_hash FROM auth_sessions WHERE user_id = ?", ("secure-user",)
            ).fetchone()[0]
        self.assertEqual(len(stored_hash), 64)
        self.assertNotEqual(stored_hash, token)

        self.publish()
        worker = OutboxWorker(
            self.app.outbox,
            self.app.consumer,
            poll_interval_seconds=0.01,
            worker_id="unit-worker",
        )
        worker.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.app.interaction.repository.notification_count("reader") == 0:
            time.sleep(0.01)
        worker.stop(timeout_seconds=2)
        self.assertFalse(worker.is_alive)
        self.assertEqual(self.app.interaction.repository.notification_count("reader"), 1)


if __name__ == "__main__":
    unittest.main()
