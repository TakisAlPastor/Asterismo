"""Unit tests for configuration schemas, models, and atomic secrets store."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.core.config.constants import (
    AUDIO_EXTENSIONS_SUPPORTED,
    LOCAL_MATCH_EARLY_STOP_THRESHOLD,
    LOCAL_MATCH_MIN_HASH_THRESHOLD,
    MIGRATION_V3_NAME,
    MIGRATION_V3_VERSION,
    DEFAULT_FP_WORKER_COUNT,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DEFAULT_SPLIT_SEGMENT_SECONDS,
    DEFAULT_SPLIT_THRESHOLD_SECONDS,
    REDACTED_SECRET_PLACEHOLDER,
    ALL_RECOGNITION_ENGINES,
    ENGINE_NAME_AUDFPRINT,
    ENGINE_NAME_SHAZAM,
    ENGINE_NAME_ACOUSTID,
    ENGINE_NAME_AUDIOTAG,
    SHAZAM_REQUEST_DELAY_SECONDS,
    ACOUSTID_LOOKUP_ENDPOINT,
    AUDIOTAG_ENDPOINT,
)
from src.core.config.models import (
    LocalMatchResult,
    MatchOptions,
    ScanCandidateRecord,
    ScanCandidateStatus,
    ScanMatchRecord,
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


def test_workspace_paths_locks_bin_dir_on_deserialization(tmp_path: Path) -> None:
    """Verify that bin_dir cannot be overridden via serialized dictionary payloads."""
    base_dir = tmp_path / "app_root"
    spoofed_data = {
        "input_dir": str(tmp_path / "custom_in"),
        "archive_dir": str(tmp_path / "custom_arch"),
        "db_dir": str(tmp_path / "custom_db"),
        "output_dir": str(tmp_path / "custom_out"),
        "logs_dir": str(tmp_path / "custom_logs"),
        "bin_dir": str(tmp_path / "hijacked_bin"),
    }
    restored = WorkspacePaths.from_dict(spoofed_data, base_dir=base_dir)
    assert restored.bin_dir == base_dir.resolve() / "bin"


def test_workspace_paths_ensure_directories(tmp_path: Path) -> None:
    """Verify that ensure_directories creates all configured folders on disk."""
    base_dir = tmp_path / "custom_root"
    paths = WorkspacePaths(
        input_dir=base_dir / "my_in",
        db_dir=base_dir / "my_db",
        output_dir=base_dir / "my_out",
        logs_dir=base_dir / "my_logs",
        bin_dir=base_dir / "my_bin",
        archive_dir=base_dir / "my_archive",
    )
    for p in (paths.input_dir, paths.db_dir, paths.output_dir, paths.logs_dir, paths.bin_dir, paths.archive_dir):
        assert not p.exists()

    paths.ensure_directories()

    for p in (paths.input_dir, paths.db_dir, paths.output_dir, paths.logs_dir, paths.bin_dir, paths.archive_dir):
        assert p.is_dir()


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


def test_milestone3_constants_defined() -> None:
    """Verify Milestone 3 constants, thresholds, and migration tokens."""
    assert MIGRATION_V3_VERSION == 3
    assert MIGRATION_V3_NAME == "003_scan_candidates_and_matches"
    assert LOCAL_MATCH_MIN_HASH_THRESHOLD == 5
    assert LOCAL_MATCH_EARLY_STOP_THRESHOLD == 15
    assert ".mp3" in AUDIO_EXTENSIONS_SUPPORTED
    assert ".flac" in AUDIO_EXTENSIONS_SUPPORTED


def test_scan_candidate_record_validation(tmp_path: Path) -> None:
    """Verify validation and attributes of ScanCandidateRecord."""
    valid_path = tmp_path / "song.mp3"
    rec = ScanCandidateRecord(
        file_path=valid_path,
        file_name="song.mp3",
        relative_path="lostwave/song.mp3",
        file_size_bytes=1024,
        duration_seconds=180.5,
        status=ScanCandidateStatus.PENDING,
    )
    assert rec.file_name == "song.mp3"
    assert rec.status == ScanCandidateStatus.PENDING
    assert rec.duration_seconds == 180.5

    # Validation errors
    with pytest.raises(ConfigurationError, match="file_path cannot be empty"):
        ScanCandidateRecord(
            file_path=Path(""),
            file_name="s.mp3",
            relative_path="s.mp3",
            file_size_bytes=10,
        )

    with pytest.raises(ConfigurationError, match="file_size_bytes must be >= 0"):
        ScanCandidateRecord(
            file_path=valid_path,
            file_name="s.mp3",
            relative_path="s.mp3",
            file_size_bytes=-1,
        )

    with pytest.raises(ConfigurationError, match="duration_seconds must be >= 0"):
        ScanCandidateRecord(
            file_path=valid_path,
            file_name="s.mp3",
            relative_path="s.mp3",
            file_size_bytes=10,
            duration_seconds=-5.0,
        )


def test_scan_match_record_validation() -> None:
    """Verify validation and attributes of ScanMatchRecord."""
    match = ScanMatchRecord(
        candidate_id=1,
        matched_track="Lost Track 1984",
        matched_database="commercials.pklz",
        aligned_hashes=12,
        raw_hashes=45,
        offset_seconds=14.2,
        confidence=0.85,
    )
    assert match.candidate_id == 1
    assert match.aligned_hashes == 12
    assert match.matched_track == "Lost Track 1984"

    # Validation errors
    with pytest.raises(ConfigurationError, match="candidate_id must be >= 1"):
        ScanMatchRecord(
            candidate_id=0,
            matched_track="Test",
            matched_database="db.pklz",
        )

    with pytest.raises(ConfigurationError, match="matched_track cannot be empty"):
        ScanMatchRecord(
            candidate_id=1,
            matched_track="",
            matched_database="db.pklz",
        )

    with pytest.raises(ConfigurationError, match="aligned_hashes must be >= 0"):
        ScanMatchRecord(
            candidate_id=1,
            matched_track="Test",
            matched_database="db.pklz",
            aligned_hashes=-1,
        )


def test_local_match_result_and_options(tmp_path: Path) -> None:
    """Verify LocalMatchResult and MatchOptions validation."""
    audio_path = tmp_path / "query.mp3"
    db_path = tmp_path / "reference.pklz"

    options = MatchOptions(
        target_databases=(db_path,),
        stop_on_first_match=True,
        min_aligned_hashes=5,
        early_stop_threshold=15,
        enabled_engines=(ENGINE_NAME_AUDFPRINT, ENGINE_NAME_SHAZAM),
    )
    assert options.stop_on_first_match is True
    assert len(options.target_databases) == 1
    assert options.enabled_engines == (
        ENGINE_NAME_AUDFPRINT, ENGINE_NAME_SHAZAM)

    # Verify cloud recognition constants
    assert SHAZAM_REQUEST_DELAY_SECONDS > 0
    assert "acoustid.org" in ACOUSTID_LOOKUP_ENDPOINT
    assert "audiotag.info" in AUDIOTAG_ENDPOINT
    assert ENGINE_NAME_ACOUSTID in ALL_RECOGNITION_ENGINES
    assert ENGINE_NAME_AUDIOTAG in ALL_RECOGNITION_ENGINES

    with pytest.raises(ConfigurationError, match="min_aligned_hashes must be >= 1"):
        MatchOptions(min_aligned_hashes=0)

    with pytest.raises(ConfigurationError, match="early_stop_threshold"):
        MatchOptions(min_aligned_hashes=20, early_stop_threshold=10)

    with pytest.raises(ConfigurationError, match="Invalid engine in enabled_engines"):
        MatchOptions(enabled_engines=("unsupported_engine",))

    result = LocalMatchResult(
        candidate_path=audio_path,
        candidate_id=5,
        databases_searched=3,
        elapsed_seconds=1.25,
        early_stopped=True,
    )
    assert result.candidate_id == 5
    assert result.early_stopped is True
