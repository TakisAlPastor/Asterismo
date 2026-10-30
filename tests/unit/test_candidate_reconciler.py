"""Unit tests for CandidateReconciler."""
from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import MagicMock
import pytest

from src.core.config.constants import MEMORY_DB_TARGET
from src.core.config.models import ScanCandidateStatus
from src.core.database.candidate_repository import CandidateRepository
from src.core.database.connection import DatabaseManager
from src.core.database.migrations import ALL_MIGRATIONS, MigrationManager
from src.core.matcher.candidate_reconciler import (
    CandidateReconciler,
    CandidateReconcilerCancelledError,
    CandidateReconcilerError,
)


@pytest.fixture
def memory_db() -> DatabaseManager:
    """Fixture providing initialized database with all schema migrations."""
    db = DatabaseManager(MEMORY_DB_TARGET)
    manager = MigrationManager(db, migrations=ALL_MIGRATIONS)
    manager.apply_all()
    return db


def test_reconcile_discovers_audio_and_ignores_others(
    memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify recursive audio discovery and skipping non-audio files."""
    repo = CandidateRepository(memory_db)
    mock_splitter = MagicMock()
    mock_splitter.probe_duration.return_value = 142.5

    reconciler = CandidateReconciler(
        candidate_repo=repo, splitter=mock_splitter)

    # Setup directories and files
    input_dir = tmp_path / "audio"
    sub_dir = input_dir / "cassettes"
    sub_dir.mkdir(parents=True)

    f1 = input_dir / "song1.mp3"
    f2 = sub_dir / "tape_sideA.flac"
    f_ignore = input_dir / "notes.txt"

    f1.write_bytes(b"MP3_DATA")
    f2.write_bytes(b"FLAC_DATA")
    f_ignore.write_text("Ignore this")

    res = reconciler.reconcile_directory(input_dir)

    assert res.discovered_files == 2
    assert res.new_candidates == 2
    assert res.missing_candidates == 0

    candidates = repo.list_candidates()
    assert len(candidates) == 2

    # Check relative path
    c_sub = repo.get_candidate_by_path(f2)
    assert c_sub is not None
    assert c_sub.relative_path == "cassettes/tape_sideA.flac"
    assert c_sub.duration_seconds == 142.5
    assert c_sub.status == ScanCandidateStatus.PENDING


def test_reconcile_preserves_scanned_status(
    memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify that re-running reconciliation preserves quick_scanned_at and long_scanned_at."""
    repo = CandidateRepository(memory_db)
    reconciler = CandidateReconciler(candidate_repo=repo)

    input_dir = tmp_path / "audio"
    input_dir.mkdir()
    f1 = input_dir / "track.wav"
    f1.write_bytes(b"WAV_DATA")

    reconciler.reconcile_directory(input_dir)
    c1 = repo.get_candidate_by_path(f1)
    assert c1 is not None

    # Mark scanned
    repo.record_quick_scan(c1.id)
    repo.record_long_scan(c1.id)

    # Second reconciliation
    res2 = reconciler.reconcile_directory(input_dir)
    assert res2.discovered_files == 1
    assert res2.new_candidates == 0

    c1_after = repo.get_candidate_by_path(f1)
    assert c1_after is not None
    assert c1_after.status == ScanCandidateStatus.SCANNED
    assert c1_after.quick_scanned_at is not None
    assert c1_after.long_scanned_at is not None


def test_reconcile_marks_missing_and_restores(
    memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify deleted files are marked as missing, and restored if recreated."""
    repo = CandidateRepository(memory_db)
    reconciler = CandidateReconciler(candidate_repo=repo)

    input_dir = tmp_path / "audio"
    input_dir.mkdir()
    f1 = input_dir / "temp.mp3"
    f1.write_bytes(b"DATA")

    reconciler.reconcile_directory(input_dir)
    assert len(repo.list_candidates(status=ScanCandidateStatus.PENDING)) == 1

    # Delete file from disk
    f1.unlink()

    res = reconciler.reconcile_directory(input_dir)
    assert res.discovered_files == 0
    assert res.missing_candidates == 1

    c = repo.get_candidate_by_path(f1)
    assert c is not None
    assert c.status == ScanCandidateStatus.MISSING

    # Restore file
    f1.write_bytes(b"RESTORED_DATA")
    res_restore = reconciler.reconcile_directory(input_dir)
    assert res_restore.discovered_files == 1

    c_restored = repo.get_candidate_by_path(f1)
    assert c_restored is not None
    assert c_restored.status == ScanCandidateStatus.PENDING


def test_reconcile_cancellation(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify cancellation halts reconciliation."""
    repo = CandidateRepository(memory_db)
    reconciler = CandidateReconciler(candidate_repo=repo)

    input_dir = tmp_path / "audio"
    input_dir.mkdir()
    (input_dir / "1.mp3").write_bytes(b"D")

    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(CandidateReconcilerCancelledError):
        reconciler.reconcile_directory(input_dir, cancel_event=cancel_event)


def test_reconcile_invalid_dir(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify non-existent directory raises CandidateReconcilerError."""
    repo = CandidateRepository(memory_db)
    reconciler = CandidateReconciler(candidate_repo=repo)

    with pytest.raises(CandidateReconcilerError, match="not found"):
        reconciler.reconcile_directory(tmp_path / "does_not_exist")
