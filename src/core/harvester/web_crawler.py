"""Open web directory crawler and direct HTTP file downloader."""
from __future__ import annotations

import datetime
import hashlib
import html.parser
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
import threading

import httpx

from src.core.config.constants import (
    BUFFER_CHUNK_SIZE_BYTES,
    DEFAULT_HTTP_TIMEOUT_SEC,
    DIRECTORY_INDEX_SIGNATURES,
    IIS_PARENT_DIR_TEXT,
    PART_FILE_SUFFIX,
    SUPPORTED_AUDIO_EXTENSIONS,
    UTF8_ENCODING,
    WEB_CRAWLER_MAX_DEPTH,
    WEB_CRAWLER_MAX_FOLDERS,
    WEB_CRAWLER_MAX_PAGE_BYTES,
)

_ISO_DATE_REGEX: re.Pattern[str] = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DMY_DATE_REGEX: re.Pattern[str] = re.compile(
    r"(?<!\d)(\d{1,2})-([A-Za-z]{3})-(\d{4})(?!\d)")
_MONTH_NAME_MAP: dict[str, int] = {
    month: index
    for index, month in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_SAFE_URL_CHARS: str = "/%:@!$&'()*+,;=~"


class WebCrawlerError(Exception):
    """Raised when web crawling or direct file downloading fails."""


class WebCrawlerCancelledError(WebCrawlerError):
    """Raised when an ongoing crawl or download is cancelled by the caller."""


@dataclass(frozen=True)
class WebCrawlerListingResult:
    """Parsed items from an open web directory HTML page."""

    files: tuple[tuple[str, str | None], ...]  # (url, optional YYYYMMDD date)
    folders: tuple[str, ...]
    signature: str


def sanitize_web_url(url: str) -> str | None:
    """Ensure URL is valid HTTP/HTTPS, percent-encoded where necessary, without fragments."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    scheme_lower = parts.scheme.lower()
    if scheme_lower not in ("http", "https") or not parts.netloc:
        return None

    path = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", parts.path or "/")
    encoded_path = quote(path, safe=_SAFE_URL_CHARS)
    return urlunsplit((scheme_lower, parts.netloc, encoded_path, parts.query, ""))


def ensure_folder_url(url: str) -> str:
    """Ensure a directory listing URL ends with a trailing slash."""
    parts = urlsplit(url)
    if parts.path.endswith("/"):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path + "/", parts.query, ""))


def parse_listing_date(text: str) -> str | None:
    """Extract a YYYYMMDD date from Apache/nginx/IIS table row text."""
    iso_match = _ISO_DATE_REGEX.search(text)
    if iso_match:
        year, month, day = int(iso_match.group(1)), int(
            iso_match.group(2)), int(iso_match.group(3))
    else:
        dmy_match = _DMY_DATE_REGEX.search(text)
        if not dmy_match:
            return None
        month_name = dmy_match.group(2).lower()
        if month_name not in _MONTH_NAME_MAP:
            return None
        year = int(dmy_match.group(3))
        month = _MONTH_NAME_MAP[month_name]
        day = int(dmy_match.group(1))

    try:
        return datetime.date(year, month, day).strftime("%Y%m%d")
    except ValueError:
        return None


class _DirectoryAnchorParser(html.parser.HTMLParser):
    """HTML parser extracting links and contextual date text from directory indexes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str = ""
        self.heading: str = ""
        # href, link text, text after link, text before link
        self.links: list[list[str]] = []
        self._current_tag: str = ""
        self._captured_tags: set[str] = set()
        self._in_anchor: bool = False
        self._after_anchor: bool = False
        self._before_buffer: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("title", "h1") and tag not in self._captured_tags:
            self._current_tag = tag
        elif tag == "a":
            href = dict(attrs).get("href")
            self._after_anchor = False
            if href:
                self.links.append([href, "", "", self._before_buffer])
                self._in_anchor = True
            self._before_buffer = ""
        elif tag == "tr":
            self._after_anchor = False

    def handle_endtag(self, tag: str) -> None:
        if tag == self._current_tag:
            self._captured_tags.add(tag)
            self._current_tag = ""
        elif tag == "a" and self._in_anchor:
            self._in_anchor = False
            self._after_anchor = True
        elif tag == "tr":
            self._after_anchor = False

    def handle_data(self, data: str) -> None:
        if self._current_tag == "title":
            self.title += data
        elif self._current_tag == "h1":
            self.heading += data

        if self._in_anchor and self.links:
            self.links[-1][1] += data
            return
        if self._after_anchor and self.links:
            self.links[-1][2] += data
        self._before_buffer += data


class WebDirectoryCrawler:
    """Crawler for open web directories (Apache, nginx, IIS) and direct audio streaming."""

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._external_client = http_client

    def _get_client(self) -> httpx.Client:
        if self._external_client is not None:
            return self._external_client
        return httpx.Client(
            timeout=DEFAULT_HTTP_TIMEOUT_SEC,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Asterismo/0.1.0"},
        )

    def parse_directory_page(self, html_text: str, base_url: str) -> WebCrawlerListingResult | None:
        """Parse HTML directory page text and return contained files and subfolders."""
        parser = _DirectoryAnchorParser()
        try:
            parser.feed(html_text)
            parser.close()
        except Exception:
            return None

        is_iis = any(link[1].strip().lower() ==
                     IIS_PARENT_DIR_TEXT for link in parser.links)
        title_lower = parser.title.lower().strip()
        heading_lower = parser.heading.lower().strip()
        is_directory_index = (
            any(title_lower.startswith(sig)
                for sig in DIRECTORY_INDEX_SIGNATURES)
            or any(heading_lower.startswith(sig) for sig in DIRECTORY_INDEX_SIGNATURES)
            or is_iis
        )
        if not is_directory_index:
            return None

        clean_base = sanitize_web_url(base_url) or base_url
        here = urlsplit(ensure_folder_url(clean_base))

        files: list[tuple[str, str | None]] = []
        folders: list[str] = []
        seen_links: set[str] = set()

        for href, _text, after, before in parser.links:
            href_clean = href.strip()
            if not href_clean or href_clean.startswith(("?", "#")):
                continue

            try:
                candidate_url = sanitize_web_url(
                    urljoin(urlunsplit(here), href_clean))
            except ValueError:
                continue

            if candidate_url is None or candidate_url in seen_links:
                continue

            parts = urlsplit(candidate_url)
            # Enforce same-host and subpath containment
            if (
                parts.query
                or parts.netloc.lower() != here.netloc.lower()
                or not parts.path.startswith(here.path)
                or parts.path == here.path
                or any(seg in (".", "..") for seg in unquote(parts.path).split("/"))
            ):
                continue

            seen_links.add(candidate_url)
            if parts.path.endswith("/"):
                folders.append(candidate_url)
            else:
                ext = posixpath.splitext(unquote(parts.path))[1].lower()
                if ext in SUPPORTED_AUDIO_EXTENSIONS:
                    date_context = before if is_iis else after
                    files.append(
                        (candidate_url, parse_listing_date(date_context)))

        signature_data = (
            [(posixpath.basename(urlsplit(f).path), d) for f, d in files],
            [posixpath.basename(urlsplit(f).path.rstrip("/"))
             for f in folders],
        )
        signature = hashlib.sha1(repr(signature_data).encode(
            UTF8_ENCODING, "surrogatepass")).hexdigest()

        return WebCrawlerListingResult(
            files=tuple(files),
            folders=tuple(folders),
            signature=signature,
        )

    def crawl(
        self,
        start_url: str,
        max_depth: int = WEB_CRAWLER_MAX_DEPTH,
        max_folders: int = WEB_CRAWLER_MAX_FOLDERS,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> list[tuple[str, str | None]]:
        """Recursively crawl an open directory structure to discover all audio tracks.

        Returns list of (file_url, optional_date_str) tuples.
        """
        client = self._get_client()
        should_close_client = self._external_client is None

        queue: list[tuple[str, int]] = [(ensure_folder_url(start_url), 0)]
        discovered_files: list[tuple[str, str | None]] = []
        visited_urls: set[str] = set()
        seen_signatures: set[str] = set()
        folders_processed = 0

        try:
            while queue:
                if cancel_event is not None and cancel_event.is_set():
                    raise WebCrawlerCancelledError("Crawl cancelled by user.")

                if folders_processed >= max_folders:
                    break

                current_url, current_depth = queue.pop(0)
                if current_url in visited_urls or current_depth > max_depth:
                    continue

                visited_urls.add(current_url)
                folders_processed += 1

                if progress_callback is not None:
                    progress_callback(
                        current_url, folders_processed, len(discovered_files))

                try:
                    response = client.get(current_url)
                    response.raise_for_status()
                    html_content = response.text[:WEB_CRAWLER_MAX_PAGE_BYTES]
                except Exception as exc:
                    # Non-fatal error for individual directories; continue crawl
                    continue

                listing = self.parse_directory_page(
                    html_content, str(response.url))
                if listing is None:
                    continue

                if listing.signature in seen_signatures:
                    # Loop detected (symlink or duplicate listing structure)
                    continue
                seen_signatures.add(listing.signature)

                discovered_files.extend(listing.files)

                if current_depth + 1 <= max_depth:
                    for subfolder in listing.folders:
                        if subfolder not in visited_urls:
                            queue.append((subfolder, current_depth + 1))

        finally:
            if should_close_client:
                client.close()

        return discovered_files

    def download_file(
        self,
        url: str,
        target_path: Path,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Stream download a direct web file to target_path with cancel_event support."""
        client = self._get_client()
        should_close_client = self._external_client is None

        target_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = target_path.with_name(target_path.name + PART_FILE_SUFFIX)

        try:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                total_bytes = int(response.headers.get("content-length", 0))
                downloaded_bytes = 0

                with open(part_path, "wb") as file_out:
                    for chunk in response.iter_bytes(chunk_size=BUFFER_CHUNK_SIZE_BYTES):
                        if cancel_event is not None and cancel_event.is_set():
                            raise WebCrawlerCancelledError(
                                "Download cancelled by user.")
                        file_out.write(chunk)
                        downloaded_bytes += len(chunk)
                        if progress_callback is not None:
                            progress_callback(downloaded_bytes, total_bytes)

            if part_path.exists():
                part_path.replace(target_path)
            return target_path

        except Exception:
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            if should_close_client:
                client.close()
