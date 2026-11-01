"""Unit tests for Shazam recognition adapter."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.core.config.constants import ENGINE_NAME_SHAZAM
from src.core.matcher.adapters.base import (
    RecognitionEngine,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
)
from src.core.matcher.adapters.shazam_adapter import ShazamAdapter


@pytest.fixture
def dummy_audio_file(tmp_path: Path) -> Path:
    audio = tmp_path / "sample.mp3"
    audio.write_bytes(b"\xFF\xFB\x90\x00")
    return audio


def test_shazam_adapter_implements_protocol() -> None:
    """Verify ShazamAdapter conforms to RecognitionEngine protocol."""
    mock_client = MagicMock()
    adapter = ShazamAdapter(shazam_client=mock_client)
    assert isinstance(adapter, RecognitionEngine)
    assert adapter.engine_name == ENGINE_NAME_SHAZAM
    assert adapter.is_available is True

    # When no client is injected, adapter is still available
    default_adapter = ShazamAdapter()
    assert default_adapter.is_available is True


def test_shazam_adapter_positive_match(dummy_audio_file: Path) -> None:
    """Verify successful recognition response mapping."""
    async def _run() -> None:
        mock_shazam = MagicMock()
        mock_shazam.recognize = AsyncMock(
            return_value={
                "matches": [{"id": "12345", "offset": 14.8}],
                "track": {
                    "title": "Subways Of Your Mind",
                    "subtitle": "FEX",
                    "key": "999888",
                },
            }
        )

        adapter = ShazamAdapter(shazam_client=mock_shazam, pacing_delay=0.0)
        results = await adapter.identify(dummy_audio_file)

        assert len(results) == 1
        match = results[0]
        assert match.engine == ENGINE_NAME_SHAZAM
        assert match.matched_track == "FEX - Subways Of Your Mind"
        assert match.matched_database == "Shazam"
        assert match.confidence == 1.0
        assert match.offset_seconds == 14.8
        assert "999888" in match.details_json

    asyncio.run(_run())


def test_shazam_adapter_no_match(dummy_audio_file: Path) -> None:
    """Verify empty tuple when Shazam finds no matches."""
    async def _run() -> None:
        mock_shazam = MagicMock()
        mock_shazam.recognize = AsyncMock(return_value={"matches": []})

        adapter = ShazamAdapter(shazam_client=mock_shazam, pacing_delay=0.0)
        results = await adapter.identify(dummy_audio_file)

        assert results == ()

    asyncio.run(_run())


def test_shazam_adapter_file_not_found(tmp_path: Path) -> None:
    """Verify error raised when audio file is missing."""
    async def _run() -> None:
        adapter = ShazamAdapter(shazam_client=MagicMock(), pacing_delay=0.0)
        missing_file = tmp_path / "non_existent.mp3"

        with pytest.raises(RecognitionEngineError, match="does not exist"):
            await adapter.identify(missing_file)

    asyncio.run(_run())


def test_shazam_adapter_cancelled(dummy_audio_file: Path) -> None:
    """Verify cancellation token stops query before execution."""
    async def _run() -> None:
        cancel_token = threading.Event()
        cancel_token.set()

        adapter = ShazamAdapter(shazam_client=MagicMock(), pacing_delay=0.0)
        with pytest.raises(RecognitionEngineCancelledError):
            await adapter.identify(dummy_audio_file, cancel_token=cancel_token)

    asyncio.run(_run())


def test_shazam_adapter_pacing_delay(dummy_audio_file: Path) -> None:
    """Verify pacing delay is triggered on rapid consecutive requests."""
    async def _run() -> None:
        mock_shazam = MagicMock()
        mock_shazam.recognize = AsyncMock(return_value={"matches": []})

        adapter = ShazamAdapter(shazam_client=mock_shazam, pacing_delay=0.1)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # First request sets last_request_time
            await adapter.identify(dummy_audio_file)
            mock_sleep.assert_not_called()

            # Second immediate request triggers sleep
            await adapter.identify(dummy_audio_file)
            mock_sleep.assert_called_once()

    asyncio.run(_run())


def test_shazam_adapter_client_error(dummy_audio_file: Path) -> None:
    """Verify client network/API exception is wrapped in RecognitionEngineError."""
    async def _run() -> None:
        mock_shazam = MagicMock()
        mock_shazam.recognize = AsyncMock(
            side_effect=RuntimeError("Connection reset"))

        adapter = ShazamAdapter(shazam_client=mock_shazam, pacing_delay=0.0)
        with pytest.raises(RecognitionEngineError, match="Shazam request failed: Connection reset"):
            await adapter.identify(dummy_audio_file)

    asyncio.run(_run())
