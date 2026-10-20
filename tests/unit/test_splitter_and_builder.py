"""Unit tests for AudioSplitterService and FingerprintBuilderService."""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config.constants import (
    DEFAULT_SPLIT_SEGMENT_SECONDS,
    DEFAULT_SPLIT_THRESHOLD_SECONDS,
    PKLZ_EXTENSION,
)
from src.core.config.models import PklzCatalogEntry
from src.core.fingerprinter.builder import (
    FingerprintBatchResult,
    FingerprintBuilderCancelledError,
    FingerprintBuilderError,
    FingerprintBuilderService,
    PklzInspectionStats,
    compute_file_sha256,
    partition_into_batches,
    resolve_unique_pklz_path,
)
from src.core.harvester.audio_splitter import (
    AudioSplitDirectorySummary,
    AudioSplitResult,
    AudioSplitterCancelledError,
    AudioSplitterError,
    AudioSplitterService,
    compute_split_segments,
    format_split_piece_name,
)
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import ProcessResult, SilentProcessRunner


# ==============================================================================
# Audio Splitter Pure Function Tests
# ==============================================================================


def test_compute_split_segments_short_file() -> None:
    # 540s or below should never be split
    assert compute_split_segments(540.0) == []
    assert compute_split_segments(300.0) == []
    assert compute_split_segments(0.0) == []


def test_compute_split_segments_tail_merge_floor() -> None:
    # 720s or below is not strictly greater than default threshold (720s), so left whole
    assert compute_split_segments(720.0) == []
    assert compute_split_segments(600.0) == []

    # With threshold 540s: 720s yields exactly two 360s segments
    segments_720 = compute_split_segments(720.0, threshold_seconds=540)
    assert len(segments_720) == 2
    assert segments_720[0] == (0.0, 360.0)
    assert segments_720[1] == (360.0, 720.0)

    # 750s: 2 segments of 360s = 720s leaves 30s remainder.
    # Remainder < 360s, so tail merges into segment 2 -> [0..360, 360..750] (piece 2 is 390s >= 360s)
    segments_750 = compute_split_segments(750.0)
    assert len(segments_750) == 2
    assert segments_750[0] == (0.0, 360.0)
    assert segments_750[1] == (360.0, 750.0)

    # 960s: 2 segments of 360s = 720s leaves 240s remainder.
    # Remainder < 360s, so tail merges into segment 2 -> [0..360, 360..960] (piece 2 is 600s >= 360s)
    segments_960 = compute_split_segments(960.0)
    assert len(segments_960) == 2
    assert segments_960[0] == (0.0, 360.0)
    assert segments_960[1] == (360.0, 960.0)

    # 1080s: Exactly three 360s segments
    segments_1080 = compute_split_segments(1080.0)
    assert len(segments_1080) == 3
    assert segments_1080[0] == (0.0, 360.0)
    assert segments_1080[1] == (360.0, 720.0)
    assert segments_1080[2] == (720.0, 1080.0)


def test_format_split_piece_name() -> None:
    assert (
        format_split_piece_name("concert", 0.0, 360.0, ".mp3")
        == "concert_split_000m-006m.mp3"
    )
    assert (
        format_split_piece_name("concert", 360.0, 765.0, ".m4a")
        == "concert_split_006m-012m45s.m4a"
    )


# ==============================================================================
# Audio Splitter Service Tests
# ==============================================================================


def test_probe_duration(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    mock_locator.locate.return_value = Path("C:/tools/ffprobe.exe")

    mock_runner.run.return_value = ProcessResult(
        returncode=0,
        stdout="345.678\n",
        stderr="",
        command=("ffprobe",),
    )

    service = AudioSplitterService(runner=mock_runner, locator=mock_locator)
    dummy_file = tmp_path / "song.mp3"
    duration = service.probe_duration(dummy_file)

    assert duration == pytest.approx(345.678)
    mock_runner.run.assert_called_once()


def test_probe_duration_failure(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    mock_locator.locate.return_value = Path("C:/tools/ffprobe.exe")

    mock_runner.run.return_value = ProcessResult(
        returncode=1,
        stdout="",
        stderr="Invalid data found when processing input",
        command=("ffprobe",),
    )

    service = AudioSplitterService(runner=mock_runner, locator=mock_locator)
    dummy_file = tmp_path / "corrupt.mp3"
    assert service.probe_duration(dummy_file) is None


def test_split_file_in_place_success(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    mock_locator.locate.return_value = Path("C:/tools/ffmpeg.exe")

    # When runner.run is called for slicing, create fake .part file
    def _fake_slice(cmd: list[str], **kwargs: object) -> ProcessResult:
        part_arg = Path(cmd[-1])
        part_arg.write_bytes(b"FAKE_SLICE_AUDIO")
        return ProcessResult(0, "", "", tuple(cmd))

    mock_runner.run.side_effect = _fake_slice

    service = AudioSplitterService(runner=mock_runner, locator=mock_locator)
    source_file = tmp_path / "podcast.mp3"
    source_file.write_bytes(b"ORIGINAL_AUDIO_CONTENT")

    # 720s duration with threshold=540s -> 2 pieces: 0..360 and 360..720
    result = service.split_file_in_place(
        file_path=source_file,
        threshold_seconds=540,
        duration=720.0,
    )

    assert result.was_split is True
    assert not source_file.exists()
    assert len(result.piece_paths) == 2
    assert result.piece_paths[0].exists()
    assert result.piece_paths[1].exists()
    assert result.piece_paths[0].name == "podcast_split_000m-006m.mp3"
    assert result.piece_paths[1].name == "podcast_split_006m-012m.mp3"


def test_split_file_in_place_rollback_on_failure(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    mock_locator.locate.return_value = Path("C:/tools/ffmpeg.exe")

    calls = [0]

    def _fake_slice(cmd: list[str], **kwargs: object) -> ProcessResult:
        calls[0] += 1
        part_arg = Path(cmd[-1])
        if calls[0] == 1:
            part_arg.write_bytes(b"PARTIAL_SLICE_1")
            return ProcessResult(0, "", "", tuple(cmd))
        # Second slice fails
        return ProcessResult(1, "", "Muxer failed", tuple(cmd))

    mock_runner.run.side_effect = _fake_slice

    service = AudioSplitterService(runner=mock_runner, locator=mock_locator)
    source_file = tmp_path / "long_track.mp3"
    source_file.write_bytes(b"ORIGINAL_AUDIO_CONTENT")

    result = service.split_file_in_place(source_file, duration=720.0)

    # Must rollback: original file kept, pieces deleted
    assert result.was_split is False
    assert source_file.exists()
    assert not (tmp_path / "long_track_split_000m-006m.mp3").exists()


def test_process_directory_skips_probing_when_disabled(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    service = AudioSplitterService(runner=mock_runner, locator=mock_locator)

    (tmp_path / "track1.mp3").write_bytes(b"A")
    (tmp_path / "track2.wav").write_bytes(b"B")

    summary = service.process_directory(tmp_path, split_enabled=False)

    assert summary.total_audio_files == 2
    assert summary.files_split == 0
    assert len(summary.resulting_files) == 2
    mock_runner.run.assert_not_called()


def test_probe_durations_parallel(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    mock_locator.locate.return_value = Path("C:/tools/ffprobe.exe")

    file1 = tmp_path / "track1.mp3"
    file2 = tmp_path / "track2.wav"
    file3 = tmp_path / "bad.mp3"

    def _fake_run(cmd: list[str], **kwargs: object) -> ProcessResult:
        path_str = cmd[-1]
        if "track1" in path_str:
            return ProcessResult(0, "120.5\n", "", tuple(cmd))
        if "track2" in path_str:
            return ProcessResult(0, "340.0\n", "", tuple(cmd))
        return ProcessResult(1, "", "corrupt file", tuple(cmd))

    mock_runner.run.side_effect = _fake_run

    service = AudioSplitterService(runner=mock_runner, locator=mock_locator)
    durations = service.probe_durations_parallel(
        [file1, file2, file3], worker_count=3)

    assert len(durations) == 3
    assert durations[file1] == pytest.approx(120.5)
    assert durations[file2] == pytest.approx(340.0)
    assert durations[file3] is None


def test_probe_durations_parallel_cancelled(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    mock_locator.locate.return_value = Path("C:/tools/ffprobe.exe")

    service = AudioSplitterService(runner=mock_runner, locator=mock_locator)
    cancel_evt = threading.Event()
    cancel_evt.set()

    with pytest.raises(AudioSplitterCancelledError):
        service.probe_durations_parallel(
            [tmp_path / "f1.mp3", tmp_path / "f2.mp3"],
            cancel_event=cancel_evt,
        )


# ==============================================================================
# Fingerprint Builder Service Tests
# ==============================================================================


def test_partition_into_batches() -> None:
    files = [Path(f"track_{i}.mp3") for i in range(25)]
    batches = partition_into_batches(files, batch_size=10)

    assert len(batches) == 3
    assert len(batches[0]) == 10
    assert len(batches[1]) == 10
    assert len(batches[2]) == 5


def test_resolve_unique_pklz_path(tmp_path: Path) -> None:
    path1 = resolve_unique_pklz_path(
        tmp_path, "@Artist", batch_index=1, overwrite=False)
    assert path1.name == "@Artist-1.pklz"
    path1.write_bytes(b"FAKE_PKLZ")

    path2 = resolve_unique_pklz_path(
        tmp_path, "@Artist", batch_index=1, overwrite=False)
    assert path2.name == "@Artist-1_2.pklz"

    path_overwrite = resolve_unique_pklz_path(
        tmp_path, "@Artist", batch_index=1, overwrite=True)
    assert path_overwrite.name == "@Artist-1.pklz"


def test_builder_inspect_and_register_in_catalog(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    mock_catalog_repo = MagicMock()

    service = FingerprintBuilderService(
        runner=mock_runner,
        locator=mock_locator,
        catalog_repo=mock_catalog_repo,
    )

    fake_pklz = tmp_path / "test-1.pklz"
    fake_pklz.write_bytes(b"FAKE_PKLZ_CONTENT_12345")

    stats = PklzInspectionStats(
        file_path=fake_pklz,
        track_count=10,
        total_hashes=5000,
        stored_hashes=4950,
        dropped_hashes=50,
        dropped_percent=1.0,
        sample_rate=11025,
        hashbits=20,
        bucketsize=100,
        maxtime=16384,
        file_size_bytes=fake_pklz.stat().st_size,
        sha256_hash="abcdef1234567890",
    )

    service.register_in_catalog(
        stats, prefix_label="TestPrefix", batch_index=1)
    mock_catalog_repo.upsert_entry.assert_called_once_with(
        file_path=fake_pklz,
        prefix_label="TestPrefix",
        batch_index=1,
        track_count=10,
        total_hashes=5000,
        file_size_bytes=fake_pklz.stat().st_size,
        sha256_hash="abcdef1234567890",
    )


def test_builder_compute_file_sha256(tmp_path: Path) -> None:
    test_file = tmp_path / "sample.bin"
    test_file.write_bytes(b"Asterismo landmark test string")
    sha = compute_file_sha256(test_file)
    assert len(sha) == 64
    assert isinstance(sha, str)


def test_inspect_hashtable_in_memory(tmp_path: Path) -> None:
    mock_runner = MagicMock(spec=SilentProcessRunner)
    mock_locator = MagicMock(spec=BinaryLocator)
    service = FingerprintBuilderService(
        runner=mock_runner, locator=mock_locator)

    fake_file = tmp_path / "mock.pklz"
    fake_file.write_bytes(b"DATA")

    # Construct mock HashTable
    mock_ht = MagicMock()
    mock_ht.names = ["track1.wav", "track2.wav", None]
    mock_ht.counts = [100, 200, 50]
    mock_ht.depth = 100
    mock_ht.params = {"samplerate": 11025}
    mock_ht.hashbits = 20
    mock_ht.maxtimebits = 14

    stats = service.inspect_hashtable(
        mock_ht, file_path=fake_file, compute_sha256=True)

    assert stats.track_count == 2
    assert stats.total_hashes == 350
    # min(100, 100) + min(100, 200) + min(100, 50) = 100 + 100 + 50 = 250
    assert stats.stored_hashes == 250
    assert stats.dropped_hashes == 100
    assert stats.sample_rate == 11025
    assert stats.hashbits == 20
    assert stats.maxtime == 16384
    assert stats.file_size_bytes == 4
    assert len(stats.sha256_hash) == 64
