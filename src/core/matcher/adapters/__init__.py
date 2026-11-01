"""Cloud recognition engine adapters and protocol definitions."""
from __future__ import annotations

from src.core.matcher.adapters.base import (
    CloudMatchResult,
    RecognitionEngine,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
    RecognitionQuotaExhaustedError,
)
from src.core.matcher.adapters.shazam_adapter import ShazamAdapter
from src.core.matcher.adapters.acoustid_adapter import AcoustIDAdapter
from src.core.matcher.adapters.audiotag_adapter import AudioTagAdapter

__all__ = [
    "AcoustIDAdapter",
    "AudioTagAdapter",
    "CloudMatchResult",
    "RecognitionEngine",
    "RecognitionEngineCancelledError",
    "RecognitionEngineError",
    "RecognitionQuotaExhaustedError",
    "ShazamAdapter",
]
