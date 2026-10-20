"""Application constants and default configuration tokens."""

from __future__ import annotations

# Application metadata
APP_NAME: str = "Asterismo"
APP_VERSION: str = "0.1.0"
MIN_PYTHON_VERSION: tuple[int, int] = (3, 12)

# File and directory names
STATE_DB_FILENAME: str = "asterismo_state.db"
SECRETS_FILENAME: str = ".secrets.json"
SECRETS_TEMP_SUFFIX: str = ".secrets.json.tmp"
DEFAULT_INPUT_DIRNAME: str = "input"
DEFAULT_DB_DIRNAME: str = "databases"
DEFAULT_OUTPUT_DIRNAME: str = "output"
DEFAULT_LOGS_DIRNAME: str = "logs"
BIN_DIRNAME: str = "bin"
BIN_STAGING_DIRNAME: str = ".staging"

# Audio slicing and processing domain defaults
DEFAULT_SPLIT_LONG_AUDIO: bool = True
DEFAULT_SPLIT_THRESHOLD_SECONDS: int = 720  # 12 minutes
DEFAULT_SPLIT_SEGMENT_SECONDS: int = 360    # 6 minutes
DEFAULT_MAX_CONCURRENT_DOWNLOADS: int = 4
DEFAULT_FP_WORKER_COUNT: int = 4
DEFAULT_BATCH_SIZE: int = 1000
MIN_POSITIVE_INT: int = 1

# Masking & Security
REDACTED_SECRET_PLACEHOLDER: str = "***REDACTED***"

# SQLite configuration pragmas (ADR-002 & sqlite-database-expert)
SQLITE_JOURNAL_MODE: str = "WAL"
SQLITE_SYNCHRONOUS: str = "NORMAL"
SQLITE_TEMP_STORE: str = "MEMORY"
SQLITE_BUSY_TIMEOUT_MS: int = 5000
SQLITE_CACHE_SIZE_KB: int = -64000  # 64 MB
SQLITE_FOREIGN_KEYS_ON: int = 1
MEMORY_DB_TARGET: str = ":memory:"
APP_SETTINGS_ROW_KEY: str = "global_app_settings"
MIGRATION_V1_VERSION: int = 1
MIGRATION_V1_NAME: str = "001_initial_app_settings"
MIGRATION_V2_VERSION: int = 2
MIGRATION_V2_NAME: str = "002_milestone2_harvester_and_catalog"

# Harvester & Web Crawler Constants
MAX_QUEUE_LINKS: int = 1000
WEB_CRAWLER_MAX_DEPTH: int = 25
WEB_CRAWLER_MAX_FOLDERS: int = 5000
WEB_CRAWLER_MAX_PAGE_BYTES: int = 2 * 1024 * \
    1024  # 2 MiB HTML parse safety limit
DEFAULT_HTTP_TIMEOUT_SEC: float = 30.0
BUFFER_CHUNK_SIZE_BYTES: int = 64 * 1024  # 64 KiB streaming chunk
DIRECTORY_INDEX_SIGNATURES: tuple[str, ...] = (
    "index of", "directory listing for")
IIS_PARENT_DIR_TEXT: str = "[to parent directory]"
YTDLP_BEST_AUDIO_FORMAT: str = "ba/b"
YTDLP_FILENAME_TEMPLATE: str = "%(id)s.%(ext)s"
YOUTUBE_DEFAULT_BASE_URL: str = "https://www.youtube.com"
ARCHIVE_ORG_HOST_SUBSTRING: str = "archive.org"
BACKOFF_INITIAL_SECONDS: float = 30.0
BACKOFF_MAX_SECONDS: float = 600.0
BACKOFF_MULTIPLIER: float = 2.0
SUPPORTED_AUDIO_EXTENSIONS: tuple[str, ...] = (
    ".mp3",
    ".mp4",
    ".wav",
    ".flac",
    ".m4a",
    ".wma",
    ".webm",
    ".ogg",
    ".aac",
    ".opus",
)
TRACKING_QUERY_PARAMS: frozenset[str] = frozenset({
    "si",
    "pp",
    "feature",
    "fbclid",
    "gclid",
    "igshid",
    "ref_src",
    "ref_url",
})
TRACKING_QUERY_PREFIX: str = "utm_"

# Audio Splitter & .pklz Builder Constants
PART_FILE_SUFFIX: str = ".part"
PKLZ_EXTENSION: str = ".pklz"
PKLZ_TEMP_SUFFIX: str = ".pklz.tmp"
FFMPEG_SLICE_TIMEOUT_SEC: float = 300.0
FFPROBE_TIMEOUT_SEC: float = 30.0
AUDFPRINT_BATCH_TIMEOUT_SEC: float = 7200.0
SPLIT_TOLERANCE_SECONDS: float = 0.5
SPLIT_MUXER_MAP: dict[str, str] = {
    ".m4a": "ipod",
    ".mp4": "mp4",
    ".mp3": "mp3",
    ".flac": "flac",
    ".wav": "wav",
    ".ogg": "ogg",
    ".opus": "opus",
    ".aac": "adts",
    ".webm": "webm",
    ".wma": "asf",
}
AUDFPRINT_DEFAULT_DENSITY: float = 20.0
AUDFPRINT_DEFAULT_HASHBITS: int = 20
AUDFPRINT_DEFAULT_BUCKETSIZE: int = 100
AUDFPRINT_DEFAULT_MAXTIME: int = 16384
AUDFPRINT_DEFAULT_SAMPLERATE: int = 11025
AUDFPRINT_DEFAULT_SHIFTS: int = 1
AUDFPRINT_DEFAULT_FANOUT: int = 3
AUDFPRINT_DEFAULT_PKS_PER_FRAME: int = 5
AUDFPRINT_DEFAULT_FREQ_SD: float = 30.0
AUDFPRINT_MIN_FILES_FOR_MULTIPART: int = 16
AUDFPRINT_MIN_FILES_PER_PART: int = 8

# Subprocess runner tokens (Windows & general)
WIN32_CREATE_NO_WINDOW: int = 0x08000000
WIN32_SW_HIDE: int = 0
DEFAULT_SUBPROCESS_TIMEOUT_SEC: float = 60.0
UTF8_ENCODING: str = "utf-8"
ENCODING_ERROR_POLICY: str = "replace"

# External Binary & Dependency Names
DEP_KEY_FFMPEG: str = "ffmpeg"
DEP_KEY_FFPROBE: str = "ffprobe"
DEP_KEY_FPCALC: str = "fpcalc"
DEP_KEY_AUDFPRINT: str = "audfprint"
DEP_KEY_PYTHON: str = "python"
DEP_KEY_WINGET: str = "winget"

BIN_NAME_FFMPEG: str = "ffmpeg.exe"
BIN_NAME_FFPROBE: str = "ffprobe.exe"
BIN_NAME_FPCALC: str = "fpcalc.exe"
BIN_NAME_WINGET: str = "winget.exe"

# Required Python Packages for Doctor Verification
REQUIRED_PYTHON_PACKAGES: tuple[str, ...] = (
    "PySide6",
    "numpy",
    "scipy",
    "joblib",
    "httpx",
    "yt_dlp",
    "curl_cffi",
)

# Vendored audfprint integrity requirements
AUDFPRINT_REQUIRED_FILES: tuple[str, ...] = (
    "__init__.py",
    "audfprint.py",
    "audfprint_analyze.py",
    "audfprint_match.py",
    "audio_read.py",
    "hash_table.py",
    "stft.py",
)

AUDFPRINT_INTEGRITY_MARKERS: dict[str, str] = {
    "audfprint.py": 'reconfigure(encoding="utf-8")',
    "audio_read.py": "-vn",
    "hash_table.py": "_NumpyCoreCompatUnpickler",
}

# Winget Package IDs for anti-AV provisioning
WINGET_FFMPEG_PACKAGE_ID: str = "Gyan.FFmpeg"
WINGET_CHROMAPRINT_PACKAGE_ID: str = "AcoustID.Chromaprint"
