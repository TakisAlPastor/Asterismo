"""Strongly-typed domain models, enums, and configuration structures."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from src.core.config.constants import (
    DEFAULT_ARCHIVE_DIRNAME,
    DEFAULT_DB_DIRNAME,
    DEFAULT_FP_WORKER_COUNT,
    DEFAULT_INPUT_DIRNAME,
    DEFAULT_LOGS_DIRNAME,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DEFAULT_OUTPUT_DIRNAME,
    DEFAULT_SPLIT_LONG_AUDIO,
    DEFAULT_SPLIT_SEGMENT_SECONDS,
    DEFAULT_SPLIT_THRESHOLD_SECONDS,
    BIN_DIRNAME,
    MIN_POSITIVE_INT,
    REDACTED_SECRET_PLACEHOLDER,
)


class ConfigurationError(ValueError):
    """Raised when configuration validation or deserialization fails."""


class ThemeMode(StrEnum):
    """Supported UI theme modes."""

    DARK = "dark"
    LIGHT = "light"
    SYSTEM = "system"


class ScanMode(StrEnum):
    """Supported identification scan modes."""

    QUICK = "quick"
    LONG = "long"
    BOTH = "both"


class HarvestTaskStatus(StrEnum):
    """Lifecycle status of a harvest job in the queue."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    STOPPED = "stopped"


class HarvestSourceType(StrEnum):
    """Source classification of audio input for harvesting."""

    YTDLP_URL = "ytdlp_url"
    WEB_DIRECTORY = "web_directory"
    LOCAL_DISK = "local_disk"


class HarvestItemStatus(StrEnum):
    """Processing status of an individual audio track within a harvest task."""

    PENDING = "pending"
    DOWNLOADED = "downloaded"
    FINGERPRINTED = "fingerprinted"
    SKIPPED = "skipped"
    FAILED = "failed"


class BrowserCookieSource(StrEnum):
    """Supported browsers for cookie extraction in yt-dlp."""

    NONE = "none"
    FIREFOX = "firefox"
    CHROME = "chrome"
    EDGE = "edge"
    BRAVE = "brave"


class DependencyState(StrEnum):
    """State of an external binary or package dependency."""

    OK = "ok"
    MISSING = "missing"
    BROKEN = "broken"
    WRONG_VERSION = "wrong_version"


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """Detailed health check status of an external dependency or tool."""

    name: str
    state: DependencyState
    path: Path | None = None
    version: str | None = None
    details: str = ""

    @property
    def is_ok(self) -> bool:
        """Convenience check if dependency is operational."""
        return self.state == DependencyState.OK


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """Explicit filesystem paths used across Asterismo services."""

    input_dir: Path
    db_dir: Path
    output_dir: Path
    logs_dir: Path
    bin_dir: Path
    archive_dir: Path

    @classmethod
    def default(cls, base_dir: Path) -> WorkspacePaths:
        """Derive standard default directory layout relative to base_dir."""
        resolved = base_dir.resolve()
        return cls(
            input_dir=resolved / DEFAULT_INPUT_DIRNAME,
            archive_dir=resolved / DEFAULT_ARCHIVE_DIRNAME,
            db_dir=resolved / DEFAULT_DB_DIRNAME,
            output_dir=resolved / DEFAULT_OUTPUT_DIRNAME,
            logs_dir=resolved / DEFAULT_LOGS_DIRNAME,
            bin_dir=resolved / BIN_DIRNAME,
        )

    def to_dict(self) -> dict[str, str]:
        """Serialize paths to string dictionary."""
        return {
            "input_dir": str(self.input_dir),
            "archive_dir": str(self.archive_dir),
            "db_dir": str(self.db_dir),
            "output_dir": str(self.output_dir),
            "logs_dir": str(self.logs_dir),
            "bin_dir": str(self.bin_dir),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], base_dir: Path) -> WorkspacePaths:
        """Construct WorkspacePaths from mapping with fallback to base_dir defaults."""
        defaults = cls.default(base_dir)
        return cls(
            input_dir=Path(data.get("input_dir", defaults.input_dir)),
            archive_dir=Path(data.get("archive_dir", defaults.archive_dir)),
            db_dir=Path(data.get("db_dir", defaults.db_dir)),
            output_dir=Path(data.get("output_dir", defaults.output_dir)),
            logs_dir=Path(data.get("logs_dir", defaults.logs_dir)),
            bin_dir=Path(data.get("bin_dir", defaults.bin_dir)),
        )


@dataclass(slots=True)
class AppSettings:
    """Portable application configuration stored in SQLite state store."""

    paths: WorkspacePaths
    theme: ThemeMode = ThemeMode.SYSTEM
    scan_mode: ScanMode = ScanMode.QUICK
    split_long_audio: bool = DEFAULT_SPLIT_LONG_AUDIO
    split_threshold_seconds: int = DEFAULT_SPLIT_THRESHOLD_SECONDS
    split_segment_seconds: int = DEFAULT_SPLIT_SEGMENT_SECONDS
    max_concurrent_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    fp_worker_count: int = DEFAULT_FP_WORKER_COUNT
    keep_downloaded_audio: bool = False
    cookies_browser: BrowserCookieSource = BrowserCookieSource.NONE
    skip_already_fingerprinted: bool = True
    extra_ytdlp_args: str = ""

    def __post_init__(self) -> None:
        """Validate bounds and coerce string enums safely."""
        if not isinstance(self.theme, ThemeMode):
            try:
                self.theme = ThemeMode(str(self.theme).lower())
            except ValueError as err:
                raise ConfigurationError(
                    f"Invalid theme mode: {self.theme}") from err

        if not isinstance(self.scan_mode, ScanMode):
            try:
                self.scan_mode = ScanMode(str(self.scan_mode).lower())
            except ValueError as err:
                raise ConfigurationError(
                    f"Invalid scan mode: {self.scan_mode}") from err

        if not isinstance(self.cookies_browser, BrowserCookieSource):
            try:
                self.cookies_browser = BrowserCookieSource(
                    str(self.cookies_browser).lower())
            except ValueError as err:
                raise ConfigurationError(
                    f"Invalid browser cookie source: {self.cookies_browser}") from err

        if self.split_threshold_seconds < MIN_POSITIVE_INT:
            raise ConfigurationError(
                f"split_threshold_seconds must be >= {MIN_POSITIVE_INT}, got {self.split_threshold_seconds}"
            )

        if self.split_segment_seconds < MIN_POSITIVE_INT:
            raise ConfigurationError(
                f"split_segment_seconds must be >= {MIN_POSITIVE_INT}, got {self.split_segment_seconds}"
            )

        if self.split_segment_seconds > self.split_threshold_seconds:
            raise ConfigurationError(
                f"split_segment_seconds ({self.split_segment_seconds}) cannot exceed "
                f"split_threshold_seconds ({self.split_threshold_seconds})"
            )

        if self.max_concurrent_downloads < MIN_POSITIVE_INT:
            raise ConfigurationError(
                f"max_concurrent_downloads must be >= {MIN_POSITIVE_INT}, got {self.max_concurrent_downloads}"
            )

        if self.fp_worker_count < MIN_POSITIVE_INT:
            raise ConfigurationError(
                f"fp_worker_count must be >= {MIN_POSITIVE_INT}, got {self.fp_worker_count}"
            )

    @classmethod
    def default(cls, base_dir: Path) -> AppSettings:
        """Create default settings populated relative to base_dir."""
        return cls(paths=WorkspacePaths.default(base_dir))

    def to_dict(self) -> dict[str, Any]:
        """Serialize settings to JSON-compatible dictionary."""
        return {
            "paths": self.paths.to_dict(),
            "theme": str(self.theme.value),
            "scan_mode": str(self.scan_mode.value),
            "split_long_audio": self.split_long_audio,
            "split_threshold_seconds": self.split_threshold_seconds,
            "split_segment_seconds": self.split_segment_seconds,
            "max_concurrent_downloads": self.max_concurrent_downloads,
            "fp_worker_count": self.fp_worker_count,
            "keep_downloaded_audio": self.keep_downloaded_audio,
            "cookies_browser": str(self.cookies_browser.value),
            "skip_already_fingerprinted": self.skip_already_fingerprinted,
            "extra_ytdlp_args": self.extra_ytdlp_args,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], base_dir: Path) -> AppSettings:
        """Construct AppSettings from dictionary."""
        raw_paths = data.get("paths", {})
        if not isinstance(raw_paths, Mapping):
            raw_paths = {}

        paths = WorkspacePaths.from_dict(raw_paths, base_dir=base_dir)
        defaults = cls.default(base_dir)

        return cls(
            paths=paths,
            theme=data.get("theme", defaults.theme),
            scan_mode=data.get("scan_mode", defaults.scan_mode),
            split_long_audio=bool(
                data.get("split_long_audio", defaults.split_long_audio)),
            split_threshold_seconds=int(
                data.get("split_threshold_seconds", defaults.split_threshold_seconds)),
            split_segment_seconds=int(
                data.get("split_segment_seconds", defaults.split_segment_seconds)),
            max_concurrent_downloads=int(
                data.get("max_concurrent_downloads", defaults.max_concurrent_downloads)),
            fp_worker_count=int(
                data.get("fp_worker_count", defaults.fp_worker_count)),
            keep_downloaded_audio=bool(
                data.get("keep_downloaded_audio", defaults.keep_downloaded_audio)),
            cookies_browser=data.get(
                "cookies_browser", defaults.cookies_browser),
            skip_already_fingerprinted=bool(
                data.get("skip_already_fingerprinted", defaults.skip_already_fingerprinted)),
            extra_ytdlp_args=str(
                data.get("extra_ytdlp_args", defaults.extra_ytdlp_args)),
        )


@dataclass(frozen=True, slots=True)
class SecretCredentials:
    """Sensitive API keys and external webhook URLs."""
    acoustid_api_key: str = field(default="")
    audiotag_api_key: str = field(default="")
    discord_webhook_url: str = field(default="")

    def __post_init__(self) -> None:
        """Normalize inputs by stripping accidental whitespace."""
        object.__setattr__(self, "acoustid_api_key",
                           self.acoustid_api_key.strip())
        object.__setattr__(self, "audiotag_api_key",
                           self.audiotag_api_key.strip())
        object.__setattr__(self, "discord_webhook_url",
                           self.discord_webhook_url.strip())

    @property
    def has_acoustid(self) -> bool:
        """Check if AcoustID key is configured."""
        return bool(self.acoustid_api_key)

    @property
    def has_audiotag(self) -> bool:
        """Check if AudioTag key is configured."""
        return bool(self.audiotag_api_key)

    @property
    def has_discord_webhook(self) -> bool:
        """Check if Discord Webhook URL is configured."""
        return bool(self.discord_webhook_url)

    def to_dict(self) -> dict[str, str]:
        """Serialize credentials to dictionary for storage."""
        return {
            "acoustid_api_key": self.acoustid_api_key,
            "audiotag_api_key": self.audiotag_api_key,
            "discord_webhook_url": self.discord_webhook_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SecretCredentials:
        """Construct SecretCredentials from mapping."""
        return cls(
            acoustid_api_key=str(data.get("acoustid_api_key", "")),
            audiotag_api_key=str(data.get("audiotag_api_key", "")),
            discord_webhook_url=str(data.get("discord_webhook_url", "")),
        )

    def __repr__(self) -> str:
        """Ensure secrets are never leaked to logs or console representations."""
        return (
            f"SecretCredentials("
            f"acoustid_api_key={REDACTED_SECRET_PLACEHOLDER if self.has_acoustid else '""'}, "
            f"audiotag_api_key={REDACTED_SECRET_PLACEHOLDER if self.has_audiotag else '""'}, "
            f"discord_webhook_url={REDACTED_SECRET_PLACEHOLDER if self.has_discord_webhook else '""'}"
            f")"
        )

    def __str__(self) -> str:
        """Same masking behavior for string conversion."""
        return self.__repr__()


@dataclass(frozen=True, slots=True)
class HarvestTask:
    """Representation of an audio ingestion job in the harvest queue."""

    id: int | None
    raw_url: str
    normalized_url: str
    source_type: HarvestSourceType
    prefix_label: str
    title: str = ""
    status: HarvestTaskStatus = HarvestTaskStatus.PENDING
    total_items: int = 0
    completed_items: int = 0
    skipped_items: int = 0
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        """Validate URLs and bounds."""
        if not self.raw_url.strip():
            raise ConfigurationError("raw_url cannot be empty")
        if not self.normalized_url.strip():
            raise ConfigurationError("normalized_url cannot be empty")
        if not self.prefix_label.strip():
            raise ConfigurationError("prefix_label cannot be empty")
        if self.total_items < 0:
            raise ConfigurationError(
                f"total_items must be >= 0, got {self.total_items}")
        if self.completed_items < 0:
            raise ConfigurationError(
                f"completed_items must be >= 0, got {self.completed_items}")
        if self.skipped_items < 0:
            raise ConfigurationError(
                f"skipped_items must be >= 0, got {self.skipped_items}")


@dataclass(frozen=True, slots=True)
class HarvestItemRecord:
    """Representation of an individual audio item tracked under a harvest task."""

    id: int | None
    task_id: int
    item_key: str
    title: str = ""
    source_url: str = ""
    duration_seconds: float | None = None
    status: HarvestItemStatus = HarvestItemStatus.PENDING
    pklz_filename: str = ""
    fingerprinted_at: str | None = None

    def __post_init__(self) -> None:
        """Validate item key and task reference."""
        if not self.item_key.strip():
            raise ConfigurationError("item_key cannot be empty")
        if self.task_id < 1:
            raise ConfigurationError(
                f"task_id must be >= 1, got {self.task_id}")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ConfigurationError(
                f"duration_seconds must be >= 0, got {self.duration_seconds}"
            )


@dataclass(frozen=True, slots=True)
class PklzCatalogEntry:
    """Indexed metadata for an acoustic landmark .pklz database file."""

    file_path: Path
    file_name: str
    prefix_label: str
    batch_index: int = 1
    track_count: int = 0
    total_hashes: int = 0
    file_size_bytes: int = 0
    sha256_hash: str = ""
    created_at: str = ""
    updated_at: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        """Validate positive counts and hash string."""
        if not str(self.file_path).strip():
            raise ConfigurationError("file_path cannot be empty")
        if not self.file_name.strip():
            raise ConfigurationError("file_name cannot be empty")
        if not self.prefix_label.strip():
            raise ConfigurationError("prefix_label cannot be empty")
        if self.batch_index < 1:
            raise ConfigurationError(
                f"batch_index must be >= 1, got {self.batch_index}")
        if self.track_count < 0:
            raise ConfigurationError(
                f"track_count must be >= 0, got {self.track_count}")
        if self.total_hashes < 0:
            raise ConfigurationError(
                f"total_hashes must be >= 0, got {self.total_hashes}")
        if self.file_size_bytes < 0:
            raise ConfigurationError(
                f"file_size_bytes must be >= 0, got {self.file_size_bytes}")
