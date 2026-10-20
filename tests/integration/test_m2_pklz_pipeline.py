"""Integration tests for Milestone 2 audio splitting, .pklz compilation, and catalog registration."""
from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
import scipy.io.wavfile

from src.core.config.constants import (
    AUDFPRINT_DEFAULT_DENSITY,
    AUDFPRINT_DEFAULT_SAMPLERATE,
    DEP_KEY_FFMPEG,
    DEP_KEY_FFPROBE,
    MEMORY_DB_TARGET,
)
from src.core.database.catalog_repository import CatalogRepository
from src.core.database.connection import DatabaseManager
from src.core.database.migrations import ALL_MIGRATIONS, MigrationManager
from src.core.fingerprinter.builder import (
    FingerprintBuilderService,
    ensure_audfprint_importable,
)
from src.core.harvester.audio_splitter import AudioSplitterService
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import SilentProcessRunner


@pytest.fixture
def test_db_manager() -> Iterator[DatabaseManager]:
    """Provide an in-memory SQLite database initialized with all migrations."""
    db = DatabaseManager(MEMORY_DB_TARGET)
    mgr = MigrationManager(db, migrations=ALL_MIGRATIONS)
    mgr.apply_all()
    yield db
    db.close()


def _create_synthetic_wav(path: Path, duration_sec: float, freq_hz: float = 440.0, sr: int = 11025) -> Path:
    """Generate a rich pulsed PCM audio .wav file that produces landmark peaks."""
    total_samples = int(duration_sec * sr)
    t = np.linspace(0, duration_sec, total_samples, endpoint=False)
    # Background noise floor
    audio = np.random.uniform(-0.05, 0.05, total_samples)
    # Frequency pulses spaced across time to trigger audfprint onsets
    pulse_interval = 0.25
    pulse_dur = 0.12
    for p_time in np.arange(0.1, duration_sec - pulse_dur, pulse_interval):
        idx = int(p_time * sr)
        p_len = int(pulse_dur * sr)
        freq = freq_hz + ((idx % 5) * 200.0)
        audio[idx: idx + p_len] += 0.8 * np.sin(2 * np.pi * freq * t[:p_len])

    max_amp = np.max(np.abs(audio)) or 1.0
    audio_int16 = (audio * 32767 / max_amp).astype(np.int16)
    scipy.io.wavfile.write(str(path), sr, audio_int16)
    return path


def test_real_audio_splitter_pipeline(tmp_path: Path) -> None:
    """Verify real ffprobe and ffmpeg slicing on synthetic audio."""
    locator = BinaryLocator()
    if locator.locate(DEP_KEY_FFMPEG) is None or locator.locate(DEP_KEY_FFPROBE) is None:
        pytest.skip("ffmpeg and/or ffprobe not available in test environment")

    runner = SilentProcessRunner()
    splitter = AudioSplitterService(runner=runner, locator=locator)

    # 6-second audio file
    wav_path = tmp_path / "long_audio.wav"
    _create_synthetic_wav(wav_path, duration_sec=6.0, freq_hz=440.0)

    # Probe duration
    duration = splitter.probe_duration(wav_path)
    assert duration is not None
    assert duration == pytest.approx(6.0, abs=0.2)

    # Split with custom threshold=4s, segment=2s -> segments [0..2, 2..4, 4..6]
    split_result = splitter.split_file_in_place(
        file_path=wav_path,
        threshold_seconds=4,
        segment_seconds=2,
    )

    assert split_result.was_split is True
    assert not wav_path.exists()  # Original deleted on success
    assert len(split_result.piece_paths) == 3

    for piece in split_result.piece_paths:
        assert piece.exists()
        assert piece.stat().st_size > 0
        piece_dur = splitter.probe_duration(piece)
        assert piece_dur is not None
        assert piece_dur == pytest.approx(2.0, abs=0.3)


def test_real_fingerprint_builder_inprocess_and_catalog(
    tmp_path: Path,
    test_db_manager: DatabaseManager,
) -> None:
    """Verify in-process .pklz building, landmark extraction, and SQLite registration."""
    locator = BinaryLocator()
    runner = SilentProcessRunner()
    catalog_repo = CatalogRepository(test_db_manager)

    builder = FingerprintBuilderService(
        runner=runner,
        locator=locator,
        catalog_repo=catalog_repo,
    )

    # Generate 3 distinct synthetic audio tracks
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    track1 = _create_synthetic_wav(
        audio_dir / "track1.wav", duration_sec=3.0, freq_hz=440.0)
    track2 = _create_synthetic_wav(
        audio_dir / "track2.wav", duration_sec=4.0, freq_hz=660.0)
    track3 = _create_synthetic_wav(
        audio_dir / "track3.wav", duration_sec=3.5, freq_hz=880.0)

    output_dir = tmp_path / "pklz_out"
    results = builder.build_batches(
        audio_files=[track1, track2, track3],
        output_dir=output_dir,
        archive_prefix="@TestArtist",
        batch_size=10,
        worker_count=1,
    )

    assert len(results) == 1
    res = results[0]

    assert res.batch_index == 1
    assert res.pklz_path.exists()
    assert res.pklz_path.suffix == ".pklz"

    # Verify extracted landmark statistics
    assert res.stats.track_count == 3
    assert res.stats.total_hashes > 0
    assert res.stats.file_size_bytes > 0
    assert len(res.stats.sha256_hash) == 64

    # Verify registration in SQLite catalog
    assert res.catalog_entry is not None
    assert res.catalog_entry.prefix_label == "@TestArtist"
    assert res.catalog_entry.track_count == 3
    assert res.catalog_entry.total_hashes == res.stats.total_hashes

    # Fetch from repository directly
    saved_entry = catalog_repo.get_by_path(res.pklz_path)
    assert saved_entry is not None
    assert saved_entry.sha256_hash == res.stats.sha256_hash


def test_real_fingerprint_builder_multipart_subprocess(
    tmp_path: Path,
    test_db_manager: DatabaseManager,
) -> None:
    """Verify multi-part subprocess audfprint execution and newmerge combining."""
    locator = BinaryLocator()
    runner = SilentProcessRunner()
    catalog_repo = CatalogRepository(test_db_manager)

    builder = FingerprintBuilderService(
        runner=runner,
        locator=locator,
        catalog_repo=catalog_repo,
    )

    # Generate 4 small tracks
    audio_dir = tmp_path / "multi_audio"
    audio_dir.mkdir()
    tracks = [
        _create_synthetic_wav(
            audio_dir / f"song_{i}.wav", duration_sec=2.5, freq_hz=300.0 + i * 150.0)
        for i in range(4)
    ]

    target_pklz = tmp_path / "multipart.pklz"
    work_dir = tmp_path / "work"

    output_path, unreadable = builder.build_batch_subprocess(
        audio_files=tracks,
        output_pklz=target_pklz,
        work_dir=work_dir,
        parts=2,
    )

    assert output_path.exists()
    assert len(unreadable) == 0

    stats = builder.inspect_pklz(output_path)
    assert stats.track_count == 4
    assert stats.total_hashes > 0

    # Ensure intermediate parts were cleanly deleted
    assert not (work_dir / "multipart.part1.pklz").exists()
    assert not (work_dir / "multipart.part2.pklz").exists()
