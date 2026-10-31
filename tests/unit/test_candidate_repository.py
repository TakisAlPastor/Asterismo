"""Unit tests for CandidateRepository and MIGRATION_V3 integration."""
from __future__ import annotations

from pathlib import Path
import pytest

from src.core.config.constants import MEMORY_DB_TARGET
from src.core.config.models import ScanCandidateStatus
from src.core.database.candidate_repository import CandidateRepository
from src.core.database.connection import DatabaseManager
from src.core.database.migrations import ALL_MIGRATIONS, MigrationManager


@pytest.fixture
def db_with_v3() -> DatabaseManager:
    """Fixture providing an active in-memory DatabaseManager with all migrations applied."""
    db = DatabaseManager(MEMORY_DB_TARGET)
    manager = MigrationManager(db, migrations=ALL_MIGRATIONS)
    manager.apply_all()
    return db


def test_candidate_upsert_and_fetch(db_with_v3: DatabaseManager, tmp_path: Path) -> None:
    """Verify upserting and fetching candidates by path and primary key."""
    repo = CandidateRepository(db_with_v3)
    track_path = tmp_path / "song1.mp3"

    # Insert
    candidate = repo.upsert_candidate(
        file_path=track_path,
        file_name="song1.mp3",
        relative_path="lostwave/song1.mp3",
        file_size_bytes=4096,
        duration_seconds=210.5,
    )
    assert candidate.id is not None
    assert candidate.file_name == "song1.mp3"
    assert candidate.status == ScanCandidateStatus.PENDING
    assert candidate.duration_seconds == 210.5

    # Fetch by path
    by_path = repo.get_candidate_by_path(track_path)
    assert by_path is not None
    assert by_path.id == candidate.id

    # Fetch by id
    by_id = repo.get_candidate_by_id(candidate.id)
    assert by_id is not None
    assert by_id.relative_path == "lostwave/song1.mp3"


def test_candidate_upsert_idempotent_and_updates(db_with_v3: DatabaseManager, tmp_path: Path) -> None:
    """Verify upserting an existing candidate updates attributes without overwriting scanned status."""
    repo = CandidateRepository(db_with_v3)
    track_path = tmp_path / "song1.mp3"

    candidate = repo.upsert_candidate(
        file_path=track_path,
        file_name="song1.mp3",
        relative_path="lostwave/song1.mp3",
        file_size_bytes=4096,
        duration_seconds=200.0,
    )
    repo.record_quick_scan(candidate.id)

    # Re-upsert with updated size and duration
    updated = repo.upsert_candidate(
        file_path=track_path,
        file_name="song1.mp3",
        relative_path="lostwave/song1.mp3",
        file_size_bytes=8192,
        duration_seconds=205.0,
    )
    assert updated.file_size_bytes == 8192
    assert updated.duration_seconds == 205.0
    assert updated.status == ScanCandidateStatus.SCANNED
    assert updated.quick_scanned_at is not None


def test_candidate_list_and_filter(db_with_v3: DatabaseManager, tmp_path: Path) -> None:
    """Verify listing candidates and filtering by lifecycle status."""
    repo = CandidateRepository(db_with_v3)
    p1 = tmp_path / "a.mp3"
    p2 = tmp_path / "b.mp3"

    c1 = repo.upsert_candidate(p1, "a.mp3", "a.mp3", 1000)
    c2 = repo.upsert_candidate(p2, "b.mp3", "b.mp3", 2000)

    repo.record_quick_scan(c1.id)

    all_candidates = repo.list_candidates()
    assert len(all_candidates) == 2

    pending = repo.list_candidates(status=ScanCandidateStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].id == c2.id

    scanned = repo.list_candidates(status=ScanCandidateStatus.SCANNED)
    assert len(scanned) == 1
    assert scanned[0].id == c1.id


def test_record_scan_modes_and_mark_missing(db_with_v3: DatabaseManager, tmp_path: Path) -> None:
    """Verify quick scan, long scan, and bulk marking as missing."""
    repo = CandidateRepository(db_with_v3)
    p1 = tmp_path / "track1.mp3"
    p2 = tmp_path / "track2.mp3"

    c1 = repo.upsert_candidate(p1, "track1.mp3", "track1.mp3", 1000)
    c2 = repo.upsert_candidate(p2, "track2.mp3", "track2.mp3", 2000)

    repo.record_quick_scan(c1.id)
    repo.record_long_scan(c1.id)

    fetched = repo.get_candidate_by_id(c1.id)
    assert fetched is not None
    assert fetched.quick_scanned_at is not None
    assert fetched.long_scanned_at is not None

    # Mark c2 missing
    count = repo.mark_missing([c2.id])
    assert count == 1
    c2_fetched = repo.get_candidate_by_id(c2.id)
    assert c2_fetched is not None
    assert c2_fetched.status == ScanCandidateStatus.MISSING


def test_match_crud_and_cascade_delete(db_with_v3: DatabaseManager, tmp_path: Path) -> None:
    """Verify adding matches, querying by candidate, and cascade deletion."""
    repo = CandidateRepository(db_with_v3)
    p = tmp_path / "song.mp3"
    candidate = repo.upsert_candidate(p, "song.mp3", "song.mp3", 1000)

    # Add matches
    m1 = repo.add_match(
        candidate_id=candidate.id,
        matched_track="Reference Track A",
        matched_database="collection_vol1.pklz",
        aligned_hashes=18,
        raw_hashes=50,
        offset_seconds=12.5,
        confidence=0.92,
    )
    m2 = repo.add_match(
        candidate_id=candidate.id,
        matched_track="Reference Track B",
        matched_database="collection_vol2.pklz",
        aligned_hashes=6,
        raw_hashes=25,
        offset_seconds=3.0,
        confidence=0.55,
    )

    matches = repo.get_matches_for_candidate(candidate.id)
    assert len(matches) == 2
    # Ordered by aligned_hashes descending
    assert matches[0].aligned_hashes == 18
    assert matches[1].aligned_hashes == 6

    # Cascade delete on candidate deletion
    deleted = repo.delete_candidate(candidate.id)
    assert deleted is True
    assert repo.get_candidate_by_id(candidate.id) is None
    assert len(repo.get_matches_for_candidate(candidate.id)) == 0


def test_clear_matches_for_candidate(db_with_v3: DatabaseManager, tmp_path: Path) -> None:
    """Verify clear_matches_for_candidate purges previous match history."""
    repo = CandidateRepository(db_with_v3)
    p = tmp_path / "song.mp3"
    candidate = repo.upsert_candidate(p, "song.mp3", "song.mp3", 1000)

    repo.add_match(
        candidate_id=candidate.id,
        matched_track="Old Match",
        matched_database="db.pklz",
        aligned_hashes=15,
    )
    assert len(repo.get_matches_for_candidate(candidate.id)) == 1

    cleared = repo.clear_matches_for_candidate(candidate.id)
    assert cleared == 1
    assert len(repo.get_matches_for_candidate(candidate.id)) == 0


def test_delete_candidates_batch_and_missing(db_with_v3: DatabaseManager, tmp_path: Path) -> None:
    """Verify batch deletion and missing candidates deletion with cascade to scan_matches."""
    repo = CandidateRepository(db_with_v3)
    c1 = repo.upsert_candidate(tmp_path / "1.mp3", "1.mp3", "1.mp3", 100)
    c2 = repo.upsert_candidate(tmp_path / "2.mp3", "2.mp3", "2.mp3", 200)
    c3 = repo.upsert_candidate(tmp_path / "3.mp3", "3.mp3", "3.mp3", 300)

    # Attach match to c2
    repo.add_match(candidate_id=c2.id, matched_track="Ref", matched_database="db.pklz")

    # Mark c2 and c3 missing
    repo.mark_missing([c2.id, c3.id])

    # Delete missing candidates
    deleted_missing = repo.delete_missing_candidates()
    assert deleted_missing == 2
    assert repo.get_candidate_by_id(c2.id) is None
    assert repo.get_candidate_by_id(c3.id) is None
    assert len(repo.get_matches_for_candidate(c2.id)) == 0
    assert repo.get_candidate_by_id(c1.id) is not None

    # Delete batch
    deleted_batch = repo.delete_candidates([c1.id])
    assert deleted_batch == 1
    assert repo.get_candidate_by_id(c1.id) is None
