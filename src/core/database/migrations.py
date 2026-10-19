"""Transactional schema migration engine.

Tracks applied schema versions and supports clean rollbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from src.core.config.constants import (
    MIGRATION_V1_NAME,
    MIGRATION_V1_VERSION,
    MIGRATION_V2_NAME,
    MIGRATION_V2_VERSION,
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

# V2: Harvester queue, items deduplication, and pklz catalog
MIGRATION_V2 = Migration(
    version=MIGRATION_V2_VERSION,
    name=MIGRATION_V2_NAME,
    up_sql="""
    CREATE TABLE IF NOT EXISTS harvest_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_url TEXT NOT NULL,
        normalized_url TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        prefix_label TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        total_items INTEGER NOT NULL DEFAULT 0,
        completed_items INTEGER NOT NULL DEFAULT 0,
        skipped_items INTEGER NOT NULL DEFAULT 0,
        error_message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_harvest_queue_status ON harvest_queue(status);

    CREATE TABLE IF NOT EXISTS harvest_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        item_key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        duration_seconds REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        pklz_filename TEXT NOT NULL DEFAULT '',
        fingerprinted_at TEXT,
        FOREIGN KEY (task_id) REFERENCES harvest_queue(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_harvest_items_task_status ON harvest_items(task_id, status);

    CREATE TABLE IF NOT EXISTS pklz_catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        prefix_label TEXT NOT NULL,
        batch_index INTEGER NOT NULL DEFAULT 1,
        track_count INTEGER NOT NULL DEFAULT 0,
        total_hashes INTEGER NOT NULL DEFAULT 0,
        file_size_bytes INTEGER NOT NULL DEFAULT 0,
        sha256_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_pklz_catalog_prefix ON pklz_catalog(prefix_label);
    """,
    down_sql="""
    DROP INDEX IF EXISTS idx_pklz_catalog_prefix;
    DROP TABLE IF EXISTS pklz_catalog;
    DROP INDEX IF EXISTS idx_harvest_items_task_status;
    DROP TABLE IF EXISTS harvest_items;
    DROP INDEX IF EXISTS idx_harvest_queue_status;
    DROP TABLE IF EXISTS harvest_queue;
    """,
)

ALL_MIGRATIONS: tuple[Migration, ...] = (MIGRATION_V1, MIGRATION_V2)


class MigrationManager:
    """Executes schema migrations in transactional sequence."""

    def __init__(
        self,
        db: DatabaseManager,
        migrations: tuple[Migration, ...] = ALL_MIGRATIONS,
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
