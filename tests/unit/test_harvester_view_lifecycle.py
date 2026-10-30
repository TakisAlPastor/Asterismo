"""Unit tests for worker thread lifecycle teardown in HarvesterView and DependencyDoctorView."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from src.core.config.models import (
    AppSettings,
    DependencyState,
    DependencyStatus,
    WorkspacePaths,
)
from src.core.harvester.service import HarvestRunSummary
from src.ui.views.dependency_doctor_view import DependencyDoctorView
from src.ui.views.harvester_view import HarvesterView


def _wait_for_thread_completion(qapp: QApplication, view_obj: object, timeout_ms: int = 3000) -> None:
    """Spin the Qt event loop until the view's worker thread finishes and deferred deletions execute."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)

    while getattr(view_obj, "_worker_thread", None) is not None and timer.isActive():
        qapp.processEvents()

    timer.stop()
    # Process one extra round of deferred deletion events (deleteLater)
    qapp.sendPostedEvents()
    qapp.processEvents()


def test_harvester_view_teardown_slot_resets_references(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Verify _on_worker_teardown explicitly clears _worker and _worker_thread and restores controls."""
    mock_service = MagicMock()
    mock_service.list_queue.return_value = []
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(paths=WorkspacePaths.default(tmp_path))

    view = HarvesterView(harvester_service=mock_service, settings_repo=mock_settings_repo)
    assert not view._is_worker_running()

    # Simulate populated attributes during a run
    view._worker = MagicMock()
    view._worker_thread = MagicMock()
    view._worker_thread.isRunning.return_value = True
    view._set_controls_running(True)

    assert view._is_worker_running()
    assert not view._start_queue_btn.isEnabled()

    # Trigger teardown
    view._on_worker_teardown()

    assert view._worker is None
    assert view._worker_thread is None
    assert not view._is_worker_running()
    assert view._start_queue_btn.isEnabled()


def test_harvester_view_consecutive_queue_runs_do_not_crash(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Verify running start_queue_harvest twice consecutively does not raise RuntimeError on deleted QThread."""
    mock_service = MagicMock()
    mock_service.list_queue.return_value = []
    mock_service.run_queue.return_value = HarvestRunSummary(
        tasks_completed=1,
        tasks_skipped=0,
        tasks_failed=0,
        pklz_files_created=(),
        was_stopped=False,
    )
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(paths=WorkspacePaths.default(tmp_path))

    view = HarvesterView(harvester_service=mock_service, settings_repo=mock_settings_repo)

    # First run
    view.start_queue_harvest()
    _wait_for_thread_completion(qapp, view)

    assert view._worker_thread is None
    assert view._worker is None
    assert mock_service.run_queue.call_count == 1

    # Second consecutive run (previously raised RuntimeError: Internal C++ object already deleted)
    view.start_queue_harvest()
    _wait_for_thread_completion(qapp, view)

    assert view._worker_thread is None
    assert view._worker is None
    assert mock_service.run_queue.call_count == 2


def test_harvester_view_consecutive_disk_builds_do_not_crash(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Verify running start_disk_build twice consecutively cleans up QThread references cleanly."""
    mock_service = MagicMock()
    mock_service.list_queue.return_value = []
    mock_service.run_local_disk_folder.return_value = []
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(paths=WorkspacePaths.default(tmp_path))

    view = HarvesterView(harvester_service=mock_service, settings_repo=mock_settings_repo)
    view._disk_folder_edit.setText(str(tmp_path))
    view._disk_prefix_edit.setText("test_batch")

    # First disk build run
    view.start_disk_build()
    _wait_for_thread_completion(qapp, view)

    assert view._worker_thread is None
    assert view._worker is None
    assert mock_service.run_local_disk_folder.call_count == 1

    # Second consecutive disk build run
    view.start_disk_build()
    _wait_for_thread_completion(qapp, view)

    assert view._worker_thread is None
    assert view._worker is None
    assert mock_service.run_local_disk_folder.call_count == 2


def test_dependency_doctor_view_consecutive_diagnostics_do_not_crash(
    qapp: QApplication,
) -> None:
    """Verify DependencyDoctorView resets _worker_thread on completion and supports re-entrant runs."""
    ok_status = DependencyStatus(name="mock", state=DependencyState.OK)
    mock_doctor_service = MagicMock()
    mock_doctor_service.check_binary.return_value = ok_status
    mock_doctor_service.check_audfprint.return_value = ok_status
    mock_doctor_service.check_python_packages.return_value = ok_status

    view = DependencyDoctorView(doctor_service=mock_doctor_service)
    assert not view._is_worker_running()

    # First diagnostic run
    view.start_diagnostics()
    _wait_for_thread_completion(qapp, view)

    assert view._worker_thread is None
    assert view._worker is None
    assert mock_doctor_service.check_audfprint.call_count == 1

    # Second consecutive diagnostic run
    view.start_diagnostics()
    _wait_for_thread_completion(qapp, view)

    assert view._worker_thread is None
    assert view._worker is None
    assert mock_doctor_service.check_audfprint.call_count == 2
