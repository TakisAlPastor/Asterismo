"""Unit tests for LocalMatcherService."""
from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from src.core.config.constants import MEMORY_DB_TARGET
from src.core.config.models import (
    MatchOptions,
    ScanCandidateRecord,
    ScanCandidateStatus,
)
from src.core.database.candidate_repository import CandidateRepository
from src.core.database.connection import DatabaseManager
from src.core.database.migrations import ALL_MIGRATIONS, MigrationManager
from src.core.matcher.local_matcher import (
    LocalMatcherCancelledError,
    LocalMatcherError,
    LocalMatcherService,
)


@pytest.fixture
def memory_db() -> DatabaseManager:
    """Fixture providing initialized database with all schema migrations."""
    db = DatabaseManager(MEMORY_DB_TARGET)
    manager = MigrationManager(db, migrations=ALL_MIGRATIONS)
    manager.apply_all()
    return db


def test_extract_hashes_mocked(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify landmark hash extraction calls Analyzer.wavfile2hashes."""
    repo = CandidateRepository(memory_db)
    service = LocalMatcherService(candidate_repo=repo)

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"DATA")

    with patch("audfprint_analyze.Analyzer") as mock_analyzer_cls:
        mock_analyzer = MagicMock()
        mock_analyzer.wavfile2hashes.return_value = [[10, 12345], [20, 67890]]
        mock_analyzer_cls.return_value = mock_analyzer

        hashes = service.extract_hashes(audio_file)
        assert len(hashes) == 2
        mock_analyzer.wavfile2hashes.assert_called_once_with(str(audio_file.resolve()))


def test_match_hashes_against_database_hits(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify matching query hashes against HashTable returns ScanMatchRecords."""
    repo = CandidateRepository(memory_db)
    service = LocalMatcherService(candidate_repo=repo)

    db_path = tmp_path / "test.pklz"
    db_path.write_bytes(b"PKLZ")

    query_hashes = np.array([[10, 12345], [20, 67890]], dtype=np.int32)

    with (
        patch("hash_table.HashTable") as mock_ht_cls,
        patch("audfprint_analyze.Analyzer") as mock_analyzer_cls,
        patch("audfprint_match.Matcher") as mock_matcher_cls,
    ):
        mock_ht = MagicMock()
        mock_ht.names = ["Artist - Track Title", "Other Song"]
        mock_ht_cls.return_value = mock_ht

        mock_analyzer = MagicMock()
        mock_analyzer.n_hop = 256
        mock_analyzer.target_sr = 11025
        mock_analyzer_cls.return_value = mock_analyzer

        mock_matcher = MagicMock()
        # [id, filtered_count, time_skew, raw_count, ...]
        mock_matcher.match_hashes.return_value = np.array([[0, 18, 50.0, 40, 0, 0, 0]])
        mock_matcher_cls.return_value = mock_matcher

        matches = service.match_hashes_against_database(
            query_hashes=query_hashes,
            database_path=db_path,
            candidate_id=42,
            min_aligned_hashes=5,
        )

        assert len(matches) == 1
        m = matches[0]
        assert m.candidate_id == 42
        assert m.matched_track == "Artist - Track Title"
        assert m.matched_database == "test.pklz"
        assert m.aligned_hashes == 18
        assert m.offset_seconds == round(50.0 * (256 / 11025), 2)
        assert m.confidence == round(min(1.0, 18 / 30.0), 3)


def test_match_single_candidate_early_stopping(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify that early stopping stops searching subsequent databases upon high confidence hit."""
    repo = CandidateRepository(memory_db)
    candidate_file = tmp_path / "query.mp3"
    candidate_file.write_bytes(b"AUDIO")

    cand_rec = repo.upsert_candidate(
        file_path=candidate_file,
        file_name="query.mp3",
        relative_path="query.mp3",
        file_size_bytes=5,
    )

    db1 = tmp_path / "db1.pklz"
    db2 = tmp_path / "db2.pklz"
    db1.write_bytes(b"DB1")
    db2.write_bytes(b"DB2")

    service = LocalMatcherService(candidate_repo=repo)

    with (
        patch.object(service, "extract_hashes", return_value=np.array([[1, 2]])),
        patch.object(service, "match_hashes_against_database") as mock_match,
    ):
        from src.core.config.models import ScanMatchRecord

        # First database returns 20 aligned hashes (>= 15 early stop threshold)
        hit = ScanMatchRecord(
            candidate_id=cand_rec.id,
            matched_track="Hit Track",
            matched_database="db1.pklz",
            aligned_hashes=20,
        )
        mock_match.return_value = [hit]

        options = MatchOptions(
            target_databases=(db1, db2),
            stop_on_first_match=True,
            early_stop_threshold=15,
        )

        result = service.match_single_candidate(
            candidate=cand_rec,
            target_databases=(db1, db2),
            options=options,
        )

        assert result.early_stopped is True
        assert result.databases_searched == 1
        assert len(result.matches) == 1
        # db2 should not have been called
        assert mock_match.call_count == 1

        # Check DB was updated with quick scan timestamp
        updated_cand = repo.get_candidate_by_id(cand_rec.id)
        assert updated_cand is not None
        assert updated_cand.status == ScanCandidateStatus.SCANNED
        assert updated_cand.quick_scanned_at is not None


def test_match_single_candidate_exhaustive_without_early_stop(
    memory_db: DatabaseManager, tmp_path: Path
) -> None:
    """Verify exhaustive matching when stop_on_first_match is False."""
    repo = CandidateRepository(memory_db)
    cand_file = tmp_path / "cand.mp3"
    cand_file.write_bytes(b"A")

    cand_rec = repo.upsert_candidate(cand_file, "cand.mp3", "cand.mp3", 1)

    db1 = tmp_path / "db1.pklz"
    db2 = tmp_path / "db2.pklz"
    db1.write_bytes(b"1")
    db2.write_bytes(b"2")

    service = LocalMatcherService(candidate_repo=repo)

    with (
        patch.object(service, "extract_hashes", return_value=np.array([[1, 2]])),
        patch.object(service, "match_hashes_against_database") as mock_match,
    ):
        from src.core.config.models import ScanMatchRecord

        mock_match.side_effect = [
            [ScanMatchRecord(candidate_id=cand_rec.id, matched_track="Track 1", matched_database="db1.pklz", aligned_hashes=25)],
            [ScanMatchRecord(candidate_id=cand_rec.id, matched_track="Track 2", matched_database="db2.pklz", aligned_hashes=12)],
        ]

        options = MatchOptions(stop_on_first_match=False)
        result = service.match_single_candidate(
            candidate=cand_rec,
            target_databases=(db1, db2),
            options=options,
        )

        assert result.early_stopped is False
        assert result.databases_searched == 2
        assert len(result.matches) == 2
        assert mock_match.call_count == 2


def test_batch_match_and_cancellation(memory_db: DatabaseManager, tmp_path: Path) -> None:
    """Verify batch orchestration progress callbacks and cancellation."""
    repo = CandidateRepository(memory_db)
    c1_file = tmp_path / "c1.mp3"
    c2_file = tmp_path / "c2.mp3"
    c1_file.write_bytes(b"1")
    c2_file.write_bytes(b"2")

    c1 = repo.upsert_candidate(c1_file, "c1.mp3", "c1.mp3", 1)
    c2 = repo.upsert_candidate(c2_file, "c2.mp3", "c2.mp3", 2)

    db = tmp_path / "ref.pklz"
    db.write_bytes(b"D")

    service = LocalMatcherService(candidate_repo=repo)

    progress_records = []
    def on_prog(cur: int, tot: int, cand: ScanCandidateRecord, res: object) -> None:
        progress_records.append((cur, tot, cand.file_name))

    with (
        patch.object(service, "extract_hashes", return_value=np.array([[1, 2]])),
        patch.object(service, "match_hashes_against_database", return_value=[]),
    ):
        results = service.batch_match(
            candidates=[c1, c2],
            target_databases=[db],
            progress_callback=on_prog,
        )

        assert len(results) == 2
        assert len(progress_records) == 2
        assert progress_records[0] == (1, 2, "c1.mp3")
        assert progress_records[1] == (2, 2, "c2.mp3")

    # Test cancellation
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(LocalMatcherCancelledError):
        service.batch_match(
            candidates=[c1, c2],
            target_databases=[db],
            cancel_event=cancel_event,
        )
