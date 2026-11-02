"""Unit tests for MultiEngineMatcherService and cascade orchestrator."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.core.config.constants import (
    ENGINE_NAME_ACOUSTID,
    ENGINE_NAME_AUDIOTAG,
    ENGINE_NAME_AUDFPRINT,
    ENGINE_NAME_SHAZAM,
)
from src.core.config.models import (
    ConfigurationError,
    LocalMatchResult,
    MatchOptions,
    ScanCandidateRecord,
    ScanMatchRecord,
)
from src.core.matcher.adapters.base import (
    CloudMatchResult,
    RecognitionEngineCancelledError,
)
from src.core.matcher.multi_matcher import (
    MultiEngineCandidateResult,
    MultiEngineMatcherService,
)


@pytest.fixture
def candidate_record(tmp_path: Path) -> ScanCandidateRecord:
    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"\xFF\xFB\x90\x00" * 50)
    return ScanCandidateRecord(
        file_path=audio_file,
        file_name="song.mp3",
        relative_path="song.mp3",
        file_size_bytes=len(audio_file.read_bytes()),
        duration_seconds=30.0,
        id=42,
    )


def test_multi_engine_candidate_result_validation(tmp_path: Path) -> None:
    """Verify bounds and validation in MultiEngineCandidateResult."""
    with pytest.raises(ConfigurationError, match="candidate_path cannot be empty"):
        MultiEngineCandidateResult(
            candidate_path=Path(""),
            candidate_id=1,
            matches=(),
            engines_queried=(),
            elapsed_seconds=1.0,
        )

    with pytest.raises(ConfigurationError, match="elapsed_seconds must be >= 0"):
        MultiEngineCandidateResult(
            candidate_path=tmp_path / "song.mp3",
            candidate_id=1,
            matches=(),
            engines_queried=(),
            elapsed_seconds=-0.5,
        )


def test_cascade_early_stop_on_local_audfprint_match(
    candidate_record: ScanCandidateRecord, tmp_path: Path
) -> None:
    """Verify that a confident local match bypasses cloud queries when early stop is enabled."""
    async def _run() -> None:
        mock_repo = MagicMock()
        mock_local = MagicMock()

        # Local matcher returns a top hit exceeding early stop threshold (15)
        local_hit = ScanMatchRecord(
            candidate_id=42,
            matched_track="Reference Song A",
            matched_database="database.pklz",
            engine=ENGINE_NAME_AUDFPRINT,
            aligned_hashes=22,
            raw_hashes=50,
            confidence=0.90,
        )
        mock_local.match_single_candidate.return_value = LocalMatchResult(
            candidate_path=candidate_record.file_path,
            candidate_id=candidate_record.id,
            matches=(local_hit,),
            databases_searched=1,
            elapsed_seconds=0.1,
            early_stopped=True,
        )

        mock_shazam = MagicMock()
        mock_shazam.engine_name = ENGINE_NAME_SHAZAM
        mock_shazam.is_available = True
        mock_shazam.identify = AsyncMock()

        mock_audiotag = MagicMock()
        mock_audiotag.engine_name = ENGINE_NAME_AUDIOTAG
        mock_audiotag.is_available = True
        mock_audiotag.identify = AsyncMock()

        service = MultiEngineMatcherService(
            candidate_repo=mock_repo,
            local_matcher=mock_local,
            cloud_adapters=(mock_shazam, mock_audiotag),
        )

        options = MatchOptions(
            target_databases=(tmp_path / "ref.pklz",),
            stop_on_first_match=True,
            early_stop_threshold=15,
            enabled_engines=(ENGINE_NAME_AUDFPRINT,
                             ENGINE_NAME_SHAZAM, ENGINE_NAME_AUDIOTAG),
        )

        result = await service.match_candidate(candidate_record, options)

        # Assert local matcher was called and cleared stale matches
        mock_repo.clear_matches_for_candidate.assert_called_once_with(42)
        mock_local.match_single_candidate.assert_called_once()

        # Assert cloud engines were bypassed
        mock_shazam.identify.assert_not_called()
        mock_audiotag.identify.assert_not_called()

        assert result.early_stopped is True
        assert result.engines_queried == (ENGINE_NAME_AUDFPRINT,)
        assert len(result.matches) == 1
        assert result.matches[0].matched_track == "Reference Song A"
        mock_repo.record_quick_scan.assert_called_once_with(42)

    asyncio.run(_run())


def test_cascade_early_stop_on_shazam_match(
    candidate_record: ScanCandidateRecord, tmp_path: Path
) -> None:
    """Verify that confident Shazam match halts subsequent paid cloud engines."""
    async def _run() -> None:
        mock_repo = MagicMock()
        saved_shazam_match = ScanMatchRecord(
            candidate_id=42,
            matched_track="FEX - Subways Of Your Mind",
            matched_database="Shazam",
            engine=ENGINE_NAME_SHAZAM,
            confidence=1.0,
            id=101,
        )
        mock_repo.add_match.return_value = saved_shazam_match

        # Local matcher finds no matches
        mock_local = MagicMock()
        mock_local.match_single_candidate.return_value = LocalMatchResult(
            candidate_path=candidate_record.file_path,
            candidate_id=candidate_record.id,
            matches=(),
            databases_searched=1,
            elapsed_seconds=0.1,
            early_stopped=False,
        )

        # Shazam returns hit
        mock_shazam = MagicMock()
        mock_shazam.engine_name = ENGINE_NAME_SHAZAM
        mock_shazam.is_available = True
        mock_shazam.identify = AsyncMock(
            return_value=(
                CloudMatchResult(
                    engine=ENGINE_NAME_SHAZAM,
                    matched_track="FEX - Subways Of Your Mind",
                    matched_database="Shazam",
                    confidence=1.0,
                ),
            )
        )

        # AudioTag should be skipped
        mock_audiotag = MagicMock()
        mock_audiotag.engine_name = ENGINE_NAME_AUDIOTAG
        mock_audiotag.is_available = True
        mock_audiotag.identify = AsyncMock()

        service = MultiEngineMatcherService(
            candidate_repo=mock_repo,
            local_matcher=mock_local,
            cloud_adapters=(mock_shazam, mock_audiotag),
        )

        options = MatchOptions(
            target_databases=(tmp_path / "ref.pklz",),
            stop_on_first_match=True,
            enabled_engines=(ENGINE_NAME_AUDFPRINT,
                             ENGINE_NAME_SHAZAM, ENGINE_NAME_AUDIOTAG),
        )

        result = await service.match_candidate(candidate_record, options)

        mock_local.match_single_candidate.assert_called_once()
        mock_shazam.identify.assert_called_once()
        mock_audiotag.identify.assert_not_called()

        assert result.early_stopped is True
        assert result.engines_queried == (
            ENGINE_NAME_AUDFPRINT, ENGINE_NAME_SHAZAM)
        assert len(result.matches) == 1
        assert result.matches[0].matched_track == "FEX - Subways Of Your Mind"
        mock_repo.add_match.assert_called_once()

    asyncio.run(_run())


def test_exhaustive_scan_when_early_stop_disabled(
    candidate_record: ScanCandidateRecord, tmp_path: Path
) -> None:
    """Verify all engines are queried when early stop is disabled."""
    async def _run() -> None:
        mock_repo = MagicMock()
        mock_repo.add_match.side_effect = lambda **kwargs: ScanMatchRecord(
            candidate_id=kwargs["candidate_id"],
            matched_track=kwargs["matched_track"],
            matched_database=kwargs["matched_database"],
            engine=kwargs["engine"],
            confidence=kwargs["confidence"],
        )

        mock_shazam = MagicMock()
        mock_shazam.engine_name = ENGINE_NAME_SHAZAM
        mock_shazam.is_available = True
        mock_shazam.identify = AsyncMock(
            return_value=(
                CloudMatchResult(
                    engine=ENGINE_NAME_SHAZAM,
                    matched_track="Song 1",
                    matched_database="Shazam",
                    confidence=1.0,
                ),
            )
        )

        mock_acoustid = MagicMock()
        mock_acoustid.engine_name = ENGINE_NAME_ACOUSTID
        mock_acoustid.is_available = True
        mock_acoustid.identify = AsyncMock(
            return_value=(
                CloudMatchResult(
                    engine=ENGINE_NAME_ACOUSTID,
                    matched_track="Song 2",
                    matched_database="MusicBrainz",
                    confidence=0.85,
                ),
            )
        )

        service = MultiEngineMatcherService(
            candidate_repo=mock_repo,
            local_matcher=None,
            cloud_adapters=(mock_shazam, mock_acoustid),
        )

        options = MatchOptions(
            stop_on_first_match=False,
            enabled_engines=(ENGINE_NAME_SHAZAM, ENGINE_NAME_ACOUSTID),
        )

        result = await service.match_candidate(candidate_record, options)

        mock_shazam.identify.assert_called_once()
        mock_acoustid.identify.assert_called_once()

        assert result.early_stopped is False
        assert result.engines_queried == (
            ENGINE_NAME_SHAZAM, ENGINE_NAME_ACOUSTID)
        assert len(result.matches) == 2

    asyncio.run(_run())


def test_skips_unavailable_engines(candidate_record: ScanCandidateRecord) -> None:
    """Verify that unavailable engines are omitted from scan execution."""
    async def _run() -> None:
        mock_repo = MagicMock()

        mock_acoustid = MagicMock()
        mock_acoustid.engine_name = ENGINE_NAME_ACOUSTID
        mock_acoustid.is_available = False  # E.g. Missing API key
        mock_acoustid.identify = AsyncMock()

        service = MultiEngineMatcherService(
            candidate_repo=mock_repo,
            local_matcher=None,
            cloud_adapters=(mock_acoustid,),
        )

        options = MatchOptions(
            enabled_engines=(ENGINE_NAME_ACOUSTID,),
        )

        result = await service.match_candidate(candidate_record, options)

        mock_acoustid.identify.assert_not_called()
        assert result.engines_queried == ()
        assert result.matches == ()

    asyncio.run(_run())


def test_batch_match_callback_and_cancellation(
    candidate_record: ScanCandidateRecord,
) -> None:
    """Verify batch processing invokes progress callback and respects cancellation."""
    async def _run() -> None:
        mock_repo = MagicMock()
        mock_shazam = MagicMock()
        mock_shazam.engine_name = ENGINE_NAME_SHAZAM
        mock_shazam.is_available = True
        mock_shazam.identify = AsyncMock(return_value=())

        service = MultiEngineMatcherService(
            candidate_repo=mock_repo,
            local_matcher=None,
            cloud_adapters=(mock_shazam,),
        )

        options = MatchOptions(enabled_engines=(ENGINE_NAME_SHAZAM,))
        callback_calls = []

        def _cb(curr, tot, cand, res):
            callback_calls.append((curr, tot, cand.id))

        results = await service.batch_match(
            candidates=[candidate_record],
            options=options,
            progress_callback=_cb,
        )

        assert len(results) == 1
        assert len(callback_calls) == 1
        assert callback_calls[0] == (1, 1, 42)

        # Test cancellation
        cancel_token = threading.Event()
        cancel_token.set()
        with pytest.raises(RecognitionEngineCancelledError):
            await service.batch_match(
                candidates=[candidate_record],
                options=options,
                cancel_token=cancel_token,
            )

    asyncio.run(_run())


def test_multi_matcher_aclose_cascades() -> None:
    """Verify aclose cascades to all registered cloud adapters and suppresses errors."""
    async def _run() -> None:
        mock_adapter1 = MagicMock()
        mock_adapter1.engine_name = "shazam"
        mock_adapter1.aclose = AsyncMock()

        mock_adapter2 = MagicMock()
        mock_adapter2.engine_name = "acoustid"
        mock_adapter2.aclose = AsyncMock(
            side_effect=RuntimeError("Adapter 2 fail"))

        mock_adapter3 = MagicMock()
        # Adapter without aclose method should be safely ignored
        del mock_adapter3.aclose

        service = MultiEngineMatcherService(
            candidate_repo=MagicMock(),
            local_matcher=None,
            cloud_adapters=(mock_adapter1, mock_adapter2, mock_adapter3),
        )

        await service.aclose()
        mock_adapter1.aclose.assert_awaited_once()
        mock_adapter2.aclose.assert_awaited_once()

    asyncio.run(_run())
