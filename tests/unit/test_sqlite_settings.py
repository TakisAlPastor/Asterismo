"""Unit tests for SQLite WAL connection, migrations, and SettingsRepository."""
from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import pytest

from src.core.config.constants import (
    MEMORY_DB_TARGET,
    SQLITE_JOURNAL_MODE,
)
from src.core.config.models import (
    AppSettings,
    ConfigurationError,
    ScanMode,
    ThemeMode,
    WorkspacePaths,
)
from src.core.database.connection import DatabaseError, DatabaseManager
from src.core.database.migrations import (
    MIGRATION_V1,
    Migration,
    MigrationManager,
)
from src.core.database.settings_repository import SettingsRepository


@pytest.fixture
def in_memory_db() -> Iterator[DatabaseManager]:
    """Fixture providing an active in-memory DatabaseManager."""
    db = DatabaseManager(MEMORY_DB_TARGET)
    yield db
    db.close()


def test_database_manager_enforces_wal_and_foreign_keys(tmp_path: Path) -> None:
    """Verify that file-backed SQLite connections activate WAL mode and foreign keys."""
    # Arrange
    db_file = tmp_path / "test_store.db"
    db = DatabaseManager(db_file)

    try:
        # Act
        conn = db.connect()
        cur_journal = conn.execute("PRAGMA journal_mode;").fetchone()
        cur_fk = conn.execute("PRAGMA foreign_keys;").fetchone()

        # Assert
        assert cur_journal[0].lower() == SQLITE_JOURNAL_MODE.lower()
        assert cur_fk[0] == 1
    finally:
        db.close()


def test_migration_manager_apply_and_rollback_v1(in_memory_db: DatabaseManager) -> None:
    """Verify applying V1 migration creates tables and rollback properly destroys them."""
    # Arrange
    manager = MigrationManager(in_memory_db, migrations=(MIGRATION_V1,))

    # Act 1: Initial state
    assert manager.current_version() == 0

    # Act 2: Apply V1
    applied = manager.apply_all()
    assert applied == 1
    assert manager.current_version() == 1

    # Verify app_settings table exists
    row = in_memory_db.query_one(
        "SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name='app_settings'"
    )
    assert row is not None
    assert row["cnt"] == 1

    # Act 3: Rollback to 0
    rolled_back = manager.rollback_to(0)
    assert rolled_back == 1
    assert manager.current_version() == 0

    # Verify app_settings table is dropped
    row_after = in_memory_db.query_one(
        "SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name='app_settings'"
    )
    assert row_after is not None
    assert row_after["cnt"] == 0


def test_migration_manager_is_idempotent(in_memory_db: DatabaseManager) -> None:
    """Verify subsequent calls to apply_all() do not re-apply existing migrations."""
    # Arrange
    manager = MigrationManager(in_memory_db, migrations=(MIGRATION_V1,))

    # Act
    assert manager.apply_all() == 1
    assert manager.apply_all() == 0
    assert manager.current_version() == 1


def test_settings_repository_returns_defaults_when_empty(
    in_memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify load() returns AppSettings.default when table is empty."""
    # Arrange
    MigrationManager(in_memory_db).apply_all()
    repo = SettingsRepository(in_memory_db, base_dir=tmp_path)

    # Act
    settings = repo.load()

    # Assert
    expected = AppSettings.default(tmp_path)
    assert settings.theme == expected.theme
    assert settings.scan_mode == expected.scan_mode
    assert settings.paths == expected.paths


def test_settings_repository_save_and_load_roundtrip(
    in_memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify save() upserts AppSettings and load() faithfully restores them."""
    # Arrange
    MigrationManager(in_memory_db).apply_all()
    repo = SettingsRepository(in_memory_db, base_dir=tmp_path)

    custom_paths = WorkspacePaths(
        input_dir=tmp_path / "custom_in",
        db_dir=tmp_path / "custom_db",
        output_dir=tmp_path / "custom_out",
        logs_dir=tmp_path / "custom_logs",
        bin_dir=tmp_path / "custom_bin",
        archive_dir=tmp_path / "custom_archive",
    )
    custom_settings = AppSettings(
        paths=custom_paths,
        theme=ThemeMode.LIGHT,
        scan_mode=ScanMode.LONG,
        split_long_audio=False,
        split_threshold_seconds=900,
        split_segment_seconds=450,
        max_concurrent_downloads=8,
        fp_worker_count=6,
        keep_downloaded_audio=True,
    )

    # Act 1: Initial Save & Load
    repo.save(custom_settings)
    loaded = repo.load()

    # Assert 1
    assert loaded.theme == ThemeMode.LIGHT
    assert loaded.scan_mode == ScanMode.LONG
    assert loaded.split_long_audio is False
    assert loaded.split_threshold_seconds == 900
    assert loaded.split_segment_seconds == 450
    assert loaded.max_concurrent_downloads == 8
    assert loaded.fp_worker_count == 6
    assert loaded.keep_downloaded_audio is True
    assert loaded.paths.input_dir == tmp_path / "custom_in"

    # Act 2: Update existing settings (tests UPSERT / ON CONFLICT)
    custom_settings.theme = ThemeMode.SYSTEM
    repo.save(custom_settings)
    updated = repo.load()

    # Assert 2
    assert updated.theme == ThemeMode.SYSTEM


def test_settings_repository_sql_injection_prevented(
    in_memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify that SQL injection payloads are safely stored as literal values."""
    # Arrange
    MigrationManager(in_memory_db).apply_all()
    repo = SettingsRepository(in_memory_db, base_dir=tmp_path)

    malicious_key = "test_key'; DROP TABLE app_settings; --"
    malicious_payload = json.dumps({"value": "admin' OR '1'='1"})

    # Act: Attempt to insert with malicious key and value
    repo.set_value(malicious_key, malicious_payload)
    read_back = repo.get_value(malicious_key)

    # Assert: Key and payload treated strictly as data
    assert read_back == malicious_payload

    # Assert: Table was NOT dropped
    row = in_memory_db.query_one(
        "SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name='app_settings'"
    )
    assert row is not None
    assert row["cnt"] == 1


def test_database_manager_transaction_rolls_back_on_error(
    in_memory_db: DatabaseManager,
) -> None:
    """Verify transaction context manager rolls back changes on exception."""
    # Arrange
    in_memory_db.execute(
        "CREATE TABLE test_tx (id INTEGER PRIMARY KEY, val TEXT);")

    # Act & Assert
    with pytest.raises(DatabaseError, match="Transaction failed"):
        with in_memory_db.transaction() as conn:
            conn.execute("INSERT INTO test_tx (val) VALUES ('first_value');")
            # Force an error
            raise RuntimeError("Simulated transaction fault")

    # Verify rollback
    count_row = in_memory_db.query_one("SELECT count(*) as cnt FROM test_tx;")
    assert count_row is not None
    assert count_row["cnt"] == 0
