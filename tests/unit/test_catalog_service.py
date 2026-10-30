"""Unit tests for CatalogService."""
from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import MagicMock
import pytest

from src.core.config.constants import MEMORY_DB_TARGET
from src.core.database.catalog_repository import CatalogRepository
from src.core.database.connection import DatabaseManager
from src.core.database.migrations import ALL_MIGRATIONS, MigrationManager
from src.core.fingerprinter.builder import FingerprintBuilderService, PklzInspectionStats
from src.core.fingerprinter.catalog_service import (
    CatalogService,
    CatalogServiceCancelledError,
    CatalogServiceError,
    extract_prefix_and_batch,
)


@pytest.fixture
def memory_db() -> DatabaseManager:
    """Fixture providing initialized database with all schema migrations."""
    db = DatabaseManager(MEMORY_DB_TARGET)
    manager = MigrationManager(db, migrations=ALL_MIGRATIONS)
    manager.apply_all()
    return db


def test_extract_prefix_and_batch() -> None:
    """Verify prefix label and batch index derivation."""
    root = Path("/media/databases")
    p1 = root / "ChannelA" / "Music.part3.pklz"
    prefix, batch = extract_prefix_and_batch(p1, root)
    assert prefix == "ChannelA/Music"
    assert batch == 3

    p2 = root / "SingleDB.pklz"
    prefix2, batch2 = extract_prefix_and_batch(p2, root)
    assert prefix2 == "SingleDB"
    assert batch2 == 1


def test_catalog_service_reconciliation_and_summary(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify directory reconciliation, metadata upsert into SQLite, and summary computation."""
    repo = CatalogRepository(memory_db)
    mock_builder = MagicMock(spec=FingerprintBuilderService)

    db_dir = tmp_path / "databases"
    db_dir.mkdir()
    db1 = db_dir / "db1.pklz"
    db2 = db_dir / "db2.pklz"
    db1.write_bytes(b"PKLZ1")
    db2.write_bytes(b"PKLZ2")

    mock_builder.inspect_pklz.side_effect = [
        PklzInspectionStats(
            file_path=db1.resolve(),
            track_count=10,
            total_hashes=500,
            stored_hashes=500,
            dropped_hashes=0,
            dropped_percent=0.0,
            sample_rate=11025,
            hashbits=20,
            bucketsize=100,
            maxtime=16384,
            file_size_bytes=len(b"PKLZ1"),
            sha256_hash="hash1",
        ),
        PklzInspectionStats(
            file_path=db2.resolve(),
            track_count=20,
            total_hashes=1200,
            stored_hashes=1200,
            dropped_hashes=0,
            dropped_percent=0.0,
            sample_rate=11025,
            hashbits=20,
            bucketsize=100,
            maxtime=16384,
            file_size_bytes=len(b"PKLZ2"),
            sha256_hash="hash2",
        ),
    ]

    service = CatalogService(catalog_repo=repo, builder=mock_builder)

    progress_events = []
    def on_progress(cur: int, tot: int, name: str) -> None:
        progress_events.append((cur, tot, name))

    res = service.reconcile_directory(db_dir, progress_callback=on_progress)

    assert res.discovered_files == 2
    assert res.new_or_updated == 2
    assert res.total_tracks == 30
    assert res.total_hashes == 1700
    assert len(progress_events) == 2

    summary = service.get_catalog_summary()
    assert summary["total_databases"] == 2
    assert summary["total_tracks"] == 30
    assert summary["total_hashes"] == 1700

    # Test fast path: re-running reconciliation without force_reindex skips inspect_pklz
    mock_builder.inspect_pklz.reset_mock()
    res2 = service.reconcile_directory(db_dir)
    assert res2.discovered_files == 2
    assert res2.new_or_updated == 0
    mock_builder.inspect_pklz.assert_not_called()


def test_catalog_service_prune_missing(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify obsolete database entries are pruned during reconciliation."""
    repo = CatalogRepository(memory_db)
    mock_builder = MagicMock(spec=FingerprintBuilderService)
    service = CatalogService(catalog_repo=repo, builder=mock_builder)

    db_dir = tmp_path / "databases"
    db_dir.mkdir()
    f1 = db_dir / "valid.pklz"
    f2 = db_dir / "deleted.pklz"
    f1.write_bytes(b"DATA")

    # Manually register f1 and f2 in repo
    repo.upsert_entry(f1.resolve(), "valid", track_count=5, file_size_bytes=4)
    repo.upsert_entry(f2.resolve(), "deleted", track_count=10, file_size_bytes=4)

    assert len(repo.list_entries()) == 2

    # Reconcile db_dir (f2 does not exist on disk)
    res = service.reconcile_directory(db_dir, prune_missing=True)
    assert res.pruned_entries == 1
    assert len(repo.list_entries()) == 1
    assert repo.get_by_path(f1.resolve()) is not None
    assert repo.get_by_path(f2.resolve()) is None


def test_catalog_service_cancellation(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify reconciliation honours cancellation signal."""
    repo = CatalogRepository(memory_db)
    service = CatalogService(catalog_repo=repo)

    db_dir = tmp_path / "databases"
    db_dir.mkdir()
    (db_dir / "test.pklz").write_bytes(b"DATA")

    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(CatalogServiceCancelledError):
        service.reconcile_directory(db_dir, cancel_event=cancel_event)


def test_catalog_service_missing_dir_raises(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify non-existent directory raises CatalogServiceError."""
    repo = CatalogRepository(memory_db)
    service = CatalogService(catalog_repo=repo)

    with pytest.raises(CatalogServiceError, match="Directory not found"):
        service.reconcile_directory(tmp_path / "non_existent")
