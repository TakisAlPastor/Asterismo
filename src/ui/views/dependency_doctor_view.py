"""Dependency Doctor dashboard view and dialog for checking and repairing external tools."""

from __future__ import annotations

from typing import Mapping
from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.core.config.constants import (
    DEP_KEY_FFMPEG,
    DEP_KEY_FFPROBE,
    DEP_KEY_FPCALC,
    DEP_KEY_WINGET,
    WINGET_CHROMAPRINT_PACKAGE_ID,
    WINGET_FFMPEG_PACKAGE_ID,
)
from src.core.config.models import DependencyState, DependencyStatus
from src.core.process.dependency_doctor import DependencyDoctorService
from src.ui.theme import (
    CARD_BORDER_RADIUS,
    DARK_PALETTE,
    FONT_SIZE_BODY,
    FONT_SIZE_HEADER,
    FONT_SIZE_SMALL,
    FONT_SIZE_SUBHEADER,
    FONT_SIZE_TITLE,
    LAYOUT_MARGIN_STD,
    LAYOUT_SPACING_STD,
)
from src.ui.workers.doctor_worker import DoctorWorker


class DependencyCard(QFrame):
    """Visual card displaying the diagnostic state of an external dependency."""

    def __init__(self, key: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setProperty("class", "card")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD)
        layout.setSpacing(6)

        # Header: Name and Status Badge
        top_layout = QHBoxLayout()
        self._title_label = QLabel(title, self)
        self._title_label.setStyleSheet(
            f"font-weight: 600; font-size: {FONT_SIZE_SUBHEADER}px;")
        top_layout.addWidget(self._title_label)
        top_layout.addStretch()

        self._badge = QLabel("UNKNOWN", self)
        self._badge.setStyleSheet(
            f"background-color: #64748b; color: #ffffff; padding: 2px 8px; "
            f"border-radius: {CARD_BORDER_RADIUS // 2}px; font-weight: bold; font-size: {FONT_SIZE_SMALL}px;"
        )
        top_layout.addWidget(self._badge)
        layout.addLayout(top_layout)

        # Path details
        self._path_label = QLabel("Path: Not checked", self)
        self._path_label.setStyleSheet(
            f"color: #94a3b8; font-size: {FONT_SIZE_SMALL}px;")
        layout.addWidget(self._path_label)

        # Version & Info details
        self._version_label = QLabel("Version: -", self)
        self._version_label.setStyleSheet(f"font-size: {FONT_SIZE_SMALL}px;")
        layout.addWidget(self._version_label)

        # Status text
        self._details_label = QLabel("", self)
        self._details_label.setWordWrap(True)
        self._details_label.setStyleSheet(
            f"color: #cbd5e1; font-size: {FONT_SIZE_SMALL}px;")
        layout.addWidget(self._details_label)

    def update_status(self, status: DependencyStatus) -> None:
        """Update the card with fresh status data."""
        self._badge.setText(status.state.value.upper())
        if status.state == DependencyState.OK:
            bg_color = DARK_PALETTE.status_ok
        elif status.state == DependencyState.MISSING:
            bg_color = DARK_PALETTE.status_missing
        else:
            bg_color = DARK_PALETTE.status_broken

        self._badge.setStyleSheet(
            f"background-color: {bg_color}; color: #ffffff; padding: 2px 8px; "
            f"border-radius: {CARD_BORDER_RADIUS // 2}px; font-weight: bold; font-size: {FONT_SIZE_SMALL}px;"
        )

        path_text = f"Path: {status.path}" if status.path else "Path: None"
        self._path_label.setText(path_text)
        self._version_label.setText(f"Version: {status.version or 'N/A'}")
        self._details_label.setText(status.details)


class DependencyDoctorView(QWidget):
    """Focused diagnostic view displaying only external binary tools (US-1.2)."""

    log_requested = Signal(str)

    def __init__(
        self,
        doctor_service: DependencyDoctorService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._doctor_service = doctor_service
        self._worker_thread: QThread | None = None
        self._worker: DoctorWorker | None = None
        self._cards: dict[str, DependencyCard] = {}
        self._last_results: dict[str, DependencyStatus] = {}

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(
            LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD)
        main_layout.setSpacing(LAYOUT_SPACING_STD)

        # Title & Subtitle
        title_label = QLabel("External Binary Dependencies", self)
        title_label.setProperty("class", "title")
        main_layout.addWidget(title_label)

        desc_label = QLabel(
            "Verifies external CLI binaries required for audio extraction, splitting, and acoustic fingerprinting. "
            "Provisioning uses Microsoft winget CLI to install safe, official packages without antivirus conflicts.",
            self,
        )
        desc_label.setProperty("class", "muted")
        main_layout.addWidget(desc_label)

        # Action Toolbar
        action_layout = QHBoxLayout()
        self._run_check_btn = QPushButton("Run Binary Check", self)
        self._run_check_btn.setProperty("class", "primary")
        self._run_check_btn.clicked.connect(self.start_diagnostics)
        action_layout.addWidget(self._run_check_btn)

        self._winget_ffmpeg_btn = QPushButton("Install FFmpeg (winget)", self)
        self._winget_ffmpeg_btn.setEnabled(False)
        self._winget_ffmpeg_btn.clicked.connect(
            lambda: self.start_winget_provision(WINGET_FFMPEG_PACKAGE_ID))
        action_layout.addWidget(self._winget_ffmpeg_btn)

        self._winget_fpcalc_btn = QPushButton(
            "Install Chromaprint (winget)", self)
        self._winget_fpcalc_btn.setEnabled(False)
        self._winget_fpcalc_btn.clicked.connect(
            lambda: self.start_winget_provision(WINGET_CHROMAPRINT_PACKAGE_ID))
        action_layout.addWidget(self._winget_fpcalc_btn)

        action_layout.addStretch()
        main_layout.addLayout(action_layout)

        # Cards Scroll Area
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        grid_layout = QGridLayout(container)
        grid_layout.setSpacing(LAYOUT_SPACING_STD)

        # 3 external binary dependencies only (clean UX)
        components = [
            (DEP_KEY_FFMPEG, "FFmpeg Audio Engine", 0, 0),
            (DEP_KEY_FFPROBE, "FFprobe Media Analyzer", 0, 1),
            (DEP_KEY_FPCALC, "Chromaprint fpcalc (AcoustID)", 1, 0),
        ]

        for key, title, row, col in components:
            card = DependencyCard(key, title, container)
            self._cards[key] = card
            grid_layout.addWidget(card, row, col)

        scroll_area.setWidget(container)
        main_layout.addWidget(scroll_area)

    @Slot()
    def start_diagnostics(self) -> None:
        """Spawn DoctorWorker in background QThread to run binary diagnostic check."""
        if self._worker_thread and self._worker_thread.isRunning():
            return

        self._set_actions_enabled(False)
        self.log_requested.emit(
            "Initiating binary dependency diagnostic sweep...")

        self._worker_thread = QThread(self)
        self._worker = DoctorWorker(self._doctor_service)
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run_diagnostics)
        self._worker.log_emitted.connect(self.log_requested)
        self._worker.check_completed.connect(self.on_check_completed)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker._doctor_worker_cleanup if hasattr(self._worker, "_doctor_worker_cleanup") else self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(
            lambda: self._set_actions_enabled(True))

        self._worker_thread.start()

    @Slot(str)
    def start_winget_provision(self, package_id: str) -> None:
        """Spawn background worker to provision an approved package via winget."""
        if self._worker_thread and self._worker_thread.isRunning():
            return

        self._set_actions_enabled(False)
        self.log_requested.emit(
            f"Spawning winget installer for {package_id}...")

        self._worker_thread = QThread(self)
        self._worker = DoctorWorker(self._doctor_service)
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(
            lambda: self._worker.run_winget_provision(package_id))
        self._worker.log_emitted.connect(self.log_requested)
        self._worker.provision_completed.connect(self.on_provision_completed)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(
            lambda: self._set_actions_enabled(True))

        self._worker_thread.start()

    @Slot(dict)
    def on_check_completed(self, results: dict[str, DependencyStatus]) -> None:
        """Handle completed diagnostic status results on the main thread."""
        self._last_results = results
        for key, status in results.items():
            if key in self._cards:
                self._cards[key].update_status(status)

        # Update winget button states based on diagnostics
        winget_ok = results.get(DEP_KEY_WINGET, DependencyStatus(
            name=DEP_KEY_WINGET, state=DependencyState.MISSING)).is_ok
        ffmpeg_ok = results.get(DEP_KEY_FFMPEG, DependencyStatus(
            name=DEP_KEY_FFMPEG, state=DependencyState.MISSING)).is_ok
        fpcalc_ok = results.get(DEP_KEY_FPCALC, DependencyStatus(
            name=DEP_KEY_FPCALC, state=DependencyState.MISSING)).is_ok

        self._winget_ffmpeg_btn.setEnabled(winget_ok and not ffmpeg_ok)
        self._winget_fpcalc_btn.setEnabled(winget_ok and not fpcalc_ok)

    @Slot(bool, str)
    def on_provision_completed(self, success: bool, message: str) -> None:
        """Handle completion of winget provisioning and re-run check if successful."""
        if success:
            self.start_diagnostics()

    def _set_actions_enabled(self, enabled: bool) -> None:
        self._run_check_btn.setEnabled(enabled)
        if not enabled:
            self._winget_ffmpeg_btn.setEnabled(False)
            self._winget_fpcalc_btn.setEnabled(False)

    def get_card(self, key: str) -> DependencyCard | None:
        """Card accessor helper for testing."""
        return self._cards.get(key)


class DependencyDoctorDialog(QDialog):
    """Modal dialog wrapper for DependencyDoctorView accessible from Settings."""

    def __init__(
        self,
        doctor_service: DependencyDoctorService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("System Diagnostics & External Binaries")
        self.resize(760, 480)

        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(0, 0, 0, 0)

        self._view = DependencyDoctorView(doctor_service, self)
        dialog_layout.addWidget(self._view)

    @property
    def doctor_view(self) -> DependencyDoctorView:
        return self._view
