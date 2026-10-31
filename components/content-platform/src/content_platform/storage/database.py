"""Explicit SQLite or opt-in PostgreSQL connection and migration boundary.

Database instances hold only a path/DSN and settings. Every operation opens and
closes its own connection; callers must initialize explicitly before use.
"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Sequence
import re

from .migrations import MIGRATIONS, POSTGRES_MIGRATIONS


def _is_postgresql_dsn(value: str) -> bool:
    return value.startswith(("postgresql://", "postgres://"))


def _qmark_to_pyformat(statement: str) -> str:
    """Translate DB-API qmark placeholders outside SQL strings/comments."""
    output: list[str] = []
    index = 0
    length = len(statement)
    while index < length:
        char = statement[index]
        if char in ("'", '"'):
            quote = char
            output.append(char)
            index += 1
            while index < length:
                current = statement[index]
                output.append(current)
                index += 1
                if current == quote:
                    if index < length and statement[index] == quote:
                        output.append(statement[index])
                        index += 1
                        continue
                    break
            continue
        if statement.startswith("--", index):
            end = statement.find("\n", index)
            if end < 0:
                output.append(statement[index:])
                break
            output.append(statement[index:end + 1])
            index = end + 1
            continue
        if statement.startswith("/*", index):
            end = statement.find("*/", index + 2)
            if end < 0:
                output.append(statement[index:])
                break
            output.append(statement[index:end + 2])
            index = end + 2
            continue
        if char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$", statement[index:])
            if match:
                delimiter = match.group(0)
                end = statement.find(delimiter, index + len(delimiter))
                if end < 0:
                    output.append(statement[index:])
                    break
                after = end + len(delimiter)
                output.append(statement[index:after])
                index = after
                continue
        output.append("%s" if char == "?" else char)
        index += 1
    return "".join(output)


class _PostgresConnection:
    """Small DB-API compatibility layer for repositories written with qmarks."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, statement: str, parameters: Sequence[object] = ()) -> Any:
        return self._connection.execute(_qmark_to_pyformat(statement), parameters)

    def executemany(
        self, statement: str, parameters: Iterable[Sequence[object]]
    ) -> Any:
        cursor = self._connection.cursor()
        try:
            cursor.executemany(_qmark_to_pyformat(statement), parameters)
            return cursor.rowcount
        finally:
            cursor.close()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


class Database:
    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = str(path)
        self.dialect = "postgresql" if _is_postgresql_dsn(self.path) else "sqlite"
        if self.dialect == "sqlite" and self.path == ":memory:":
            raise ValueError("Use a file-backed database; each operation owns a connection.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self._uri = self.dialect == "sqlite" and self.path.startswith("file:")

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.dialect == "postgresql":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL storage requires the explicit 'postgres' extra: "
                    "python -m pip install -e '.[postgres]'"
                ) from exc
            raw_connection = psycopg.connect(
                self.path,
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=max(1, int(self.timeout_seconds)),
            )
            try:
                yield _PostgresConnection(raw_connection)
            finally:
                raw_connection.close()
            return

        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
            check_same_thread=True,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[Any]:
        with self.connect() as connection:
            begin = "BEGIN IMMEDIATE" if immediate and self.dialect == "sqlite" else "BEGIN"
            connection.execute(begin)
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        """Create the parent directory and apply all unapplied migrations."""
        if self.dialect == "sqlite":
            path = Path(self.path)
            if not self._uri:
                path.parent.mkdir(parents=True, exist_ok=True)
        migrations = MIGRATIONS if self.dialect == "sqlite" else POSTGRES_MIGRATIONS
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            known_versions = set(migrations)
            if applied - known_versions:
                raise RuntimeError(
                    f"Database has unsupported migration versions: {sorted(applied - known_versions)}"
                )
            from ..timeutil import utc_timestamp

            for version, statements in sorted(migrations.items()):
                if version in applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_timestamp()),
                )

    def is_integrity_error(self, error: BaseException) -> bool:
        if self.dialect == "sqlite":
            return isinstance(error, sqlite3.IntegrityError)
        return str(getattr(error, "sqlstate", "")).startswith("23")

    def is_operational_error(self, error: BaseException) -> bool:
        if self.dialect == "sqlite":
            return isinstance(error, sqlite3.OperationalError)
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))

    def is_unique_violation(
        self, error: BaseException, *constraint_names: str
    ) -> bool:
        if self.dialect == "sqlite":
            return isinstance(error, sqlite3.IntegrityError) and (
                "UNIQUE constraint failed" in str(error)
            )
        if getattr(error, "sqlstate", None) != "23505":
            return False
        diagnostic = getattr(error, "diag", None)
        return getattr(diagnostic, "constraint_name", None) in constraint_names

    def is_user_id_conflict(self, error: BaseException) -> bool:
        if self.dialect == "sqlite":
            return isinstance(error, sqlite3.IntegrityError) and (
                "UNIQUE constraint failed: users.user_id" in str(error)
            )
        return self.is_unique_violation(error, "users_pkey")
