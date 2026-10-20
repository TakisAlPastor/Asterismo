"""Audio Splitter Service using ffprobe duration probing and ffmpeg stream-copy slicing."""
from __future__ import annotations

import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
import threading

from src.core.config.constants import (
    DEFAULT_PROBE_WORKER_COUNT,
    DEFAULT_SPLIT_SEGMENT_SECONDS,
    DEFAULT_SPLIT_THRESHOLD_SECONDS,
    DEP_KEY_FFMPEG,
    DEP_KEY_FFPROBE,
    FFMPEG_SLICE_TIMEOUT_SEC,
    FFPROBE_TIMEOUT_SEC,
    PART_FILE_SUFFIX,
    SPLIT_MUXER_MAP,
    SPLIT_TOLERANCE_SECONDS,
    SUPPORTED_AUDIO_EXTENSIONS,
)
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import SilentProcessRunner

logger = logging.getLogger(__name__)


class AudioSplitterError(Exception):
    """Base exception for audio splitting failures."""


class AudioSplitterCancelledError(AudioSplitterError):
    """Raised when an audio splitting operation is cancelled by the caller."""


@dataclass(frozen=True, slots=True)
class AudioSplitResult:
    """Result of splitting a single audio file."""

    original_path: Path
    was_split: bool
    duration_seconds: float | None
    piece_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class AudioSplitDirectorySummary:
    """Summary of processing an entire directory of audio files."""

    total_audio_files: int
    files_split: int
    pieces_created: int
    unreadable_files: int
    resulting_files: tuple[Path, ...]


def compute_split_segments(
    duration: float,
    threshold_seconds: int = DEFAULT_SPLIT_THRESHOLD_SECONDS,
    segment_seconds: int = DEFAULT_SPLIT_SEGMENT_SECONDS,
) -> list[tuple[float, float]]:
    """Compute (start, end) time ranges for splitting audio with a tail-merge floor.

    Files shorter than or equal to threshold_seconds are left intact ([]).
    Files longer than threshold_seconds are partitioned into segment_seconds slices.
    If the final remainder slice would be shorter than segment_seconds, it is merged
    into the preceding segment, ensuring every resulting slice satisfies
    (end - start) >= segment_seconds - SPLIT_TOLERANCE_SECONDS.
    """
    if duration <= threshold_seconds:
        return []

    count = int(math.ceil(duration / float(segment_seconds)))
    if count >= 2:
        tail = duration - (count - 1) * segment_seconds
        if 0 < tail < segment_seconds:
            count -= 1

    if count < 2:
        return []

    segments: list[tuple[float, float]] = []
    for i in range(count):
        start = float(i * segment_seconds)
        end = duration if i == count - \
            1 else min(float((i + 1) * segment_seconds), duration)
        if end > start:
            segments.append((start, end))

    # Validate that every segment meets the minimum chunk floor
    for start, end in segments:
        chunk_len = end - start
        if chunk_len < (segment_seconds - SPLIT_TOLERANCE_SECONDS):
            raise AudioSplitterError(
                f"Generated short segment {chunk_len:.2f}s from {duration:.2f}s audio"
            )

    return segments


def format_split_piece_name(stem: str, start: float, end: float, suffix: str) -> str:
    """Format self-describing filename for split piece: <stem>_split_<MMM>m[SSs]-<MMM>m[SSs]<ext>."""

    def _format_time_label(seconds: float) -> str:
        total_sec = max(0, int(round(seconds)))
        minutes, sec = divmod(total_sec, 60)
        if sec == 0:
            return f"{minutes:03d}m"
        return f"{minutes:03d}m{sec:02d}s"

    start_lbl = _format_time_label(start)
    end_lbl = _format_time_label(end)
    return f"{stem}_split_{start_lbl}-{end_lbl}{suffix}"


class AudioSplitterService:
    """Service for probing audio duration and stream-copy slicing long audio files."""

    def __init__(self, runner: SilentProcessRunner, locator: BinaryLocator) -> None:
        self._runner = runner
        self._locator = locator

    def probe_duration(self, file_path: Path) -> float | None:
        """Probe audio duration in seconds using ffprobe. Returns None if unreadable."""
        ffprobe_path = self._locator.locate(DEP_KEY_FFPROBE)
        if ffprobe_path is None:
            raise AudioSplitterError(
                "ffprobe binary not found on local bin or system PATH.")

        cmd = [
            str(ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        try:
            result = self._runner.run(cmd, timeout=FFPROBE_TIMEOUT_SEC)
            if not result.is_success:
                return None
            return float(result.stdout.strip())
        except (ValueError, Exception) as exc:
            logger.debug("Failed probing duration for %s: %s", file_path, exc)
            return None

    def probe_durations_parallel(
        self,
        files: Sequence[Path],
        worker_count: int = DEFAULT_PROBE_WORKER_COUNT,
        cancel_event: threading.Event | None = None,
    ) -> dict[Path, float | None]:
        """Probe audio durations for multiple files in parallel using ffprobe."""
        if not files:
            return {}

        effective_workers = max(1, min(len(files), worker_count))
        results: dict[Path, float | None] = {}

        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            future_to_file = {
                pool.submit(self.probe_duration, f): f for f in files
            }
            for future in as_completed(future_to_file):
                if cancel_event is not None and cancel_event.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise AudioSplitterCancelledError(
                        "Parallel duration probing cancelled.")
                file_path = future_to_file[future]
                try:
                    results[file_path] = future.result()
                except Exception as exc:
                    logger.debug(
                        "Parallel probe error for %s: %s", file_path, exc)
                    results[file_path] = None

        return results

    def slice_segment(self, src: Path, start: float, end: float, dst: Path) -> bool:
        """Slice [start, end) segment of src into dst using stream-copy (-c copy).

        Writes to a temporary .part file and atomically renames upon clean exit.
        Returns True on success, False on failure.
        """
        length = end - start
        if length <= 0:
            return False

        ffmpeg_path = self._locator.locate(DEP_KEY_FFMPEG)
        if ffmpeg_path is None:
            raise AudioSplitterError(
                "ffmpeg binary not found on local bin or system PATH.")

        dst.parent.mkdir(parents=True, exist_ok=True)
        part_path = dst.with_name(dst.name + PART_FILE_SUFFIX)

        suffix_lower = dst.suffix.lower()
        muxer_format = SPLIT_MUXER_MAP.get(
            suffix_lower, suffix_lower.lstrip("."))

        cmd = [
            str(ffmpeg_path),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.6f}",
            "-i",
            str(src),
            "-t",
            f"{length:.6f}",
            "-vn",
            "-c",
            "copy",
            "-f",
            muxer_format,
            str(part_path),
        ]

        try:
            result = self._runner.run(cmd, timeout=FFMPEG_SLICE_TIMEOUT_SEC)
            success = result.is_success and part_path.is_file() and part_path.stat().st_size > 0
            if not success:
                if part_path.exists():
                    part_path.unlink()
                return False

            os.replace(part_path, dst)
            return True
        except Exception as exc:
            logger.warning("Error slicing %s: %s", src, exc)
            if part_path.exists():
                part_path.unlink()
            return False

    def split_file_in_place(
        self,
        file_path: Path,
        threshold_seconds: int = DEFAULT_SPLIT_THRESHOLD_SECONDS,
        segment_seconds: int = DEFAULT_SPLIT_SEGMENT_SECONDS,
        duration: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AudioSplitResult:
        """Split a single audio file in place if duration > threshold_seconds.

        If cancelled or any segment fails, deletes written pieces and preserves
        the original file. If all segments succeed, deletes the original file.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise AudioSplitterCancelledError(
                f"Cancelled before splitting {file_path.name}")

        if duration is None:
            duration = self.probe_duration(file_path)

        if duration is None:
            return AudioSplitResult(
                original_path=file_path,
                was_split=False,
                duration_seconds=None,
                piece_paths=(file_path,),
            )

        segments = compute_split_segments(
            duration=duration,
            threshold_seconds=threshold_seconds,
            segment_seconds=segment_seconds,
        )

        if not segments:
            return AudioSplitResult(
                original_path=file_path,
                was_split=False,
                duration_seconds=duration,
                piece_paths=(file_path,),
            )

        written_pieces: list[Path] = []
        stem = file_path.stem
        suffix = file_path.suffix
        parent_dir = file_path.parent

        for start, end in segments:
            if cancel_event is not None and cancel_event.is_set():
                for piece in written_pieces:
                    piece.unlink(missing_ok=True)
                raise AudioSplitterCancelledError(
                    f"Cancelled while splitting {file_path.name}")

            out_piece_name = format_split_piece_name(stem, start, end, suffix)
            out_piece_path = parent_dir / out_piece_name

            if out_piece_path.exists() and out_piece_path != file_path:
                out_piece_path.unlink(missing_ok=True)

            slice_ok = self.slice_segment(
                file_path, start, end, out_piece_path)
            if slice_ok:
                written_pieces.append(out_piece_path)
            else:
                # Rollback on failure: keep original file intact
                for piece in written_pieces:
                    piece.unlink(missing_ok=True)
                return AudioSplitResult(
                    original_path=file_path,
                    was_split=False,
                    duration_seconds=duration,
                    piece_paths=(file_path,),
                )

        if len(written_pieces) == len(segments):
            try:
                file_path.unlink()
            except OSError as exc:
                logger.warning(
                    "Could not delete original file after split %s: %s", file_path, exc)
            return AudioSplitResult(
                original_path=file_path,
                was_split=True,
                duration_seconds=duration,
                piece_paths=tuple(written_pieces),
            )

        # Partial failure safety fallback
        for piece in written_pieces:
            piece.unlink(missing_ok=True)
        return AudioSplitResult(
            original_path=file_path,
            was_split=False,
            duration_seconds=duration,
            piece_paths=(file_path,),
        )

    def process_directory(
        self,
        folder: Path,
        recursive: bool = False,
        split_enabled: bool = True,
        threshold_seconds: int = DEFAULT_SPLIT_THRESHOLD_SECONDS,
        segment_seconds: int = DEFAULT_SPLIT_SEGMENT_SECONDS,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
        worker_count: int = DEFAULT_PROBE_WORKER_COUNT,
    ) -> AudioSplitDirectorySummary:
        """Scan folder for audio files and split long tracks if split_enabled=True.

        Probes audio durations concurrently with worker_count threads before slicing.
        Returns AudioSplitDirectorySummary containing all resulting file paths.
        """
        if not folder.is_dir():
            return AudioSplitDirectorySummary(0, 0, 0, 0, ())

        if recursive:
            discovered = [
                p for p in folder.rglob("*")
                if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            ]
        else:
            discovered = [
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            ]

        audio_files = sorted(discovered)
        total_audio = len(audio_files)

        if not split_enabled or total_audio == 0:
            return AudioSplitDirectorySummary(
                total_audio_files=total_audio,
                files_split=0,
                pieces_created=0,
                unreadable_files=0,
                resulting_files=tuple(audio_files),
            )

        resulting: list[Path] = []
        files_split_count = 0
        pieces_created_count = 0
        unreadable_count = 0

        durations = self.probe_durations_parallel(
            files=audio_files,
            worker_count=worker_count,
            cancel_event=cancel_event,
        )

        for idx, file_path in enumerate(audio_files):
            if cancel_event is not None and cancel_event.is_set():
                raise AudioSplitterCancelledError(
                    "Directory audio split processing cancelled.")

            if progress_callback is not None:
                progress_callback(file_path.name, idx + 1, total_audio)

            cached_duration = durations.get(file_path)

            res = self.split_file_in_place(
                file_path=file_path,
                threshold_seconds=threshold_seconds,
                segment_seconds=segment_seconds,
                duration=cached_duration,
                cancel_event=cancel_event,
            )

            if res.duration_seconds is None:
                unreadable_count += 1

            if res.was_split:
                files_split_count += 1
                pieces_created_count += len(res.piece_paths)
                resulting.extend(res.piece_paths)
            else:
                resulting.append(file_path)

        return AudioSplitDirectorySummary(
            total_audio_files=total_audio,
            files_split=files_split_count,
            pieces_created=pieces_created_count,
            unreadable_files=unreadable_count,
            resulting_files=tuple(resulting),
        )
