"""Harvester orchestration service managing the URL queue, crawling, downloading, and fingerprinting."""
from __future__ import annotations

import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
import threading

from src.core.config.constants import (
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    BACKOFF_MULTIPLIER,
    DEFAULT_BATCH_SIZE,
    MAX_QUEUE_LINKS,
    SUPPORTED_AUDIO_EXTENSIONS,
    UTF8_ENCODING,
)
from src.core.config.models import (
    BrowserCookieSource,
    HarvestItemRecord,
    HarvestItemStatus,
    HarvestSourceType,
    HarvestTask,
    HarvestTaskStatus,
)
from src.core.database.catalog_repository import CatalogRepository
from src.core.database.harvest_repository import (
    DuplicateHarvestTaskError,
    HarvestRepository,
)
from src.core.database.settings_repository import SettingsRepository
from src.core.fingerprinter.builder import (
    FingerprintBatchResult,
    FingerprintBuilderService,
)
from src.core.harvester.audio_splitter import AudioSplitterService
from src.core.harvester.url_utils import (
    classify_url,
    generate_item_key,
    normalize_url,
    slugify_title,
)
from src.core.harvester.web_crawler import WebDirectoryCrawler
from src.core.harvester.ytdlp_adapter import (
    YtDlpAdapter,
    YtDlpCancelledError,
    YtDlpError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HarvesterCallbacks:
    """Callback bundle for streaming harvester pipeline events to the UI."""

    on_log: Callable[[str], None] | None = None
    on_status: Callable[[str], None] | None = None
    on_progress: Callable[[int, int, str], None] | None = None
    on_task_updated: Callable[[int], None] | None = None
    on_backoff: Callable[[float, str], None] | None = None


@dataclass(frozen=True, slots=True)
class HarvestRunSummary:
    """Summary of a completed or stopped harvester queue run."""

    tasks_completed: int
    tasks_skipped: int
    tasks_failed: int
    pklz_files_created: tuple[Path, ...]
    was_stopped: bool


class HarvesterService:
    """Domain orchestrator executing queue harvesting and local audio .pklz compilation."""

    def __init__(
        self,
        harvest_repo: HarvestRepository,
        catalog_repo: CatalogRepository,
        settings_repo: SettingsRepository,
        splitter: AudioSplitterService,
        builder: FingerprintBuilderService,
        crawler: WebDirectoryCrawler | None = None,
        ytdlp_factory: Callable[[BrowserCookieSource,
                                 str], YtDlpAdapter] | None = None,
    ) -> None:
        self._harvest_repo = harvest_repo
        self._catalog_repo = catalog_repo
        self._settings_repo = settings_repo
        self._splitter = splitter
        self._builder = builder
        self._crawler = crawler or WebDirectoryCrawler()
        self._ytdlp_factory = ytdlp_factory or (
            lambda cookies, impersonate: YtDlpAdapter(
                cookie_source=cookies,
                impersonate_target=impersonate,
            )
        )

        self._pause_gate = threading.Event()
        self._pause_gate.set()  # set() = running, clear() = paused
        self._stop_event = threading.Event()
        self._skip_event = threading.Event()
        self._is_running = False
        self._lock = threading.Lock()
        self._backoff_lock = threading.Lock()
        self._backoff_until = 0.0

    # --------------------------------------------------------------------------
    # Lifecycle State Machine Controls
    # --------------------------------------------------------------------------

    def pause(self) -> None:
        """Pause active queue processing between items or operations."""
        self._pause_gate.clear()

    def resume(self) -> None:
        """Resume active queue processing."""
        self._pause_gate.set()

    @property
    def is_paused(self) -> bool:
        """True if the harvester is currently suspended."""
        return not self._pause_gate.is_set()

    @property
    def is_running(self) -> bool:
        """True if a queue or disk harvest job is currently active."""
        return self._is_running

    def skip_current(self) -> None:
        """Signal the currently running task to abort and advance to the next link."""
        self._skip_event.set()

    def stop(self) -> None:
        """Signal immediate cooperative termination of the entire run."""
        self._stop_event.set()
        self.resume()  # Unblock any paused worker so it can exit

    def wait_if_paused(self) -> None:
        """Block while the pause gate is cleared, unless stopped."""
        while not self._pause_gate.is_set():
            if self._stop_event.is_set():
                break
            time.sleep(0.2)

    # --------------------------------------------------------------------------
    # Queue Management CRUD
    # --------------------------------------------------------------------------

    def list_queue(self) -> list[HarvestTask]:
        """Fetch all tasks in the queue ordered chronologically."""
        return self._harvest_repo.list_tasks()

    def add_links(self, raw_inputs: Sequence[str]) -> list[HarvestTask]:
        """Normalize, deduplicate, and enqueue links into the harvest queue."""
        added_tasks: list[HarvestTask] = []
        current_queue = self._harvest_repo.list_tasks()
        remaining_capacity = max(0, MAX_QUEUE_LINKS - len(current_queue))

        tokens: list[str] = []
        for line in raw_inputs:
            for item in line.strip().split():
                clean_item = item.strip()
                if clean_item and not clean_item.startswith("#"):
                    tokens.append(clean_item)

        for raw_url in tokens[:remaining_capacity]:
            norm_url = normalize_url(raw_url)
            if not norm_url:
                continue

            source_type = classify_url(norm_url)
            # Default prefix from url (or handle)
            prefix = self._derive_initial_prefix(norm_url)

            try:
                task = self._harvest_repo.enqueue_task(
                    raw_url=raw_url,
                    normalized_url=norm_url,
                    source_type=source_type,
                    prefix_label=prefix,
                    title=prefix,
                )
                added_tasks.append(task)
            except DuplicateHarvestTaskError:
                logger.debug("Skipped duplicate URL: %s", norm_url)
                continue

        return added_tasks

    def import_links_from_file(self, file_path: Path) -> list[HarvestTask]:
        """Read URLs from a text file (one per line) and add them to the queue."""
        if not file_path.is_file():
            return []
        try:
            lines = file_path.read_text(encoding=UTF8_ENCODING).splitlines()
            return self.add_links(lines)
        except Exception as exc:
            logger.error("Failed importing links from %s: %s", file_path, exc)
            return []

    def export_links_to_file(self, file_path: Path) -> int:
        """Export all raw URLs from the harvest queue to a text file."""
        tasks = self._harvest_repo.list_tasks()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(task.raw_url for task in tasks)
        file_path.write_text(content, encoding=UTF8_ENCODING)
        return len(tasks)

    def remove_task(self, task_id: int) -> bool:
        """Remove a task from the queue."""
        return self._harvest_repo.delete_task(task_id)

    def clear_completed_tasks(self) -> int:
        """Remove all completed or skipped tasks from the queue."""
        tasks = self._harvest_repo.list_tasks()
        removed = 0
        for task in tasks:
            if task.status in (HarvestTaskStatus.COMPLETED, HarvestTaskStatus.SKIPPED):
                if self._harvest_repo.delete_task(task.id):
                    removed += 1
        return removed

    def reset_task_to_pending(self, task_id: int) -> None:
        """Reset a failed, stopped, or skipped task back to pending status."""
        self._harvest_repo.update_task_status(
            task_id, HarvestTaskStatus.PENDING)

    # --------------------------------------------------------------------------
    # Main Queue Execution Pipeline
    # --------------------------------------------------------------------------

    def run_queue(self, callbacks: HarvesterCallbacks | None = None) -> HarvestRunSummary:
        """Execute the entire pending queue through discovery, download, split, and .pklz build."""
        with self._lock:
            self._is_running = True
            self._stop_event.clear()
            self._skip_event.clear()
            self._pause_gate.set()

        cb = callbacks or HarvesterCallbacks()
        settings = self._settings_repo.load()

        tasks_to_process = [
            t
            for t in self._harvest_repo.list_tasks()
            if t.status in (
                HarvestTaskStatus.PENDING,
                HarvestTaskStatus.PAUSED,
                HarvestTaskStatus.STOPPED,
                HarvestTaskStatus.FAILED,
            )
        ]

        completed_count = 0
        skipped_count = 0
        failed_count = 0
        created_pklz_list: list[Path] = []
        backoff_delay = BACKOFF_INITIAL_SECONDS

        if cb.on_log:
            cb.on_log(
                f"Starting harvester queue run: {len(tasks_to_process)} task(s) queued.")

        try:
            for task_idx, task in enumerate(tasks_to_process, start=1):
                self.wait_if_paused()
                if self._stop_event.is_set():
                    break

                self._skip_event.clear()
                self._harvest_repo.update_task_status(
                    task.id, HarvestTaskStatus.RUNNING)
                if cb.on_task_updated:
                    cb.on_task_updated(task.id)
                if cb.on_status:
                    cb.on_status(
                        f"Processing task {task_idx}/{len(tasks_to_process)}: {task.prefix_label}")

                # Staging directory for this task's audio files
                staging_dir = settings.paths.input_dir / \
                    f".staging_task_{task.id}_{task.prefix_label}"
                staging_dir.mkdir(parents=True, exist_ok=True)

                try:
                    # 1. Discovery Phase
                    if cb.on_log:
                        cb.on_log(
                            f"[*] Discovering items for: {task.normalized_url}")
                    discovered_items, effective_prefix = self._discover_items(
                        task, settings, cb)

                    if self._skip_event.is_set():
                        self._handle_skipped_task(task, staging_dir, cb)
                        skipped_count += 1
                        continue

                    if not discovered_items:
                        if cb.on_log:
                            cb.on_log(
                                f"[!] No audio tracks found for {task.normalized_url}.")
                        self._harvest_repo.update_task_status(
                            task.id, HarvestTaskStatus.COMPLETED)
                        if cb.on_task_updated:
                            cb.on_task_updated(task.id)
                        completed_count += 1
                        shutil.rmtree(staging_dir, ignore_errors=True)
                        continue

                    # Update metadata with discovered total and refined prefix
                    self._harvest_repo.update_task_metadata(
                        task.id,
                        title=effective_prefix,
                        prefix_label=effective_prefix,
                        total_items=len(discovered_items),
                    )

                    # Deduplicate against existing completed items
                    candidate_keys = [item["item_key"]
                                      for item in discovered_items]
                    already_fingerprinted = self._harvest_repo.get_fingerprinted_item_keys(
                        candidate_keys)
                    items_to_download = [
                        item for item in discovered_items if item["item_key"] not in already_fingerprinted
                    ]

                    if cb.on_log:
                        cb.on_log(
                            f"[+] {len(discovered_items)} track(s) discovered, "
                            f"{len(already_fingerprinted)} already done, "
                            f"{len(items_to_download)} to download."
                        )

                    # 2. Download Phase with Rate-Limit Backoff
                    if items_to_download:
                        download_ok, backoff_delay = self._download_task_items(
                            task=task,
                            items=items_to_download,
                            staging_dir=staging_dir,
                            settings=settings,
                            backoff_delay=backoff_delay,
                            callbacks=cb,
                        )
                        if not download_ok:
                            if self._skip_event.is_set():
                                self._handle_skipped_task(
                                    task, staging_dir, cb)
                                skipped_count += 1
                                continue
                            if self._stop_event.is_set():
                                break

                    # Reset backoff on success
                    backoff_delay = BACKOFF_INITIAL_SECONDS

                    # 3. Audio Retention Phase (Pre-Split)
                    # Retain original whole tracks before splitting breaks them down
                    if settings.keep_downloaded_audio and staging_dir.exists():
                        self._retain_downloaded_audio(
                            staging_dir, settings.paths.input_dir / effective_prefix, cb)

                    # 4. Audio Splitting Phase
                    self.wait_if_paused()
                    if self._stop_event.is_set() or self._skip_event.is_set():
                        if self._skip_event.is_set():
                            self._handle_skipped_task(task, staging_dir, cb)
                            skipped_count += 1
                            continue
                        break

                    if cb.on_status:
                        cb.on_status(
                            "Checking audio durations and splitting long tracks...")
                    split_summary = self._splitter.process_directory(
                        folder=staging_dir,
                        recursive=False,
                        split_enabled=settings.split_long_audio,
                        threshold_seconds=settings.split_threshold_seconds,
                        segment_seconds=settings.split_segment_seconds,
                        cancel_event=self._stop_event,
                        progress_callback=lambda fname, done, tot: cb.on_progress(
                            done, tot, f"Splitting: {fname}")
                        if cb.on_progress
                        else None,
                    )

                    # 5. .pklz Database Building & Catalog Registration Phase
                    self.wait_if_paused()
                    if self._stop_event.is_set() or self._skip_event.is_set():
                        if self._skip_event.is_set():
                            self._handle_skipped_task(task, staging_dir, cb)
                            skipped_count += 1
                            continue
                        break

                    if split_summary.resulting_files:
                        if cb.on_status:
                            cb.on_status(
                                f"Compiling .pklz fingerprint database for {effective_prefix}...")
                        batch_results = self._builder.build_batches(
                            audio_files=split_summary.resulting_files,
                            output_dir=settings.paths.db_dir,
                            archive_prefix=effective_prefix,
                            batch_size=DEFAULT_BATCH_SIZE,
                            worker_count=settings.fp_worker_count,
                            cancel_event=self._stop_event,
                            progress_callback=lambda done, tot, fname: cb.on_progress(
                                done, tot, f"Landmarks: {fname}"
                            )
                            if cb.on_progress
                            else None,
                        )

                        for b_res in batch_results:
                            created_pklz_list.append(b_res.pklz_path)
                            # Mark newly fingerprinted items
                            self._harvest_repo.mark_items_fingerprinted(
                                candidate_keys,
                                pklz_filename=b_res.pklz_path.name,
                            )
                            if cb.on_log:
                                cb.on_log(
                                    f"[+] Created {b_res.pklz_path.name} "
                                    f"({b_res.stats.track_count} tracks, "
                                    f"{b_res.stats.total_hashes} hashes)."
                                )

                    # 6. Cleanup Staging Directory
                    shutil.rmtree(staging_dir, ignore_errors=True)

                    # Mark Task Completed
                    self._harvest_repo.update_task_status(
                        task.id, HarvestTaskStatus.COMPLETED)
                    self._harvest_repo.update_task_progress(
                        task.id,
                        completed_items=len(discovered_items),
                        skipped_items=0,
                    )
                    if cb.on_task_updated:
                        cb.on_task_updated(task.id)
                    completed_count += 1

                except Exception as exc:
                    logger.error("Error processing harvest task %s: %s",
                                 task.id, exc, exc_info=True)
                    if self._skip_event.is_set():
                        self._handle_skipped_task(task, staging_dir, cb)
                        skipped_count += 1
                    else:
                        self._harvest_repo.update_task_status(
                            task.id,
                            HarvestTaskStatus.FAILED,
                            error_message=str(exc)[:300],
                        )
                        if cb.on_task_updated:
                            cb.on_task_updated(task.id)
                        if cb.on_log:
                            cb.on_log(f"[X] Task {task.id} failed: {exc}")
                        failed_count += 1
                        shutil.rmtree(staging_dir, ignore_errors=True)

            was_stopped = self._stop_event.is_set()
            if was_stopped and cb.on_log:
                cb.on_log("[!] Harvester queue processing stopped by user.")
            elif cb.on_log:
                cb.on_log(
                    f"[+] Queue processing finished: {completed_count} completed, "
                    f"{skipped_count} skipped, {failed_count} failed."
                )

            return HarvestRunSummary(
                tasks_completed=completed_count,
                tasks_skipped=skipped_count,
                tasks_failed=failed_count,
                pklz_files_created=tuple(created_pklz_list),
                was_stopped=was_stopped,
            )

        finally:
            with self._lock:
                self._is_running = False

    # --------------------------------------------------------------------------
    # Standalone Local Disk Folder ("Audio on Disk" US-2.2)
    # --------------------------------------------------------------------------

    def run_local_disk_folder(
        self,
        source_folder: Path,
        prefix_label: str = "",
        split_override: bool | None = None,
        callbacks: HarvesterCallbacks | None = None,
    ) -> list[FingerprintBatchResult]:
        """Split and build .pklz databases from local audio already on disk."""
        with self._lock:
            self._is_running = True
            self._stop_event.clear()
            self._pause_gate.set()

        cb = callbacks or HarvesterCallbacks()
        settings = self._settings_repo.load()

        effective_prefix = slugify_title(
            prefix_label.strip() or source_folder.name or "local_archive")
        split_enabled = settings.split_long_audio if split_override is None else split_override

        if cb.on_log:
            cb.on_log(
                f"[*] Starting local audio build from: {source_folder} (prefix: {effective_prefix})")

        try:
            # 1. In-Place Splitting
            if cb.on_status:
                cb.on_status(
                    "Checking audio durations and splitting long files...")
            split_summary = self._splitter.process_directory(
                folder=source_folder,
                recursive=True,
                split_enabled=split_enabled,
                threshold_seconds=settings.split_threshold_seconds,
                segment_seconds=settings.split_segment_seconds,
                cancel_event=self._stop_event,
                progress_callback=lambda fname, done, tot: cb.on_progress(
                    done, tot, f"Splitting: {fname}")
                if cb.on_progress
                else None,
            )

            if not split_summary.resulting_files:
                if cb.on_log:
                    cb.on_log("[!] No valid audio files found in folder.")
                return []

            # 2. .pklz Building
            if cb.on_status:
                cb.on_status(
                    f"Compiling .pklz fingerprint database for {effective_prefix}...")

            batch_results = self._builder.build_batches(
                audio_files=split_summary.resulting_files,
                output_dir=settings.paths.db_dir,
                archive_prefix=effective_prefix,
                batch_size=DEFAULT_BATCH_SIZE,
                worker_count=settings.fp_worker_count,
                cancel_event=self._stop_event,
                progress_callback=lambda done, tot, fname: cb.on_progress(
                    done, tot, f"Landmarks: {fname}"
                )
                if cb.on_progress
                else None,
            )

            if cb.on_log:
                cb.on_log(
                    f"[+] Successfully compiled {len(batch_results)} .pklz database(s).")
            return batch_results

        finally:
            with self._lock:
                self._is_running = False

    # --------------------------------------------------------------------------
    # Discovery & Download Helper Methods
    # --------------------------------------------------------------------------

    def _discover_items(
        self,
        task: HarvestTask,
        settings: Any,
        cb: HarvesterCallbacks,
    ) -> tuple[list[dict[str, Any]], str]:
        """Extract item listings from a task URL via yt-dlp or web directory crawler."""
        items: list[dict[str, Any]] = []
        prefix = task.prefix_label

        if task.source_type == HarvestSourceType.WEB_DIRECTORY:
            # Check if direct single audio file
            ext = os.path.splitext(task.normalized_url.lower())[1]
            if ext in SUPPORTED_AUDIO_EXTENSIONS:
                key = generate_item_key(
                    HarvestSourceType.WEB_DIRECTORY, task.normalized_url)
                name = os.path.basename(task.normalized_url)
                items.append({
                    "id": key,
                    "item_key": key,
                    "title": name,
                    "url": task.normalized_url,
                    "duration": None,
                })
                self._harvest_repo.upsert_item(
                    task.id, key, name, task.normalized_url)
                return items, prefix

            # Open directory crawler
            crawl_results = self._crawler.crawl(
                start_url=task.normalized_url,
                cancel_event=self._stop_event,
                progress_callback=lambda curr_url, folders, files: cb.on_progress(
                    files, files, f"Crawling: {folders} folders"
                )
                if cb.on_progress
                else None,
            )
            for file_url, date_str in crawl_results:
                key = generate_item_key(
                    HarvestSourceType.WEB_DIRECTORY, file_url)
                title = os.path.basename(file_url)
                items.append({
                    "id": key,
                    "item_key": key,
                    "title": title,
                    "url": file_url,
                    "duration": None,
                })
                self._harvest_repo.upsert_item(task.id, key, title, file_url)
            return items, prefix

        # YtDlp Discovery
        ytdlp = self._ytdlp_factory(
            settings.cookies_browser, settings.extra_ytdlp_args)
        try:
            info = ytdlp.extract_info(
                task.normalized_url, cancel_event=self._stop_event)
        except YtDlpError as exc:
            # Automatic fallback to web crawler if extraction fails on open web directory
            logger.debug(
                "yt-dlp extract_info failed on %s, trying web crawler fallback", task.normalized_url)
            crawl_results = self._crawler.crawl(
                start_url=task.normalized_url, cancel_event=self._stop_event)
            if crawl_results:
                for file_url, date_str in crawl_results:
                    key = generate_item_key(
                        HarvestSourceType.WEB_DIRECTORY, file_url)
                    title = os.path.basename(file_url)
                    items.append({
                        "id": key,
                        "item_key": key,
                        "title": title,
                        "url": file_url,
                        "duration": None,
                    })
                    self._harvest_repo.upsert_item(
                        task.id, key, title, file_url)
                return items, prefix
            raise exc

        # Determine refined prefix
        raw_title = info.get("title") or info.get(
            "uploader") or info.get("channel") or ""
        if raw_title:
            prefix = slugify_title(raw_title)

        raw_entries = info.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else [info]

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            entry_id = str(entry.get("id") or "")
            if not entry_id:
                continue

            item_key = generate_item_key(HarvestSourceType.YTDLP_URL, entry_id)
            title = str(entry.get("title") or entry_id)
            url = str(entry.get("webpage_url") or entry.get("url")
                      or f"https://www.youtube.com/watch?v={entry_id}")
            dur = entry.get("duration")
            duration_sec = float(dur) if dur is not None else None

            items.append({
                "id": entry_id,
                "item_key": item_key,
                "title": title,
                "url": url,
                "duration": duration_sec,
            })
            self._harvest_repo.upsert_item(
                task.id, item_key, title, url, duration_seconds=duration_sec)

        return items, prefix

    def _download_task_items(
        self,
        task: HarvestTask,
        items: list[dict[str, Any]],
        staging_dir: Path,
        settings: Any,
        backoff_delay: float,
        callbacks: HarvesterCallbacks,
    ) -> tuple[bool, float]:
        """Download pending audio tracks with rate-limit detection and exponential backoff."""
        ytdlp = self._ytdlp_factory(
            settings.cookies_browser, settings.extra_ytdlp_args)
        total_items = len(items)
        max_workers = max(1, getattr(settings, "max_concurrent_downloads", 1))

        # Thread-safe tracking state
        state_lock = threading.Lock()
        completed_count = [0]
        current_backoff = [backoff_delay]
        aborted = [False]

        def _download_single_item(idx: int, item: dict[str, Any]) -> bool:
            item_title = item.get("title", f"Track {idx}")
            item_url = item.get("url", "")

            while not aborted[0] and not self._stop_event.is_set() and not self._skip_event.is_set():
                self.wait_if_paused()

                # Wait on global backoff cooldown if another worker triggered backoff
                with self._backoff_lock:
                    target_time = self._backoff_until
                now = time.monotonic()
                if now < target_time:
                    sleep_dur = target_time - now
                    t_end = time.monotonic() + sleep_dur
                    while time.monotonic() < t_end:
                        if self._stop_event.is_set() or self._skip_event.is_set():
                            return False
                        time.sleep(0.2)

                try:
                    if task.source_type == HarvestSourceType.WEB_DIRECTORY or item_url.endswith(
                        SUPPORTED_AUDIO_EXTENSIONS
                    ):
                        target_file = staging_dir / os.path.basename(item_url)
                        self._crawler.download_file(
                            url=item_url,
                            target_path=target_file,
                            cancel_event=self._stop_event,
                        )
                    else:
                        ytdlp.download_track(
                            url=item_url,
                            output_dir=staging_dir,
                            cancel_event=self._stop_event,
                        )

                    with state_lock:
                        completed_count[0] += 1
                        done = completed_count[0]
                    if callbacks.on_progress:
                        callbacks.on_progress(
                            done, total_items, f"Downloaded: {item_title}")
                    return True

                except (YtDlpCancelledError, Exception) as exc:
                    if self._stop_event.is_set() or self._skip_event.is_set():
                        return False

                    err_msg = str(exc).lower()
                    is_rate_limit = any(
                        marker in err_msg
                        for marker in ("429", "403", "too many requests", "bot", "cloudflare", "rate-limited")
                    )

                    if is_rate_limit:
                        with self._backoff_lock:
                            delay = current_backoff[0]
                            now_mono = time.monotonic()
                            # Extend backoff gate for all workers
                            self._backoff_until = max(
                                self._backoff_until, now_mono + delay)
                            current_backoff[0] = min(
                                BACKOFF_MAX_SECONDS, delay * BACKOFF_MULTIPLIER)

                        if callbacks.on_backoff:
                            callbacks.on_backoff(delay, f"Rate limited: {exc}")
                        if callbacks.on_log:
                            callbacks.on_log(
                                f"[!] Rate limit detected ({exc}). Backing off for {delay:.1f}s..."
                            )

                        # Wait out this worker's backoff window
                        t_end = time.monotonic() + delay
                        while time.monotonic() < t_end:
                            if self._stop_event.is_set() or self._skip_event.is_set():
                                return False
                            time.sleep(0.5)
                    else:
                        logger.warning(
                            "Download error on track %s: %s", item_title, exc)
                        if callbacks.on_log:
                            callbacks.on_log(
                                f"  ! Failed downloading '{item_title}': {exc}")
                        # Non-rate-limit failure: mark single track done to let pipeline proceed
                        with state_lock:
                            completed_count[0] += 1
                        return False

            return False

        if max_workers <= 1 or total_items <= 1:
            for idx, item in enumerate(items, start=1):
                self.wait_if_paused()
                if self._stop_event.is_set() or self._skip_event.is_set():
                    return False, current_backoff[0]
                _download_single_item(idx, item)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(_download_single_item, idx, item): item
                    for idx, item in enumerate(items, start=1)
                }
                for fut in as_completed(futures):
                    if self._stop_event.is_set() or self._skip_event.is_set():
                        aborted[0] = True
                        pool.shutdown(wait=False, cancel_futures=True)
                        return False, current_backoff[0]
                    try:
                        fut.result()
                    except Exception as err:
                        logger.warning(
                            "Worker thread exception during download: %s", err)

        if self._stop_event.is_set() or self._skip_event.is_set():
            return False, current_backoff[0]

        return True, current_backoff[0]

    def _handle_skipped_task(
        self,
        task: HarvestTask,
        staging_dir: Path,
        cb: HarvesterCallbacks,
    ) -> None:
        """Mark task skipped, clean up its staging files, and notify listeners."""
        shutil.rmtree(staging_dir, ignore_errors=True)
        self._harvest_repo.update_task_status(
            task.id, HarvestTaskStatus.SKIPPED)
        if cb.on_task_updated:
            cb.on_task_updated(task.id)
        if cb.on_log:
            cb.on_log(f"[*] Skipped link {task.prefix_label}.")

    @staticmethod
    def _derive_initial_prefix(url: str) -> str:
        """Derive clean, alphanumeric archive prefix from URL or channel handle."""
        from urllib.parse import urlsplit

        if "/@" in url:
            handle = url.split("/@")[1].split("/")[0].split("?")[0]
            return f"@{slugify_title(handle)}"

        parts = urlsplit(url)
        host = parts.netloc.replace("www.", "")
        path_slug = slugify_title(parts.path.strip("/").replace("/", "_"))
        combined = f"{host}_{path_slug}".strip("_")
        return slugify_title(combined, max_length=40)

    @staticmethod
    def _retain_downloaded_audio(
        staging_dir: Path,
        dest_dir: Path,
        cb: HarvesterCallbacks,
    ) -> None:
        """Preserve downloaded audio files via zero-copy hardlinks before splitting."""
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Could not create retention folder %s: %s", dest_dir, exc)
            return

        retained = 0
        for f in staging_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue

            target_file = dest_dir / f.name
            if target_file.exists() and target_file.stat().st_size == f.stat().st_size:
                continue

            try:
                # Primary attempt: OS hardlink (0 bytes extra disk space, instant on same volume)
                os.link(f, target_file)
                retained += 1
            except OSError:
                try:
                    # Fallback: cross-device/volume copy
                    shutil.copy2(f, target_file)
                    retained += 1
                except Exception as copy_err:
                    logger.warning(
                        "Failed retaining audio file %s: %s", f.name, copy_err)

        if retained > 0 and cb.on_log:
            cb.on_log(
                f"[*] Retained {retained} whole audio track(s) in: {dest_dir}")
