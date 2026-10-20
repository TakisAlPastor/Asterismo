"""Unit tests for configuration schemas, models, and atomic secrets store."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.core.config.constants import (
    DEFAULT_FP_WORKER_COUNT,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DEFAULT_SPLIT_SEGMENT_SECONDS,
    DEFAULT_SPLIT_THRESHOLD_SECONDS,
    REDACTED_SECRET_PLACEHOLDER,
)
from src.core.config.models import (
    AppSettings,
    ConfigurationError,
    ScanMode,
    SecretCredentials,
    ThemeMode,
    WorkspacePaths,
)
from src.core.config.secrets_store import SecretsStore


def test_workspace_paths_default_and_roundtrip(tmp_path: Path) -> None:
    """Verify default directory resolution and lossless dict serialization."""
    # Arrange
    base_dir = tmp_path / "app_root"

    # Act
    paths = WorkspacePaths.default(base_dir)
    data = paths.to_dict()
    restored = WorkspacePaths.from_dict(data, base_dir=base_dir)

    # Assert
    assert paths.input_dir == base_dir.resolve() / "input"
    assert paths.db_dir == base_dir.resolve() / "databases"
    assert paths.output_dir == base_dir.resolve() / "output"
    assert paths.logs_dir == base_dir.resolve() / "logs"
    assert paths.bin_dir == base_dir.resolve() / "bin"
    assert paths.archive_dir == base_dir.resolve() / "archive"
    assert restored == paths


def test_app_settings_default_and_roundtrip(tmp_path: Path) -> None:
    """Verify default settings initialization and JSON roundtrip."""
    # Arrange
    base_dir = tmp_path / "app_root"

    # Act
    settings = AppSettings.default(base_dir)
    payload = settings.to_dict()
    restored = AppSettings.from_dict(payload, base_dir=base_dir)

    # Assert
    assert settings.theme == ThemeMode.SYSTEM
    assert settings.scan_mode == ScanMode.QUICK

    assert settings.split_threshold_seconds == DEFAULT_SPLIT_THRESHOLD_SECONDS
    assert settings.split_segment_seconds == DEFAULT_SPLIT_SEGMENT_SECONDS
    assert settings.max_concurrent_downloads == DEFAULT_MAX_CONCURRENT_DOWNLOADS
    assert settings.fp_worker_count == DEFAULT_FP_WORKER_COUNT
    assert restored.to_dict() == payload


@pytest.mark.parametrize(
    ("kwargs", "expected_msg"),
    [
        ({"split_threshold_seconds": 0}, "split_threshold_seconds must be >="),
        ({"split_segment_seconds": 0}, "split_segment_seconds must be >="),
        ({"split_segment_seconds": 800, "split_threshold_seconds": 720},
         "cannot exceed split_threshold_seconds"),
        ({"max_concurrent_downloads": 0}, "max_concurrent_downloads must be >="),
        ({"fp_worker_count": -1}, "fp_worker_count must be >="),
    ],
)
def test_app_settings_rejects_invalid_numeric_bounds(
    tmp_path: Path, kwargs: dict[str, int], expected_msg: str
) -> None:
    """Verify that boundary violations raise ConfigurationError."""
    # Arrange
    paths = WorkspacePaths.default(tmp_path)

    # Act & Assert
    with pytest.raises(ConfigurationError, match=expected_msg):
        AppSettings(paths=paths, **kwargs)


def test_app_settings_coerces_and_validates_enums(tmp_path: Path) -> None:
    """Verify enum coercion and error handling for unknown enum variants."""
    # Arrange
    paths = WorkspacePaths.default(tmp_path)

    # Act: string coercion
    settings = AppSettings(paths=paths, theme="light",
                           scan_mode="long")  # type: ignore[arg-type]

    # Assert
    assert settings.theme == ThemeMode.LIGHT
    assert settings.scan_mode == ScanMode.LONG

    # Invalid enum variants
    with pytest.raises(ConfigurationError, match="Invalid theme mode"):
        # type: ignore[arg-type]
        AppSettings(paths=paths, theme="invalid_theme")

    with pytest.raises(ConfigurationError, match="Invalid scan mode"):
        # type: ignore[arg-type]
        AppSettings(paths=paths, scan_mode="invalid_scan")


def test_secret_credentials_masks_repr_and_str() -> None:
    """Verify that credentials are never exposed through repr or str representations."""
    # Arrange
    creds = SecretCredentials(
        acoustid_api_key="  secret_acoustid_123  ",
        audiotag_api_key="secret_audiotag_456",
        discord_webhook_url="https://discord.com/api/webhooks/123/xyz",
    )

    # Act
    repr_str = repr(creds)
    str_val = str(creds)

    # Assert: White space normalized
    assert creds.acoustid_api_key == "secret_acoustid_123"
    assert creds.has_acoustid is True
    assert creds.has_audiotag is True
    assert creds.has_discord_webhook is True

    # Assert: Raw values are NOT in repr/str
    assert "secret_acoustid_123" not in repr_str
    assert "secret_audiotag_456" not in repr_str
    assert "https://discord.com" not in repr_str
    assert REDACTED_SECRET_PLACEHOLDER in repr_str
    assert repr_str == str_val


def test_secrets_store_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """Verify that a missing secrets file returns empty SecretCredentials."""
    # Arrange
    secrets_file = tmp_path / "non_existent_secrets.json"
    store = SecretsStore(secrets_file)

    # Act
    creds = store.load()

    # Assert
    assert creds.acoustid_api_key == ""
    assert creds.audiotag_api_key == ""
    assert creds.discord_webhook_url == ""
    assert creds.has_acoustid is False


def test_secrets_store_atomic_save_and_load(tmp_path: Path) -> None:
    """Verify atomic persistence and subsequent loading of SecretCredentials."""
    # Arrange
    secrets_file = tmp_path / "subfolder" / ".secrets.json"
    store = SecretsStore(secrets_file)
    creds = SecretCredentials(
        acoustid_api_key="acoustid_token",
        audiotag_api_key="audiotag_token",
        discord_webhook_url="https://discord.com/api/webhook/test",
    )

    # Act
    store.save(creds)
    loaded = store.load()

    # Assert
    assert secrets_file.is_file()
    assert not (tmp_path / "subfolder" /
                ".secrets.json.secrets.json.tmp").exists()
    assert loaded.acoustid_api_key == "acoustid_token"
    assert loaded.audiotag_api_key == "audiotag_token"
    assert loaded.discord_webhook_url == "https://discord.com/api/webhook/test"


def test_secrets_store_raises_configuration_error_on_corrupt_json(tmp_path: Path) -> None:
    """Verify that corrupted or non-mapping JSON files raise ConfigurationError."""
    # Arrange
    secrets_file = tmp_path / ".secrets.json"
    secrets_file.write_text("{corrupt_json: true", encoding="utf-8")
    store = SecretsStore(secrets_file)

    # Act & Assert
    with pytest.raises(ConfigurationError, match="Invalid JSON in secrets file"):
        store.load()

    # Non-dictionary root
    secrets_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="expected JSON object"):
        store.load()
