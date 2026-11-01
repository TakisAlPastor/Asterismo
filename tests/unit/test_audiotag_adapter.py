"""Unit tests for AudioTag cloud recognition adapter."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.core.config.constants import ENGINE_NAME_AUDIOTAG
from src.core.matcher.adapters.base import (
    RecognitionEngine,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
    RecognitionQuotaExhaustedError,
)
from src.core.matcher.adapters.audiotag_adapter import AudioTagAdapter
from src.core.process.runner import ProcessResult


@pytest.fixture
def dummy_audio_file(tmp_path: Path) -> Path:
    audio = tmp_path / "lostwave.mp3"
    audio.write_bytes(b"\xFF\xFB\x90\x00" * 100)
    return audio


def test_audiotag_adapter_protocol_and_availability() -> None:
    """Verify AudioTagAdapter conforms to protocol and checks credentials."""
    adapter = AudioTagAdapter(api_key="valid_tag_key")
    assert isinstance(adapter, RecognitionEngine)
    assert adapter.engine_name == ENGINE_NAME_AUDIOTAG
    assert adapter.is_available is True

    adapter_no_key = AudioTagAdapter(api_key="")
    assert adapter_no_key.is_available is False


def test_audiotag_adapter_immediate_match(dummy_audio_file: Path) -> None:
    """Verify phase 1 immediate match mapping."""
    async def _run() -> None:
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "job_status": "found",
            "data": [
                {
                    "tracks": [
                        {
                            "title": "Stay The Second Time Around",
                            "artist": "The Sponges",
                            "confidence": 98,
                        }
                    ]
                }
            ],
        }
        mock_http.post = AsyncMock(return_value=mock_resp)

        adapter = AudioTagAdapter(
            api_key="valid_key",
            http_client=mock_http,
            cooldown_min=0.0,
            cooldown_max=0.0,
        )

        results = await adapter.identify(dummy_audio_file)
        assert len(results) == 1
        match = results[0]
        assert match.engine == ENGINE_NAME_AUDIOTAG
        assert match.matched_track == "The Sponges - Stay The Second Time Around"
        assert match.matched_database == "AudioTag"
        assert match.confidence == 0.98

    asyncio.run(_run())


def test_audiotag_adapter_two_phase_polling(dummy_audio_file: Path) -> None:
    """Verify phase 2 polling loop until found status."""
    async def _run() -> None:
        mock_http = MagicMock()

        init_resp = MagicMock()
        init_resp.status_code = 200
        init_resp.json.return_value = {
            "success": True,
            "job_status": "wait",
            "token": "tok_xyz_123",
        }

        poll_resp_wait = MagicMock()
        poll_resp_wait.status_code = 200
        poll_resp_wait.json.return_value = {
            "success": True,
            "result": "wait",
        }

        poll_resp_found = MagicMock()
        poll_resp_found.status_code = 200
        poll_resp_found.json.return_value = {
            "success": True,
            "result": "found",
            "data": [
                {
                    "tracks": [
                        {
                            "title": "On The Roof",
                            "artist": "Johan Lindell",
                            "confidence": 100,
                        }
                    ]
                }
            ],
        }

        mock_http.post = AsyncMock(
            side_effect=[init_resp, poll_resp_wait, poll_resp_found])

        adapter = AudioTagAdapter(
            api_key="valid_key",
            http_client=mock_http,
            poll_interval=0.01,
            cooldown_min=0.0,
            cooldown_max=0.0,
        )

        results = await adapter.identify(dummy_audio_file)
        assert len(results) == 1
        assert results[0].matched_track == "Johan Lindell - On The Roof"
        assert results[0].confidence == 1.0

    asyncio.run(_run())


def test_audiotag_adapter_credit_exhaustion(dummy_audio_file: Path) -> None:
    """Verify quota circuit breaker is tripped on credit exhaustion."""
    async def _run() -> None:
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": False,
            "error": "credit balance exhausted",
        }
        mock_http.post = AsyncMock(return_value=mock_resp)

        adapter = AudioTagAdapter(
            api_key="exhausted_key",
            http_client=mock_http,
            cooldown_min=0.0,
            cooldown_max=0.0,
        )

        assert adapter.is_available is True
        with pytest.raises(RecognitionQuotaExhaustedError, match="quota balance exhausted"):
            await adapter.identify(dummy_audio_file)

        # Quota breaker is tripped and engine becomes unavailable
        assert adapter.is_quota_exhausted is True
        assert adapter.is_available is False

        # Subsequent attempts are rejected immediately
        with pytest.raises(RecognitionQuotaExhaustedError, match="marked exhausted"):
            await adapter.identify(dummy_audio_file)

    asyncio.run(_run())


def test_audiotag_adapter_loop_extension(tmp_path: Path, dummy_audio_file: Path) -> None:
    """Verify loop extension triggers when clip duration is under threshold."""
    async def _run() -> None:
        mock_locator = MagicMock()
        mock_locator.locate.side_effect = lambda name: Path(f"/bin/{name}")

        mock_runner = MagicMock()
        # ffprobe reports duration of 8 seconds (< 20 seconds threshold)
        mock_runner.run.side_effect = [
            ProcessResult(returncode=0, stdout="8.0\n",
                          stderr="", command=("ffprobe",)),
            ProcessResult(returncode=0, stdout="",
                          stderr="", command=("ffmpeg",)),
        ]

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "job_status": "not found",
        }
        mock_http.post = AsyncMock(return_value=mock_resp)

        adapter = AudioTagAdapter(
            api_key="valid_key",
            locator=mock_locator,
            runner=mock_runner,
            http_client=mock_http,
            cooldown_min=0.0,
            cooldown_max=0.0,
            min_duration=20.0,
        )

        results = await adapter.identify(dummy_audio_file)
        assert results == ()
        # Assert ffprobe and ffmpeg were both executed
        assert mock_runner.run.call_count == 2
        ffmpeg_call = mock_runner.run.call_args_list[1][0][0]
        assert "-stream_loop" in ffmpeg_call

    asyncio.run(_run())


def test_audiotag_adapter_cancelled(dummy_audio_file: Path) -> None:
    """Verify cancellation token stops AudioTag before upload."""
    async def _run() -> None:
        cancel_token = threading.Event()
        cancel_token.set()

        adapter = AudioTagAdapter(
            api_key="valid_key",
            cooldown_min=0.0,
            cooldown_max=0.0,
        )

        with pytest.raises(RecognitionEngineCancelledError):
            await adapter.identify(dummy_audio_file, cancel_token=cancel_token)

    asyncio.run(_run())
