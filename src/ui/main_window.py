"""Main application window for desktop shell."""

from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from src.core.config.constants import APP_NAME, APP_VERSION
from src.core.config.models import AppSettings, ThemeMode
from src.core.config.secrets_store import SecretsStore
from src.core.database.settings_repository import SettingsRepository
from src.core.process.dependency_doctor import DependencyDoctorService
from src.core.process.runner import SilentProcessRunner
from src.ui.components.log_console import LogConsole
from src.ui.theme import (
    LOG_CONSOLE_DEFAULT_HEIGHT,
    LOG_CONSOLE_MIN_HEIGHT,
    SIDEBAR_FIXED_WIDTH,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    build_stylesheet,
)
from src.ui.views.database_catalog_view import DatabaseCatalogView
from src.ui.views.harvester_view import HarvesterView
from src.ui.views.scanner_view import ScannerView
from src.ui.views.settings_view import SettingsView
from src.core.fingerprinter.catalog_service import CatalogService
from src.core.harvester.service import HarvesterService
from src.core.database.candidate_repository import CandidateRepository
from src.core.database.catalog_repository import CatalogRepository
from src.core.matcher.candidate_reconciler import CandidateReconciler
from src.core.matcher.local_matcher import LocalMatcherService


class MainWindow(QMainWindow):
    """PySide6 application shell composing sidebar, view stack, and log console."""

    def __init__(
        self,
        base_dir: Path,
        settings_repo: SettingsRepository,
        secrets_store: SecretsStore,
        doctor_service: DependencyDoctorService,
        process_runner: SilentProcessRunner,
        harvester_service: HarvesterService | None = None,
        catalog_service: CatalogService | None = None,
        scanner_view: ScannerView | None = None,
        candidate_repo: CandidateRepository | None = None,
        catalog_repo: CatalogRepository | None = None,
        matcher_service: LocalMatcherService | None = None,
        candidate_reconciler: CandidateReconciler | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._base_dir = base_dir
        self._settings_repo = settings_repo
        self._secrets_store = secrets_store
        self._doctor_service = doctor_service
        self._process_runner = process_runner
        self._harvester_service = harvester_service
        self._catalog_service = catalog_service
        self._scanner_view = scanner_view
        self._candidate_repo = candidate_repo
        self._catalog_repo = catalog_repo
        self._matcher_service = matcher_service
        self._candidate_reconciler = candidate_reconciler

        self._init_window()
        self._init_layout()
        self._init_signals()

    def _init_window(self) -> None:
        self.setWindowTitle(
            f"{APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

    def _init_layout(self) -> None:
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Left Sidebar Navigation
        self._sidebar = QListWidget(self)
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setFixedWidth(SIDEBAR_FIXED_WIDTH)

        nav_items = [
            "Media Harvester",
            "Database Catalog",
            "Multi-Engine Scanner",
            "Settings",
        ]
        self._sidebar.addItems(nav_items)
        root_layout.addWidget(self._sidebar)

        # 2. Main Content Area (Vertical Splitter: Top = Stacked Views, Bottom = LogConsole)
        content_splitter = QSplitter(Qt.Orientation.Vertical, self)

        # Stacked Views Container
        self._view_stack = QStackedWidget(content_splitter)

        # Milestone 2 Harvester View (or placeholder fallback if service not wired)
        if self._harvester_service is not None:
            self._harvester_view = HarvesterView(
                harvester_service=self._harvester_service,
                settings_repo=self._settings_repo,
                parent=self._view_stack,
            )
            self._view_stack.addWidget(self._harvester_view)
        else:
            self._harvester_view = None
            self._harvester_placeholder = self._create_placeholder(
                "Media Harvester (Milestone 2)",
                "Batch YouTube/SoundCloud audio ingestion, directory crawling, and audio splitting.",
            )
            self._view_stack.addWidget(self._harvester_placeholder)

        if self._catalog_service is not None:
            self._catalog_view = DatabaseCatalogView(
                catalog_service=self._catalog_service,
                settings_repo=self._settings_repo,
                parent=self._view_stack,
            )
            self._catalog_placeholder = None
            self._view_stack.addWidget(self._catalog_view)
        else:
            self._catalog_view = None
            self._catalog_placeholder = self._create_placeholder(
                "Database Catalog (Milestone 3)",
                "Searchable .pklz fingerprint database inspection and subset selection.",
            )
            self._view_stack.addWidget(self._catalog_placeholder)

        if self._scanner_view is not None:
            self._scanner_placeholder = None
            self._view_stack.addWidget(self._scanner_view)
        elif (
            self._candidate_repo is not None
            and self._catalog_repo is not None
            and self._matcher_service is not None
            and self._candidate_reconciler is not None
        ):
            self._scanner_view = ScannerView(
                candidate_repo=self._candidate_repo,
                catalog_repo=self._catalog_repo,
                matcher_service=self._matcher_service,
                reconciler=self._candidate_reconciler,
                settings_repo=self._settings_repo,
                parent=self._view_stack,
            )
            self._scanner_placeholder = None
            self._view_stack.addWidget(self._scanner_view)
        else:
            self._scanner_view = None
            self._scanner_placeholder = self._create_placeholder(
                "Multi-Engine Scanner (Milestone 3/4)",
                "Local audfprint matching + Cloud Shazam/AcoustID/AudioTag recognition.",
            )
            self._view_stack.addWidget(self._scanner_placeholder)

        # Active Milestone 1 View: SettingsView (with integrated DependencyDoctor access)
        self._settings_view = SettingsView(
            settings_repo=self._settings_repo,
            secrets_store=self._secrets_store,
            base_dir=self._base_dir,
            doctor_service=self._doctor_service,
            parent=self._view_stack,
        )

        self._view_stack.addWidget(self._settings_view)

        content_splitter.addWidget(self._view_stack)

        # Log Console
        self._log_console = LogConsole(content_splitter)
        self._log_console.setMinimumHeight(LOG_CONSOLE_MIN_HEIGHT)
        content_splitter.addWidget(self._log_console)

        # Set initial splitter proportion
        content_splitter.setSizes(
            [WINDOW_MIN_HEIGHT - LOG_CONSOLE_DEFAULT_HEIGHT, LOG_CONSOLE_DEFAULT_HEIGHT])

        root_layout.addWidget(content_splitter)

        # Status Bar
        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

    def _create_placeholder(self, title: str, description: str) -> QWidget:

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QFrame()
        card.setProperty("class", "card")
        card_layout = QVBoxLayout(card)
        card_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title_lbl = QLabel(title, card)
        title_lbl.setProperty("class", "title")
        card_layout.addWidget(title_lbl)

        desc_lbl = QLabel(description, card)
        desc_lbl.setProperty("class", "muted")
        card_layout.addWidget(desc_lbl)

        layout.addWidget(card)
        return widget

    def _init_signals(self) -> None:
        self._sidebar.currentRowChanged.connect(self._on_navigation_changed)
        self._settings_view.settings_saved.connect(self.on_settings_saved)
        self._settings_view.log_requested.connect(self._log_console.append_log)

        if self._catalog_view is not None:
            self._catalog_view.log_requested.connect(
                self._log_console.append_log)
            if self._scanner_view is not None:
                self._catalog_view.catalog_updated.connect(
                    self._scanner_view.reload_databases)
            if self._harvester_view is not None:
                self._harvester_view.catalog_updated.connect(
                    self._catalog_view.refresh_catalog)
                if self._scanner_view is not None:
                    self._harvester_view.catalog_updated.connect(
                        self._scanner_view.reload_databases)

        if self._scanner_view is not None:
            self._scanner_view.log_requested.connect(
                self._log_console.append_log)

        if self._harvester_view is not None:
            self._harvester_view.log_requested.connect(
                self._log_console.append_log)
            # Default to Media Harvester (index 0) when available
            self._sidebar.setCurrentRow(0)
            self._view_stack.setCurrentIndex(0)
        else:
            # Set initial view to Settings (index 3) and synchronize view stack
            self._sidebar.setCurrentRow(3)
            self._view_stack.setCurrentIndex(3)

        # Log initial startup
        self._log_console.append_log(f"{APP_NAME} desktop shell initialized.")

    @Slot(int)
    def _on_navigation_changed(self, index: int) -> None:
        """Handle view stack tab switching and refresh active views if needed."""
        self._view_stack.setCurrentIndex(index)
        if index == 1 and self._catalog_view is not None:
            self._catalog_view.refresh_catalog()
        elif index == 2 and self._scanner_view is not None:
            self._scanner_view.reload_databases()

    @Slot(AppSettings)
    def on_settings_saved(self, settings: AppSettings) -> None:
        """Apply theme updates and runtime settings dynamically when settings are saved."""
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_stylesheet(settings.theme))
        if self._doctor_service and self._doctor_service.binary_locator:
            self._doctor_service.binary_locator.bin_dir = settings.paths.bin_dir
        if self._catalog_view is not None:
            self._catalog_view.set_archive_dir(settings.paths.db_dir)
            self._catalog_view.refresh_catalog()
        if self._scanner_view is not None:
            self._scanner_view.reload_databases()
        self._status_bar.showMessage("Settings saved successfully.", 3000)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Ensure all child processes and worker tasks are safely terminated before window closure."""
        self._log_console.append_log(
            "Closing application, terminating active subprocesses...")
        self._process_runner.terminate_all()
        event.accept()

    @property
    def log_console(self) -> LogConsole:
        return self._log_console

    @property
    def scanner_view(self) -> ScannerView | None:
        return self._scanner_view

    @property
    def catalog_view(self) -> DatabaseCatalogView | None:
        return self._catalog_view

    @property
    def harvester_view(self) -> HarvesterView | None:
        return self._harvester_view

    @property
    def settings_view(self) -> SettingsView:
        return self._settings_view

    @property
    def view_stack(self) -> QStackedWidget:
        return self._view_stack
