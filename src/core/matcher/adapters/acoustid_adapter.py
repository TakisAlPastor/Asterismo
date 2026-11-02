"""AcoustID and MusicBrainz acoustic recognition adapter."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Sequence

import httpx

from src.core.config.constants import (
    ACOUSTID_BLACKLISTED_REGEX,
    ACOUSTID_FPCALC_MAX_DURATION_SECONDS,
    ACOUSTID_LOOKUP_ENDPOINT,
    ACOUSTID_MIN_SCORE_THRESHOLD,
    ACOUSTID_RATE_LIMIT_DELAY_SECONDS,
    BIN_NAME_FPCALC,
    DEFAULT_HTTP_TIMEOUT_SEC,
    ENGINE_NAME_ACOUSTID,
)
from src.core.matcher.adapters.base import (
    CloudMatchResult,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
)
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import SilentProcessRunner

logger = logging.getLogger(__name__)


class AcoustIDAdapter:
    """Asynchronous recognition engine querying AcoustID and MusicBrainz."""

    def __init__(
        self,
        api_key: str = "",
        locator: BinaryLocator | None = None,
        runner: SilentProcessRunner | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limit_delay: float = ACOUSTID_RATE_LIMIT_DELAY_SECONDS,
        min_score: float = ACOUSTID_MIN_SCORE_THRESHOLD,
    ) -> None:
        self._api_key = api_key.strip()
        self._locator = locator or BinaryLocator()
        self._runner = runner or SilentProcessRunner()
        self._http_client = http_client
        self._rate_limit_delay = rate_limit_delay
        self._min_score = min_score
        self._last_request_time: float = 0.0
        self._blacklist_pattern = re.compile(ACOUSTID_BLACKLISTED_REGEX)

    @property
    def engine_name(self) -> str:
        """Unique identifier of the engine."""
        return ENGINE_NAME_ACOUSTID

    @property
    def is_available(self) -> bool:
        """Available only when API key is provided and fpcalc is present."""
        if not self._api_key:
            return False
        return self._locator.locate(BIN_NAME_FPCALC) is not None

    def _extract_fingerprint(self, audio_path: Path) -> tuple[float, str]:
        """Compute Chromaprint fingerprint and duration using fpcalc."""
        fpcalc_path = self._locator.locate(BIN_NAME_FPCALC)
        if not fpcalc_path:
            raise RecognitionEngineError(
                f"Cannot calculate fingerprint: '{BIN_NAME_FPCALC}' binary not found.")

        result = self._runner.run(
            [
                str(fpcalc_path),
                "-length",
                str(ACOUSTID_FPCALC_MAX_DURATION_SECONDS),
                "-json",
                str(audio_path),
            ],
            timeout=DEFAULT_HTTP_TIMEOUT_SEC,
        )

        if not result.is_success:
            raise RecognitionEngineError(
                f"fpcalc failed ({result.returncode}): {result.stderr.strip()}")

        try:
            data = json.loads(result.stdout)
            duration = float(data.get("duration", 0.0))
            fingerprint = str(data.get("fingerprint", "")).strip()
            if duration <= 0 or not fingerprint:
                raise ValueError("Missing duration or fingerprint data")
            return duration, fingerprint
        except Exception as exc:
            raise RecognitionEngineError(
                f"Failed to parse fpcalc JSON output: {exc}") from exc

    async def identify(
        self,
        audio_path: Path,
        cancel_token: threading.Event | None = None,
    ) -> Sequence[CloudMatchResult]:
        """Extract fingerprint and query AcoustID REST API with rate limiting."""
        if cancel_token and cancel_token.is_set():
            raise RecognitionEngineCancelledError(
                "AcoustID identification cancelled before start.")

        if not self._api_key:
            raise RecognitionEngineError("AcoustID API key is not configured.")

        if not audio_path.is_file():
            raise RecognitionEngineError(
                f"Audio file does not exist: {audio_path}")

        duration, fingerprint = self._extract_fingerprint(audio_path)

        # Enforce minimum delay between calls to respect rate limits
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_delay:
            await asyncio.sleep(self._rate_limit_delay - elapsed)

        if cancel_token and cancel_token.is_set():
            raise RecognitionEngineCancelledError(
                "AcoustID identification cancelled during cooldown.")

        params = {
            "client": self._api_key,
            "format": "json",
            "duration": str(int(round(duration))),
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups",
        }

        try:
            self._last_request_time = time.monotonic()
            if self._http_client is not None:
                resp = await self._http_client.post(ACOUSTID_LOOKUP_ENDPOINT, data=params)
                resp.raise_for_status()
                payload = resp.json()
            else:
                async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT_SEC) as client:
                    resp = await client.post(ACOUSTID_LOOKUP_ENDPOINT, data=params)
                    resp.raise_for_status()
                    payload = resp.json()
        except Exception as exc:
            logger.warning("AcoustID request failed for %s: %s",
                           audio_path.name, exc)
            raise RecognitionEngineError(
                f"AcoustID request failed: {exc}") from exc

        if payload.get("status") != "ok":
            error_data = payload.get("error", {})
            error_msg = error_data.get("message", "Unknown API error")
            if "invalid API key" in error_msg.lower():
                error_msg = (
                    "Invalid AcoustID API key. AcoustID requires an Application (Client) Key "
                    "registered at https://acoustid.org/my-applications (not a personal User Key)."
                )
            raise RecognitionEngineError(f"AcoustID API error: {error_msg}")

        results_data = payload.get("results", [])
        matches: list[CloudMatchResult] = []

        for item in results_data:
            if not isinstance(item, dict):
                continue
            track_id = str(item.get("id", "")).strip()
            if self._blacklist_pattern.match(track_id):
                continue

            score = float(item.get("score", 0.0))
            if score < self._min_score:
                continue

            recordings = item.get("recordings", [])
            matched_track = f"AcoustID Track {track_id}"
            if recordings and isinstance(recordings[0], dict):
                rec = recordings[0]
                rec_title = rec.get("title", "").strip() or "Unknown Title"
                artists = rec.get("artists", [])
                artist_name = ""
                if artists and isinstance(artists[0], dict):
                    artist_name = artists[0].get("name", "").strip()
                matched_track = f"{artist_name} - {rec_title}" if artist_name else rec_title

            matches.append(
                CloudMatchResult(
                    engine=ENGINE_NAME_ACOUSTID,
                    matched_track=matched_track,
                    matched_database="MusicBrainz",
                    confidence=min(1.0, max(0.0, score)),
                    details_json=json.dumps(item),
                )
            )

        return tuple(matches)
