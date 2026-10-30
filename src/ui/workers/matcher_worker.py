"""Background worker executing LocalMatcherService identification scans."""
from __future__ import annotations

import logging
from pathlib import Path
import threading
from typing import Sequence
from PySide6.QtCore import QObject, Signal, Slot

from src.core.config.models import (
    LocalMatchResult,
    MatchOptions,
    ScanCandidateRecord,
    ScanMatchRecord,
)
from src.core.matcher.local_matcher import (
    LocalMatcherCancelledError,
    LocalMatcherError,
    LocalMatcherService,
)

logger = logging.getLogger(__name__)


class LocalMatcherWorker(QObject):
    """Thread-safe background worker orchestrating batch acoustic landmark matching."""

    progress_updated = Signal(int, int, str)  # current, total, candidate_name
    candidate_completed = Signal(object)  # LocalMatchResult
    match_detected = Signal(object)  # ScanMatchRecord
    scan_completed = Signal(list)  # list[LocalMatchResult]
    status_changed = Signal(str)
    log_emitted = Signal(str)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(
        self,
        matcher_service: LocalMatcherService,
        candidates: Sequence[ScanCandidateRecord],
        target_databases: Sequence[Path],
        options: MatchOptions | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = matcher_service
        self._candidates = tuple(candidates)
        self._target_databases = tuple(target_databases)
        self._options = options or MatchOptions()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Signal worker to abort scan execution gracefully."""
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        """Execute identification matching on dedicated background thread."""
        total = len(self._candidates)
        self.status_changed.emit(
            f"Starting identification scan ({total} candidates)...")
        self.log_emitted.emit(
            f"Beginning local scan for {total} tracks against {len(self._target_databases)} databases."
        )

        all_results: list[LocalMatchResult] = []

        try:
            for idx, candidate in enumerate(self._candidates):
                if self._cancel_event.is_set():
                    raise LocalMatcherCancelledError("Scan cancelled by user.")

                self.status_changed.emit(
                    f"Scanning ({idx + 1}/{total}): {candidate.file_name}")
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
                    self.log_emitted.emit(
                        f"[MATCH] '{candidate.file_name}' -> '{m.matched_track}' "
                        f"({m.aligned_hashes} hashes in {m.matched_database})"
                    )

            self.status_changed.emit("Scan completed successfully.")
            self.scan_completed.emit(all_results)
            self.log_emitted.emit(
                f"[OK] Scan completed. Processed {total} candidates.")
        except LocalMatcherCancelledError:
            self.status_changed.emit("Scan cancelled.")
            self.log_emitted.emit("[!] Identification scan cancelled by user.")
        except (LocalMatcherError, Exception) as exc:
            logger.exception("LocalMatcherWorker error: %s", exc)
            self.error_occurred.emit(str(exc))
            self.log_emitted.emit(f"[X] Scan Error: {exc}")
        finally:
            self.finished.emit()
