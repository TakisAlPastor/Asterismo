"""Adapter for yt-dlp library operations with browser cookies and cooperative cancellation."""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

from src.core.config.constants import (
    ARCHIVE_ORG_HOST_SUBSTRING,
    YTDLP_BEST_AUDIO_FORMAT,
    YTDLP_FILENAME_TEMPLATE,
)
from src.core.config.models import BrowserCookieSource

logger = logging.getLogger(__name__)


class YtDlpError(Exception):
    """Base exception for yt-dlp adapter errors."""


class YtDlpCancelledError(YtDlpError):
    """Raised when an ongoing extraction or download is cancelled by user request."""


class YtDlpAdapter:
    """Encapsulates yt-dlp operations with browser cookie and cancellation support."""

    def __init__(
        self,
        cookie_source: BrowserCookieSource = BrowserCookieSource.NONE,
        impersonate_target: str = "",
        extra_opts: dict[str, Any] | None = None,
    ) -> None:
        self._cookie_source = cookie_source
        self._impersonate_target = impersonate_target.strip()
        self._extra_opts = dict(extra_opts) if extra_opts else {}

    def _build_opts(
        self,
        outtmpl: str | None = None,
        flat_playlist: bool = False,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Construct a baseline options dictionary for YoutubeDL."""
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "format": YTDLP_BEST_AUDIO_FORMAT,
            "noplaylist": True,
            "ignoreerrors": False,
        }

        if flat_playlist:
            opts["extract_flat"] = "in_playlist"
            opts["noplaylist"] = False

        if outtmpl:
            opts["outtmpl"] = outtmpl

        if progress_hook:
            opts["progress_hooks"] = [progress_hook]

        if self._cookie_source != BrowserCookieSource.NONE:
            opts["cookiesfrombrowser"] = (self._cookie_source.value,)

        if self._impersonate_target:
            try:
                target_obj = ImpersonateTarget.from_str(
                    self._impersonate_target)
                opts["impersonate"] = target_obj
            except Exception as exc:
                logger.warning(
                    "Failed to configure impersonate target '%s': %s",
                    self._impersonate_target,
                    exc,
                )

        opts.update(self._extra_opts)
        return opts

    @staticmethod
    def disambiguate_archive_org_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Disambiguate Archive.org items where multiple tracks share the parent webpage_url.

        In multi-file Archive.org uploads, each file entry receives the parent item's
        webpage_url by default. When downloading by webpage_url, yt-dlp attempts to fetch
        the whole collection repeatedly without finding direct audio codecs. Replacing
        shared webpage_url with the direct file url forces yt-dlp to download individual files.
        """
        shared_urls: dict[str, int] = {}
        for entry in entries:
            if isinstance(entry, dict) and entry.get("webpage_url"):
                webpage = str(entry["webpage_url"])
                shared_urls[webpage] = shared_urls.get(webpage, 0) + 1

        for entry in entries:
            if isinstance(entry, dict):
                webpage = str(entry.get("webpage_url") or "")
                file_url = str(entry.get("url") or "")
                if shared_urls.get(webpage, 0) > 1 and "://" in file_url:
                    entry["webpage_url"] = file_url

        return entries

    def extract_info(
        self,
        url: str,
        download: bool = False,
        process: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Extract metadata and item listings from a URL without downloading media files."""
        if cancel_event is not None and cancel_event.is_set():
            raise YtDlpCancelledError("Extraction cancelled before starting.")

        opts = self._build_opts(flat_playlist=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    url, download=download, process=process)
        except Exception as exc:
            raise YtDlpError(
                f"Failed to extract info from {url}: {exc}") from exc

        if not isinstance(info, dict):
            raise YtDlpError(
                f"Expected dict info metadata, got {type(info).__name__}")

        if cancel_event is not None and cancel_event.is_set():
            raise YtDlpCancelledError("Extraction cancelled after completion.")

        # Disambiguate multi-track items on Archive.org
        if ARCHIVE_ORG_HOST_SUBSTRING in url.lower():
            raw_entries = info.get("entries")
            if isinstance(raw_entries, list):
                self.disambiguate_archive_org_entries(raw_entries)

        return info

    def download_track(
        self,
        url: str,
        output_dir: Path,
        filename_template: str = YTDLP_FILENAME_TEMPLATE,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download a single audio track using yt-dlp into output_dir.

        Raises YtDlpCancelledError if cancel_event is set during download.
        Returns Path to the downloaded audio file.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(output_dir / filename_template)
        downloaded_filepath: list[str] = []

        def _hook(status_dict: dict[str, Any]) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise YtDlpCancelledError("Download cancelled by user.")

            status = status_dict.get("status")
            if status == "downloading" and progress_callback is not None:
                downloaded = int(status_dict.get("downloaded_bytes") or 0)
                total = int(
                    status_dict.get("total_bytes")
                    or status_dict.get("total_bytes_estimate")
                    or 0
                )
                progress_callback(downloaded, total)
            elif status == "finished":
                filename = status_dict.get("filename")
                if filename and os.path.exists(filename):
                    downloaded_filepath.append(filename)

        opts = self._build_opts(outtmpl=outtmpl, progress_hook=_hook)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except YtDlpCancelledError:
            raise
        except Exception as exc:
            raise YtDlpError(f"Failed downloading {url}: {exc}") from exc

        if downloaded_filepath and os.path.exists(downloaded_filepath[0]):
            return Path(downloaded_filepath[0])

        # If yt-dlp downloaded but filename wasn't captured in hook, search output_dir
        # for newest file matching output pattern
        candidates = list(output_dir.glob("*"))
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return candidates[0]

        raise YtDlpError(
            f"File was not found in {output_dir} after yt-dlp reported success.")
