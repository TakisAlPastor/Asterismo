"""Harvester View for URL queue management, crawling, downloading, and local .pklz compilation."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.config.constants import MIN_POSITIVE_INT
from src.core.config.models import (
    AppSettings,
    BrowserCookieSource,
    HarvestTask,
    HarvestTaskStatus,
)
from src.core.database.settings_repository import SettingsRepository
from src.core.harvester.service import HarvesterService, HarvestRunSummary
from src.ui.theme import (
    CARD_BORDER_RADIUS,
    FONT_SIZE_BODY,
    FONT_SIZE_HEADER,
    FONT_SIZE_SMALL,
    FONT_SIZE_SUBHEADER,
    FONT_SIZE_TITLE,
    LAYOUT_MARGIN_STD,
    LAYOUT_SPACING_STD,
)
from src.ui.workers.harvester_worker import HarvesterWorker

logger = logging.getLogger(__name__)


class HarvesterView(QWidget):
    """View managing URL queue harvesting (US-2.1, US-2.3) and disk audio .pklz building (US-2.2)."""

    log_requested = Signal(str)
    catalog_updated = Signal()

    def __init__(
        self,
        harvester_service: HarvesterService,
        settings_repo: SettingsRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = harvester_service
        self._settings_repo = settings_repo
        self._worker_thread: QThread | None = None
        self._worker: HarvesterWorker | None = None

        self._init_ui()
        self.refresh_queue_table()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(
            LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD
        )
        main_layout.setSpacing(LAYOUT_SPACING_STD)

        # Title
        title_label = QLabel("Archive Harvester & .pklz Builder", self)
        title_label.setProperty("class", "title")
        main_layout.addWidget(title_label)

        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(LAYOUT_SPACING_STD)

        # ----------------------------------------------------------------------
        # Card 1: URL & Web Directory Harvesting Queue (US-2.1, US-2.3)
        # ----------------------------------------------------------------------
        queue_card = QFrame(container)
        queue_card.setProperty("class", "card")
        queue_layout = QVBoxLayout(queue_card)

        queue_title = QLabel("URL & Open Directory Queue", queue_card)
        queue_title.setProperty("class", "header")
        queue_layout.addWidget(queue_title)

        queue_desc = QLabel(
            "Batch ingest YouTube channels/playlists, Archive.org items, or open HTTP web directories. "
            "Supports channel @handles, deduplication against previously compiled items, and cookie authentication.",
            queue_card,
        )
        queue_desc.setProperty("class", "muted")
        queue_layout.addWidget(queue_desc)

        # Add Links Row
        add_row = QHBoxLayout()
        self._url_input = QLineEdit(queue_card)
        self._url_input.setPlaceholderText(
            "Enter URL, @handle, or open directory link (space/newline separated)...")
        add_row.addWidget(self._url_input, stretch=3)

        self._add_btn = QPushButton("Add to Queue", queue_card)
        self._add_btn.setProperty("class", "primary")
        self._add_btn.clicked.connect(self.on_add_links)
        add_row.addWidget(self._add_btn)

        self._import_btn = QPushButton("Import .txt...", queue_card)
        self._import_btn.clicked.connect(self.on_import_links)
        add_row.addWidget(self._import_btn)

        self._export_btn = QPushButton("Export .txt...", queue_card)
        self._export_btn.clicked.connect(self.on_export_links)
        add_row.addWidget(self._export_btn)

        queue_layout.addLayout(add_row)

        # Quick Options Bar
        opts_row = QHBoxLayout()
        opts_row.setSpacing(LAYOUT_SPACING_STD)
        opts_row.addWidget(QLabel("Browser Cookies:", queue_card))
        self._cookie_combo = QComboBox(queue_card)
        self._cookie_combo.addItems([e.value for e in BrowserCookieSource])
        settings = self._settings_repo.load()
        self._cookie_combo.setCurrentText(settings.cookies_browser.value)
        self._cookie_combo.currentTextChanged.connect(
            self.on_cookie_source_changed)
        opts_row.addWidget(self._cookie_combo)

        self._keep_cb = QCheckBox("Keep Downloaded Audio", queue_card)
        self._keep_cb.setChecked(settings.keep_downloaded_audio)
        self._keep_cb.toggled.connect(self.on_keep_audio_changed)
        opts_row.addWidget(self._keep_cb)

        self._split_cb = QCheckBox("Split Long Audio", queue_card)
        self._split_cb.setChecked(settings.split_long_audio)
        self._split_cb.toggled.connect(self.on_split_option_changed)
        opts_row.addWidget(self._split_cb)

        self._threshold_label = QLabel("Threshold (s):", queue_card)
        opts_row.addWidget(self._threshold_label)
        self._threshold_spin = QSpinBox(queue_card)
        self._threshold_spin.setRange(MIN_POSITIVE_INT, 86400)
        self._threshold_spin.setValue(settings.split_threshold_seconds)
        self._threshold_spin.setEnabled(settings.split_long_audio)
        self._threshold_spin.valueChanged.connect(self.on_threshold_changed)
        opts_row.addWidget(self._threshold_spin)

        self._segment_label = QLabel("Segment (s):", queue_card)
        opts_row.addWidget(self._segment_label)
        self._segment_spin = QSpinBox(queue_card)
        self._segment_spin.setRange(MIN_POSITIVE_INT, 86400)
        self._segment_spin.setValue(settings.split_segment_seconds)
        self._segment_spin.setEnabled(settings.split_long_audio)
        self._segment_spin.valueChanged.connect(self.on_segment_changed)
        opts_row.addWidget(self._segment_spin)

        opts_row.addStretch()

        self._remove_btn = QPushButton("Remove Selected", queue_card)
        self._remove_btn.clicked.connect(self.on_remove_selected)
        opts_row.addWidget(self._remove_btn)

        self._clear_done_btn = QPushButton("Clear Completed", queue_card)
        self._clear_done_btn.clicked.connect(self.on_clear_completed)
        opts_row.addWidget(self._clear_done_btn)

        queue_layout.addLayout(opts_row)

        # Queue Table
        self._queue_table = QTableWidget(queue_card)
        self._queue_table.setColumnCount(7)
        self._queue_table.setHorizontalHeaderLabels([
            "ID",
            "URL / Source",
            "Type",
            "Prefix",
            "Status",
            "Progress",
            "Details",
        ])
        self._queue_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self._queue_table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Stretch)
        self._queue_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._queue_table.setMinimumHeight(220)
        queue_layout.addWidget(self._queue_table)

        # Execution Controls
        ctrl_row = QHBoxLayout()
        self._start_queue_btn = QPushButton("Start Queue", queue_card)
        self._start_queue_btn.setProperty("class", "primary")
        self._start_queue_btn.clicked.connect(self.start_queue_harvest)
        ctrl_row.addWidget(self._start_queue_btn)

        self._pause_btn = QPushButton("Pause", queue_card)
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self.on_toggle_pause)
        ctrl_row.addWidget(self._pause_btn)

        self._skip_btn = QPushButton("Skip Link", queue_card)
        self._skip_btn.setEnabled(False)
        self._skip_btn.clicked.connect(self.on_skip_link)
        ctrl_row.addWidget(self._skip_btn)

        self._stop_btn = QPushButton("Stop", queue_card)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.on_stop)
        ctrl_row.addWidget(self._stop_btn)

        ctrl_row.addStretch()
        queue_layout.addLayout(ctrl_row)

        container_layout.addWidget(queue_card)

        # ----------------------------------------------------------------------
        # Card 2: Local Audio on Disk -> .pklz Builder (US-2.2)
        # ----------------------------------------------------------------------
        disk_card = QFrame(container)
        disk_card.setProperty("class", "card")
        disk_layout = QVBoxLayout(disk_card)

        disk_title = QLabel("Audio on Disk → .pklz Compiler", disk_card)
        disk_title.setProperty("class", "header")
        disk_layout.addWidget(disk_title)

        disk_desc = QLabel(
            "Compile an existing local folder of audio files directly into .pklz acoustic landmark databases "
            "with optional in-place slicing and automatic registration in the Database Catalog.",
            disk_card,
        )
        disk_desc.setProperty("class", "muted")
        disk_layout.addWidget(disk_desc)

        disk_row = QHBoxLayout()
        disk_row.addWidget(QLabel("Source Folder:", disk_card))
        self._disk_folder_edit = QLineEdit(disk_card)
        self._disk_folder_edit.setText(str(settings.paths.input_dir))
        disk_row.addWidget(self._disk_folder_edit, stretch=2)

        self._disk_browse_btn = QPushButton("Browse...", disk_card)
        self._disk_browse_btn.clicked.connect(self.on_browse_disk_folder)
        disk_row.addWidget(self._disk_browse_btn)

        disk_row.addWidget(QLabel("Archive Prefix:", disk_card))
        self._disk_prefix_edit = QLineEdit(disk_card)
        self._disk_prefix_edit.setPlaceholderText(
            "e.g. @MyArchive or Album_Name")
        disk_row.addWidget(self._disk_prefix_edit, stretch=1)

        self._run_disk_btn = QPushButton(
            "Build .pklz from Disk Folder", disk_card)
        self._run_disk_btn.clicked.connect(self.start_disk_build)
        disk_row.addWidget(self._run_disk_btn)

        disk_layout.addLayout(disk_row)
        container_layout.addWidget(disk_card)

        # ----------------------------------------------------------------------
        # Section 3: Live Progress & Backoff Banner
        # ----------------------------------------------------------------------
        progress_card = QFrame(container)
        progress_card.setProperty("class", "card")
        progress_layout = QVBoxLayout(progress_card)

        self._status_label = QLabel("Ready", progress_card)
        self._status_label.setStyleSheet("font-weight: 600;")
        progress_layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar(progress_card)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        progress_layout.addWidget(self._progress_bar)

        self._backoff_banner = QLabel("", progress_card)
        self._backoff_banner.setStyleSheet(
            "color: #f59e0b; font-weight: bold;")
        self._backoff_banner.setVisible(False)
        progress_layout.addWidget(self._backoff_banner)

        container_layout.addWidget(progress_card)

        scroll_area.setWidget(container)
        main_layout.addWidget(scroll_area)

    # --------------------------------------------------------------------------
    # UI Slots & Event Handlers
    # --------------------------------------------------------------------------

    @Slot()
    def on_add_links(self) -> None:
        """Add input links to the queue and refresh table."""
        text = self._url_input.text().strip()
        if not text:
            return

        added = self._service.add_links([text])
        if added:
            self._url_input.clear()
            self.refresh_queue_table()
            self.log_requested.emit(
                f"Added {len(added)} link(s) to harvester queue.")

    @Slot()
    def on_import_links(self) -> None:
        """Open file dialog to import links from a .txt file."""
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Import Links from File", "", "Text Files (*.txt);;All Files (*)")
        if chosen:
            added = self._service.import_links_from_file(Path(chosen))
            self.refresh_queue_table()
            self.log_requested.emit(
                f"Imported {len(added)} link(s) from {chosen}.")

    @Slot()
    def on_export_links(self) -> None:
        """Export all URLs in the queue to a .txt file."""
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export Queue Links", "harvest_queue.txt", "Text Files (*.txt)")
        if chosen:
            exported_count = self._service.export_links_to_file(Path(chosen))
            self.log_requested.emit(
                f"Exported {exported_count} link(s) to {chosen}.")

    @Slot()
    def on_remove_selected(self) -> None:
        """Remove highlighted rows from the queue table and SQLite repository."""
        selected_rows = sorted(
            set(idx.row() for idx in self._queue_table.selectedIndexes()), reverse=True)
        if not selected_rows:
            return

        for row in selected_rows:
            id_item = self._queue_table.item(row, 0)
            if id_item:
                task_id = int(id_item.text())
                self._service.remove_task(task_id)

        self.refresh_queue_table()
        self.log_requested.emit(
            f"Removed {len(selected_rows)} task(s) from queue.")

    @Slot()
    def on_clear_completed(self) -> None:
        """Purge completed and skipped tasks from the queue."""
        removed = self._service.clear_completed_tasks()
        self.refresh_queue_table()
        self.log_requested.emit(f"Cleared {removed} completed task(s).")

    @Slot(str)
    def on_cookie_source_changed(self, cookie_text: str) -> None:
        """Persist updated browser cookie source selection."""
        try:
            source = BrowserCookieSource(cookie_text)
            current = self._settings_repo.load()
            updated = replace(current, cookies_browser=source)
            self._settings_repo.save(updated)
            self.log_requested.emit(
                f"Updated browser cookies source to '{source.value}'.")
        except Exception as exc:
            logger.warning("Failed updating cookie source: %s", exc)

    @Slot(bool)
    def on_split_option_changed(self, enabled: bool) -> None:
        """Persist long-audio splitting checkbox preference and toggle threshold inputs."""
        self._threshold_spin.setEnabled(enabled)
        self._segment_spin.setEnabled(enabled)
        current = self._settings_repo.load()
        updated = replace(current, split_long_audio=enabled)
        self._settings_repo.save(updated)

    @Slot(int)
    def on_threshold_changed(self, value: int) -> None:
        """Persist split threshold seconds ensuring segment size does not exceed threshold."""
        if self._segment_spin.value() > value:
            self._segment_spin.setValue(value)
        current = self._settings_repo.load()
        if value < current.split_segment_seconds:
            updated = replace(
                current,
                split_threshold_seconds=value,
                split_segment_seconds=value,
            )
        else:
            updated = replace(current, split_threshold_seconds=value)
        self._settings_repo.save(updated)

    @Slot(int)
    def on_segment_changed(self, value: int) -> None:
        """Persist split segment seconds ensuring threshold is at least segment size."""
        if self._threshold_spin.value() < value:
            self._threshold_spin.setValue(value)
        current = self._settings_repo.load()
        if value > current.split_threshold_seconds:
            updated = replace(
                current,
                split_threshold_seconds=value,
                split_segment_seconds=value,
            )
        else:
            updated = replace(current, split_segment_seconds=value)
        self._settings_repo.save(updated)

    @Slot(bool)
    def on_keep_audio_changed(self, enabled: bool) -> None:
        """Persist keep downloaded audio preference."""
        current = self._settings_repo.load()
        updated = replace(current, keep_downloaded_audio=enabled)
        self._settings_repo.save(updated)

    @Slot()
    def on_browse_disk_folder(self) -> None:
        """Select a local directory of audio files for building .pklz databases."""
        chosen = QFileDialog.getExistingDirectory(
            self, "Select Audio Folder", self._disk_folder_edit.text())
        if chosen:
            self._disk_folder_edit.setText(chosen)
            if not self._disk_prefix_edit.text().strip():
                self._disk_prefix_edit.setText(Path(chosen).name)

    # --------------------------------------------------------------------------
    # Pipeline Execution Triggers & Worker Thread Management
    # --------------------------------------------------------------------------

    @Slot()
    def start_queue_harvest(self) -> None:
        """Launch the background HarvesterWorker to process the URL queue."""
        if self._worker_thread and self._worker_thread.isRunning():
            return

        self._set_controls_running(True)
        self._backoff_banner.setVisible(False)
        self.log_requested.emit("Initiating Harvester queue execution...")

        self._worker_thread = QThread(self)
        self._worker = HarvesterWorker(self._service, mode="queue")
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.log_emitted.connect(self.log_requested)
        self._worker.status_changed.connect(self._status_label.setText)
        self._worker.progress_updated.connect(self.on_worker_progress)
        self._worker.task_updated.connect(self.on_worker_task_updated)
        self._worker.backoff_triggered.connect(self.on_worker_backoff)
        self._worker.queue_completed.connect(self.on_queue_finished)
        self._worker.error_occurred.connect(
            lambda err: self.log_requested.emit(f"[X] Harvester Error: {err}"))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(
            lambda: self._set_controls_running(False))

        self._worker_thread.start()

    @Slot()
    def start_disk_build(self) -> None:
        """Launch background HarvesterWorker to process local disk folder into .pklz."""
        if self._worker_thread and self._worker_thread.isRunning():
            return

        folder_str = self._disk_folder_edit.text().strip()
        if not folder_str or not Path(folder_str).is_dir():
            self.log_requested.emit(f"[X] Invalid folder path: {folder_str}")
            return

        folder = Path(folder_str)
        prefix = self._disk_prefix_edit.text().strip() or folder.name

        self._set_controls_running(True)
        self._backoff_banner.setVisible(False)
        self.log_requested.emit(
            f"Initiating local audio compilation from '{folder}'...")

        self._worker_thread = QThread(self)
        self._worker = HarvesterWorker(
            self._service,
            mode="disk",
            disk_folder=folder,
            disk_prefix=prefix,
        )
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.log_emitted.connect(self.log_requested)
        self._worker.status_changed.connect(self._status_label.setText)
        self._worker.progress_updated.connect(self.on_worker_progress)
        self._worker.disk_completed.connect(self.on_disk_finished)
        self._worker.error_occurred.connect(
            lambda err: self.log_requested.emit(f"[X] Disk Build Error: {err}"))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(
            lambda: self._set_controls_running(False))

        self._worker_thread.start()

    @Slot()
    def on_toggle_pause(self) -> None:
        """Toggle pipeline pause/resume state."""
        if self._service.is_paused:
            self._service.resume()
            self._pause_btn.setText("Pause")
            self.log_requested.emit("Harvester resumed.")
        else:
            self._service.pause()
            self._pause_btn.setText("Resume")
            self.log_requested.emit("Harvester paused.")

    @Slot()
    def on_skip_link(self) -> None:
        """Skip currently active task and advance to next."""
        self._service.skip_current()
        self.log_requested.emit("Requested skip of current link.")

    @Slot()
    def on_stop(self) -> None:
        """Stop the harvester pipeline immediately."""
        self._service.stop()
        self.log_requested.emit("Requested harvester pipeline stop.")

    # --------------------------------------------------------------------------
    # Worker Signal Handlers
    # --------------------------------------------------------------------------

    @Slot(int, int, str)
    def on_worker_progress(self, current: int, total: int, phase: str) -> None:
        """Update progress bar and status text."""
        pct = int(100.0 * current / max(1, total)) if total > 0 else 0
        self._progress_bar.setValue(pct)
        self._status_label.setText(f"{phase} ({current}/{total} - {pct}%)")

    @Slot(int)
    def on_worker_task_updated(self, task_id: int) -> None:
        """Refresh table rows when a task status changes."""
        self.refresh_queue_table()

    @Slot(float, str)
    def on_worker_backoff(self, seconds: float, reason: str) -> None:
        """Display backoff alert banner."""
        self._backoff_banner.setText(
            f"Rate limited: backing off for {seconds:.1f}s ({reason})")
        self._backoff_banner.setVisible(True)

    @Slot(object)
    def on_queue_finished(self, summary: HarvestRunSummary) -> None:
        """Handle queue pipeline completion."""
        self.refresh_queue_table()
        self._progress_bar.setValue(
            100 if not summary.was_stopped else self._progress_bar.value())
        self._status_label.setText(
            "Finished queue execution." if not summary.was_stopped else "Stopped.")
        self._backoff_banner.setVisible(False)
        self.catalog_updated.emit()

    @Slot(list)
    def on_disk_finished(self, results: list) -> None:
        """Handle disk folder .pklz build completion."""
        self._progress_bar.setValue(100)
        self._status_label.setText(
            f"Successfully compiled {len(results)} .pklz database(s).")
        self.catalog_updated.emit()

    # --------------------------------------------------------------------------
    # Table & Control Helpers
    # --------------------------------------------------------------------------

    def refresh_queue_table(self) -> None:
        """Reload all tasks from SQLite into the QTableWidget."""
        tasks = self._service.list_queue()
        self._queue_table.setRowCount(len(tasks))

        for row, task in enumerate(tasks):
            self._queue_table.setItem(row, 0, QTableWidgetItem(str(task.id)))
            self._queue_table.setItem(
                row, 1, QTableWidgetItem(task.normalized_url))
            self._queue_table.setItem(
                row, 2, QTableWidgetItem(task.source_type.value))
            self._queue_table.setItem(
                row, 3, QTableWidgetItem(task.prefix_label))

            status_item = QTableWidgetItem(task.status.value.upper())
            if task.status == HarvestTaskStatus.COMPLETED:
                status_item.setForeground(Qt.GlobalColor.green)
            elif task.status == HarvestTaskStatus.FAILED:
                status_item.setForeground(Qt.GlobalColor.red)
            elif task.status == HarvestTaskStatus.RUNNING:
                status_item.setForeground(Qt.GlobalColor.cyan)
            self._queue_table.setItem(row, 4, status_item)

            progress_str = f"{task.completed_items}/{task.total_items}"
            self._queue_table.setItem(row, 5, QTableWidgetItem(progress_str))

            details = task.error_message or task.title
            self._queue_table.setItem(row, 6, QTableWidgetItem(details))

    def _set_controls_running(self, running: bool) -> None:
        """Update button enabled states during background execution."""
        self._start_queue_btn.setEnabled(not running)
        self._run_disk_btn.setEnabled(not running)
        self._pause_btn.setEnabled(running)
        self._skip_btn.setEnabled(running)
        self._stop_btn.setEnabled(running)
        self._pause_btn.setText("Pause")
