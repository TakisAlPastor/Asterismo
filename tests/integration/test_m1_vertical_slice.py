"""End-to-End Vertical Slice integration tests for Milestone 1."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

import pytest

from src.core.config.models import AppSettings, ScanMode, SecretCredentials, ThemeMode
from src.core.config.secrets_store import SecretsStore
from src.core.database.connection import DatabaseManager
from src.core.database.migrations import MigrationManager
from src.core.database.settings_repository import SettingsRepository
from src.core.process.binary_locator import BinaryLocator
from src.core.process.dependency_doctor import DependencyDoctorService
from src.core.process.runner import SilentProcessRunner
from src.main import bootstrap_application
from src.ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Session-level offscreen QApplication fixture for headless UI testing."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestMilestone1VerticalSlice:
    """End-to-end vertical slice verification of M1 UI, SQLite, and Dependency Doctor."""

    def test_bootstrap_and_navigation(self, qapp: QApplication, tmp_path: Path) -> None:
        # Arrange & Act: Bootstrap application in temporary directory
        app, window = bootstrap_application(base_dir=tmp_path)

        # Assert: Shell elements exist and view stack initializes
        assert isinstance(window, MainWindow)
        assert window.view_stack.count() == 4
        assert window.log_console is not None
        assert "Asterismo desktop shell initialized." in window.log_console.get_text()

        # Switch to each view index and ensure no exceptions
        for idx in range(4):
            window._sidebar.setCurrentRow(idx)
            assert window.view_stack.currentIndex() == idx

        window.close()

    def test_settings_view_ui_roundtrip_persistence(self, qapp: QApplication, tmp_path: Path) -> None:
        # Arrange: Bootstrap fresh app
        app, window = bootstrap_application(base_dir=tmp_path)
        settings_view = window.settings_view

        # Act: Fill modified values in UI widgets
        new_input = tmp_path / "custom_input"
        new_db = tmp_path / "custom_db"
        new_archive = tmp_path / "custom_archive"
        settings_view._input_dir_edit.setText(str(new_input))
        settings_view._db_dir_edit.setText(str(new_db))
        settings_view._archive_dir_edit.setText(str(new_archive))
        settings_view._theme_combo.setCurrentText(ThemeMode.LIGHT.value)
        settings_view._scan_combo.setCurrentText(ScanMode.LONG.value)
        settings_view._downloads_spin.setValue(6)
        settings_view._workers_spin.setValue(8)

        settings_view._acoustid_edit.setText("test_acoustid_key_999")
        settings_view._audiotag_edit.setText("test_audiotag_key_888")
        settings_view._discord_edit.setText(
            "https://discord.com/api/webhooks/test")

        # Save via UI button slot
        settings_view.save_data()
        window.close()

        # Assert 1: Re-instantiate a fresh window on the SAME directory and assert persistence
        app2, window2 = bootstrap_application(base_dir=tmp_path)
        sv2 = window2.settings_view

        assert sv2._input_dir_edit.text() == str(new_input)
        assert sv2._db_dir_edit.text() == str(new_db)
        assert sv2._archive_dir_edit.text() == str(new_archive)
        assert sv2._theme_combo.currentText() == ThemeMode.LIGHT.value
        assert sv2._scan_combo.currentText() == ScanMode.LONG.value
        assert sv2._downloads_spin.value() == 6
        assert sv2._workers_spin.value() == 8

        assert sv2._acoustid_edit.text() == "test_acoustid_key_999"
        assert sv2._audiotag_edit.text() == "test_audiotag_key_888"
        assert sv2._discord_edit.text() == "https://discord.com/api/webhooks/test"

        window2.close()

    def test_dependency_doctor_modal_diagnostics(self, qapp: QApplication, tmp_path: Path) -> None:
        # Arrange
        app, window = bootstrap_application(base_dir=tmp_path)
        settings_view = window.settings_view

        # Instantiate DependencyDoctorDialog with doctor_service from settings
        from src.ui.views.dependency_doctor_view import DependencyDoctorDialog
        assert settings_view._doctor_service is not None
        dialog = DependencyDoctorDialog(settings_view._doctor_service, settings_view)
        doctor_view = dialog.doctor_view

        # Act: Trigger diagnostics and wait for worker thread completion via Qt event loop
        loop = QEventLoop()
        doctor_view.start_diagnostics()

        assert doctor_view._worker_thread is not None
        doctor_view._worker_thread.finished.connect(loop.quit)

        QTimer.singleShot(20000, loop.quit)
        loop.exec()

        # Assert: Check that 3 binary cards received updated status
        ffmpeg_card = doctor_view.get_card("ffmpeg")
        ffprobe_card = doctor_view.get_card("ffprobe")
        fpcalc_card = doctor_view.get_card("fpcalc")

        assert ffmpeg_card is not None
        assert ffprobe_card is not None
        assert fpcalc_card is not None

        # Verify badge text updated from initial UNKNOWN
        assert ffmpeg_card._badge.text() in ("OK", "MISSING", "BROKEN")
        assert ffprobe_card._badge.text() in ("OK", "MISSING", "BROKEN")
        assert fpcalc_card._badge.text() in ("OK", "MISSING", "BROKEN")

        dialog.close()
        window.close()

