"""Offline checks for the opt-in PostgreSQL DB-API adapter and SQL paths."""

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from content_platform.jobs.outbox import OutboxRepository  # noqa: E402
from content_platform.projection import ProjectionEventSource  # noqa: E402
from content_platform.storage.database import Database, _qmark_to_pyformat  # noqa: E402
from content_platform.storage.migrations import POSTGRES_MIGRATIONS  # noqa: E402
from content_platform.storage.repositories import SearchRepository  # noqa: E402


class FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeDriverCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = -1
        self.closed = False

    def executemany(self, statement, parameters):
        rows = list(parameters)
        self.connection.executemany_calls.append((statement, rows))
        self.rowcount = len(rows)

    def close(self):
        self.closed = True


class FakeRawConnection:
    def __init__(self):
        self.executed = []
        self.executemany_calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.cursors = []

    def execute(self, statement, parameters=()):
        self.executed.append((statement, parameters))
        return FakeCursor()

    def cursor(self):
        cursor = FakeDriverCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class LeaseConnection(FakeRawConnection):
    def __init__(self, *, update_rowcount=1):
        super().__init__()
        self.update_rowcount = update_rowcount

    def execute(self, statement, parameters=()):
        self.executed.append((statement, parameters))
        if "SELECT status FROM outbox" in statement:
            return FakeCursor([{"status": "claimed"}])
        if "SELECT attempts FROM outbox" in statement:
            return FakeCursor([{"attempts": 1}])
        return FakeCursor(rowcount=self.update_rowcount)


class KnownProjectionConnection(FakeRawConnection):
    def execute(self, statement, parameters=()):
        self.executed.append((statement, parameters))
        if "SELECT 1 FROM projection_events" in statement:
            return FakeCursor([{"known": 1}])
        return FakeCursor(rowcount=1)


def fake_psycopg(raw_connection):
    psycopg = ModuleType("psycopg")
    psycopg.connect = lambda *args, **kwargs: raw_connection
    rows = ModuleType("psycopg.rows")
    rows.dict_row = object()
    return psycopg, rows


class StubDatabase:
    dialect = "postgresql"

    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection

    @contextmanager
    def transaction(self, *, immediate=True):
        yield self.connection


class PostgresStorageTests(unittest.TestCase):
    def test_postgresql_dsn_selects_explicit_backend_without_creating_a_file(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as tempdir:
            marker = Path(tempdir) / "not-a-sqlite-file"
            database = Database("postgresql://user:secret@db.example/content")

            self.assertEqual(database.dialect, "postgresql")
            self.assertFalse(marker.exists())
            self.assertEqual(Database(Path(tempdir) / "service.sqlite3").dialect, "sqlite")

    def test_missing_optional_driver_fails_explicitly_without_sqlite_fallback(self):
        database = Database("postgresql://user:secret@db.example/content")
        with patch.dict(sys.modules, {"psycopg": None, "psycopg.rows": None}):
            with self.assertRaisesRegex(RuntimeError, "explicit 'postgres' extra"):
                with database.connect():
                    self.fail("a missing driver must not open another backend")

    def test_placeholder_translation_ignores_sql_literals_comments_and_dollar_body(self):
        statement = "SELECT '?', \"?\", value FROM t WHERE a = ? -- ?\n/* ? */ AND b = $tag$?$tag$"
        self.assertEqual(
            _qmark_to_pyformat(statement),
            "SELECT '?', \"?\", value FROM t WHERE a = %s -- ?\n/* ? */ AND b = $tag$?$tag$",
        )

    def test_connection_uses_mapping_rows_and_closes_underlying_driver(self):
        raw = FakeRawConnection()
        psycopg, rows = fake_psycopg(raw)
        with patch.dict(sys.modules, {"psycopg": psycopg, "psycopg.rows": rows}):
            database = Database("postgresql://user:secret@db.example/content")
            with database.connect() as connection:
                connection.execute("SELECT '?' WHERE user_id = ?", ("reader",))
                inserted = connection.executemany(
                    "INSERT INTO follows(follower_id) VALUES (?)", [("a",), ("b",)]
                )

        self.assertTrue(raw.closed)
        self.assertEqual(inserted, 2)
        self.assertTrue(raw.cursors[0].closed)
        self.assertEqual(
            raw.executemany_calls[0],
            ("INSERT INTO follows(follower_id) VALUES (%s)", [("a",), ("b",)]),
        )
        self.assertEqual(
            raw.executed[0],
            ("SELECT '?' WHERE user_id = %s", ("reader",)),
        )

    def test_postgres_transactions_commit_and_rollback_explicitly(self):
        raw = FakeRawConnection()
        psycopg, rows = fake_psycopg(raw)
        with patch.dict(sys.modules, {"psycopg": psycopg, "psycopg.rows": rows}):
            database = Database("postgresql://user:secret@db.example/content")
            with database.transaction():
                pass
            with self.assertRaisesRegex(ValueError, "abort"):
                with database.transaction():
                    raise ValueError("abort")

        self.assertEqual([sql for sql, _ in raw.executed], ["BEGIN", "BEGIN"])
        self.assertEqual((raw.commits, raw.rollbacks), (1, 1))

    def test_integrity_and_unique_conflicts_use_postgres_sqlstate_and_constraint(self):
        database = Database("postgresql://user:secret@db.example/content")
        duplicate_user = SimpleNamespace(
            sqlstate="23505", diag=SimpleNamespace(constraint_name="users_pkey")
        )
        foreign_key = SimpleNamespace(
            sqlstate="23503", diag=SimpleNamespace(constraint_name="notifications_recipient_id_fkey")
        )

        self.assertTrue(database.is_integrity_error(duplicate_user))
        self.assertTrue(database.is_user_id_conflict(duplicate_user))
        self.assertTrue(database.is_integrity_error(foreign_key))
        self.assertFalse(database.is_user_id_conflict(foreign_key))

    def test_postgres_migrations_include_search_outbox_and_durable_epochs(self):
        sql = "\n".join(statement for version in POSTGRES_MIGRATIONS.values() for statement in version)

        self.assertIn("tsvector", sql)
        self.assertIn("USING GIN", sql)
        self.assertIn("to_tsvector('simple', NEW.body)", sql)
        self.assertIn("CREATE TABLE outbox", sql)
        self.assertIn("CREATE TABLE consumer_receipts", sql)
        self.assertIn("CREATE TABLE cache_epochs", sql)
        self.assertIn("CREATE TABLE projection_events", sql)
        self.assertIn("CREATE TABLE projection_event_receipts", sql)
        self.assertIn("projection_events_immutable", sql)

    def test_postgres_search_uses_all_terms_and_tsvector_rank(self):
        connection = FakeRawConnection()
        repository = SearchRepository(StubDatabase(connection))

        self.assertEqual(repository.search('"moss" AND "forest"', limit=7), [])
        sql, parameters = connection.executed[0]
        self.assertIn("plainto_tsquery('simple', ?)", sql)
        self.assertIn("c.search_vector @@ search_query.tsquery", sql)
        self.assertIn("ts_rank(c.search_vector", sql)
        self.assertEqual(parameters, ("moss forest", 7))
        self.assertNotIn("content_search MATCH", sql)

    def test_postgres_outbox_claim_uses_skip_locked(self):
        connection = FakeRawConnection()
        repository = OutboxRepository(StubDatabase(connection))

        self.assertEqual(repository.claim("relay-a", now=100.0), [])
        claim_sql = connection.executed[1][0]
        self.assertIn("FOR UPDATE SKIP LOCKED", claim_sql)

    def test_projection_pending_and_ack_use_independent_consumer_receipts(self):
        pending_connection = FakeRawConnection()
        pending_source = ProjectionEventSource(StubDatabase(pending_connection))
        self.assertEqual(pending_source.pending("consumer-a", limit=17), [])
        pending_sql, pending_parameters = pending_connection.executed[0]
        self.assertIn("NOT EXISTS", pending_sql)
        self.assertIn("projection_event_receipts", pending_sql)
        self.assertIn("ORDER BY e.occurred_at ASC, e.event_id ASC", pending_sql)
        self.assertNotIn("outbox", pending_sql)
        self.assertEqual(pending_parameters, ("consumer-a", 17))

        ack_connection = KnownProjectionConnection()
        ack_source = ProjectionEventSource(StubDatabase(ack_connection))
        self.assertTrue(ack_source.acknowledge("consumer-a", "event-1"))
        ack_sql = ack_connection.executed[1][0]
        self.assertIn("INSERT INTO projection_event_receipts", ack_sql)
        self.assertIn("ON CONFLICT(consumer_id, event_id) DO NOTHING", ack_sql)
        self.assertNotIn("consumer_receipts", ack_sql)

    def test_postgres_lease_ack_and_nack_are_owner_and_expiry_conditional(self):
        ack_connection = LeaseConnection()
        repository = OutboxRepository(StubDatabase(ack_connection))
        self.assertTrue(repository.ack("event-1", "relay-a", now=100.0))
        ack_sql = ack_connection.executed[1][0]
        self.assertIn("lease_owner = ?", ack_sql)
        self.assertIn("lease_until > ?", ack_sql)

        stale_connection = LeaseConnection(update_rowcount=0)
        stale_repository = OutboxRepository(StubDatabase(stale_connection))
        self.assertFalse(stale_repository.ack("event-1", "relay-old", now=100.0))
        self.assertFalse(
            stale_repository.nack("event-1", "relay-old", "send failed", now=100.0)
        )
        nack_sql = stale_connection.executed[3][0]
        self.assertIn("lease_owner = ?", nack_sql)
        self.assertIn("lease_until > ?", nack_sql)


if __name__ == "__main__":
    unittest.main()
