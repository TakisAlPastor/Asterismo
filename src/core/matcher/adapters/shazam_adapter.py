"""Shazam acoustic recognition adapter utilizing shazamio."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sys
import threading
import time
from typing import Any, Sequence

from src.core.config.constants import (
    ENGINE_NAME_SHAZAM,
    SHAZAM_REQUEST_DELAY_SECONDS,
)
from src.core.matcher.adapters.base import (
    CloudMatchResult,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
)

logger = logging.getLogger(__name__)


class ShazamAdapter:
    """Asynchronous recognition engine adapter querying Shazam via shazamio."""

    def __init__(
        self,
        shazam_client: Any | None = None,
        pacing_delay: float = SHAZAM_REQUEST_DELAY_SECONDS,
    ) -> None:
        self._client = shazam_client
        self._pacing_delay = pacing_delay
        self._last_request_time: float = 0.0

    @property
    def engine_name(self) -> str:
        """Unique identifier of the engine."""
        return ENGINE_NAME_SHAZAM

    @property
    def is_available(self) -> bool:
        """Shazam requires no API keys or local binaries."""
        return True

    async def _resolve_client(self) -> Any:
        """Resolve or lazily initialize Shazam client."""
        if self._client is None:
            try:
                from shazamio import Shazam
                self._client = Shazam()
            except Exception as exc:
                raise RecognitionEngineError(
                    f"Failed to initialize Shazam client: {exc}") from exc
        return self._client

    async def identify(
        self,
        audio_path: Path,
        cancel_token: threading.Event | None = None,
    ) -> Sequence[CloudMatchResult]:
        """Perform Shazam recognition query with pacing cooldown."""
        if cancel_token and cancel_token.is_set():
            raise RecognitionEngineCancelledError(
                "Shazam identification cancelled before start.")

        if not audio_path.is_file():
            raise RecognitionEngineError(
                f"Audio file does not exist: {audio_path}")

        client = await self._resolve_client()

        # Enforce rate-limiting pacing interval between queries
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._pacing_delay:
            await asyncio.sleep(self._pacing_delay - elapsed)

        if cancel_token and cancel_token.is_set():
            raise RecognitionEngineCancelledError(
                "Shazam identification cancelled during pacing delay.")

        try:
            self._last_request_time = time.monotonic()
            response: dict[str, Any] = await client.recognize(str(audio_path))
        except Exception as exc:
            logger.warning(
                "Shazam recognition request failed for %s: %s", audio_path.name, exc)
            raise RecognitionEngineError(
                f"Shazam request failed: {exc}") from exc

        matches = response.get("matches", [])
        if not matches:
            return ()

        track = response.get("track", {})
        title = track.get("title", "").strip() or "Unknown Title"
        subtitle = track.get("subtitle", "").strip() or "Unknown Artist"
        matched_track = f"{subtitle} - {title}"

        offset_seconds = 0.0
        if matches and isinstance(matches[0], dict):
            offset_seconds = float(matches[0].get("offset", 0.0))

        details_json = json.dumps(track)

        return (
            CloudMatchResult(
                engine=ENGINE_NAME_SHAZAM,
                matched_track=matched_track,
                matched_database="Shazam",
                confidence=1.0,
                offset_seconds=offset_seconds,
                details_json=details_json,
            ),
        )

    async def aclose(self) -> None:
        """Close active network sessions and release connection pools."""
        if self._client is not None:
            client = self._client
            self._client = None
            try:
                if hasattr(client, "close"):
                    close_res = client.close()
                    if asyncio.iscoroutine(close_res):
                        await close_res
            except Exception as exc:
                logger.warning("Error closing Shazam client session: %s", exc)
