"""Multi-engine identification orchestrator coordinating local and cloud recognition pipelines."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import logging
from pathlib import Path
import threading
import time
from typing import Callable, Sequence

from src.core.config.constants import (
    ENGINE_NAME_ACOUSTID,
    ENGINE_NAME_AUDIOTAG,
    ENGINE_NAME_AUDFPRINT,
    ENGINE_NAME_SHAZAM,
)
from src.core.config.models import (
    ConfigurationError,
    MatchOptions,
    ScanCandidateRecord,
    ScanMatchRecord,
)
from src.core.database.candidate_repository import CandidateRepository
from src.core.matcher.adapters.base import (
    RecognitionEngine,
    RecognitionEngineCancelledError,
    RecognitionEngineError,
    RecognitionQuotaExhaustedError,
)
from src.core.matcher.local_matcher import LocalMatcherService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MultiEngineCandidateResult:
    """Aggregated scan result across all executed local and cloud engines for a candidate."""

    candidate_path: Path
    candidate_id: int | None
    matches: tuple[ScanMatchRecord, ...]
    engines_queried: tuple[str, ...]
    elapsed_seconds: float
    early_stopped: bool = False

    def __post_init__(self) -> None:
        """Validate result parameters."""
        if not str(self.candidate_path).strip() or not self.candidate_path.name:
            raise ConfigurationError("candidate_path cannot be empty")
        if self.elapsed_seconds < 0:
            raise ConfigurationError(
                f"elapsed_seconds must be >= 0, got {self.elapsed_seconds}"
            )


class MultiEngineMatcherService:
    """Orchestrates identification scans across local and cloud recognition adapters in sequential cascade."""

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        local_matcher: LocalMatcherService | None = None,
        cloud_adapters: Sequence[RecognitionEngine] = (),
    ) -> None:
        self._repo = candidate_repo
        self._local_matcher = local_matcher
        self._cloud_adapters = tuple(cloud_adapters)

    @property
    def local_matcher(self) -> LocalMatcherService | None:
        """Return the configured local audfprint matcher service, if any."""
        return self._local_matcher

    @property
    def cloud_adapters(self) -> tuple[RecognitionEngine, ...]:
        """Return sequence of registered cloud recognition adapters."""
        return self._cloud_adapters

    async def match_candidate(
        self,
        candidate: ScanCandidateRecord,
        options: MatchOptions,
        cancel_token: threading.Event | None = None,
        on_engine_finished: Callable[[
            str, Sequence[ScanMatchRecord]], None] | None = None,
        on_engine_error: Callable[[str, str], None] | None = None,
    ) -> MultiEngineCandidateResult:
        """Execute sequential identification cascade for a single audio candidate."""
        if cancel_token and cancel_token.is_set():
            raise RecognitionEngineCancelledError(
                "Scan cancelled before candidate processing.")

        t0 = time.monotonic()
        all_matches: list[ScanMatchRecord] = []
        queried_engines: list[str] = []
        early_stopped = False

        # Purge existing matches for candidate before fresh scan
        if candidate.id is not None:
            self._repo.clear_matches_for_candidate(candidate.id)

        # 1. Local audfprint matching (if enabled and target databases specified)
        if (
            ENGINE_NAME_AUDFPRINT in options.enabled_engines
            and self._local_matcher is not None
            and options.target_databases
        ):
            if cancel_token and cancel_token.is_set():
                raise RecognitionEngineCancelledError(
                    "Scan cancelled before local matching.")

            queried_engines.append(ENGINE_NAME_AUDFPRINT)
            local_res = self._local_matcher.match_single_candidate(
                candidate=candidate,
                target_databases=options.target_databases,
                options=options,
                cancel_event=cancel_token,
            )

            all_matches.extend(local_res.matches)
            if on_engine_finished is not None:
                on_engine_finished(ENGINE_NAME_AUDFPRINT, local_res.matches)

            # Early stop condition for local landmark matcher
            if options.stop_on_first_match and local_res.matches:
                top_hit = max(
                    (m.aligned_hashes for m in local_res.matches), default=0)
                if top_hit >= options.early_stop_threshold:
                    early_stopped = True

        # 2. Sequential cloud engine cascade (Shazam -> AcoustID -> AudioTag)
        cloud_order = (ENGINE_NAME_SHAZAM, ENGINE_NAME_ACOUSTID,
                       ENGINE_NAME_AUDIOTAG)
        adapter_map = {
            adapter.engine_name: adapter for adapter in self._cloud_adapters}

        for engine_name in cloud_order:
            if early_stopped:
                break

            if cancel_token and cancel_token.is_set():
                raise RecognitionEngineCancelledError(
                    f"Scan cancelled before querying {engine_name}."
                )

            if engine_name not in options.enabled_engines:
                continue

            adapter = adapter_map.get(engine_name)
            if adapter is None or not adapter.is_available:
                logger.info(
                    "Engine '%s' is unavailable or not configured. Skipping.",
                    engine_name,
                )
                continue

            queried_engines.append(engine_name)
            try:
                cloud_hits = await adapter.identify(
                    candidate.file_path,
                    cancel_token=cancel_token,
                )
            except RecognitionQuotaExhaustedError as exc:
                logger.warning("Quota exhausted for %s: %s", engine_name, exc)
                cloud_hits = ()
                if on_engine_error is not None:
                    on_engine_error(engine_name, str(exc))
            except RecognitionEngineError as exc:
                logger.warning(
                    "Recognition engine %s failed for %s: %s",
                    engine_name,
                    candidate.file_name,
                    exc,
                )
                cloud_hits = ()
                if on_engine_error is not None:
                    on_engine_error(engine_name, str(exc))

            # Persist cloud match records
            engine_matches: list[ScanMatchRecord] = []
            for hit in cloud_hits:
                if candidate.id is not None:
                    saved_match = self._repo.add_match(
                        candidate_id=candidate.id,
                        matched_track=hit.matched_track,
                        matched_database=hit.matched_database,
                        engine=hit.engine,
                        aligned_hashes=hit.aligned_hashes,
                        raw_hashes=hit.raw_hashes,
                        offset_seconds=hit.offset_seconds,
                        confidence=hit.confidence,
                        details_json=hit.details_json,
                    )
                    engine_matches.append(saved_match)
                else:
                    engine_matches.append(
                        ScanMatchRecord(
                            candidate_id=1,
                            matched_track=hit.matched_track,
                            matched_database=hit.matched_database,
                            engine=hit.engine,
                            aligned_hashes=hit.aligned_hashes,
                            raw_hashes=hit.raw_hashes,
                            offset_seconds=hit.offset_seconds,
                            confidence=hit.confidence,
                            details_json=hit.details_json,
                        )
                    )

            all_matches.extend(engine_matches)
            if on_engine_finished is not None:
                on_engine_finished(engine_name, engine_matches)

            # Check early stop on confident cloud match
            if options.stop_on_first_match and engine_matches:
                if any(m.confidence >= 0.80 for m in engine_matches):
                    early_stopped = True
                    break

        if candidate.id is not None:
            self._repo.record_quick_scan(candidate.id)

        elapsed = round(time.monotonic() - t0, 3)
        return MultiEngineCandidateResult(
            candidate_path=candidate.file_path,
            candidate_id=candidate.id,
            matches=tuple(all_matches),
            engines_queried=tuple(queried_engines),
            elapsed_seconds=elapsed,
            early_stopped=early_stopped,
        )

    async def batch_match(
        self,
        candidates: Sequence[ScanCandidateRecord],
        options: MatchOptions,
        cancel_token: threading.Event | None = None,
        progress_callback: Callable[[
            int, int, ScanCandidateRecord, MultiEngineCandidateResult], None] | None = None,
    ) -> list[MultiEngineCandidateResult]:
        """Execute multi-engine identification across an entire batch of candidate tracks."""
        total = len(candidates)
        results: list[MultiEngineCandidateResult] = []

        for idx, candidate in enumerate(candidates):
            if cancel_token and cancel_token.is_set():
                raise RecognitionEngineCancelledError(
                    "Batch scan cancelled by user.")

            cand_result = await self.match_candidate(
                candidate=candidate,
                options=options,
                cancel_token=cancel_token,
            )
            results.append(cand_result)

            if progress_callback is not None:
                progress_callback(idx + 1, total, candidate, cand_result)

        return results

    async def aclose(self) -> None:
        """Close all registered cloud adapters and release network connection pools."""
        for adapter in self._cloud_adapters:
            if hasattr(adapter, "aclose"):
                try:
                    res = adapter.aclose()
                    if inspect.isawaitable(res):
                        await res
                except Exception as exc:
                    logger.warning(
                        "Error during teardown for engine adapter %s: %s",
                        getattr(adapter, "engine_name", adapter),
                        exc,
                    )
