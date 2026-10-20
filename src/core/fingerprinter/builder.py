"""Fingerprint Builder Service compiling audio tracks into audfprint .pklz databases."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
import threading

import numpy as np

from src.core.config.constants import (
    AUDFPRINT_BATCH_TIMEOUT_SEC,
    AUDFPRINT_DEFAULT_BUCKETSIZE,
    AUDFPRINT_DEFAULT_DENSITY,
    AUDFPRINT_DEFAULT_FANOUT,
    AUDFPRINT_DEFAULT_FREQ_SD,
    AUDFPRINT_DEFAULT_HASHBITS,
    AUDFPRINT_DEFAULT_MAXTIME,
    AUDFPRINT_DEFAULT_PKS_PER_FRAME,
    AUDFPRINT_DEFAULT_SAMPLERATE,
    AUDFPRINT_DEFAULT_SHIFTS,
    AUDFPRINT_MIN_FILES_FOR_MULTIPART,
    AUDFPRINT_MIN_FILES_PER_PART,
    BUFFER_CHUNK_SIZE_BYTES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FP_WORKER_COUNT,
    DEP_KEY_FFMPEG,
    DEP_KEY_PYTHON,
    PKLZ_EXTENSION,
    PKLZ_TEMP_SUFFIX,
    UTF8_ENCODING,
)
from src.core.config.models import PklzCatalogEntry
from src.core.database.catalog_repository import CatalogRepository
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import SilentProcessRunner

logger = logging.getLogger(__name__)

_AUDFPRINT_DIR = Path(__file__).resolve().parent / "audfprint"
_INGESTING_REGEX = re.compile(r"ingesting\s+#(\d+):\s+(.+?)(?:\s+\.\.\.)?$")
_UNREADABLE_MARKER = "wavfile2peaks: Error reading "


def ensure_audfprint_importable() -> Path:
    """Ensure the vendored audfprint directory is registered on sys.path."""
    dir_str = str(_AUDFPRINT_DIR.resolve())
    if dir_str not in sys.path:
        sys.path.insert(0, dir_str)
    return _AUDFPRINT_DIR


class FingerprintBuilderError(Exception):
    """Base exception for fingerprint building failures."""


class FingerprintBuilderCancelledError(FingerprintBuilderError):
    """Raised when fingerprint building is cancelled by user request."""


@dataclass(frozen=True, slots=True)
class PklzInspectionStats:
    """Metadata and integrity stats extracted from a compiled .pklz file."""

    file_path: Path
    track_count: int
    total_hashes: int
    stored_hashes: int
    dropped_hashes: int
    dropped_percent: float
    sample_rate: int
    hashbits: int
    bucketsize: int
    maxtime: int
    file_size_bytes: int
    sha256_hash: str


@dataclass(frozen=True, slots=True)
class FingerprintBatchResult:
    """Result of building a single .pklz batch."""

    batch_index: int
    pklz_path: Path
    stats: PklzInspectionStats
    catalog_entry: PklzCatalogEntry | None
    unreadable_files: tuple[Path, ...]
    elapsed_seconds: float


def partition_into_batches(
    audio_files: Sequence[Path],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[Path]]:
    """Partition a list of audio files into deterministically ordered chunks."""
    sorted_files = sorted(audio_files)
    effective_size = max(1, batch_size)
    return [
        list(sorted_files[i: i + effective_size])
        for i in range(0, len(sorted_files), effective_size)
    ]


def compute_file_sha256(file_path: Path) -> str:
    """Compute SHA-256 checksum of a file on disk."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(BUFFER_CHUNK_SIZE_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


def resolve_unique_pklz_path(
    output_dir: Path,
    prefix_label: str,
    batch_index: int,
    overwrite: bool = False,
) -> Path:
    """Generate a destination .pklz path, avoiding collisions if overwrite=False."""
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{prefix_label}-{batch_index}{PKLZ_EXTENSION}"
    target = output_dir / base_name

    if overwrite or not target.exists():
        return target

    # Avoid clobbering existing target: append _2, _3, ...
    counter = 2
    while target.exists():
        target = output_dir / \
            f"{prefix_label}-{batch_index}_{counter}{PKLZ_EXTENSION}"
        counter += 1
    return target


class FingerprintBuilderService:
    """Service orchestrating audfprint landmark extraction and .pklz database construction."""

    def __init__(
        self,
        runner: SilentProcessRunner,
        locator: BinaryLocator,
        catalog_repo: CatalogRepository | None = None,
    ) -> None:
        self._runner = runner
        self._locator = locator
        self._catalog_repo = catalog_repo
        ensure_audfprint_importable()

    def inspect_pklz(self, pklz_path: Path, compute_sha256: bool = True) -> PklzInspectionStats:
        """Inspect and parse an existing .pklz file into a PklzInspectionStats summary."""
        ensure_audfprint_importable()
        import hash_table  # type: ignore[import-not-found]

        if not pklz_path.is_file():
            raise FingerprintBuilderError(
                f"Database file not found: {pklz_path}")

        try:
            ht = hash_table.HashTable(str(pklz_path))
        except Exception as exc:
            raise FingerprintBuilderError(
                f"Failed loading {pklz_path.name}: {exc}") from exc

        track_count = sum(1 for n in ht.names if n is not None)
        total_hashes = int(np.sum(ht.counts))
        stored_hashes = int(np.sum(np.minimum(ht.depth, ht.counts)))
        dropped_hashes = max(0, total_hashes - stored_hashes)
        dropped_pct = (100.0 * dropped_hashes /
                       max(1, total_hashes)) if total_hashes > 0 else 0.0

        sample_rate = int(ht.params.get(
            "samplerate", AUDFPRINT_DEFAULT_SAMPLERATE))
        hashbits = int(ht.hashbits)
        bucketsize = int(ht.depth)
        maxtime = int(1 << ht.maxtimebits)
        file_size = pklz_path.stat().st_size
        sha256 = compute_file_sha256(pklz_path) if compute_sha256 else ""

        return PklzInspectionStats(
            file_path=pklz_path,
            track_count=track_count,
            total_hashes=total_hashes,
            stored_hashes=stored_hashes,
            dropped_hashes=dropped_hashes,
            dropped_percent=dropped_pct,
            sample_rate=sample_rate,
            hashbits=hashbits,
            bucketsize=bucketsize,
            maxtime=maxtime,
            file_size_bytes=file_size,
            sha256_hash=sha256,
        )

    def register_in_catalog(
        self,
        stats: PklzInspectionStats,
        prefix_label: str,
        batch_index: int = 1,
    ) -> PklzCatalogEntry | None:
        """Upsert inspected .pklz metadata into SQLite catalog repository if available."""
        if self._catalog_repo is None:
            return None

        return self._catalog_repo.upsert_entry(
            file_path=stats.file_path,
            prefix_label=prefix_label,
            batch_index=batch_index,
            track_count=stats.track_count,
            total_hashes=stats.total_hashes,
            file_size_bytes=stats.file_size_bytes,
            sha256_hash=stats.sha256_hash,
        )

    def build_batch_inprocess(
        self,
        audio_files: Sequence[Path],
        output_pklz: Path,
        density: float = AUDFPRINT_DEFAULT_DENSITY,
        hashbits: int = AUDFPRINT_DEFAULT_HASHBITS,
        bucketsize: int = AUDFPRINT_DEFAULT_BUCKETSIZE,
        maxtime: int = AUDFPRINT_DEFAULT_MAXTIME,
        sample_rate: int = AUDFPRINT_DEFAULT_SAMPLERATE,
        shifts: int = AUDFPRINT_DEFAULT_SHIFTS,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> tuple[Path, list[Path]]:
        """Build a .pklz file using direct in-process audfprint API calls."""
        ensure_audfprint_importable()
        import audfprint_analyze  # type: ignore[import-not-found]
        import hash_table  # type: ignore[import-not-found]

        # Ensure ffmpeg directory is prepended to PATH for audio_read
        ffmpeg_path = self._locator.locate(DEP_KEY_FFMPEG)
        if ffmpeg_path:
            bin_dir = str(ffmpeg_path.parent)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

        analyzer = audfprint_analyze.Analyzer(density=density)
        analyzer.target_sr = sample_rate
        analyzer.shifts = shifts
        analyzer.maxpksperframe = AUDFPRINT_DEFAULT_PKS_PER_FRAME
        analyzer.maxpairsperpeak = AUDFPRINT_DEFAULT_FANOUT
        analyzer.f_sd = AUDFPRINT_DEFAULT_FREQ_SD
        analyzer.fail_on_error = False

        ht = hash_table.HashTable(
            hashbits=hashbits, depth=bucketsize, maxtime=maxtime)
        ht.params["samplerate"] = sample_rate

        unreadable: list[Path] = []
        total_files = len(audio_files)

        output_pklz.parent.mkdir(parents=True, exist_ok=True)
        tmp_pklz = output_pklz.with_name(output_pklz.name + PKLZ_TEMP_SUFFIX)
        if tmp_pklz.exists():
            tmp_pklz.unlink(missing_ok=True)

        try:
            for idx, file_path in enumerate(audio_files):
                if cancel_event is not None and cancel_event.is_set():
                    raise FingerprintBuilderCancelledError(
                        f"Fingerprinting cancelled at #{idx}")

                file_str = str(file_path.resolve())
                if progress_callback is not None:
                    progress_callback(idx + 1, total_files, file_path.name)

                try:
                    dur, num_hashes = analyzer.ingest(ht, file_str)
                    if dur <= 0.0 or num_hashes == 0:
                        unreadable.append(file_path)
                except Exception as exc:
                    logger.warning("Failed ingesting %s: %s",
                                   file_path.name, exc)
                    unreadable.append(file_path)

            if cancel_event is not None and cancel_event.is_set():
                raise FingerprintBuilderCancelledError(
                    "Fingerprinting cancelled before saving.")

            ht.save(str(tmp_pklz))
            os.replace(tmp_pklz, output_pklz)
            return output_pklz, unreadable

        finally:
            if tmp_pklz.exists():
                tmp_pklz.unlink(missing_ok=True)

    def build_batch_subprocess(
        self,
        audio_files: Sequence[Path],
        output_pklz: Path,
        work_dir: Path,
        parts: int = 1,
        density: float = AUDFPRINT_DEFAULT_DENSITY,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> tuple[Path, list[Path]]:
        """Build a .pklz file executing isolated audfprint CLI subprocess worker(s)."""
        audfprint_dir = ensure_audfprint_importable()
        script_path = audfprint_dir / "audfprint.py"
        python_path = sys.executable

        work_dir.mkdir(parents=True, exist_ok=True)
        output_pklz.parent.mkdir(parents=True, exist_ok=True)
        tmp_pklz = output_pklz.with_name(output_pklz.name + PKLZ_TEMP_SUFFIX)
        if tmp_pklz.exists():
            tmp_pklz.unlink(missing_ok=True)

        env = os.environ.copy()
        env["PYTHONPATH"] = f"{audfprint_dir}{os.pathsep}{env.get('PYTHONPATH', '')}"
        ffmpeg_path = self._locator.locate(DEP_KEY_FFMPEG)
        if ffmpeg_path:
            env["PATH"] = f"{ffmpeg_path.parent}{os.pathsep}{env.get('PATH', '')}"

        unreadable: list[Path] = []
        total_files = len(audio_files)

        if parts <= 1:
            list_file = work_dir / f"{output_pklz.stem}.txt"
            list_file.write_text("\n".join(str(f.resolve())
                                 for f in audio_files), encoding=UTF8_ENCODING)

            cmd = [
                python_path,
                "-u",
                str(script_path),
                "new",
                "-C",
                "--density",
                str(density),
                "--ncores",
                "1",
                "--dbase",
                str(tmp_pklz),
                "--list",
                str(list_file),
            ]

            def _handle_line(line: str) -> None:
                if line.startswith(_UNREADABLE_MARKER):
                    raw_path = line[len(_UNREADABLE_MARKER)
                                        :].rstrip(" skipping")
                    unreadable.append(Path(raw_path))
                m = _INGESTING_REGEX.search(line)
                if m and progress_callback is not None:
                    done_idx = int(m.group(1)) + 1
                    track_name = Path(m.group(2)).name
                    progress_callback(done_idx, total_files, track_name)

            try:
                res = self._runner.run_streaming(
                    cmd,
                    on_stdout_line=_handle_line,
                    cancel_event=cancel_event,
                    timeout=AUDFPRINT_BATCH_TIMEOUT_SEC,
                    env=env,
                    cwd=str(work_dir),
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise FingerprintBuilderCancelledError(
                        "Subprocess batch cancelled.")
                if not res.is_success or not tmp_pklz.is_file():
                    raise FingerprintBuilderError(
                        f"audfprint failed with code {res.returncode}: {res.stderr or res.stdout}"
                    )

                os.replace(tmp_pklz, output_pklz)
                return output_pklz, unreadable
            finally:
                list_file.unlink(missing_ok=True)
                if tmp_pklz.exists():
                    tmp_pklz.unlink(missing_ok=True)
        else:
            # Multi-part parallel mode with newmerge
            effective_parts = min(parts, len(audio_files))
            part_lists = [
                work_dir / f"{output_pklz.stem}.part{k + 1}.txt" for k in range(effective_parts)]
            part_dbs = [
                work_dir / f"{output_pklz.stem}.part{k + 1}.pklz" for k in range(effective_parts)]

            for k, (lst, db) in enumerate(zip(part_lists, part_dbs)):
                lst.write_text("\n".join(str(
                    f.resolve()) for f in audio_files[k::effective_parts]), encoding=UTF8_ENCODING)
                db.unlink(missing_ok=True)

            lock = threading.Lock()
            completed_tracks = [0]

            def _run_part(part_idx: int) -> bool:
                p_lst = part_lists[part_idx]
                p_db = part_dbs[part_idx]
                p_cmd = [
                    python_path,
                    "-u",
                    str(script_path),
                    "new",
                    "-C",
                    "--density",
                    str(density),
                    "--ncores",
                    "1",
                    "--dbase",
                    str(p_db),
                    "--list",
                    str(p_lst),
                ]

                def _part_line(line: str) -> None:
                    if line.startswith(_UNREADABLE_MARKER):
                        raw_p = line[len(_UNREADABLE_MARKER)
                                         :].rstrip(" skipping")
                        with lock:
                            unreadable.append(Path(raw_p))
                    m = _INGESTING_REGEX.search(line)
                    if m and progress_callback is not None:
                        with lock:
                            completed_tracks[0] += 1
                            curr = completed_tracks[0]
                        progress_callback(curr, total_files,
                                          Path(m.group(2)).name)

                p_res = self._runner.run_streaming(
                    p_cmd,
                    on_stdout_line=_part_line,
                    cancel_event=cancel_event,
                    timeout=AUDFPRINT_BATCH_TIMEOUT_SEC,
                    env=env,
                    cwd=str(work_dir),
                )
                return p_res.is_success and p_db.is_file()

            try:
                with ThreadPoolExecutor(max_workers=effective_parts) as pool:
                    futures = [pool.submit(_run_part, k)
                               for k in range(effective_parts)]
                    for fut in as_completed(futures):
                        if not fut.result():
                            raise FingerprintBuilderError(
                                "One or more audfprint part jobs failed.")

                if cancel_event is not None and cancel_event.is_set():
                    raise FingerprintBuilderCancelledError(
                        "Cancelled before merging parts.")

                # Merge part databases using newmerge
                merge_cmd = [
                    python_path,
                    "-u",
                    str(script_path),
                    "newmerge",
                    "--dbase",
                    str(tmp_pklz),
                    *(str(db) for db in part_dbs),
                ]
                merge_res = self._runner.run(
                    merge_cmd, timeout=AUDFPRINT_BATCH_TIMEOUT_SEC, env=env)
                if not merge_res.is_success or not tmp_pklz.is_file():
                    raise FingerprintBuilderError(
                        f"audfprint newmerge failed with code {merge_res.returncode}: {merge_res.stderr}"
                    )

                os.replace(tmp_pklz, output_pklz)
                return output_pklz, unreadable
            finally:
                for path in (*part_lists, *part_dbs):
                    path.unlink(missing_ok=True)
                if tmp_pklz.exists():
                    tmp_pklz.unlink(missing_ok=True)

    def build_batches(
        self,
        audio_files: Sequence[Path],
        output_dir: Path,
        archive_prefix: str,
        work_dir: Path | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        worker_count: int = DEFAULT_FP_WORKER_COUNT,
        density: float = AUDFPRINT_DEFAULT_DENSITY,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[FingerprintBatchResult]:
        """Compile a collection of audio tracks into batched .pklz databases and catalog them."""
        batches = partition_into_batches(audio_files, batch_size=batch_size)
        if not batches:
            return []

        effective_work_dir = work_dir or output_dir / ".work"
        effective_work_dir.mkdir(parents=True, exist_ok=True)

        results: list[FingerprintBatchResult] = []
        num_batches = len(batches)

        for batch_idx, batch_tracks in enumerate(batches, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise FingerprintBuilderCancelledError(
                    "Batch build process cancelled.")

            target_pklz = resolve_unique_pklz_path(
                output_dir=output_dir,
                prefix_label=archive_prefix,
                batch_index=batch_idx,
                overwrite=True,
            )

            # Determine intra-batch part parallelism
            parts = 1
            if len(batch_tracks) >= AUDFPRINT_MIN_FILES_FOR_MULTIPART and worker_count > 1:
                parts = max(1, min(worker_count, len(
                    batch_tracks) // AUDFPRINT_MIN_FILES_PER_PART))

            t0 = time.monotonic()
            if parts > 1:
                final_path, unreadable = self.build_batch_subprocess(
                    audio_files=batch_tracks,
                    output_pklz=target_pklz,
                    work_dir=effective_work_dir,
                    parts=parts,
                    density=density,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            else:
                final_path, unreadable = self.build_batch_inprocess(
                    audio_files=batch_tracks,
                    output_pklz=target_pklz,
                    density=density,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            elapsed = time.monotonic() - t0

            stats = self.inspect_pklz(final_path, compute_sha256=True)
            catalog_entry = self.register_in_catalog(
                stats, prefix_label=archive_prefix, batch_index=batch_idx)

            results.append(
                FingerprintBatchResult(
                    batch_index=batch_idx,
                    pklz_path=final_path,
                    stats=stats,
                    catalog_entry=catalog_entry,
                    unreadable_files=tuple(unreadable),
                    elapsed_seconds=elapsed,
                )
            )

        return results
