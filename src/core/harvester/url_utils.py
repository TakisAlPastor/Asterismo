"""URL normalization, source classification, and identifier utilities for Harvester."""
from __future__ import annotations

import hashlib
import posixpath
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from src.core.config.constants import (
    SUPPORTED_AUDIO_EXTENSIONS,
    TRACKING_QUERY_PARAMS,
    TRACKING_QUERY_PREFIX,
    YOUTUBE_DEFAULT_BASE_URL,
)
from src.core.config.models import HarvestSourceType

_YOUTUBE_DOMAINS: frozenset[str] = frozenset(
    {"youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"})
_SLUG_INVALID_CHARS_REGEX: re.Pattern[str] = re.compile(
    r"[^\w\s-]", re.UNICODE)
_SLUG_WHITESPACE_REGEX: re.Pattern[str] = re.compile(r"[-\s]+")


def clean_tracking_params(url: str) -> str:
    """Strip known tracking and analytics parameters from a URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    if not parts.query:
        return url

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept_pairs = [
        (k, v)
        for k, v in pairs
        if k.lower() not in TRACKING_QUERY_PARAMS and not k.lower().startswith(TRACKING_QUERY_PREFIX)
    ]

    if len(kept_pairs) == len(pairs):
        return url

    new_query = urlencode(kept_pairs)
    return urlunsplit(parts._replace(query=new_query))


def normalize_url(raw_url: str) -> str:
    """Normalize input URL for canonical deduplication and consistent crawler/yt-dlp handling.

    - Strips whitespace.
    - Expands bare '@channel' to 'https://www.youtube.com/@channel'.
    - Canonicalizes scheme to https if missing.
    - Normalizes youtu.be/<id> to https://www.youtube.com/watch?v=<id>.
    - Strips tracking parameters (utm_*, si, fbclid, etc.).
    - Collapses youtube channel '/videos' tab to bare channel page.
    """
    url = raw_url.strip()
    if not url:
        return ""

    if url.startswith("@"):
        url = f"{YOUTUBE_DEFAULT_BASE_URL}/{url}"
    elif "://" not in url:
        url = f"https://{url}"

    cleaned = clean_tracking_params(url)
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path
    query = parts.query

    # Strip default ports if present
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Normalize youtu.be shortlinks
    clean_host = netloc[4:] if netloc.startswith("www.") else netloc
    if clean_host == "youtu.be" and path.strip("/"):
        video_id = path.strip("/")
        netloc = "www.youtube.com"
        path = "/watch"
        query_pairs = parse_qsl(query)
        query = urlencode([("v", video_id)] + [(k, v)
                          for k, v in query_pairs if k != "v"])

    # Lowercase YouTube handles: /@Handle -> /@handle
    if "youtube.com" in clean_host:
        path = re.sub(r"^/@[^/?#]+", lambda m: m.group(0).lower(), path)

    # Canonicalize YouTube channels: /@handle/videos -> /@handle
    if "youtube.com" in clean_host:
        channel_videos_match = re.match(
            r"^(/(?:@[^/?#]+|channel/UC[\w-]+|c/[^/?#]+|user/[^/?#]+))/videos/?$",
            path,
        )
        if channel_videos_match:
            path = channel_videos_match.group(1)

    # Normalize empty path
    if not path:
        path = "/"

    return urlunsplit((scheme, netloc, path, query, ""))


def classify_url(url: str) -> HarvestSourceType:
    """Classify a normalized URL into its respective harvest source type."""
    normalized = normalize_url(url)
    try:
        parts = urlsplit(normalized)
    except ValueError:
        return HarvestSourceType.YTDLP_URL

    path_lower = parts.path.lower()
    ext = posixpath.splitext(path_lower)[1]

    if ext in SUPPORTED_AUDIO_EXTENSIONS or path_lower.endswith("/"):
        return HarvestSourceType.WEB_DIRECTORY

    host = parts.netloc.lower()
    host_no_www = host[4:] if host.startswith("www.") else host
    if host_no_www in _YOUTUBE_DOMAINS:
        return HarvestSourceType.YTDLP_URL

    return HarvestSourceType.YTDLP_URL


def generate_item_key(
    source_type: HarvestSourceType,
    identifier: str,
    normalized_url: str | None = None,
) -> str:
    """Generate a deterministic, clash-free item key for harvest item deduplication.

    - YTDLP_URL: 'ytdlp:<extractor>:<id>' or 'ytdlp:<id>'
    - WEB_DIRECTORY: 'web:<site>-<sha1[:10]>'
    - LOCAL_DISK: 'local:<sha1[:16]>'
    """
    if source_type == HarvestSourceType.YTDLP_URL:
        clean_id = identifier.strip().replace(" ", "_")
        return f"ytdlp:{clean_id}"

    if source_type == HarvestSourceType.WEB_DIRECTORY:
        target_url = normalized_url or identifier
        try:
            parts = urlsplit(target_url)
            host = parts.netloc.lower()
            host = host[4:] if host.startswith("www.") else host
            site = re.sub(r"[^a-z0-9.-]+", "-", host.rsplit("@", 1)
                          [-1].split(":", 1)[0]).strip("-.")[:40] or "web"
            raw_target = f"{host}{unquote(parts.path)}"
        except Exception:
            site = "web"
            raw_target = target_url

        digest = hashlib.sha1(raw_target.encode(
            "utf-8", "surrogatepass")).hexdigest()[:10]
        return f"web:{site}-{digest}"

    # LOCAL_DISK or fallback
    digest = hashlib.sha1(identifier.encode(
        "utf-8", "replace")).hexdigest()[:16]
    return f"local:{digest}"


def slugify_title(title: str, max_length: int = 64) -> str:
    """Create a safe alphanumeric filename / slug string from a title or label.

    Strips filesystem-unsafe characters, normalizes whitespace to underscores,
    and truncates to max_length.
    """
    cleaned = _SLUG_INVALID_CHARS_REGEX.sub("", title.strip()).strip()
    slug = _SLUG_WHITESPACE_REGEX.sub("_", cleaned).strip("-_")
    if not slug:
        slug = "archive"
    return slug[:max_length].rstrip("-_")
