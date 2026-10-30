"""Local acoustic landmark identification matcher utilizing in-process audfprint hash tables."""
from __future__ import annotations

import gc
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable, Sequence

import numpy as np

from src.core.config.constants import (
    AUDFPRINT_DEFAULT_DENSITY,
    DEP_KEY_FFMPEG,
    LOCAL_MATCH_EARLY_STOP_THRESHOLD,
    LOCAL_MATCH_MIN_HASH_THRESHOLD,
)
from src.core.config.models import (
    LocalMatchResult,
    MatchOptions,
    ScanCandidateRecord,
    ScanMatchRecord,
)
from src.core.database.candidate_repository import CandidateRepository
from src.core.fingerprinter.builder import ensure_audfprint_importable
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import SilentProcessRunner

logger = logging.getLogger(__name__)


class LocalMatcherError(Exception):
    """Base exception for local identification matcher errors."""


class LocalMatcherCancelledError(LocalMatcherError):
    """Raised when identification scan is cancelled by user request."""


class LocalMatcherService:
    """Orchestrates acoustic landmark extraction, sequential .pklz search, and match persistence."""

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        locator: BinaryLocator | None = None,
        runner: SilentProcessRunner | None = None,
    ) -> None:
        self._repo = candidate_repo
        self._locator = locator or BinaryLocator()
        self._runner = runner or SilentProcessRunner()
        self._ensure_environment()

    def _ensure_environment(self) -> None:
        """Prepend ffmpeg directory to system PATH for audio_read compatibility."""
        ensure_audfprint_importable()
        ffmpeg_path = self._locator.locate(DEP_KEY_FFMPEG)
        if ffmpeg_path is not None:
            bin_dir = str(ffmpeg_path.parent)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def extract_hashes(
        self, audio_path: Path, density: float = AUDFPRINT_DEFAULT_DENSITY
    ) -> np.ndarray:
        """Extract acoustic landmark hashes for a candidate recording."""
        import audfprint_analyze  # type: ignore[import-not-found]

        resolved_path = audio_path.resolve()
        if not resolved_path.is_file():
            raise LocalMatcherError(
                f"Candidate audio file not found: {resolved_path}")

        try:
            analyzer = audfprint_analyze.Analyzer(density=density)
            hashes = analyzer.wavfile2hashes(str(resolved_path))
            if len(hashes) == 0:
                return np.empty((0, 2), dtype=np.int32)
            return np.asarray(hashes)
        except Exception as exc:
            raise LocalMatcherError(
                f"Failed extracting landmark hashes from {audio_path.name}: {exc}"
            ) from exc

    def match_hashes_against_database(
        self,
        query_hashes: np.ndarray,
        database_path: Path,
        candidate_id: int | None = None,
        min_aligned_hashes: int = LOCAL_MATCH_MIN_HASH_THRESHOLD,
        density: float = AUDFPRINT_DEFAULT_DENSITY,
    ) -> list[ScanMatchRecord]:
        """Query in-memory hashes against a single .pklz hash table and return matching hits."""
        import audfprint_analyze  # type: ignore[import-not-found]
        import audfprint_match  # type: ignore[import-not-found]
        import hash_table  # type: ignore[import-not-found]

        resolved_db = database_path.resolve()
        if not resolved_db.is_file():
            raise LocalMatcherError(
                f"Target database file not found: {resolved_db}")

        if len(query_hashes) == 0:
            return []

        ht = None
        try:
            ht = hash_table.HashTable(str(resolved_db))
            analyzer = audfprint_analyze.Analyzer(density=density)
            matcher = audfprint_match.Matcher()
            matcher.threshcount = min_aligned_hashes

            results = matcher.match_hashes(ht, query_hashes)
            if results is None or len(results) == 0:
                return []

            hop_time = analyzer.n_hop / float(analyzer.target_sr)
            matches: list[ScanMatchRecord] = []

            for row in results:
                hit_id = int(row[0])
                aligned_count = int(row[1])
                time_skew = float(row[2])
                raw_count = int(row[3])

                if aligned_count < min_aligned_hashes:
                    continue

                track_title = (
                    str(ht.names[hit_id])
                    if hit_id < len(ht.names) and ht.names[hit_id] is not None
                    else f"Track_{hit_id}"
                )
                offset_sec = round(time_skew * hop_time, 2)
                confidence = round(min(1.0, aligned_count / 30.0), 3)

                match_rec = ScanMatchRecord(
                    id=None,
                    candidate_id=candidate_id or 1,
                    engine="audfprint",
                    matched_track=track_title,
                    matched_database=resolved_db.name,
                    aligned_hashes=aligned_count,
                    raw_hashes=raw_count,
                    offset_seconds=offset_sec,
                    confidence=confidence,
                )
                matches.append(match_rec)

            return matches
        except Exception as exc:
            raise LocalMatcherError(
                f"Matching failed against {database_path.name}: {exc}"
            ) from exc
        finally:
            del ht
            gc.collect()

    def match_single_candidate(
        self,
        candidate: ScanCandidateRecord,
        target_databases: Sequence[Path],
        options: MatchOptions | None = None,
        cancel_event: threading.Event | None = None,
        on_database_checked: Callable[[
            Path, list[ScanMatchRecord]], None] | None = None,
    ) -> LocalMatchResult:
        """Execute identification matching for a single audio candidate across target databases."""
        opts = options or MatchOptions()
        t0 = time.monotonic()

        if cancel_event is not None and cancel_event.is_set():
            raise LocalMatcherCancelledError("Scan cancelled by user.")

        query_hashes = self.extract_hashes(candidate.file_path)
        all_matches: list[ScanMatchRecord] = []
        early_stopped = False
        dbs_searched = 0

        for db_path in target_databases:
            if cancel_event is not None and cancel_event.is_set():
                raise LocalMatcherCancelledError("Scan cancelled by user.")

            resolved_db = db_path.resolve()
            if not resolved_db.is_file():
                logger.warning(
                    "Target database file not found, skipping: %s", resolved_db)
                if on_database_checked is not None:
                    on_database_checked(db_path, [])
                continue

            dbs_searched += 1
            try:
                db_matches = self.match_hashes_against_database(
                    query_hashes=query_hashes,
                    database_path=resolved_db,
                    candidate_id=candidate.id,
                    min_aligned_hashes=opts.min_aligned_hashes,
                )
            except LocalMatcherError as err:
                logger.warning("Error matching against %s: %s",
                               db_path.name, err)
                db_matches = []

            if db_matches:
                for match in db_matches:
                    if candidate.id is not None:
                        saved_match = self._repo.add_match(
                            candidate_id=candidate.id,
                            matched_track=match.matched_track,
                            matched_database=match.matched_database,
                            engine=match.engine,
                            aligned_hashes=match.aligned_hashes,
                            raw_hashes=match.raw_hashes,
                            offset_seconds=match.offset_seconds,
                            confidence=match.confidence,
                            details_json=match.details_json,
                        )
                        all_matches.append(saved_match)
                    else:
                        all_matches.append(match)

            if on_database_checked is not None:
                on_database_checked(db_path, db_matches)

            # Evaluate early stopping condition
            if opts.stop_on_first_match and db_matches:
                top_hit = max(m.aligned_hashes for m in db_matches)
                if top_hit >= opts.early_stop_threshold:
                    early_stopped = True
                    break

        if candidate.id is not None:
            self._repo.record_quick_scan(candidate.id)

        elapsed = round(time.monotonic() - t0, 3)
        return LocalMatchResult(
            candidate_path=candidate.file_path,
            candidate_id=candidate.id,
            matches=tuple(all_matches),
            databases_searched=dbs_searched,
            elapsed_seconds=elapsed,
            early_stopped=early_stopped,
        )

    def batch_match(
        self,
        candidates: Sequence[ScanCandidateRecord],
        target_databases: Sequence[Path],
        options: MatchOptions | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[
            int, int, ScanCandidateRecord, LocalMatchResult], None] | None = None,
    ) -> list[LocalMatchResult]:
        """Orchestrate matching across a sequence of candidate tracks."""
        total_candidates = len(candidates)
        results: list[LocalMatchResult] = []

        for idx, candidate in enumerate(candidates):
            if cancel_event is not None and cancel_event.is_set():
                raise LocalMatcherCancelledError(
                    "Batch scan cancelled by user.")

            match_result = self.match_single_candidate(
                candidate=candidate,
                target_databases=target_databases,
                options=options,
                cancel_event=cancel_event,
            )
            results.append(match_result)

            if progress_callback is not None:
                progress_callback(idx + 1, total_candidates,
                                  candidate, match_result)

        return results
