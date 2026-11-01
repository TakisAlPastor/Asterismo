"""Unit tests for MatcherWorker bridging asynchronous multi-engine matching to PySide6."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from PySide6.QtWidgets import QApplication

from src.core.config.constants import ENGINE_NAME_AUDFPRINT, ENGINE_NAME_SHAZAM
from src.core.config.models import (
    MatchOptions,
    ScanCandidateRecord,
    ScanMatchRecord,
)
from src.core.matcher.adapters.base import (
    RecognitionEngineCancelledError,
    RecognitionEngineError,
)
from src.core.matcher.local_matcher import LocalMatcherService
from src.core.matcher.multi_matcher import (
    MultiEngineCandidateResult,
    MultiEngineMatcherService,
)
from src.ui.workers.matcher_worker import MatcherWorker


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def sample_candidate(tmp_path: Path) -> ScanCandidateRecord:
    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"\xFF\xFB\x90\x00" * 20)
    return ScanCandidateRecord(
        file_path=audio_file,
        file_name="song.mp3",
        relative_path="song.mp3",
        file_size_bytes=len(audio_file.read_bytes()),
        duration_seconds=30.0,
        id=1,
    )


def test_matcher_worker_multi_engine_success(qapp: QApplication, sample_candidate: ScanCandidateRecord) -> None:
    """MatcherWorker executes multi-engine scan, emits signals, and logs formatted match."""
    mock_service = MagicMock(spec=MultiEngineMatcherService)

    match_record = ScanMatchRecord(
        candidate_id=1,
        engine=ENGINE_NAME_SHAZAM,
        matched_database="Shazam Catalog",
        matched_track="Darude - Sandstorm",
        confidence=0.98,
        offset_seconds=5.2,
        aligned_hashes=0,
    )

    candidate_res = MultiEngineCandidateResult(
        candidate_path=sample_candidate.file_path,
        candidate_id=1,
        matches=(match_record,),
        engines_queried=(ENGINE_NAME_AUDFPRINT, ENGINE_NAME_SHAZAM),
        elapsed_seconds=1.2,
    )

    async def _mock_match_candidate(*args, on_engine_finished=None, **kwargs):
        if on_engine_finished:
            on_engine_finished(ENGINE_NAME_SHAZAM, [match_record])
        return candidate_res

    mock_service.match_candidate = AsyncMock(side_effect=_mock_match_candidate)

    worker = MatcherWorker(
        matcher_service=mock_service,
        candidates=[sample_candidate],
        options=MatchOptions(enabled_engines=(
            ENGINE_NAME_AUDFPRINT, ENGINE_NAME_SHAZAM)),
    )

    progress_events = []
    match_events = []
    completed_events = []
    scan_completed_events = []
    log_events = []
    finished_called = []

    worker.progress_updated.connect(
        lambda cur, tot, name: progress_events.append((cur, tot, name)))
    worker.match_detected.connect(match_events.append)
    worker.candidate_completed.connect(completed_events.append)
    worker.scan_completed.connect(scan_completed_events.append)
    worker.log_emitted.connect(log_events.append)
    worker.finished.connect(lambda: finished_called.append(True))

    worker.run()

    assert len(finished_called) == 1
    assert len(match_events) == 1
    assert match_events[0].matched_track == "Darude - Sandstorm"
    assert len(completed_events) == 1
    assert completed_events[0] == candidate_res
    assert len(scan_completed_events) == 1
    assert any("[MATCH - SHAZAM]" in log for log in log_events)
    assert any("Sandstorm" in log for log in log_events)


def test_matcher_worker_cancellation(qapp: QApplication, sample_candidate: ScanCandidateRecord) -> None:
    """MatcherWorker handles cancellation gracefully via cancel_event."""
    mock_service = MagicMock(spec=MultiEngineMatcherService)

    worker = MatcherWorker(
        matcher_service=mock_service,
        candidates=[sample_candidate],
    )

    status_events = []
    log_events = []
    finished_called = []

    worker.status_changed.connect(status_events.append)
    worker.log_emitted.connect(log_events.append)
    worker.finished.connect(lambda: finished_called.append(True))

    # Pre-cancel before run
    worker.cancel()
    worker.run()

    assert len(finished_called) == 1
    assert "Scan cancelled." in status_events
    assert any("cancelled by user" in log for log in log_events)


def test_matcher_worker_error_handling(qapp: QApplication, sample_candidate: ScanCandidateRecord) -> None:
    """MatcherWorker catches exceptions and emits error_occurred."""
    mock_service = MagicMock(spec=MultiEngineMatcherService)
    mock_service.match_candidate = AsyncMock(
        side_effect=RecognitionEngineError("Network connection reset"))

    worker = MatcherWorker(
        matcher_service=mock_service,
        candidates=[sample_candidate],
    )

    error_events = []
    finished_called = []

    worker.error_occurred.connect(error_events.append)
    worker.finished.connect(lambda: finished_called.append(True))

    worker.run()

    assert len(finished_called) == 1
    assert len(error_events) == 1
    assert "Network connection reset" in error_events[0]


def test_matcher_worker_legacy_fallback(qapp: QApplication, sample_candidate: ScanCandidateRecord) -> None:
    """MatcherWorker supports legacy LocalMatcherService fallback."""
    mock_local_service = MagicMock(spec=LocalMatcherService)

    match_record = ScanMatchRecord(
        candidate_id=1,
        engine=ENGINE_NAME_AUDFPRINT,
        matched_database="test.pklz",
        matched_track="Track01",
        confidence=0.85,
        offset_seconds=12.0,
        aligned_hashes=42,
    )
    from src.core.config.models import LocalMatchResult
    local_res = LocalMatchResult(
        candidate_path=sample_candidate.file_path,
        candidate_id=1,
        matches=(match_record,),
        elapsed_seconds=0.5,
    )
    mock_local_service.match_single_candidate.return_value = local_res

    worker = MatcherWorker(
        matcher_service=mock_local_service,
        candidates=[sample_candidate],
    )

    match_events = []
    completed_events = []
    finished_called = []

    worker.match_detected.connect(match_events.append)
    worker.candidate_completed.connect(completed_events.append)
    worker.finished.connect(lambda: finished_called.append(True))

    worker.run()

    assert len(finished_called) == 1
    assert len(match_events) == 1
    assert match_events[0].aligned_hashes == 42
    assert len(completed_events) == 1
