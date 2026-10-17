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

# Subprocess runner tokens (Windows & general)
WIN32_CREATE_NO_WINDOW: int = 0x08000000
WIN32_SW_HIDE: int = 0
DEFAULT_SUBPROCESS_TIMEOUT_SEC: float = 60.0
UTF8_ENCODING: str = "utf-8"
ENCODING_ERROR_POLICY: str = "replace"
