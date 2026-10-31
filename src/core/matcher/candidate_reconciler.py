"""Candidate Reconciler discovering and synchronizing audio files into CandidateRepository."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import threading
from typing import Callable, Sequence

from src.core.config.constants import AUDIO_EXTENSIONS_SUPPORTED
from src.core.config.models import ScanCandidateRecord, ScanCandidateStatus
from src.core.database.candidate_repository import CandidateRepository
from src.core.harvester.audio_splitter import AudioSplitterService

logger = logging.getLogger(__name__)


class CandidateReconcilerError(Exception):
    """Base exception for candidate reconciliation failures."""


class CandidateReconcilerCancelledError(CandidateReconcilerError):
    """Raised when candidate reconciliation is cancelled by user request."""


@dataclass(frozen=True, slots=True)
class CandidateReconciliationResult:
    """Summary metrics of candidate audio reconciliation."""

    discovered_files: int
    new_candidates: int
    updated_candidates: int
    missing_candidates: int
    total_candidates: int
    pruned_candidates: int = 0


class CandidateReconciler:
    """Discovers audio files from input directory and synchronizes records in SQLite."""

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        splitter: AudioSplitterService | None = None,
        supported_extensions: tuple[str, ...] = AUDIO_EXTENSIONS_SUPPORTED,
    ) -> None:
        self._repo = candidate_repo
        self._splitter = splitter
        self._extensions = tuple(ext.lower() for ext in supported_extensions)

    def reconcile_directory(
        self,
        input_dir: Path,
        probe_durations: bool = True,
        prune_missing: bool = False,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> CandidateReconciliationResult:
        """Scan input_dir recursively, register candidates, and mark or prune removed files."""
        if not input_dir.is_dir():
            raise CandidateReconcilerError(
                f"Input audio directory not found: {input_dir}")

        resolved_root = input_dir.resolve()
        discovered: list[Path] = []
        for path in resolved_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in self._extensions:
                discovered.append(path.resolve())

        discovered.sort()
        total_discovered = len(discovered)

        new_count = 0
        updated_count = 0

        for idx, file_path in enumerate(discovered):
            if cancel_event is not None and cancel_event.is_set():
                raise CandidateReconcilerCancelledError(
                    "Candidate reconciliation cancelled.")

            if progress_callback is not None:
                progress_callback(idx + 1, total_discovered, file_path.name)

            try:
                rel_path = file_path.relative_to(resolved_root).as_posix()
            except ValueError:
                rel_path = file_path.name

            curr_size = file_path.stat().st_size
            existing = self._repo.get_candidate_by_path(file_path)

            duration: float | None = None
            if existing is not None:
                duration = existing.duration_seconds

            if duration is None and probe_durations and self._splitter is not None:
                try:
                    duration = self._splitter.probe_duration(file_path)
                except Exception as exc:
                    logger.debug(
                        "Failed probing duration for %s: %s", file_path.name, exc)

            if existing is None:
                self._repo.upsert_candidate(
                    file_path=file_path,
                    file_name=file_path.name,
                    relative_path=rel_path,
                    file_size_bytes=curr_size,
                    duration_seconds=duration,
                    status=ScanCandidateStatus.PENDING,
                )
                new_count += 1
            else:
                if existing.file_size_bytes != curr_size or (existing.duration_seconds is None and duration is not None):
                    self._repo.upsert_candidate(
                        file_path=file_path,
                        file_name=file_path.name,
                        relative_path=rel_path,
                        file_size_bytes=curr_size,
                        duration_seconds=duration or existing.duration_seconds,
                    )
                    updated_count += 1
                elif existing.status == ScanCandidateStatus.MISSING:
                    # Restored file
                    self._repo.update_candidate_status(
                        existing.id, ScanCandidateStatus.PENDING)
                    updated_count += 1

        # Check for candidates residing under input_dir that no longer exist on disk
        missing_ids: list[int] = []
        all_candidates = self._repo.list_candidates()
        for cand in all_candidates:
            try:
                cand.file_path.relative_to(resolved_root)
                if not cand.file_path.is_file():
                    if cand.id is not None:
                        missing_ids.append(cand.id)
            except ValueError:
                # Not under this input_dir, leave untouched
                continue

        pruned_count = 0
        missing_count = 0
        if prune_missing and missing_ids:
            pruned_count = self._repo.delete_candidates(missing_ids)
        elif missing_ids:
            unmarked_missing = [
                cid
                for cid in missing_ids
                if any(c.id == cid and c.status != ScanCandidateStatus.MISSING for c in all_candidates)
            ]
            missing_count = (
                self._repo.mark_missing(
                    unmarked_missing) if unmarked_missing else 0
            )

        total_in_db = len(self._repo.list_candidates())

        return CandidateReconciliationResult(
            discovered_files=total_discovered,
            new_candidates=new_count,
            updated_candidates=updated_count,
            missing_candidates=missing_count,
            total_candidates=total_in_db,
            pruned_candidates=pruned_count,
        )
