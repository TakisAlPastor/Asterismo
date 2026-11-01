"""AudioTag cloud recognition adapter with polling, loop-extension, and quota circuit breaking."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import random
import tempfile
import threading
import time
from typing import Any, Sequence
import uuid

import httpx

from src.core.config.constants import (
    AUDIOTAG_COOLDOWN_MAX_SECONDS,
    AUDIOTAG_COOLDOWN_MIN_SECONDS,
    AUDIOTAG_ENDPOINT,
    AUDIOTAG_LOOP_TARGET_SECONDS,
    AUDIOTAG_MAX_POLL_ATTEMPTS,
    AUDIOTAG_MIN_DURATION_SECONDS,
    AUDIOTAG_POLL_INTERVAL_SECONDS,
    AUDIOTAG_STATUS_FOUND,
    AUDIOTAG_STATUS_NOT_FOUND,
    AUDIOTAG_STATUS_WAIT,
    BIN_NAME_FFMPEG,
    BIN_NAME_FFPROBE,
    DEFAULT_HTTP_TIMEOUT_SEC,
    ENGINE_NAME_AUDIOTAG,
)
from src.core.matcher.adapters.base import (
    CloudMatchResult,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
    RecognitionQuotaExhaustedError,
)
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import SilentProcessRunner

logger = logging.getLogger(__name__)


class AudioTagAdapter:
    """Asynchronous recognition engine adapter querying the AudioTag v1.0 API."""

    def __init__(
        self,
        api_key: str = "",
        locator: BinaryLocator | None = None,
        runner: SilentProcessRunner | None = None,
        http_client: httpx.AsyncClient | None = None,
        min_duration: float = AUDIOTAG_MIN_DURATION_SECONDS,
        loop_target: float = AUDIOTAG_LOOP_TARGET_SECONDS,
        poll_interval: float = AUDIOTAG_POLL_INTERVAL_SECONDS,
        max_poll_attempts: int = AUDIOTAG_MAX_POLL_ATTEMPTS,
        cooldown_min: float = AUDIOTAG_COOLDOWN_MIN_SECONDS,
        cooldown_max: float = AUDIOTAG_COOLDOWN_MAX_SECONDS,
    ) -> None:
        self._api_key = api_key.strip()
        self._locator = locator or BinaryLocator()
        self._runner = runner or SilentProcessRunner()
        self._http_client = http_client
        self._min_duration = min_duration
        self._loop_target = loop_target
        self._poll_interval = poll_interval
        self._max_poll_attempts = max_poll_attempts
        self._cooldown_min = cooldown_min
        self._cooldown_max = cooldown_max
        self._quota_exhausted: bool = False

    @property
    def engine_name(self) -> str:
        """Unique identifier of the engine."""
        return ENGINE_NAME_AUDIOTAG

    @property
    def is_available(self) -> bool:
        """Available when an API key is provided and quota is not exhausted."""
        return bool(self._api_key) and not self._quota_exhausted

    @property
    def is_quota_exhausted(self) -> bool:
        """Indicate whether the API key or credit balance has been exhausted."""
        return self._quota_exhausted

    def _probe_duration(self, audio_path: Path) -> float | None:
        """Probe audio file duration in seconds via ffprobe or ffmpeg."""
        ffprobe_path = self._locator.locate(BIN_NAME_FFPROBE)
        if ffprobe_path:
            cmd = [
                str(ffprobe_path),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ]
            res = self._runner.run(cmd, timeout=10.0)
            if res.is_success and res.stdout.strip():
                try:
                    return float(res.stdout.strip())
                except ValueError:
                    pass

        ffmpeg_path = self._locator.locate(BIN_NAME_FFMPEG)
        if ffmpeg_path:
            cmd = [str(ffmpeg_path), "-hide_banner", "-i",
                   str(audio_path), "-f", "null", "-"]
            res = self._runner.run(cmd, timeout=10.0)
            output = res.stderr or res.stdout
            for line in output.splitlines():
                if "Duration:" in line:
                    parts = line.split("Duration:")[1].split(",")[0].strip()
                    try:
                        h, m, s = parts.split(":")
                        return float(h) * 3600 + float(m) * 60 + float(s)
                    except Exception:
                        pass
        return None

    def _loop_extend_clip(self, audio_path: Path) -> Path:
        """Extend short audio clips by looping audio up to target seconds."""
        ffmpeg_path = self._locator.locate(BIN_NAME_FFMPEG)
        if not ffmpeg_path:
            logger.warning(
                "ffmpeg not found, skipping loop extension for short clip.")
            return audio_path

        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"audiotag_loop_{uuid.uuid4().hex[:8]}.mp3"

        cmd = [
            str(ffmpeg_path),
            "-stream_loop",
            "-1",
            "-i",
            str(audio_path),
            "-t",
            str(self._loop_target),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            "-y",
            str(output_path),
        ]

        res = self._runner.run(cmd, timeout=20.0)
        if res.is_success and output_path.is_file():
            return output_path
        return audio_path

    def _classify_error_and_raise(self, error_text: str) -> None:
        """Check error text and trip circuit breaker if credit is exhausted."""
        normalized = error_text.lower()
        if "credit balance" in normalized or "insufficient" in normalized:
            self._quota_exhausted = True
            raise RecognitionQuotaExhaustedError(
                f"AudioTag quota balance exhausted: {error_text}")
        if "api key" in normalized and ("expired" in normalized or "invalid" in normalized or "unrecognized" in normalized):
            self._quota_exhausted = True
            raise RecognitionQuotaExhaustedError(
                f"AudioTag API key invalid or expired: {error_text}")
        raise RecognitionEngineError(f"AudioTag API error: {error_text}")

    async def _post_api(self, data: dict[str, Any], files: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send HTTP POST request to AudioTag API."""
        if self._http_client is not None:
            resp = await self._http_client.post(
                AUDIOTAG_ENDPOINT,
                data=data,
                files=files,
                timeout=DEFAULT_HTTP_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            return resp.json()

        async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT_SEC) as client:
            resp = await client.post(
                AUDIOTAG_ENDPOINT,
                data=data,
                files=files,
            )
            resp.raise_for_status()
            return resp.json()

    def _parse_track_hit(self, match_data: dict[str, Any]) -> CloudMatchResult:
        """Parse matched track payload into normalized CloudMatchResult."""
        title = str(match_data.get("title", "")).strip() or "Unknown Title"
        artist = str(match_data.get("artist", "")).strip() or "Unknown Artist"
        confidence_val = float(match_data.get("confidence", 100)) / 100.0
        confidence = min(1.0, max(0.0, confidence_val))
        matched_track = f"{artist} - {title}"

        return CloudMatchResult(
            engine=ENGINE_NAME_AUDIOTAG,
            matched_track=matched_track,
            matched_database="AudioTag",
            confidence=confidence,
            details_json=json.dumps(match_data),
        )

    async def identify(
        self,
        audio_path: Path,
        cancel_token: threading.Event | None = None,
    ) -> Sequence[CloudMatchResult]:
        """Perform two-phase AudioTag identification with polling and loop extension."""
        if cancel_token and cancel_token.is_set():
            raise RecognitionEngineCancelledError(
                "AudioTag identification cancelled before start.")

        if not self._api_key:
            raise RecognitionEngineError("AudioTag API key is not configured.")

        if self._quota_exhausted:
            raise RecognitionQuotaExhaustedError(
                "AudioTag API key/quota is marked exhausted for this run.")

        if not audio_path.is_file():
            raise RecognitionEngineError(
                f"Audio file does not exist: {audio_path}")

        # Check duration and extend if clip is too short
        extended_file: Path | None = None
        target_path = audio_path
        try:
            duration = self._probe_duration(audio_path)
            if duration is not None and duration < self._min_duration:
                extended_file = self._loop_extend_clip(audio_path)
                target_path = extended_file

            # Randomized cooldown pacing before querying
            if self._cooldown_max > 0:
                cooldown = random.uniform(
                    self._cooldown_min, self._cooldown_max)
                await asyncio.sleep(cooldown)

            if cancel_token and cancel_token.is_set():
                raise RecognitionEngineCancelledError(
                    "AudioTag identification cancelled during cooldown.")

            # Phase 1: Upload audio and trigger identification job
            file_bytes = target_path.read_bytes()
            data = {
                "action": "identify",
                "apikey": self._api_key,
                "start_time": "0",
                "time_len": "180",
            }
            files = {
                "file": (target_path.name, file_bytes, "application/octet-stream"),
            }

            try:
                initial_res = await self._post_api(data=data, files=files)
            except Exception as exc:
                raise RecognitionEngineError(
                    f"AudioTag upload request failed: {exc}") from exc

            if not initial_res.get("success", True):
                error_msg = initial_res.get("error", "Unknown initial error")
                self._classify_error_and_raise(error_msg)

            job_status = initial_res.get("job_status")
            if job_status == AUDIOTAG_STATUS_FOUND:
                tracks = initial_res.get("data", [{}])[0].get("tracks", [])
                if tracks and isinstance(tracks[0], dict):
                    return (self._parse_track_hit(tracks[0]),)
                return ()

            if job_status == AUDIOTAG_STATUS_NOT_FOUND:
                return ()

            if job_status == AUDIOTAG_STATUS_WAIT:
                token = initial_res.get("token")
                if not token:
                    raise RecognitionEngineError(
                        "AudioTag returned WAIT status without token")

                # Phase 2: Polling loop
                for _ in range(self._max_poll_attempts):
                    if cancel_token and cancel_token.is_set():
                        raise RecognitionEngineCancelledError(
                            "AudioTag identification cancelled during polling.")

                    await asyncio.sleep(self._poll_interval)
                    poll_data = {
                        "action": "get_result",
                        "apikey": self._api_key,
                        "token": token,
                    }

                    try:
                        poll_res = await self._post_api(data=poll_data)
                    except Exception as exc:
                        raise RecognitionEngineError(
                            f"AudioTag polling request failed: {exc}") from exc

                    if not poll_res.get("success", True):
                        error_msg = poll_res.get(
                            "error", "Unknown polling error")
                        self._classify_error_and_raise(error_msg)

                    poll_result = poll_res.get(
                        "result", poll_res.get("job_status"))
                    if poll_result == AUDIOTAG_STATUS_FOUND:
                        tracks = poll_res.get("data", [{}])[
                            0].get("tracks", [])
                        if tracks and isinstance(tracks[0], dict):
                            return (self._parse_track_hit(tracks[0]),)
                        return ()

                    if poll_result == AUDIOTAG_STATUS_NOT_FOUND:
                        return ()

                    if poll_result == AUDIOTAG_STATUS_WAIT:
                        continue

                raise RecognitionEngineError(
                    "AudioTag identification timed out after maximum polling attempts.")

            return ()

        finally:
            if extended_file and extended_file.is_file() and extended_file != audio_path:
                try:
                    extended_file.unlink()
                except OSError:
                    pass
