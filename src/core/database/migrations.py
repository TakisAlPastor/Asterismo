"""Transactional schema migration engine.

Tracks applied schema versions and supports clean rollbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from src.core.config.constants import (
    MIGRATION_V1_NAME,
    MIGRATION_V1_VERSION,
)
from src.core.database.connection import DatabaseError, DatabaseManager


@dataclass(frozen=True, slots=True)
class Migration:
    """Represents a discrete database schema migration."""

    version: int
    name: str
    up_sql: str
    down_sql: str


# V1: Only settings and migrations table
MIGRATION_V1 = Migration(
    version=MIGRATION_V1_VERSION,
    name=MIGRATION_V1_NAME,
    up_sql="""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    down_sql="""
    DROP TABLE IF EXISTS app_settings;
    """,
)


class MigrationManager:
    """Executes schema migrations in transactional sequence."""

    def __init__(
        self,
        db: DatabaseManager,
        migrations: tuple[Migration, ...] = (MIGRATION_V1,),
    ) -> None:
        self._db = db
        # Sort migrations ascending by version
        self._migrations = tuple(sorted(migrations, key=lambda m: m.version))

    def ensure_migrations_table(self) -> None:
        """Create schema_migrations table if not already present."""
        with self._db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )

    def current_version(self) -> int:
        """Return the highest applied schema version or 0."""
        self.ensure_migrations_table()
        row = self._db.query_one(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations")
        if row is None:
            return 0
        return int(row["version"])

    def apply_all(self) -> int:
        """Apply all pending migrations in ascending order. Returns count of applied migrations."""
        self.ensure_migrations_table()
        current = self.current_version()
        applied_count = 0

        for migration in self._migrations:
            if migration.version > current:
                with self._db.transaction() as conn:
                    try:
                        conn.executescript(migration.up_sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                            (migration.version, migration.name),
                        )
                        applied_count += 1
                    except sqlite3.Error as err:
                        raise DatabaseError(
                            f"Migration {migration.version} ({migration.name}) failed: {err}"
                        ) from err

        return applied_count

    def rollback_to(self, target_version: int) -> int:
        """Rollback migrations with version > target_version in reverse order. Returns count of rolled back migrations."""
        self.ensure_migrations_table()
        current = self.current_version()
        if target_version >= current:
            return 0

        rolled_back_count = 0
        # Iterate in reverse version order
        for migration in reversed(self._migrations):
            if migration.version <= current and migration.version > target_version:
                with self._db.transaction() as conn:
                    try:
                        conn.executescript(migration.down_sql)
                        conn.execute(
                            "DELETE FROM schema_migrations WHERE version = ?",
                            (migration.version,),
                        )
                        rolled_back_count += 1
                    except sqlite3.Error as err:
                        raise DatabaseError(
                            f"Rollback of migration {migration.version} ({migration.name}) failed: {err}"
                        ) from err

        return rolled_back_count
