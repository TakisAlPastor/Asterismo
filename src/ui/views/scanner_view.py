"""Multi-Engine Scanner View for candidate selection, target database filtering, and local acoustic identification."""
from __future__ import annotations

import logging
from pathlib import Path
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QThread, Signal, Slot
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.config.constants import (
    LOCAL_MATCH_EARLY_STOP_THRESHOLD,
    LOCAL_MATCH_MIN_HASH_THRESHOLD,
)
from src.core.config.models import (
    MatchOptions,
    PklzCatalogEntry,
    ScanCandidateRecord,
    ScanCandidateStatus,
    ScanMatchRecord,
)
from src.core.database.candidate_repository import CandidateRepository
from src.core.database.catalog_repository import CatalogRepository
from src.core.database.settings_repository import SettingsRepository
from src.core.matcher.candidate_reconciler import CandidateReconciler
from src.core.matcher.local_matcher import LocalMatcherService
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
from src.ui.workers.matcher_worker import LocalMatcherWorker

logger = logging.getLogger(__name__)


def _format_duration(seconds: float | None) -> str:
    """Format duration in seconds to MM:SS representation."""
    if seconds is None or seconds <= 0:
        return "--:--"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m:02d}:{s:02d}"


class ScannerView(QWidget):
    """View managing candidate audio selection, target database filtering, and local identification scans (US-3.2)."""

    log_requested = Signal(str)

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        catalog_repo: CatalogRepository,
        matcher_service: LocalMatcherService,
        reconciler: CandidateReconciler,
        settings_repo: SettingsRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._candidate_repo = candidate_repo
        self._catalog_repo = catalog_repo
        self._matcher_service = matcher_service
        self._reconciler = reconciler
        self._settings_repo = settings_repo

        self._candidates: list[ScanCandidateRecord] = []
        self._catalog_entries: list[PklzCatalogEntry] = []
        self._worker_thread: QThread | None = None
        self._worker: LocalMatcherWorker | None = None

        self._init_ui()
        self.reload_databases()
        self.reload_candidates()

    def _init_ui(self) -> None:
        """Construct layout and child widgets."""
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD)
        root_layout.setSpacing(LAYOUT_SPACING_STD)

        # 1. Header & Controls Card
        self._init_header(root_layout)
        self._init_controls_card(root_layout)

        # 2. Main Horizontal Splitter: Candidates on left, Database selector on right
        main_splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left Container: Candidate Audio Table & Match Inspector
        left_widget = QWidget(main_splitter)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(LAYOUT_SPACING_SMALL)

        # Candidates Toolbar
        cand_toolbar = QHBoxLayout()
        cand_toolbar.addWidget(QLabel("Candidate Audio Tracks:", left_widget))

        self._cand_filter_combo = QLineEdit(left_widget)
        self._cand_filter_combo.setPlaceholderText("Filter candidate tracks...")
        self._cand_filter_combo.textChanged.connect(self._apply_candidate_filter)
        cand_toolbar.addWidget(self._cand_filter_combo, 1)

        self._btn_select_all_cand = QPushButton("Select All", left_widget)
        self._btn_select_all_cand.clicked.connect(self._select_all_candidates)
        cand_toolbar.addWidget(self._btn_select_all_cand)

        self._btn_select_unscanned = QPushButton("Select Unscanned", left_widget)
        self._btn_select_unscanned.clicked.connect(self._select_unscanned_candidates)
        cand_toolbar.addWidget(self._btn_select_unscanned)

        self._btn_refresh_cand = QPushButton("Refresh Files", left_widget)
        self._btn_refresh_cand.clicked.connect(self.reload_candidates)
        cand_toolbar.addWidget(self._btn_refresh_cand)

        self._btn_purge_missing = QPushButton("Purge Missing", left_widget)
        self._btn_purge_missing.clicked.connect(self.purge_missing_candidates)
        cand_toolbar.addWidget(self._btn_purge_missing)

        left_layout.addLayout(cand_toolbar)

        # Candidate Table
        self._cand_table = QTableWidget(0, 6, left_widget)
        self._cand_table.setHorizontalHeaderLabels(
            ["Target", "Status", "Filename", "Duration", "Quick Scanned", "Matches"]
        )
        self._cand_table.setAlternatingRowColors(True)
        self._cand_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._cand_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._cand_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._cand_table.customContextMenuRequested.connect(self._show_cand_context_menu)
        self._cand_table.installEventFilter(self)
        header = self._cand_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._cand_table.itemSelectionChanged.connect(self._on_candidate_selected)
        left_layout.addWidget(self._cand_table, 2)

        # Bottom Sub-Table: Match Hits for selected candidate
        self._matches_title = QLabel("Detected Hits (Select a candidate to view matches):", left_widget)
        self._matches_title.setStyleSheet(f"font-size: {FONT_SIZE_SMALL}px; font-weight: bold; color: #94a3b8;")
        left_layout.addWidget(self._matches_title)

        self._matches_table = QTableWidget(0, 5, left_widget)
        self._matches_table.setHorizontalHeaderLabels(
            ["Matched Title", "Source Database", "Aligned Hashes", "Offset (s)", "Confidence"]
        )
        self._matches_table.setAlternatingRowColors(True)
        self._matches_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        m_header = self._matches_table.horizontalHeader()
        m_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        m_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        m_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        m_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        m_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        left_layout.addWidget(self._matches_table, 1)

        main_splitter.addWidget(left_widget)

        # Right Container: Lightweight Target Database Selector
        right_widget = QFrame(main_splitter)
        right_widget.setObjectName("card")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD)
        right_layout.setSpacing(LAYOUT_SPACING_SMALL)

        right_title = QLabel("Target .pklz Databases", right_widget)
        right_title.setStyleSheet(f"font-size: {FONT_SIZE_SUBHEADER}px; font-weight: bold;")
        right_layout.addWidget(right_title)

        db_actions_layout = QHBoxLayout()
        self._btn_select_all_dbs = QPushButton("Select All", right_widget)
        self._btn_select_all_dbs.clicked.connect(self._select_all_databases)
        db_actions_layout.addWidget(self._btn_select_all_dbs)

        self._btn_deselect_all_dbs = QPushButton("Deselect All", right_widget)
        self._btn_deselect_all_dbs.clicked.connect(self._deselect_all_databases)
        db_actions_layout.addWidget(self._btn_deselect_all_dbs)

        self._btn_refresh_dbs = QPushButton("Refresh", right_widget)
        self._btn_refresh_dbs.clicked.connect(self.reload_databases)
        db_actions_layout.addWidget(self._btn_refresh_dbs)

        right_layout.addLayout(db_actions_layout)

        self._db_tree = QTreeWidget(right_widget)
        self._db_tree.setHeaderLabels(["Database / Collection", "Tracks"])
        self._db_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._db_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._db_tree.itemChanged.connect(self._on_db_tree_item_changed)
        right_layout.addWidget(self._db_tree, 1)

        self._selected_dbs_summary_label = QLabel("Selected: 0 databases (0 tracks)", right_widget)
        self._selected_dbs_summary_label.setStyleSheet("color: #94a3b8; font-weight: 600;")
        right_layout.addWidget(self._selected_dbs_summary_label)

        main_splitter.addWidget(right_widget)
        main_splitter.setSizes([650, 350])
        root_layout.addWidget(main_splitter, 1)

    def _init_header(self, parent_layout: QVBoxLayout) -> None:
        header_widget = QWidget(self)
        layout = QVBoxLayout(header_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        title = QLabel("Multi-Engine Scanner", header_widget)
        title.setStyleSheet(f"font-size: {FONT_SIZE_TITLE}px; font-weight: bold;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Local audfprint acoustic landmark matching against indexed reference databases.", header_widget
        )
        subtitle.setStyleSheet(f"font-size: {FONT_SIZE_BODY}px; color: #94a3b8;")
        layout.addWidget(subtitle)

        parent_layout.addWidget(header_widget)

    def _init_controls_card(self, parent_layout: QVBoxLayout) -> None:
        controls_frame = QFrame(self)
        controls_frame.setObjectName("card")
        layout = QVBoxLayout(controls_frame)
        layout.setContentsMargins(LAYOUT_MARGIN_STD, 10, LAYOUT_MARGIN_STD, 10)
        layout.setSpacing(LAYOUT_SPACING_SMALL)

        top_row = QHBoxLayout()
        top_row.setSpacing(LAYOUT_SPACING_STD)

        # Mode Selection
        self._radio_quick_scan = QRadioButton("Quick Scan (Raw Audio)", controls_frame)
        self._radio_quick_scan.setChecked(True)
        top_row.addWidget(self._radio_quick_scan)

        # Early Stop Checkbox
        self._cb_early_stop = QCheckBox(
            f"Stop track on first high-confidence match (>= {LOCAL_MATCH_EARLY_STOP_THRESHOLD} hashes)",
            controls_frame,
        )
        self._cb_early_stop.setChecked(True)
        top_row.addWidget(self._cb_early_stop)

        top_row.addStretch(1)

        # Scan Execution Buttons
        self._btn_start_scan = QPushButton("Start Local Scan", controls_frame)
        self._btn_start_scan.setObjectName("primary_button")
        self._btn_start_scan.clicked.connect(self.start_scan)
        top_row.addWidget(self._btn_start_scan)

        self._btn_cancel_scan = QPushButton("Cancel", controls_frame)
        self._btn_cancel_scan.setEnabled(False)
        self._btn_cancel_scan.clicked.connect(self.cancel_scan)
        top_row.addWidget(self._btn_cancel_scan)

        layout.addLayout(top_row)

        # Status and Progress
        status_row = QHBoxLayout()
        self._status_label = QLabel("Scanner ready", controls_frame)
        self._status_label.setStyleSheet("color: #94a3b8;")
        status_row.addWidget(self._status_label)
        layout.addLayout(status_row)

        self._progress_bar = QProgressBar(controls_frame)
        self._progress_bar.setVisible(False)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setTextVisible(False)
        layout.addWidget(self._progress_bar)

        parent_layout.addWidget(controls_frame)

    # ------------------- Data Loading & Population -------------------

    @Slot()
    def reload_databases(self) -> None:
        """Query SQLite pklz_catalog and populate the lightweight database selection tree."""
        self._catalog_entries = self._catalog_repo.list_entries()
        self._db_tree.blockSignals(True)
        self._db_tree.clear()

        groups: dict[str, list[PklzCatalogEntry]] = {}
        for entry in self._catalog_entries:
            groups.setdefault(entry.prefix_label, []).append(entry)

        for prefix, entries in sorted(groups.items()):
            valid_tracks = sum(e.track_count for e in entries if e.file_path.is_file())
            parent_item = QTreeWidgetItem([prefix, f"{valid_tracks:,} tracks"])
            parent_item.setFlags(parent_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            parent_item.setCheckState(0, Qt.CheckState.Checked)

            has_valid_child = False
            for e in entries:
                file_exists = e.file_path.is_file()
                display_name = e.file_name if file_exists else f"[Missing] {e.file_name}"
                child = QTreeWidgetItem([display_name, f"{e.track_count:,}"])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if file_exists:
                    child.setCheckState(0, Qt.CheckState.Checked)
                    has_valid_child = True
                else:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    child.setForeground(0, Qt.GlobalColor.red)
                    child.setToolTip(0, f"File missing from disk: {e.file_path}")
                child.setData(0, Qt.ItemDataRole.UserRole, str(e.file_path))
                parent_item.addChild(child)

            if not has_valid_child:
                parent_item.setCheckState(0, Qt.CheckState.Unchecked)

            self._db_tree.addTopLevelItem(parent_item)

        self._db_tree.expandAll()
        self._db_tree.blockSignals(False)
        self._update_selected_db_summary()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Handle keyboard shortcuts such as Delete key on the candidate table."""
        if watched == self._cand_table and event.type() == QEvent.Type.KeyPress:
            if isinstance(event, QKeyEvent) and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self._delete_selected_candidates()
                return True
        return super().eventFilter(watched, event)

    @Slot(QPoint)
    def _show_cand_context_menu(self, pos: QPoint) -> None:
        """Display context menu to remove selected candidates."""
        selected_rows = sorted(set(idx.row() for idx in self._cand_table.selectedIndexes()))
        if not selected_rows:
            return

        menu = QMenu(self._cand_table)
        action_remove = menu.addAction(f"Remove Candidate(s) ({len(selected_rows)})")
        action = menu.exec(self._cand_table.viewport().mapToGlobal(pos))
        if action == action_remove:
            self._delete_selected_candidates()

    @Slot()
    def purge_missing_candidates(self) -> None:
        """Delete all candidates marked as missing from the repository."""
        deleted = self._candidate_repo.delete_missing_candidates()
        if deleted > 0:
            self.log_requested.emit(f"[-] Purged {deleted} missing candidate(s) from database.")
        self.reload_candidates()

    @Slot()
    def _delete_selected_candidates(self) -> None:
        """Delete selected candidate rows from persistent storage."""
        selected_rows = sorted(set(idx.row() for idx in self._cand_table.selectedIndexes()))
        if not selected_rows:
            return

        cand_ids: list[int] = []
        for row in selected_rows:
            cb_item = self._cand_table.item(row, 0)
            if cb_item is not None:
                cid = cb_item.data(Qt.ItemDataRole.UserRole)
                if cid is not None:
                    cand_ids.append(int(cid))

        if cand_ids:
            deleted = self._candidate_repo.delete_candidates(cand_ids)
            self.log_requested.emit(f"[-] Removed {deleted} candidate(s) from database.")
            self.reload_candidates()

    @Slot()
    def reload_candidates(self) -> None:
        """Scan configured input_audio_dir and refresh candidate table."""
        settings = self._settings_repo.load()
        input_dir = settings.paths.input_dir
        if input_dir.is_dir():
            try:
                self._reconciler.reconcile_directory(input_dir, probe_durations=False)
            except Exception as exc:
                logger.warning("Error reconciling candidates: %s", exc)

        self._candidates = self._candidate_repo.list_candidates()
        self._apply_candidate_filter(self._cand_filter_combo.text())

    @Slot(str)
    def _apply_candidate_filter(self, filter_text: str = "") -> None:
        """Filter candidate table in memory."""
        query = filter_text.strip().lower()
        self._cand_table.setRowCount(0)

        for cand in self._candidates:
            if query and query not in cand.file_name.lower() and query not in cand.relative_path.lower():
                continue

            row = self._cand_table.rowCount()
            self._cand_table.insertRow(row)

            # Checkbox item
            cb_item = QTableWidgetItem()
            cb_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            cb_item.setCheckState(Qt.CheckState.Checked if cand.status != ScanCandidateStatus.MISSING else Qt.CheckState.Unchecked)
            cb_item.setData(Qt.ItemDataRole.UserRole, cand.id)
            self._cand_table.setItem(row, 0, cb_item)

            # Status badge
            status_item = QTableWidgetItem(cand.status.value.upper())
            if cand.status == ScanCandidateStatus.SCANNED:
                status_item.setForeground(Qt.GlobalColor.green)
            elif cand.status == ScanCandidateStatus.MISSING:
                status_item.setForeground(Qt.GlobalColor.red)
            else:
                status_item.setForeground(Qt.GlobalColor.yellow)
            self._cand_table.setItem(row, 1, status_item)

            # File Name & Path
            name_item = QTableWidgetItem(cand.file_name)
            name_item.setToolTip(cand.relative_path)
            self._cand_table.setItem(row, 2, name_item)

            # Duration
            self._cand_table.setItem(row, 3, QTableWidgetItem(_format_duration(cand.duration_seconds)))

            # Quick Scanned
            quick_text = cand.quick_scanned_at[:19] if cand.quick_scanned_at else "-"
            self._cand_table.setItem(row, 4, QTableWidgetItem(quick_text))

            # Matches Count
            matches = self._candidate_repo.get_matches_for_candidate(cand.id) if cand.id else []
            match_item = QTableWidgetItem(str(len(matches)))
            if matches:
                match_item.setForeground(Qt.GlobalColor.cyan)
                match_item.setToolTip(f"{len(matches)} match hits detected")
            self._cand_table.setItem(row, 5, match_item)

    # ------------------- Database Selector Helpers -------------------

    @Slot(QTreeWidgetItem, int)
    def _on_db_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        self._update_selected_db_summary()

    def _select_all_databases(self) -> None:
        self._db_tree.blockSignals(True)
        for i in range(self._db_tree.topLevelItemCount()):
            parent = self._db_tree.topLevelItem(i)
            parent.setCheckState(0, Qt.CheckState.Checked)
            for j in range(parent.childCount()):
                parent.child(j).setCheckState(0, Qt.CheckState.Checked)
        self._db_tree.blockSignals(False)
        self._update_selected_db_summary()

    def _deselect_all_databases(self) -> None:
        self._db_tree.blockSignals(True)
        for i in range(self._db_tree.topLevelItemCount()):
            parent = self._db_tree.topLevelItem(i)
            parent.setCheckState(0, Qt.CheckState.Unchecked)
            for j in range(parent.childCount()):
                parent.child(j).setCheckState(0, Qt.CheckState.Unchecked)
        self._db_tree.blockSignals(False)
        self._update_selected_db_summary()

    def _update_selected_db_summary(self) -> None:
        selected_paths = self.get_selected_database_paths()
        selected_count = len(selected_paths)
        total_tracks = sum(
            e.track_count for e in self._catalog_entries if e.file_path in selected_paths
        )
        self._selected_dbs_summary_label.setText(
            f"Selected: {selected_count} databases ({total_tracks:,} tracks)"
        )

    def get_selected_database_paths(self) -> list[Path]:
        """Collect all file paths checked in the target database tree."""
        paths: list[Path] = []
        for i in range(self._db_tree.topLevelItemCount()):
            parent = self._db_tree.topLevelItem(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    raw = child.data(0, Qt.ItemDataRole.UserRole)
                    if raw:
                        paths.append(Path(raw))
        return paths

    # ------------------- Candidate Selection Helpers -------------------

    def _select_all_candidates(self) -> None:
        for row in range(self._cand_table.rowCount()):
            item = self._cand_table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Checked)

    def _select_unscanned_candidates(self) -> None:
        for row in range(self._cand_table.rowCount()):
            status_item = self._cand_table.item(row, 1)
            cb_item = self._cand_table.item(row, 0)
            if status_item and cb_item:
                if status_item.text() == "PENDING":
                    cb_item.setCheckState(Qt.CheckState.Checked)
                else:
                    cb_item.setCheckState(Qt.CheckState.Unchecked)

    def get_selected_candidates(self) -> list[ScanCandidateRecord]:
        """Return list of candidate records checked in the table."""
        selected_ids: set[int] = set()
        for row in range(self._cand_table.rowCount()):
            cb_item = self._cand_table.item(row, 0)
            if cb_item and cb_item.checkState() == Qt.CheckState.Checked:
                cand_id = cb_item.data(Qt.ItemDataRole.UserRole)
                if cand_id:
                    selected_ids.add(int(cand_id))
        return [c for c in self._candidates if c.id in selected_ids]

    @Slot()
    def _on_candidate_selected(self) -> None:
        """Display match records for the currently selected candidate in the lower table."""
        selected_rows = self._cand_table.selectionModel().selectedRows()
        if not selected_rows:
            self._matches_table.setRowCount(0)
            self._matches_title.setText("Detected Hits (Select a candidate to view matches):")
            return

        row = selected_rows[0].row()
        cb_item = self._cand_table.item(row, 0)
        cand_id = cb_item.data(Qt.ItemDataRole.UserRole) if cb_item else None
        if not cand_id:
            return

        cand = next((c for c in self._candidates if c.id == cand_id), None)
        if cand is None:
            return

        matches = self._candidate_repo.get_matches_for_candidate(cand_id)
        self._matches_title.setText(f"Detected Hits for '{cand.file_name}' ({len(matches)} matches):")
        self._matches_table.setRowCount(0)

        for m in matches:
            m_row = self._matches_table.rowCount()
            self._matches_table.insertRow(m_row)
            clean_track = Path(m.matched_track).name or m.matched_track
            track_item = QTableWidgetItem(clean_track)
            track_item.setToolTip(m.matched_track)
            self._matches_table.setItem(m_row, 0, track_item)
            self._matches_table.setItem(m_row, 1, QTableWidgetItem(m.matched_database))
            self._matches_table.setItem(m_row, 2, QTableWidgetItem(str(m.aligned_hashes)))
            self._matches_table.setItem(m_row, 3, QTableWidgetItem(f"{m.offset_seconds:.2f}"))
            self._matches_table.setItem(m_row, 4, QTableWidgetItem(f"{m.confidence:.1%}"))

    # ------------------- Scan Orchestration Lifecycle -------------------

    @Slot()
    def start_scan(self) -> None:
        """Initiate background acoustic matching worker."""
        if self._worker_thread is not None and self._worker_thread.isRunning():
            return

        selected_candidates = self.get_selected_candidates()
        if not selected_candidates:
            self._status_label.setText("No candidates selected.")
            self.log_requested.emit("[!] Scan aborted: No candidate audio tracks selected.")
            return

        selected_dbs = self.get_selected_database_paths()
        if not selected_dbs:
            self._status_label.setText("No target databases selected.")
            self.log_requested.emit("[!] Scan aborted: At least one target .pklz database must be selected.")
            return

        options = MatchOptions(
            target_databases=tuple(selected_dbs),
            stop_on_first_match=self._cb_early_stop.isChecked(),
            min_aligned_hashes=LOCAL_MATCH_MIN_HASH_THRESHOLD,
            early_stop_threshold=LOCAL_MATCH_EARLY_STOP_THRESHOLD,
        )

        self._btn_start_scan.setEnabled(False)
        self._btn_cancel_scan.setEnabled(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(0)

        self._worker_thread = QThread(self)
        self._worker = LocalMatcherWorker(
            matcher_service=self._matcher_service,
            candidates=selected_candidates,
            target_databases=selected_dbs,
            options=options,
        )
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._status_label.setText)
        self._worker.progress_updated.connect(self._on_worker_progress)
        self._worker.log_emitted.connect(self.log_requested)
        self._worker.candidate_completed.connect(self._on_candidate_completed)
        self._worker.scan_completed.connect(self._on_scan_completed)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.finished.connect(self._on_worker_teardown)

        self._worker_thread.start()

    @Slot()
    def cancel_scan(self) -> None:
        """Request graceful abort of active scan worker."""
        if self._worker is not None:
            self._worker.cancel()
            self._status_label.setText("Cancelling scan...")

    @Slot(int, int, str)
    def _on_worker_progress(self, current: int, total: int, filename: str) -> None:
        if total > 0:
            pct = int((current / total) * 100)
            self._progress_bar.setValue(pct)

    @Slot(object)
    def _on_candidate_completed(self, result: object) -> None:
        """Update candidate table row state when a candidate completes."""
        self.reload_candidates()

    @Slot(list)
    def _on_scan_completed(self, results: list) -> None:
        """Process scan completion event."""
        self.reload_candidates()
        self._status_label.setText(f"Scan finished. Processed {len(results)} tracks.")

    @Slot()
    def _on_worker_teardown(self) -> None:
        """Restore button states when worker terminates."""
        self._worker = None
        self._worker_thread = None
        self._btn_start_scan.setEnabled(True)
        self._btn_cancel_scan.setEnabled(False)
        self._progress_bar.setVisible(False)
