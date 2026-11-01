"""Background worker executing identification scans across local and cloud recognition engines."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import threading
from typing import Sequence
from PySide6.QtCore import QObject, Signal, Slot

from src.core.config.models import (
    MatchOptions,
    ScanCandidateRecord,
    ScanMatchRecord,
)
from src.core.matcher.adapters.base import (
    RecognitionEngineCancelledError,
    RecognitionEngineError,
)
from src.core.matcher.local_matcher import (
    LocalMatcherCancelledError,
    LocalMatcherError,
    LocalMatcherService,
)
from src.core.matcher.multi_matcher import (
    MultiEngineCandidateResult,
    MultiEngineMatcherService,
)

logger = logging.getLogger(__name__)


class MatcherWorker(QObject):
    """Thread-safe background worker orchestrating batch multi-engine identification scans."""

    progress_updated = Signal(int, int, str)  # current, total, candidate_name
    # MultiEngineCandidateResult or LocalMatchResult
    candidate_completed = Signal(object)
    match_detected = Signal(object)  # ScanMatchRecord
    scan_completed = Signal(list)  # list[MultiEngineCandidateResult]
    status_changed = Signal(str)
    log_emitted = Signal(str)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(
        self,
        matcher_service: MultiEngineMatcherService | LocalMatcherService,
        candidates: Sequence[ScanCandidateRecord],
        target_databases: Sequence[Path] = (),
        options: MatchOptions | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = matcher_service
        self._candidates = tuple(candidates)
        self._target_databases = tuple(target_databases)

        # Merge target databases if provided
        if options is not None:
            if self._target_databases and not options.target_databases:
                self._options = MatchOptions(
                    target_databases=self._target_databases,
                    stop_on_first_match=options.stop_on_first_match,
                    min_aligned_hashes=options.min_aligned_hashes,
                    early_stop_threshold=options.early_stop_threshold,
                    enabled_engines=options.enabled_engines,
                )
            else:
                self._options = options
        else:
            self._options = MatchOptions(
                target_databases=self._target_databases)

        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Signal worker to abort scan execution gracefully."""
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        """Execute identification matching on dedicated background thread."""
        total = len(self._candidates)
        engines_str = ", ".join(self._options.enabled_engines)
        self.status_changed.emit(
            f"Starting identification scan ({total} candidates)..."
        )
        self.log_emitted.emit(
            f"Beginning multi-engine scan for {total} tracks with engines: [{engines_str}]."
        )

        all_results: list[object] = []

        try:
            if isinstance(self._service, MultiEngineMatcherService):
                asyncio.run(self._run_multi_engine_scan(all_results, total))
            else:
                self._run_legacy_local_scan(all_results, total)

            self.status_changed.emit("Scan completed successfully.")
            self.scan_completed.emit(all_results)
            self.log_emitted.emit(
                f"[OK] Scan completed. Processed {total} candidates."
            )
        except (RecognitionEngineCancelledError, LocalMatcherCancelledError):
            self.status_changed.emit("Scan cancelled.")
            self.log_emitted.emit("[!] Identification scan cancelled by user.")
        except (RecognitionEngineError, LocalMatcherError, Exception) as exc:
            logger.exception("MatcherWorker error: %s", exc)
            self.error_occurred.emit(str(exc))
            self.log_emitted.emit(f"[X] Scan Error: {exc}")
        finally:
            self.finished.emit()

    async def _run_multi_engine_scan(self, all_results: list[object], total: int) -> None:
        """Execute asynchronous multi-engine identification scan."""
        for idx, candidate in enumerate(self._candidates):
            if self._cancel_event.is_set():
                raise RecognitionEngineCancelledError(
                    "Scan cancelled by user.")

            self.status_changed.emit(
                f"Scanning ({idx + 1}/{total}): {candidate.file_name}"
            )
            self.progress_updated.emit(idx, total, candidate.file_name)

            def _on_engine_finished(engine_name: str, hits: Sequence[ScanMatchRecord]) -> None:
                for m in hits:
                    self.match_detected.emit(m)
                    track_display = Path(
                        m.matched_track).name or m.matched_track
                    if m.engine == "audfprint":
                        info_text = f"{m.aligned_hashes} hashes in {m.matched_database}"
                    else:
                        conf_pct = int(round(m.confidence * 100))
                        info_text = f"{conf_pct}% confidence via {m.matched_database}"
                    self.log_emitted.emit(
                        f"[MATCH - {engine_name.upper()}] '{candidate.file_name}' -> '{track_display}' ({info_text})"
                    )

            res = await self._service.match_candidate(
                candidate=candidate,
                options=self._options,
                cancel_token=self._cancel_event,
                on_engine_finished=_on_engine_finished,
            )
            all_results.append(res)
            self.progress_updated.emit(idx + 1, total, candidate.file_name)
            self.candidate_completed.emit(res)

    def _run_legacy_local_scan(self, all_results: list[object], total: int) -> None:
        """Fallback for purely synchronous LocalMatcherService execution."""
        for idx, candidate in enumerate(self._candidates):
            if self._cancel_event.is_set():
                raise LocalMatcherCancelledError("Scan cancelled by user.")

            self.status_changed.emit(
                f"Scanning ({idx + 1}/{total}): {candidate.file_name}"
            )
            self.progress_updated.emit(idx, total, candidate.file_name)

            result = self._service.match_single_candidate(
                candidate=candidate,
                target_databases=self._target_databases,
                options=self._options,
                cancel_event=self._cancel_event,
            )
            all_results.append(result)

            self.progress_updated.emit(idx + 1, total, candidate.file_name)
            self.candidate_completed.emit(result)
            for m in result.matches:
                self.match_detected.emit(m)
                track_display = Path(m.matched_track).name or m.matched_track
                self.log_emitted.emit(
                    f"[MATCH] '{candidate.file_name}' -> '{track_display}' "
                    f"({m.aligned_hashes} hashes in {m.matched_database})"
                )


# Alias for backward compatibility
LocalMatcherWorker = MatcherWorker
