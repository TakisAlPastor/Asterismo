"""Vertical slice integration tests for Milestone 2: Archive Harvester & .pklz Builder."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from src.core.config.constants import (
    AUDFPRINT_DEFAULT_SAMPLERATE,
)
from src.core.config.models import (
    BrowserCookieSource,
    HarvestItemRecord,
    HarvestSourceType,
    HarvestTaskStatus,
)
from src.core.harvester.service import HarvesterCallbacks, HarvesterService
from src.core.harvester.ytdlp_adapter import YtDlpAdapter
from src.main import bootstrap_application


def _generate_synthetic_wav(
    file_path: Path,
    duration_sec: float = 3.0,
    sample_rate: int = AUDFPRINT_DEFAULT_SAMPLERATE,
) -> Path:
    """Generate uncompressed PCM WAV with sharp pulses/chirps for audfprint landmarks."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration_sec * sample_rate)
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        data = bytearray()
        for i in range(n_samples):
            t = float(i) / float(sample_rate)
            freq = 440.0 + 800.0 * math.sin(2.0 * math.pi * 3.0 * t)
            burst = 1.0 if (i % 2500) < 400 else 0.05
            sample = int(32767.0 * 0.7 * burst *
                         math.sin(2.0 * math.pi * freq * t))
            sample = max(-32768, min(32767, sample))
            data.extend(struct.pack("<h", sample))
        wf.writeframes(data)
    return file_path


class FakeMockYtDlpAdapter:
    """Mock adapter simulating fast local downloads of synthetic audio."""

    def __init__(self, synthetic_audio_files: list[Path]) -> None:
        self.synthetic_audio_files = synthetic_audio_files
        self.download_call_count = 0

    def extract_info(
        self,
        url: str,
        download: bool = False,
        process: bool = True,
        cancel_event=None,
    ) -> dict:
        return {
            "title": "Fake Channel",
            "entries": [
                {
                    "id": f"fake_track_{i:03d}",
                    "title": f"Fake Track {i:03d}",
                    "webpage_url": f"https://www.youtube.com/watch?v=fake_{i:03d}",
                    "duration": 3.0,
                }
                for i in range(len(self.synthetic_audio_files))
            ],
        }

    def download_track(
        self,
        url: str,
        output_dir: Path,
        filename_template: str = "%(title)s.%(ext)s",
        cancel_event=None,
        progress_callback=None,
    ) -> Path:
        idx = self.download_call_count % len(self.synthetic_audio_files)
        self.download_call_count += 1
        src = self.synthetic_audio_files[idx]
        dest = output_dir / f"track_{idx:03d}_{src.name}"
        dest.write_bytes(src.read_bytes())
        return dest


def test_m2_bootstrap_and_harvester_view_mounting(qapp: QApplication, tmp_path: Path) -> None:
    """Verify MainWindow boots with HarvesterView mounted at view_stack index 0."""
    app, window = bootstrap_application(base_dir=tmp_path)
    assert window is not None
    assert window.harvester_view is not None
    assert window.view_stack.currentIndex() == 0
    assert window.view_stack.widget(0) is window.harvester_view

    # Verify HarvesterView controls initialized
    hv = window.harvester_view
    assert hv._queue_table.columnCount() == 7
    assert hv._start_queue_btn.isEnabled()
    assert not hv._pause_btn.isEnabled()
    assert not hv._stop_btn.isEnabled()


def test_m2_harvester_view_queue_crud(qapp: QApplication, tmp_path: Path) -> None:
    """Verify adding, normalizing, deduplicating, and exporting URLs via HarvesterView UI."""
    app, window = bootstrap_application(base_dir=tmp_path)
    hv = window.harvester_view
    assert hv is not None

    # 1. Add multiple links with extra spaces, duplicates, and bare handle
    input_text = """
    https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share
    @TestHandle
    https://www.youtube.com/watch?v=dQw4w9WgXcQ
    """
    hv._url_input.setText(input_text)
    hv.on_add_links()

    # Should have added 2 unique normalized links (@TestHandle and clean watch link)
    assert hv._queue_table.rowCount() == 2
    tasks = hv._service.list_queue()
    assert len(tasks) == 2
    urls = {t.normalized_url for t in tasks}
    assert "https://www.youtube.com/@testhandle" in urls
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in urls

    # 2. Export links to file
    export_file = tmp_path / "exported_queue.txt"
    hv._service.export_links_to_file(export_file)
    assert export_file.is_file()
    lines = export_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_m2_harvester_view_end_to_end_queue_run(qapp: QApplication, tmp_path: Path) -> None:
    """Verify queue run: URL -> discovery -> download -> audio split -> .pklz build -> catalog."""
    # 1. Prepare synthetic audio files
    audio_1 = _generate_synthetic_wav(
        tmp_path / "synthetic_1.wav", duration_sec=3.0)
    audio_2 = _generate_synthetic_wav(
        tmp_path / "synthetic_2.wav", duration_sec=3.0)

    # 2. Bootstrap application
    app, window = bootstrap_application(base_dir=tmp_path)
    hv = window.harvester_view
    assert hv is not None

    # Enable keep_downloaded_audio in settings to test hardlink retention
    settings = hv._settings_repo.load()
    settings.keep_downloaded_audio = True
    hv._settings_repo.save(settings)

    # Mock YtDlpAdapter factory to return fake audio downloader
    fake_adapter = FakeMockYtDlpAdapter([audio_1, audio_2])
    hv._service._ytdlp_factory = lambda c, i: fake_adapter

    # 3. Add link to queue
    added = hv._service.add_links(
        ["https://www.youtube.com/playlist?list=PL_TEST_123"])
    assert len(added) == 1
    task = added[0]
    hv.refresh_queue_table()
    assert hv._queue_table.rowCount() == 1

    # 4. Execute queue synchronously via service to assert database transactions
    logs: list[str] = []
    progress_calls: list[tuple[int, int, str]] = []
    callbacks = HarvesterCallbacks(
        on_log=logs.append,
        on_progress=lambda cur, tot, ph: progress_calls.append((cur, tot, ph)),
    )
    summary = hv._service.run_queue(callbacks=callbacks)

    # 5. Assert execution summary
    assert not summary.was_stopped
    assert summary.tasks_completed == 1
    assert summary.tasks_failed == 0
    assert len(summary.pklz_files_created) >= 1

    pklz_file = summary.pklz_files_created[0]
    assert pklz_file.is_file()

    # 6. Verify SQLite database states
    completed_task = hv._service._harvest_repo.get_task_by_id(task.id)
    assert completed_task is not None
    assert completed_task.status == HarvestTaskStatus.COMPLETED
    assert completed_task.completed_items == 2

    catalog_entries = hv._service._catalog_repo.list_entries()
    assert len(catalog_entries) >= 1
    assert catalog_entries[0].file_path == pklz_file.resolve()
    assert catalog_entries[0].track_count == 2

    # 7. Verify pre-split hardlink audio retention
    retained_dir = settings.paths.input_dir / "Fake_Channel"
    assert retained_dir.is_dir()
    retained_files = list(retained_dir.glob("*.wav"))
    assert len(retained_files) == 2


def test_m2_harvester_view_disk_folder_compilation(qapp: QApplication, tmp_path: Path) -> None:
    """Verify 'Audio on Disk' compilation card builds .pklz directly from local directory."""
    # 1. Create a directory with synthetic audio files
    source_dir = tmp_path / "local_albums"
    _generate_synthetic_wav(source_dir / "track_01.wav", duration_sec=3.0)
    _generate_synthetic_wav(source_dir / "track_02.wav", duration_sec=3.0)

    # 2. Bootstrap application
    app, window = bootstrap_application(base_dir=tmp_path)
    hv = window.harvester_view
    assert hv is not None

    # 3. Run disk compilation
    results = hv._service.run_local_disk_folder(
        source_folder=source_dir,
        prefix_label="local_test_db",
    )

    assert len(results) == 1
    res = results[0]
    assert res.pklz_path.is_file()
    assert res.stats.track_count == 2
    assert res.catalog_entry is not None

    # Verify catalog contains entry
    catalog = hv._service._catalog_repo.list_entries()
    assert any(e.prefix_label == "local_test_db" for e in catalog)
