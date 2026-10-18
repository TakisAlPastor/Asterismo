"""Thread-safe background worker for DependencyDoctor operations."""

from __future__ import annotations

from typing import Mapping
from PySide6.QtCore import QObject, Signal, Slot

from src.core.config.constants import (
    DEP_KEY_AUDFPRINT,
    DEP_KEY_FFMPEG,
    DEP_KEY_FFPROBE,
    DEP_KEY_FPCALC,
    DEP_KEY_PYTHON,
    DEP_KEY_WINGET,
)
from src.core.config.models import DependencyStatus
from src.core.process.dependency_doctor import DependencyDoctorService


class DoctorWorker(QObject):
    """Background QObject worker executing diagnostic sweeps and winget provisioning."""

    # Typed Qt 6 signals
    log_emitted = Signal(str)
    check_completed = Signal(dict)
    provision_completed = Signal(bool, str)
    finished = Signal()

    def __init__(self, doctor_service: DependencyDoctorService) -> None:
        super().__init__()
        self._doctor = doctor_service

    @Slot()
    def run_diagnostics(self) -> None:
        """Perform diagnostic sweep across all dependencies without blocking GUI."""
        try:
            self.log_emitted.emit(
                "Starting Dependency Doctor diagnostic sweep...")

            results: dict[str, DependencyStatus] = {}

            # FFmpeg
            self.log_emitted.emit("Checking FFmpeg binary...")
            results[DEP_KEY_FFMPEG] = self._doctor.check_binary(
                DEP_KEY_FFMPEG, "ffmpeg.exe", "-version"
            )
            self._log_status(results[DEP_KEY_FFMPEG])

            # FFprobe
            self.log_emitted.emit("Checking FFprobe binary...")
            results[DEP_KEY_FFPROBE] = self._doctor.check_binary(
                DEP_KEY_FFPROBE, "ffprobe.exe", "-version"
            )
            self._log_status(results[DEP_KEY_FFPROBE])

            # fpcalc
            self.log_emitted.emit("Checking Chromaprint fpcalc binary...")
            results[DEP_KEY_FPCALC] = self._doctor.check_binary(
                DEP_KEY_FPCALC, "fpcalc.exe", "-v"
            )
            self._log_status(results[DEP_KEY_FPCALC])

            # audfprint
            self.log_emitted.emit(
                "Verifying vendored audfprint & Asterismo patches...")
            results[DEP_KEY_AUDFPRINT] = self._doctor.check_audfprint()
            self._log_status(results[DEP_KEY_AUDFPRINT])

            # Python & libraries
            self.log_emitted.emit(
                "Checking Python environment and required libraries...")
            results[DEP_KEY_PYTHON] = self._doctor.check_python_packages()
            self._log_status(results[DEP_KEY_PYTHON])

            # winget
            self.log_emitted.emit("Checking Microsoft winget availability...")
            results[DEP_KEY_WINGET] = self._doctor.check_binary(
                DEP_KEY_WINGET, "winget.exe", "--version"
            )
            self._log_status(results[DEP_KEY_WINGET])

            self.log_emitted.emit("Diagnostic sweep complete.")
            self.check_completed.emit(results)
        except Exception as exc:
            self.log_emitted.emit(f"Error during diagnostic check: {exc}")
        finally:
            self.finished.emit()

    @Slot(str)
    def run_winget_provision(self, package_id: str) -> None:
        """Execute winget silent install for an approved package."""
        try:
            self.log_emitted.emit(
                f"Starting silent installation of {package_id} via winget...")
            result = self._doctor.provision_via_winget(package_id)
            if result.is_success:
                msg = f"Package {package_id} installed successfully via winget."
                self.log_emitted.emit(msg)
                self.provision_completed.emit(True, msg)
            else:
                err_msg = f"Winget returned non-zero code {result.returncode}: {result.stderr.strip()}"
                self.log_emitted.emit(err_msg)
                self.provision_completed.emit(False, err_msg)
        except Exception as exc:
            err_msg = f"Failed to provision {package_id}: {exc}"
            self.log_emitted.emit(err_msg)
            self.provision_completed.emit(False, err_msg)
        finally:
            self.finished.emit()

    def _log_status(self, status: DependencyStatus) -> None:
        """Helper to format and emit clean status messages."""
        path_str = f" [{status.path}]" if status.path else ""
        ver_str = f" ({status.version})" if status.version else ""
        self.log_emitted.emit(
            f"-> {status.name.upper()}: State={status.state.value.upper()}{path_str}{ver_str} - {status.details}"
        )
