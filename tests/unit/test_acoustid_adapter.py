"""Unit tests for AcoustID and MusicBrainz recognition adapter."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.core.config.constants import ENGINE_NAME_ACOUSTID
from src.core.matcher.adapters.base import (
    RecognitionEngine,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
)
from src.core.matcher.adapters.acoustid_adapter import AcoustIDAdapter
from src.core.process.runner import ProcessResult


@pytest.fixture
def dummy_audio_file(tmp_path: Path) -> Path:
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"\xFF\xFB\x90\x00")
    return audio


def test_acoustid_adapter_protocol_and_availability(tmp_path: Path) -> None:
    """Verify AcoustIDAdapter protocol compliance and availability checks."""
    mock_locator = MagicMock()
    mock_locator.locate.return_value = tmp_path / "fpcalc.exe"

    adapter = AcoustIDAdapter(api_key="valid_key", locator=mock_locator)
    assert isinstance(adapter, RecognitionEngine)
    assert adapter.engine_name == ENGINE_NAME_ACOUSTID
    assert adapter.is_available is True

    # Unavailable when no key
    adapter_no_key = AcoustIDAdapter(api_key="", locator=mock_locator)
    assert adapter_no_key.is_available is False

    # Unavailable when fpcalc missing
    mock_locator_missing = MagicMock()
    mock_locator_missing.locate.return_value = None
    adapter_no_fpcalc = AcoustIDAdapter(
        api_key="valid_key", locator=mock_locator_missing)
    assert adapter_no_fpcalc.is_available is False


def test_acoustid_adapter_positive_match(dummy_audio_file: Path) -> None:
    """Verify successful AcoustID lookup and MusicBrainz metadata mapping."""
    async def _run() -> None:
        mock_locator = MagicMock()
        mock_locator.locate.return_value = Path("/bin/fpcalc.exe")

        mock_runner = MagicMock()
        mock_runner.run.return_value = ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {"duration": 180.5, "fingerprint": "AQAAZEkSRYmiRAkm"}),
            stderr="",
            command=("fpcalc",),
        )

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "results": [
                {
                    "id": "11223344",
                    "score": 0.88,
                    "recordings": [
                        {
                            "id": "rec-1",
                            "title": "Blind The Wind",
                            "artists": [{"name": "FEX"}],
                        }
                    ],
                }
            ],
        }
        mock_http.get = AsyncMock(return_value=mock_resp)

        adapter = AcoustIDAdapter(
            api_key="test_api_key",
            locator=mock_locator,
            runner=mock_runner,
            http_client=mock_http,
            rate_limit_delay=0.0,
        )

        results = await adapter.identify(dummy_audio_file)
        assert len(results) == 1
        match = results[0]
        assert match.engine == ENGINE_NAME_ACOUSTID
        assert match.matched_track == "FEX - Blind The Wind"
        assert match.matched_database == "MusicBrainz"
        assert match.confidence == 0.88
        assert "11223344" in match.details_json

    asyncio.run(_run())


def test_acoustid_adapter_filters_low_score_and_blacklisted(dummy_audio_file: Path) -> None:
    """Verify blacklisted IDs and matches below score threshold are discarded."""
    async def _run() -> None:
        mock_locator = MagicMock()
        mock_locator.locate.return_value = Path("/bin/fpcalc.exe")

        mock_runner = MagicMock()
        mock_runner.run.return_value = ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {"duration": 120.0, "fingerprint": "AQAAZEkSRYmiRAkm"}),
            stderr="",
            command=("fpcalc",),
        )

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "results": [
                {"id": "94765123", "score": 0.99},  # Blacklisted regex match
                {"id": "55667788", "score": 0.40},  # Below 0.60 threshold
            ],
        }
        mock_http.get = AsyncMock(return_value=mock_resp)

        adapter = AcoustIDAdapter(
            api_key="test_key",
            locator=mock_locator,
            runner=mock_runner,
            http_client=mock_http,
            rate_limit_delay=0.0,
        )

        results = await adapter.identify(dummy_audio_file)
        assert results == ()

    asyncio.run(_run())


def test_acoustid_adapter_fpcalc_failure(dummy_audio_file: Path) -> None:
    """Verify error raised when fpcalc fails or exits non-zero."""
    async def _run() -> None:
        mock_locator = MagicMock()
        mock_locator.locate.return_value = Path("/bin/fpcalc.exe")

        mock_runner = MagicMock()
        mock_runner.run.return_value = ProcessResult(
            returncode=1,
            stdout="",
            stderr="Invalid audio header",
            command=("fpcalc",),
        )

        adapter = AcoustIDAdapter(
            api_key="test_key",
            locator=mock_locator,
            runner=mock_runner,
            rate_limit_delay=0.0,
        )

        with pytest.raises(RecognitionEngineError, match="fpcalc failed"):
            await adapter.identify(dummy_audio_file)

    asyncio.run(_run())


def test_acoustid_adapter_api_error_response(dummy_audio_file: Path) -> None:
    """Verify error raised when AcoustID API returns status != 'ok'."""
    async def _run() -> None:
        mock_locator = MagicMock()
        mock_locator.locate.return_value = Path("/bin/fpcalc.exe")

        mock_runner = MagicMock()
        mock_runner.run.return_value = ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {"duration": 120.0, "fingerprint": "AQAAZEkSRYmiRAkm"}),
            stderr="",
            command=("fpcalc",),
        )

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "error",
            "error": {"message": "invalid API key"},
        }
        mock_http.get = AsyncMock(return_value=mock_resp)

        adapter = AcoustIDAdapter(
            api_key="bad_key",
            locator=mock_locator,
            runner=mock_runner,
            http_client=mock_http,
            rate_limit_delay=0.0,
        )

        with pytest.raises(RecognitionEngineError, match="AcoustID API error: invalid API key"):
            await adapter.identify(dummy_audio_file)

    asyncio.run(_run())


def test_acoustid_adapter_cancelled(dummy_audio_file: Path) -> None:
    """Verify cancellation token stops identification before fpcalc."""
    async def _run() -> None:
        cancel_token = threading.Event()
        cancel_token.set()

        adapter = AcoustIDAdapter(api_key="test_key", rate_limit_delay=0.0)
        with pytest.raises(RecognitionEngineCancelledError):
            await adapter.identify(dummy_audio_file, cancel_token=cancel_token)

    asyncio.run(_run())
