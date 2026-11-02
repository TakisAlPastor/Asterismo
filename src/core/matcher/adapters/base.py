"""Base protocols, result models, and exception hierarchy for recognition engines."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Protocol, Sequence, runtime_checkable

from src.core.config.models import ConfigurationError


class RecognitionEngineError(Exception):
    """Base exception for recognition engine query and decoding errors."""


class RecognitionEngineCancelledError(RecognitionEngineError):
    """Raised when an in-flight engine query is cancelled by user request."""


class RecognitionQuotaExhaustedError(RecognitionEngineError):
    """Raised when an engine API quota or credit balance has been exhausted."""


@dataclass(frozen=True, slots=True)
class CloudMatchResult:
    """Normalized recognition result returned by an acoustic or cloud engine."""

    engine: str
    matched_track: str
    matched_database: str
    confidence: float
    offset_seconds: float = 0.0
    details_json: str = "{}"
    aligned_hashes: int = 0
    raw_hashes: int = 0

    def __post_init__(self) -> None:
        """Validate result parameters."""
        if not self.engine.strip():
            raise ConfigurationError("engine cannot be empty")
        if not self.matched_track.strip():
            raise ConfigurationError("matched_track cannot be empty")
        if not self.matched_database.strip():
            raise ConfigurationError("matched_database cannot be empty")
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ConfigurationError(
                f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
        if self.aligned_hashes < 0:
            raise ConfigurationError(
                f"aligned_hashes must be >= 0, got {self.aligned_hashes}"
            )
        if self.raw_hashes < 0:
            raise ConfigurationError(
                f"raw_hashes must be >= 0, got {self.raw_hashes}"
            )


@runtime_checkable
class RecognitionEngine(Protocol):
    """Pluggable interface for acoustic and cloud recognition engines."""

    @property
    def engine_name(self) -> str:
        """Unique identifier of the recognition engine."""
        ...

    @property
    def is_available(self) -> bool:
        """Indicate whether the engine has required credentials and binaries configured."""
        ...

    async def identify(
        self,
        audio_path: Path,
        cancel_token: threading.Event | None = None,
    ) -> Sequence[CloudMatchResult]:
        """Query engine for acoustic matches on candidate audio track."""
        ...

    async def aclose(self) -> None:
        """Release underlying network sessions, connection pools, or child resources."""
        ...
