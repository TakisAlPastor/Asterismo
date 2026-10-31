"""Cloud recognition engine adapters and protocol definitions."""
from __future__ import annotations

from src.core.matcher.adapters.base import (
    CloudMatchResult,
    RecognitionEngine,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
    RecognitionQuotaExhaustedError,
)

__all__ = [
    "CloudMatchResult",
    "RecognitionEngine",
    "RecognitionEngineCancelledError",
    "RecognitionEngineError",
    "RecognitionQuotaExhaustedError",
]
