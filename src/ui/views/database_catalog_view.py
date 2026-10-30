"""Database Catalog View for inspecting, auditing, and synchronizing .pklz reference databases."""
from __future__ import annotations

import logging
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.config.models import PklzCatalogEntry
from src.core.database.settings_repository import SettingsRepository
from src.core.fingerprinter.catalog_service import CatalogService, ReconciliationResult
from src.ui.theme import (
    CARD_BORDER_RADIUS,
    FONT_SIZE_BODY,
    FONT_SIZE_HEADER,
    FONT_SIZE_SMALL,
    FONT_SIZE_SUBHEADER,
    FONT_SIZE_TITLE,
    LAYOUT_MARGIN_STD,
    LAYOUT_SPACING_SMALL,
    LAYOUT_SPACING_STD,
)
from src.ui.workers.catalog_worker import CatalogWorker

logger = logging.getLogger(__name__)


def _format_size(bytes_val: int) -> str:
    """Format bytes into readable size representation."""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"


class DatabaseCatalogView(QWidget):
    """View managing .pklz archive discovery, integrity auditing, and track introspection (US-3.1)."""

    log_requested = Signal(str)

    def __init__(
        self,
        catalog_service: CatalogService,
        settings_repo: SettingsRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = catalog_service
        self._settings_repo = settings_repo
        self._worker_thread: QThread | None = None
        self._worker: CatalogWorker | None = None
        self._entries: list[PklzCatalogEntry] = []

        self._init_ui()
        self.refresh_catalog()

    def _init_ui(self) -> None:
        """Construct view layout and child widgets."""
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD
        )
        root_layout.setSpacing(LAYOUT_SPACING_STD)

        # 1. Header & Summary Cards
        self._init_header(root_layout)
        self._init_summary_cards(root_layout)

        # 2. Controls Toolbar (Directory picker, search, action buttons)
        self._init_controls(root_layout)

        # 3. Main Splitter: Tree on left, Track inspector on right
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left Container: Catalog Tree
        left_widget = QWidget(splitter)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(LAYOUT_SPACING_SMALL)

        self._tree_widget = QTreeWidget(left_widget)
        self._tree_widget.setHeaderLabels(
            ["File Name", "Prefix / Collection", "Tracks", "Hashes", "Size", "SHA256"])
        self._tree_widget.setAlternatingRowColors(True)
        self._tree_widget.setRootIsDecorated(False)
        self._tree_widget.setSortingEnabled(True)
        header = self._tree_widget.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._tree_widget.itemSelectionChanged.connect(
            self._on_tree_selection_changed)
        left_layout.addWidget(self._tree_widget)
        splitter.addWidget(left_widget)

        # Right Container: Track List Drawer
        right_widget = QFrame(splitter)
        right_widget.setObjectName("card")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(
            LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD)
        right_layout.setSpacing(LAYOUT_SPACING_SMALL)

        self._details_title = QLabel("Database Details & Tracks", right_widget)
        self._details_title.setStyleSheet(
            f"font-size: {FONT_SIZE_SUBHEADER}px; font-weight: bold;")
        right_layout.addWidget(self._details_title)

        self._details_path_label = QLabel(
            "Select a database from the table to inspect.", right_widget)
        self._details_path_label.setWordWrap(True)
        self._details_path_label.setStyleSheet(
            f"font-size: {FONT_SIZE_SMALL}px; color: #94a3b8;")
        right_layout.addWidget(self._details_path_label)

        self._tracks_count_label = QLabel("Registered Tracks: -", right_widget)
        self._tracks_count_label.setStyleSheet(
            f"font-size: {FONT_SIZE_BODY}px; font-weight: 600;")
        right_layout.addWidget(self._tracks_count_label)

        self._tracks_list = QListWidget(right_widget)
        self._tracks_list.setAlternatingRowColors(True)
        right_layout.addWidget(self._tracks_list)

        splitter.addWidget(right_widget)
        splitter.setSizes([650, 350])
        root_layout.addWidget(splitter, 1)

    def _init_header(self, parent_layout: QVBoxLayout) -> None:
        header_widget = QWidget(self)
        layout = QVBoxLayout(header_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        title = QLabel("Database Catalog", header_widget)
        title.setStyleSheet(
            f"font-size: {FONT_SIZE_TITLE}px; font-weight: bold;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Inspect, audit integrity, and index local .pklz acoustic fingerprint archives.", header_widget
        )
        subtitle.setStyleSheet(
            f"font-size: {FONT_SIZE_BODY}px; color: #94a3b8;")
        layout.addWidget(subtitle)

        parent_layout.addWidget(header_widget)

    def _init_summary_cards(self, parent_layout: QVBoxLayout) -> None:
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(LAYOUT_SPACING_STD)

        self._card_databases = self._create_metric_card("Databases", "0")
        self._card_tracks = self._create_metric_card("Total Tracks", "0")
        self._card_hashes = self._create_metric_card("Landmark Hashes", "0")
        self._card_size = self._create_metric_card("Total Size", "0 B")

        cards_layout.addWidget(self._card_databases[0])
        cards_layout.addWidget(self._card_tracks[0])
        cards_layout.addWidget(self._card_hashes[0])
        cards_layout.addWidget(self._card_size[0])

        parent_layout.addLayout(cards_layout)

    def _create_metric_card(self, title: str, initial_value: str) -> tuple[QFrame, QLabel]:
        card = QFrame(self)
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        lbl_title = QLabel(title, card)
        lbl_title.setStyleSheet(
            f"font-size: {FONT_SIZE_SMALL}px; color: #94a3b8; text-transform: uppercase;")
        layout.addWidget(lbl_title)

        lbl_value = QLabel(initial_value, card)
        lbl_value.setStyleSheet(
            f"font-size: {FONT_SIZE_HEADER}px; font-weight: bold; color: #38bdf8;")
        layout.addWidget(lbl_value)

        return card, lbl_value

    def _init_controls(self, parent_layout: QVBoxLayout) -> None:
        controls_frame = QFrame(self)
        controls_frame.setObjectName("card")
        layout = QVBoxLayout(controls_frame)
        layout.setContentsMargins(LAYOUT_MARGIN_STD, 10, LAYOUT_MARGIN_STD, 10)
        layout.setSpacing(LAYOUT_SPACING_SMALL)

        # Top row: Folder input, browse, reconcile button
        top_row = QHBoxLayout()
        top_row.setSpacing(LAYOUT_SPACING_SMALL)

        top_row.addWidget(QLabel("Archive Folder:", controls_frame))

        current_settings = self._settings_repo.load()
        default_dir = str(current_settings.paths.db_dir)
        self._dir_input = QLineEdit(default_dir, controls_frame)
        top_row.addWidget(self._dir_input, 1)

        self._browse_btn = QPushButton("Browse...", controls_frame)
        self._browse_btn.clicked.connect(self._on_browse_dir)
        top_row.addWidget(self._browse_btn)

        self._reconcile_btn = QPushButton(
            "Reconcile Directory", controls_frame)
        self._reconcile_btn.setObjectName("primary_button")
        self._reconcile_btn.clicked.connect(self.start_reconciliation)
        top_row.addWidget(self._reconcile_btn)

        layout.addLayout(top_row)

        # Bottom row: Search filter & status
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(LAYOUT_SPACING_SMALL)

        bottom_row.addWidget(QLabel("Filter:", controls_frame))
        self._search_input = QLineEdit(controls_frame)
        self._search_input.setPlaceholderText(
            "Filter by file name or prefix...")
        self._search_input.textChanged.connect(self._apply_filter)
        bottom_row.addWidget(self._search_input, 1)

        self._status_label = QLabel("Catalog ready", controls_frame)
        self._status_label.setStyleSheet("color: #94a3b8;")
        bottom_row.addWidget(self._status_label)

        layout.addLayout(bottom_row)

        # Progress bar (hidden by default)
        self._progress_bar = QProgressBar(controls_frame)
        self._progress_bar.setVisible(False)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setTextVisible(False)
        layout.addWidget(self._progress_bar)

        parent_layout.addWidget(controls_frame)

    # ------------------- Data Binding & Refresh -------------------

    @Slot()
    def refresh_catalog(self) -> None:
        """Query catalog SQLite repository and reload tree and summary counters."""
        self._entries = self._service.list_catalog()
        summary = self._service.get_catalog_summary()

        self._card_databases[1].setText(f"{summary['total_databases']:,}")
        self._card_tracks[1].setText(f"{summary['total_tracks']:,}")
        self._card_hashes[1].setText(f"{summary['total_hashes']:,}")
        self._card_size[1].setText(_format_size(summary["total_size_bytes"]))

        self._apply_filter(self._search_input.text())

    @Slot(str)
    def _apply_filter(self, filter_text: str = "") -> None:
        """Filter table items by name or prefix in memory."""
        query = filter_text.strip().lower()
        self._tree_widget.clear()

        for entry in self._entries:
            if query and query not in entry.file_name.lower() and query not in entry.prefix_label.lower():
                continue

            item = QTreeWidgetItem(
                [
                    entry.file_name,
                    entry.prefix_label,
                    f"{entry.track_count:,}",
                    f"{entry.total_hashes:,}",
                    _format_size(entry.file_size_bytes),
                    entry.sha256_hash[:16] + "..." if len(
                        entry.sha256_hash) > 16 else entry.sha256_hash,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, str(entry.file_path))
            self._tree_widget.addTopLevelItem(item)

    @Slot()
    def _on_tree_selection_changed(self) -> None:
        """Handle tree row selection and load track titles into inspector drawer."""
        items = self._tree_widget.selectedItems()
        if not items:
            self._details_title.setText("Database Details & Tracks")
            self._details_path_label.setText(
                "Select a database from the table to inspect.")
            self._tracks_count_label.setText("Registered Tracks: -")
            self._tracks_list.clear()
            return

        item = items[0]
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        if not raw_path:
            return

        file_path = Path(raw_path)
        self._details_title.setText(file_path.name)
        self._details_path_label.setText(str(file_path))

        try:
            track_names = self._service.get_database_track_names(file_path)
            self._tracks_count_label.setText(
                f"Registered Tracks: {len(track_names):,}")
            self._tracks_list.clear()
            for name in track_names:
                self._tracks_list.addItem(name)
        except Exception as exc:
            self._tracks_count_label.setText(f"Error loading tracks: {exc}")
            self._tracks_list.clear()

    # ------------------- Reconciliation Lifecycle -------------------

    @Slot()
    def _on_browse_dir(self) -> None:
        """Open native directory picker to choose database library folder."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select .pklz Databases Folder", self._dir_input.text().strip()
        )
        if folder:
            self._dir_input.setText(folder)

    @Slot()
    def start_reconciliation(self) -> None:
        """Trigger background worker reconciliation of the selected directory."""
        if self._worker_thread is not None and self._worker_thread.isRunning():
            return

        path_str = self._dir_input.text().strip()
        root_dir = Path(path_str)
        if not root_dir.is_dir():
            self._status_label.setText("Error: invalid directory path.")
            self.log_requested.emit(f"[X] Directory not found: {path_str}")
            return

        self._reconcile_btn.setEnabled(False)
        self._browse_btn.setEnabled(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(0)
        self._status_label.setText("Reconciling...")
        self.log_requested.emit(f"Starting reconciliation of '{root_dir}'...")

        self._worker_thread = QThread(self)
        self._worker = CatalogWorker(
            catalog_service=self._service,
            root_dir=root_dir,
            prune_missing=True,
        )
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress_updated.connect(self._on_worker_progress)
        self._worker.status_changed.connect(self._status_label.setText)
        self._worker.reconciliation_completed.connect(
            self._on_reconciliation_done)
        self._worker.error_occurred.connect(
            lambda err: self.log_requested.emit(
                f"[X] Reconciliation Error: {err}")
        )
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(self._on_worker_teardown)

        self._worker_thread.start()

    @Slot(int, int, str)
    def _on_worker_progress(self, current: int, total: int, filename: str) -> None:
        """Update progress bar during reconciliation."""
        if total > 0:
            pct = int((current / total) * 100)
            self._progress_bar.setValue(pct)
            self._status_label.setText(
                f"Indexing ({current}/{total}): {filename}")

    @Slot(object)
    def _on_reconciliation_done(self, result: ReconciliationResult) -> None:
        """Process completed reconciliation summary."""
        msg = (
            f"Reconciliation complete: {result.discovered_files} discovered, "
            f"{result.new_or_updated} indexed/updated, {result.pruned_entries} pruned."
        )
        self._status_label.setText(msg)
        self.log_requested.emit(f"[OK] {msg}")
        self.refresh_catalog()

    @Slot()
    def _on_worker_teardown(self) -> None:
        """Restore control states after worker thread termination."""
        self._worker = None
        self._worker_thread = None
        self._reconcile_btn.setEnabled(True)
        self._browse_btn.setEnabled(True)
        self._progress_bar.setVisible(False)
