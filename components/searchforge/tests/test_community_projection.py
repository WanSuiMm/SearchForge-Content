from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from searchforge.community import CommunityIndex, EventConflictError  # noqa: E402


def make_event(
    *,
    event_id: str,
    content_id: str = "post-1",
    version: int = 1,
    body: str = "",
    event_type: str = "content.published",
    visibility: str = "public",
    author_id: str = "author-1",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "content_id": content_id,
        "version": version,
        "visibility": visibility,
        "author_id": author_id,
        "body": body,
        "published_at": "2026-10-20T09:00:00Z",
        "occurred_at": f"2026-10-20T09:{version:02d}:00Z",
        "source_uri": f"/v1/content/{content_id}",
    }


class CommunityProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "community.sqlite3"
        self.index = CommunityIndex(self.database)
        self.index.initialize()

    def tearDown(self) -> None:
        self.index.close()
        self.temporary.cleanup()

    def test_initialize_refuses_unowned_database_without_mutating_it(self) -> None:
        source_database = Path(self.temporary.name) / "source.sqlite3"
        connection = sqlite3.connect(source_database)
        try:
            connection.execute("CREATE TABLE source_records (record_id TEXT PRIMARY KEY, body TEXT)")
            connection.execute(
                "INSERT INTO source_records(record_id, body) VALUES ('source-1', 'preserve me')"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ValueError, "nonempty database"):
            CommunityIndex(source_database).initialize()

        connection = sqlite3.connect(source_database)
        try:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            source_row = connection.execute(
                "SELECT record_id, body FROM source_records"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(table_names, {"source_records"})
        self.assertEqual(source_row, ("source-1", "preserve me"))

        connection = sqlite3.connect(source_database)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(journal_mode.lower(), "delete")

        # On Windows an unclosed failed initialization connection would keep
        # this database file locked against deletion.
        source_database.unlink()

    def test_initialize_rejects_community_table_without_valid_marker(self) -> None:
        malformed_database = Path(self.temporary.name) / "malformed.sqlite3"
        connection = sqlite3.connect(malformed_database)
        try:
            connection.execute(
                "CREATE TABLE community_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO community_meta(key, value) VALUES ('other', '1')"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ValueError, "ownership marker"):
            CommunityIndex(malformed_database).initialize()

        connection = sqlite3.connect(malformed_database)
        try:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            connection.close()
        self.assertEqual(table_names, {"community_meta"})
        connection = sqlite3.connect(malformed_database)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(journal_mode.lower(), "delete")

    def test_persisted_search_and_safe_multilingual_query(self) -> None:
        event = make_event(
            event_id="evt-1",
            body="A café guide for 東京宇宙観測 and observatory visitors.",
        )
        self.assertEqual(self.index.apply(event)["outcome"], "applied")

        results = self.index.search("café 東京宇宙観測", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["content_id"], "post-1")
        self.assertEqual(results[0]["author_id"], "author-1")
        self.assertGreaterEqual(results[0]["score"], 0.0)
        self.assertEqual(self.index.get("post-1")["version"], 1)

        # Operators and punctuation are treated as ordinary query text, never
        # interpolated as FTS5 syntax.
        self.assertEqual(self.index.search('OR "* NEAR/2 MATCH'), [])
        self.assertEqual(self.index.search("!!!"), [])

        self.index.close()
        restarted = CommunityIndex(self.database)
        restarted.initialize()
        self.assertEqual(restarted.search("observatory")[0]["content_id"], "post-1")
        restarted.close()

    def test_duplicate_event_and_payload_conflicts(self) -> None:
        event = make_event(event_id="evt-duplicate", body="durable idempotency")
        self.assertEqual(self.index.apply(event)["outcome"], "applied")
        self.assertEqual(self.index.apply(event)["outcome"], "duplicate")
        self.assertEqual(self.index.get_receipt("evt-duplicate")["outcome"], "applied")

        reused_event_id = dict(event, body="changed body")
        with self.assertRaises(EventConflictError):
            self.index.apply(reused_event_id)

        reused_version = dict(event, event_id="evt-other", body="different payload")
        with self.assertRaises(EventConflictError):
            self.index.apply(reused_version)
        self.assertEqual(self.index.get("post-1")["body"], "durable idempotency")

    def test_out_of_order_events_are_stale_and_cannot_replace_newer_content(self) -> None:
        newest = make_event(
            event_id="evt-v2", version=2, body="current moonstone handbook"
        )
        oldest = make_event(
            event_id="evt-v1", version=1, body="obsolete moonstone handbook"
        )
        self.assertEqual(self.index.apply(newest)["outcome"], "applied")
        self.assertEqual(self.index.apply(oldest)["outcome"], "stale")
        self.assertEqual(self.index.get("post-1")["version"], 2)
        self.assertEqual(self.index.get("post-1")["body"], "current moonstone handbook")
        self.assertEqual(self.index.search("obsolete"), [])
        self.assertEqual(self.index.apply(oldest)["outcome"], "duplicate")

        conflicting_oldest = dict(oldest, event_id="evt-v1-conflict", body="stale rewrite")
        with self.assertRaises(EventConflictError):
            self.index.apply(conflicting_oldest)

    def test_tombstone_survives_restart_and_old_publish_cannot_resurrect(self) -> None:
        published = make_event(event_id="evt-live", body="private comet schedule")
        withdrawn = make_event(
            event_id="evt-withdrawn",
            version=2,
            body="",
            event_type="content.withdrawn",
            visibility="withdrawn",
        )
        self.index.apply(published)
        self.index.apply(withdrawn)
        self.assertEqual(self.index.search("comet"), [])
        self.assertIsNone(self.index.get("post-1"))
        self.assertEqual(self.index.stats()["withdrawn_documents"], 1)

        self.index.close()
        self.index = CommunityIndex(self.database)
        self.index.initialize()
        self.assertEqual(self.index.apply(published)["outcome"], "duplicate")
        self.assertEqual(self.index.search("comet"), [])
        self.assertIsNone(self.index.get("post-1"))

    def test_invalid_event_has_no_partial_durable_state(self) -> None:
        before = self.index.stats()
        invalid = make_event(event_id="evt-invalid", body="bad")
        invalid["source_uri"] = "/v1/content/someone-else"
        with self.assertRaisesRegex(ValueError, "source_uri"):
            self.index.apply(invalid)
        self.assertEqual(self.index.stats(), before)
        self.assertIsNone(self.index.get_receipt("evt-invalid"))

    def test_sql_failure_rolls_back_version_receipt_document_and_fts(self) -> None:
        original = make_event(event_id="evt-v1", body="old orchid field guide")
        update = make_event(event_id="evt-v2", version=2, body="new orchid field guide")
        self.index.apply(original)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                CREATE TRIGGER reject_projection_update
                BEFORE UPDATE ON community_documents
                BEGIN SELECT RAISE(ABORT, 'injected projection write failure'); END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected projection"):
            self.index.apply(update)
        self.assertEqual(self.index.get("post-1")["version"], 1)
        self.assertEqual(self.index.search("old")[0]["content_id"], "post-1")
        self.assertEqual(self.index.search("new"), [])
        self.assertIsNone(self.index.get_receipt("evt-v2"))
        self.assertEqual(self.index.stats()["versions"], 1)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER reject_projection_update")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.index.apply(update)["outcome"], "applied")

    def test_input_bounds_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "query must be at most"):
            self.index.search("x" * 513)
        with self.assertRaisesRegex(ValueError, "at most 16 terms"):
            self.index.search(" ".join(f"term{i}" for i in range(17)))
        with self.assertRaisesRegex(ValueError, "limit"):
            self.index.search("term", limit=101)


if __name__ == "__main__":
    unittest.main()
