"""Unit tests for recognition engine adapters base protocol, models, and exceptions."""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Sequence
import pytest

from src.core.config.models import ConfigurationError
from src.core.matcher.adapters.base import (
    CloudMatchResult,
    RecognitionEngine,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
    RecognitionQuotaExhaustedError,
)


class MockCompliantEngine:
    """Mock engine implementing RecognitionEngine protocol."""

    @property
    def engine_name(self) -> str:
        return "mock_engine"

    @property
    def is_available(self) -> bool:
        return True

    async def identify(
        self,
        audio_path: Path,
        cancel_token: threading.Event | None = None,
    ) -> Sequence[CloudMatchResult]:
        return (
            CloudMatchResult(
                engine=self.engine_name,
                matched_track="Test Artist - Test Song",
                matched_database="MockDB",
                confidence=0.95,
            ),
        )

    async def aclose(self) -> None:
        pass


class MockNonCompliantEngine:
    """Class missing required protocol members."""

    def identify(self, path: Path) -> list:
        return []


def test_cloud_match_result_valid() -> None:
    """Verify valid CloudMatchResult creation and fields."""
    result = CloudMatchResult(
        engine="shazam",
        matched_track="The Most Mysterious Song - Subways Of Your Mind",
        matched_database="Shazam",
        confidence=1.0,
        offset_seconds=12.5,
        details_json='{"isrc": "US12345"}',
        aligned_hashes=0,
        raw_hashes=0,
    )
    assert result.engine == "shazam"
    assert result.confidence == 1.0
    assert result.offset_seconds == 12.5
    assert "Subways" in result.matched_track


def test_cloud_match_result_validation() -> None:
    """Verify validation boundaries on CloudMatchResult."""
    with pytest.raises(ConfigurationError, match="engine cannot be empty"):
        CloudMatchResult(
            engine="",
            matched_track="Song",
            matched_database="DB",
            confidence=0.5,
        )

    with pytest.raises(ConfigurationError, match="matched_track cannot be empty"):
        CloudMatchResult(
            engine="shazam",
            matched_track="",
            matched_database="DB",
            confidence=0.5,
        )

    with pytest.raises(ConfigurationError, match="matched_database cannot be empty"):
        CloudMatchResult(
            engine="shazam",
            matched_track="Song",
            matched_database="",
            confidence=0.5,
        )

    with pytest.raises(ConfigurationError, match="confidence must be between 0.0 and 1.0"):
        CloudMatchResult(
            engine="shazam",
            matched_track="Song",
            matched_database="DB",
            confidence=1.5,
        )

    with pytest.raises(ConfigurationError, match="confidence must be between 0.0 and 1.0"):
        CloudMatchResult(
            engine="shazam",
            matched_track="Song",
            matched_database="DB",
            confidence=-0.1,
        )

    with pytest.raises(ConfigurationError, match="aligned_hashes must be >= 0"):
        CloudMatchResult(
            engine="shazam",
            matched_track="Song",
            matched_database="DB",
            confidence=0.5,
            aligned_hashes=-1,
        )


def test_recognition_engine_protocol() -> None:
    """Verify runtime checkability of RecognitionEngine protocol."""
    engine = MockCompliantEngine()
    assert isinstance(engine, RecognitionEngine)
    assert engine.engine_name == "mock_engine"
    assert engine.is_available is True

    bad_engine = MockNonCompliantEngine()
    assert not isinstance(bad_engine, RecognitionEngine)


def test_recognition_exceptions_hierarchy() -> None:
    """Verify exception hierarchy for recognition engine errors."""
    assert issubclass(RecognitionEngineCancelledError, RecognitionEngineError)
    assert issubclass(RecognitionQuotaExhaustedError, RecognitionEngineError)
    assert issubclass(RecognitionEngineError, Exception)
