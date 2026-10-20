"""Unit tests for URL normalization, web directory crawler, and yt-dlp adapter."""
from __future__ import annotations

import io
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.core.config.constants import (
    YTDLP_BEST_AUDIO_FORMAT,
    YTDLP_FILENAME_TEMPLATE,
)
from src.core.config.models import BrowserCookieSource, HarvestSourceType
from src.core.harvester.url_utils import (
    classify_url,
    clean_tracking_params,
    generate_item_key,
    normalize_url,
    slugify_title,
)
from src.core.harvester.web_crawler import (
    WebCrawlerCancelledError,
    WebCrawlerError,
    WebDirectoryCrawler,
    ensure_folder_url,
    parse_listing_date,
    sanitize_web_url,
)
from src.core.harvester.ytdlp_adapter import (
    YtDlpAdapter,
    YtDlpCancelledError,
    YtDlpError,
)


# ==============================================================================
# URL Utils Tests
# ==============================================================================


def test_clean_tracking_params() -> None:
    url_with_tracking = (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=twitter&utm_medium=social&si=12345&feature=share"
    )
    cleaned = clean_tracking_params(url_with_tracking)
    assert cleaned == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    url_without_tracking = "https://archive.org/details/test_item?key=value"
    assert clean_tracking_params(url_without_tracking) == url_without_tracking


def test_normalize_url_bare_handle() -> None:
    assert normalize_url(
        "@LostMediaChannel") == "https://www.youtube.com/@lostmediachannel"
    assert normalize_url(
        "https://www.youtube.com/@ArtistName/videos") == "https://www.youtube.com/@artistname"


def test_normalize_url_shortlink_and_case() -> None:
    short_url = "https://youtu.be/dQw4w9WgXcQ?si=abcdef"
    normalized = normalize_url(short_url)
    assert normalized == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    uppercase_url = "HTTPS://WWW.YOUTUBE.COM/watch?v=ABCDEF12345"
    assert normalize_url(
        uppercase_url) == "https://www.youtube.com/watch?v=ABCDEF12345"


def test_classify_url() -> None:
    assert classify_url(
        "https://example.com/audio/track01.mp3") == HarvestSourceType.WEB_DIRECTORY
    assert classify_url(
        "https://example.com/audio/song.FLAC") == HarvestSourceType.WEB_DIRECTORY
    assert classify_url(
        "https://example.com/music/album/") == HarvestSourceType.WEB_DIRECTORY
    assert classify_url(
        "https://www.youtube.com/@Channel") == HarvestSourceType.YTDLP_URL
    assert classify_url(
        "https://archive.org/details/somerecording") == HarvestSourceType.YTDLP_URL


def test_generate_item_key() -> None:
    key_ytdlp = generate_item_key(
        HarvestSourceType.YTDLP_URL, "youtube:dQw4w9WgXcQ")
    assert key_ytdlp == "ytdlp:youtube:dQw4w9WgXcQ"

    key_web = generate_item_key(
        HarvestSourceType.WEB_DIRECTORY,
        "track01.mp3",
        "https://example.com/music/track01.mp3",
    )
    assert key_web.startswith("web:example.com-")
    assert len(key_web) == len("web:example.com-") + 10

    key_local = generate_item_key(
        HarvestSourceType.LOCAL_DISK, r"C:\Music\Track01.wav")
    assert key_local.startswith("local:")
    assert len(key_local) == 6 + 16


def test_slugify_title() -> None:
    raw_title = "Lost & Found: 1990s Mystery Cassette [Remastered]???"
    slug = slugify_title(raw_title)
    assert slug == "Lost_Found_1990s_Mystery_Cassette_Remastered"
    assert ":" not in slug and "?" not in slug and "[" not in slug

    # Test empty fallback
    assert slugify_title("???!!!") == "archive"


# ==============================================================================
# Web Directory Crawler Tests
# ==============================================================================


_APACHE_HTML = """
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html>
 <head>
  <title>Index of /media/music</title>
 </head>
 <body>
<h1>Index of /media/music</h1>
<table>
  <tr><th><a href="?C=N;O=D">Name</a></th><th><a href="?C=M;O=A">Last modified</a></th><th><a href="?C=S;O=A">Size</a></th></tr>
  <tr><th colspan="5"><hr></th></tr>
  <tr><td><a href="/media/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td></tr>
  <tr><td><a href="ambient/">ambient/</a></td><td align="right">2024-03-01 10:00  </td><td align="right">  - </td></tr>
  <tr><td><a href="track01.mp3">track01.mp3</a></td><td align="right">2024-03-01 10:05  </td><td align="right">4.5M</td></tr>
  <tr><td><a href="notes.txt">notes.txt</a></td><td align="right">2024-03-01 10:06  </td><td align="right">1.2K</td></tr>
  <tr><td><a href="track02.wav">track02.wav</a></td><td align="right">2024-03-01 10:10  </td><td align="right">25M</td></tr>
</table>
</body></html>
"""

_IIS_HTML = """
<html><head><title>example.com - /downloads/</title></head>
<body><H1>example.com - /downloads/</H1><hr>

<pre><A HREF="/">[To Parent Directory]</A><br><br>
 5/12/2023  2:14 PM        &lt;dir&gt; <A HREF="/downloads/episodes/">episodes</A><br>
10-May-2023 11:30        15024000 <A HREF="/downloads/song.flac">song.flac</A><br>
 1-Jan-2023 09:00            1200 <A HREF="/downloads/image.jpg">image.jpg</A><br>
</pre><hr></body></html>
"""


def test_parse_listing_date() -> None:
    assert parse_listing_date("2024-03-01 10:05") == "20240301"
    assert parse_listing_date("10-May-2023 11:30") == "20230510"
    assert parse_listing_date("invalid date string") is None


def test_parse_directory_page_apache() -> None:
    crawler = WebDirectoryCrawler()
    result = crawler.parse_directory_page(
        _APACHE_HTML, "https://example.com/media/music/")

    assert result is not None
    assert len(result.folders) == 1
    assert result.folders[0] == "https://example.com/media/music/ambient/"

    assert len(result.files) == 2
    assert result.files[0] == (
        "https://example.com/media/music/track01.mp3", "20240301")
    assert result.files[1] == (
        "https://example.com/media/music/track02.wav", "20240301")
    assert result.signature != ""


def test_parse_directory_page_iis() -> None:
    crawler = WebDirectoryCrawler()
    result = crawler.parse_directory_page(
        _IIS_HTML, "https://example.com/downloads/")

    assert result is not None
    assert len(result.folders) == 1
    assert result.folders[0] == "https://example.com/downloads/episodes/"

    assert len(result.files) == 1
    assert result.files[0] == (
        "https://example.com/downloads/song.flac", "20230510")


def test_parse_directory_page_rejects_non_index() -> None:
    crawler = WebDirectoryCrawler()
    regular_html = "<html><head><title>Welcome to My Blog</title></head><body><h1>Hello World</h1></body></html>"
    result = crawler.parse_directory_page(
        regular_html, "https://example.com/blog/")
    assert result is None


def test_web_crawler_download_file(tmp_path: Path) -> None:
    fake_audio_content = b"ID3\x03\x00\x00\x00\x00\x00\x00FAKE_AUDIO_DATA_FOR_TESTING"
    mock_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Length": str(len(fake_audio_content))},
            content=fake_audio_content,
        )
    )
    client = httpx.Client(transport=mock_transport)
    crawler = WebDirectoryCrawler(http_client=client)

    target_file = tmp_path / "audio" / "track.mp3"
    progress_recorded: list[tuple[int, int]] = []

    def _on_progress(downloaded: int, total: int) -> None:
        progress_recorded.append((downloaded, total))

    saved_path = crawler.download_file(
        "https://example.com/track.mp3",
        target_file,
        progress_callback=_on_progress,
    )

    assert saved_path.exists()
    assert saved_path.read_bytes() == fake_audio_content
    assert len(progress_recorded) > 0
    assert progress_recorded[-1] == (len(fake_audio_content),
                                     len(fake_audio_content))


def test_web_crawler_download_cancellation(tmp_path: Path) -> None:
    fake_chunk = b"X" * 1024
    cancel_event = threading.Event()
    cancel_event.set()

    mock_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Length": "10240"},
            content=fake_chunk * 10,
        )
    )
    client = httpx.Client(transport=mock_transport)
    crawler = WebDirectoryCrawler(http_client=client)

    target_file = tmp_path / "cancelled_track.mp3"
    with pytest.raises(WebCrawlerCancelledError):
        crawler.download_file(
            "https://example.com/track.mp3",
            target_file,
            cancel_event=cancel_event,
        )

    assert not target_file.exists()
    assert not target_file.with_name(target_file.name + ".part").exists()


# ==============================================================================
# YtDlpAdapter Tests
# ==============================================================================


def test_ytdlp_adapter_build_opts() -> None:
    adapter = YtDlpAdapter(
        cookie_source=BrowserCookieSource.FIREFOX,
        impersonate_target="chrome",
        extra_opts={"test_flag": True},
    )
    opts = adapter._build_opts(outtmpl="%(id)s.%(ext)s", flat_playlist=True)

    assert opts["format"] == YTDLP_BEST_AUDIO_FORMAT
    assert opts["windowsfilenames"] is True
    assert opts["noplaylist"] is False
    assert opts["extract_flat"] == "in_playlist"
    assert opts["cookiesfrombrowser"] == ("firefox",)
    assert "impersonate" in opts
    assert opts["test_flag"] is True


def test_disambiguate_archive_org_entries() -> None:
    entries = [
        {
            "id": "item1",
            "webpage_url": "https://archive.org/details/my_album",
            "url": "https://archive.org/download/my_album/track01.mp3",
        },
        {
            "id": "item2",
            "webpage_url": "https://archive.org/details/my_album",
            "url": "https://archive.org/download/my_album/track02.mp3",
        },
        {
            "id": "item3",
            "webpage_url": "https://archive.org/details/distinct_single",
            "url": "https://archive.org/download/distinct_single/track.mp3",
        },
    ]

    disambiguated = YtDlpAdapter.disambiguate_archive_org_entries(entries)

    # First two shared the same webpage_url; their webpage_url should be replaced with their file url
    assert disambiguated[0]["webpage_url"] == "https://archive.org/download/my_album/track01.mp3"
    assert disambiguated[1]["webpage_url"] == "https://archive.org/download/my_album/track02.mp3"
    # Third entry was unique, remains unchanged
    assert disambiguated[2]["webpage_url"] == "https://archive.org/details/distinct_single"


def test_ytdlp_adapter_extract_info_mocked() -> None:
    adapter = YtDlpAdapter()
    dummy_info = {
        "id": "test_id",
        "title": "Test Playlist",
        "_type": "playlist",
        "entries": [
            {"id": "v1", "title": "Track 1", "webpage_url": "https://archive.org/details/album",
                "url": "https://archive.org/download/album/1.mp3"},
            {"id": "v2", "title": "Track 2", "webpage_url": "https://archive.org/details/album",
                "url": "https://archive.org/download/album/2.mp3"},
        ],
    }

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_instance = MagicMock()
        mock_instance.extract_info.return_value = dummy_info
        mock_ydl_cls.return_value.__enter__.return_value = mock_instance

        info = adapter.extract_info("https://archive.org/details/album")

        assert info["title"] == "Test Playlist"
        # Verify Archive.org disambiguation was applied
        assert info["entries"][0]["webpage_url"] == "https://archive.org/download/album/1.mp3"
        assert info["entries"][1]["webpage_url"] == "https://archive.org/download/album/2.mp3"


def test_ytdlp_adapter_extract_cancellation() -> None:
    adapter = YtDlpAdapter()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(YtDlpCancelledError):
        adapter.extract_info(
            "https://www.youtube.com/watch?v=123", cancel_event=cancel_event)


def test_ytdlp_adapter_download_cancellation(tmp_path: Path) -> None:
    adapter = YtDlpAdapter()
    cancel_event = threading.Event()

    def _fake_download(urls: list[str]) -> None:
        cancel_event.set()
        # Trigger progress hook with status downloading after cancellation
        hook = captured_opts["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 1000})

    captured_opts: dict = {}

    def _fake_init(opts: dict) -> MagicMock:
        captured_opts.update(opts)
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.download.side_effect = _fake_download
        return mock_ydl

    with patch("yt_dlp.YoutubeDL", side_effect=_fake_init):
        with pytest.raises(YtDlpCancelledError):
            adapter.download_track(
                "https://www.youtube.com/watch?v=test",
                tmp_path,
                cancel_event=cancel_event,
            )
