"""Unit tests for Milestone 2 repositories, domain models, and Migration V2."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
import pytest

from src.core.config.constants import (
    MEMORY_DB_TARGET,
    MIGRATION_V1_VERSION,
    MIGRATION_V2_VERSION,
)
from src.core.config.models import (
    AppSettings,
    BrowserCookieSource,
    ConfigurationError,
    HarvestItemRecord,
    HarvestItemStatus,
    HarvestSourceType,
    HarvestTask,
    HarvestTaskStatus,
    PklzCatalogEntry,
    WorkspacePaths,
)
from src.core.database.catalog_repository import CatalogRepository
from src.core.database.connection import DatabaseManager
from src.core.database.harvest_repository import (
    DuplicateHarvestTaskError,
    HarvestRepository,
)
from src.core.database.migrations import (
    ALL_MIGRATIONS,
    MIGRATION_V1,
    MIGRATION_V2,
    MigrationManager,
)
from src.core.database.settings_repository import SettingsRepository


@pytest.fixture
def in_memory_db() -> Iterator[DatabaseManager]:
    """Fixture providing an active in-memory DatabaseManager."""
    db = DatabaseManager(MEMORY_DB_TARGET)
    yield db
    db.close()


def test_app_settings_v2_fields_backward_compatibility(tmp_path: Path) -> None:
    """Verify that legacy V1 JSON payloads cleanly deserialize with default V2 settings."""
    # Arrange: Simulate an M1 settings JSON dictionary without V2 fields
    v1_dict = {
        "paths": WorkspacePaths.default(tmp_path).to_dict(),
        "theme": "dark",
        "scan_mode": "quick",
        "split_long_audio": True,
        "split_threshold_seconds": 720,
        "split_segment_seconds": 360,
        "max_concurrent_downloads": 4,
        "fp_worker_count": 4,
        "keep_downloaded_audio": False,
    }

    # Act
    settings = AppSettings.from_dict(v1_dict, base_dir=tmp_path)

    # Assert: V2 fields take default values
    assert settings.cookies_browser == BrowserCookieSource.NONE
    assert settings.skip_already_fingerprinted is True
    assert settings.extra_ytdlp_args == ""

    # Test invalid BrowserCookieSource raises ConfigurationError
    invalid_dict = dict(v1_dict)
    invalid_dict["cookies_browser"] = "unsupported_browser"
    with pytest.raises(ConfigurationError, match="Invalid browser cookie source"):
        AppSettings.from_dict(invalid_dict, base_dir=tmp_path)


def test_migration_v2_apply_and_rollback_to_v1(in_memory_db: DatabaseManager) -> None:
    """Verify applying V1 and V2 migrations, table creation, and selective rollback to V1."""
    # Arrange
    manager = MigrationManager(in_memory_db, migrations=ALL_MIGRATIONS)

    # Act 1: Apply all migrations (V1 + V2)
    applied = manager.apply_all()
    assert applied == 2
    assert manager.current_version() == MIGRATION_V2_VERSION

    # Assert: All 3 new tables exist
    for table_name in ("harvest_queue", "harvest_items", "pklz_catalog"):
        row = in_memory_db.query_one(
            "SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name=?;",
            (table_name,),
        )
        assert row is not None and row["cnt"] == 1, f"Table {table_name} missing"

    # Insert test data into app_settings (V1) and harvest_queue (V2)
    settings_repo = SettingsRepository(in_memory_db, base_dir=Path("."))
    settings_repo.set_value("test_key", json.dumps({"test": 123}))

    harvest_repo = HarvestRepository(in_memory_db)
    task = harvest_repo.enqueue_task(
        raw_url="https://www.youtube.com/@test",
        normalized_url="https://www.youtube.com/@test",
        source_type=HarvestSourceType.YTDLP_URL,
        prefix_label="@test",
    )
    assert task.id is not None

    # Act 2: Rollback to V1
    rolled_back = manager.rollback_to(MIGRATION_V1_VERSION)
    assert rolled_back == 1
    assert manager.current_version() == MIGRATION_V1_VERSION

    # Assert: V2 tables are dropped, but app_settings still contains its data
    for table_name in ("harvest_queue", "harvest_items", "pklz_catalog"):
        row = in_memory_db.query_one(
            "SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name=?;",
            (table_name,),
        )
        assert row is not None and row["cnt"] == 0, f"Table {table_name} was not dropped"

    assert settings_repo.get_value("test_key") == json.dumps({"test": 123})


def test_harvest_repository_enqueue_and_duplicate_rejection(
    in_memory_db: DatabaseManager,
) -> None:
    """Verify task enqueueing, retrieval, status filtering, and duplicate rejection."""
    # Arrange
    MigrationManager(in_memory_db, migrations=ALL_MIGRATIONS).apply_all()
    repo = HarvestRepository(in_memory_db)

    # Act 1: Enqueue initial task
    task = repo.enqueue_task(
        raw_url="https://youtube.com/@ArchiveChannel?si=123",
        normalized_url="https://www.youtube.com/@ArchiveChannel",
        source_type=HarvestSourceType.YTDLP_URL,
        prefix_label="@ArchiveChannel",
        title="Archive Channel",
    )

    # Assert 1
    assert task.id == 1
    assert task.status == HarvestTaskStatus.PENDING
    assert task.prefix_label == "@ArchiveChannel"
    assert task.title == "Archive Channel"

    # Act 2: Duplicate enqueue must raise DuplicateHarvestTaskError
    with pytest.raises(DuplicateHarvestTaskError, match="already queued"):
        repo.enqueue_task(
            raw_url="https://youtube.com/@ArchiveChannel",
            normalized_url="https://www.youtube.com/@ArchiveChannel",
            source_type=HarvestSourceType.YTDLP_URL,
            prefix_label="@ArchiveChannel",
        )

    # Act 3: Metadata, status, and progress updates
    repo.update_task_metadata(
        task.id, title="Renamed Channel", prefix_label="@Renamed", total_items=25)
    repo.update_task_status(task.id, HarvestTaskStatus.RUNNING)
    repo.update_task_progress(task.id, completed_items=10, skipped_items=2)

    updated = repo.get_task_by_id(task.id)
    assert updated is not None
    assert updated.title == "Renamed Channel"
    assert updated.prefix_label == "@Renamed"
    assert updated.total_items == 25
    assert updated.completed_items == 10
    assert updated.skipped_items == 2
    assert updated.status == HarvestTaskStatus.RUNNING

    # Act 4: Filtering by status
    running_tasks = repo.list_tasks(status=HarvestTaskStatus.RUNNING)
    assert len(running_tasks) == 1
    assert running_tasks[0].id == task.id

    completed_tasks = repo.list_tasks(status=HarvestTaskStatus.COMPLETED)
    assert len(completed_tasks) == 0


def test_harvest_repository_items_cascade_and_deduplication(
    in_memory_db: DatabaseManager,
) -> None:
    """Verify item upsert, deduplication queries, and foreign key cascade deletion."""
    # Arrange
    MigrationManager(in_memory_db, migrations=ALL_MIGRATIONS).apply_all()
    repo = HarvestRepository(in_memory_db)

    task = repo.enqueue_task(
        raw_url="https://example.com/audio",
        normalized_url="https://example.com/audio",
        source_type=HarvestSourceType.WEB_DIRECTORY,
        prefix_label="web_audio",
    )
    assert task.id is not None

    # Act 1: Upsert items
    item1 = repo.upsert_item(
        task_id=task.id,
        item_key="track_001",
        title="Track 1",
        source_url="https://example.com/audio/1.mp3",
        duration_seconds=185.5,
    )
    item2 = repo.upsert_item(
        task_id=task.id,
        item_key="track_002",
        title="Track 2",
        source_url="https://example.com/audio/2.mp3",
        duration_seconds=240.0,
    )

    assert item1.status == HarvestItemStatus.PENDING
    assert item2.duration_seconds == 240.0

    # Act 2: Fingerprint item 1
    assert not repo.is_item_fingerprinted("track_001")
    count = repo.mark_items_fingerprinted(
        ["track_001"], pklz_filename="web_audio-1.pklz")
    assert count == 1
    assert repo.is_item_fingerprinted("track_001")
    assert not repo.is_item_fingerprinted("track_002")

    # Act 3: Check batch deduplication set query
    dedup_set = repo.get_fingerprinted_item_keys(
        ["track_001", "track_002", "track_999"])
    assert dedup_set == {"track_001"}

    # Act 4: Cascade deletion
    task_items = repo.list_items_for_task(task.id)
    assert len(task_items) == 2

    deleted = repo.delete_task(task.id)
    assert deleted is True

    # Items must be deleted via ON DELETE CASCADE
    remaining_items = in_memory_db.query_all(
        "SELECT * FROM harvest_items WHERE task_id = ?;", (task.id,))
    assert len(remaining_items) == 0


def test_catalog_repository_upsert_list_and_delete(
    in_memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify pklz_catalog CRUD, upsert replacement, and prefix query filtering."""
    # Arrange
    MigrationManager(in_memory_db, migrations=ALL_MIGRATIONS).apply_all()
    repo = CatalogRepository(in_memory_db)

    pklz_file = tmp_path / "databases" / "@TestChannel-1.pklz"

    # Act 1: Upsert initial entry
    entry = repo.upsert_entry(
        file_path=pklz_file,
        prefix_label="@TestChannel",
        batch_index=1,
        track_count=50,
        total_hashes=25000,
        file_size_bytes=1048576,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )

    # Assert 1
    assert entry.file_name == "@TestChannel-1.pklz"
    assert entry.prefix_label == "@TestChannel"
    assert entry.track_count == 50
    assert entry.file_size_bytes == 1048576

    # Act 2: Upsert update on same path (e.g., recalculated hashes)
    updated_entry = repo.upsert_entry(
        file_path=pklz_file,
        prefix_label="@TestChannel",
        batch_index=1,
        track_count=52,
        total_hashes=26000,
        file_size_bytes=1055000,
        sha256_hash="updated_sha256_hash",
    )
    assert updated_entry.track_count == 52
    assert updated_entry.total_hashes == 26000

    # Act 3: List entries and filter by prefix
    all_entries = repo.list_entries()
    assert len(all_entries) == 1

    filtered = repo.list_entries(prefix_label="@OtherChannel")
    assert len(filtered) == 0

    matching = repo.list_entries(prefix_label="@TestChannel")
    assert len(matching) == 1

    # Act 4: Delete by path
    deleted = repo.delete_by_path(pklz_file)
    assert deleted is True
    assert repo.get_by_path(pklz_file) is None


def test_repositories_sql_injection_prevented(
    in_memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify parameterized queries prevent SQL injection across all repositories."""
    # Arrange
    MigrationManager(in_memory_db, migrations=ALL_MIGRATIONS).apply_all()
    harvest_repo = HarvestRepository(in_memory_db)
    catalog_repo = CatalogRepository(in_memory_db)

    malicious_url = "https://example.com/test'; DROP TABLE harvest_queue; --"
    malicious_title = "Title'; DROP TABLE harvest_items; --"
    malicious_prefix = "prefix'; DROP TABLE pklz_catalog; --"

    # Act: Enqueue and upsert with malicious payloads
    task = harvest_repo.enqueue_task(
        raw_url=malicious_url,
        normalized_url=malicious_url,
        source_type=HarvestSourceType.YTDLP_URL,
        prefix_label=malicious_prefix,
        title=malicious_title,
    )
    assert task.id is not None

    item = harvest_repo.upsert_item(
        task_id=task.id,
        item_key="key'; DROP TABLE harvest_items; --",
        title=malicious_title,
        source_url=malicious_url,
    )
    assert item.item_key == "key'; DROP TABLE harvest_items; --"

    catalog_entry = catalog_repo.upsert_entry(
        file_path=tmp_path / "test'; DROP TABLE pklz_catalog; --.pklz",
        prefix_label=malicious_prefix,
        sha256_hash="malicious_sha",
    )
    assert catalog_entry.prefix_label == malicious_prefix

    # Assert: All tables remain intact
    for table_name in ("harvest_queue", "harvest_items", "pklz_catalog"):
        row = in_memory_db.query_one(
            "SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name=?;",
            (table_name,),
        )
        assert row is not None and row["cnt"] == 1
