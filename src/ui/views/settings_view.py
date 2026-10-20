"""Settings view for configuring workspace paths, defaults, and API credentials."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.config.constants import (
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    MIN_POSITIVE_INT,
)
from src.core.config.models import (
    AppSettings,
    ScanMode,
    SecretCredentials,
    ThemeMode,
    WorkspacePaths,
)
from src.core.config.secrets_store import SecretsStore
from src.core.database.settings_repository import SettingsRepository
from src.core.process.dependency_doctor import DependencyDoctorService
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


class SettingsView(QWidget):
    """Configuration view bound to SQLite AppSettings and JSON SecretCredentials."""

    settings_saved = Signal(AppSettings)
    log_requested = Signal(str)

    def __init__(
        self,
        settings_repo: SettingsRepository,
        secrets_store: SecretsStore,
        base_dir: Path,
        doctor_service: DependencyDoctorService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_repo = settings_repo
        self._secrets_store = secrets_store
        self._base_dir = base_dir
        self._doctor_service = doctor_service

        self._init_ui()
        self.load_data()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(
            LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD, LAYOUT_MARGIN_STD)
        main_layout.setSpacing(LAYOUT_SPACING_STD)

        # Title
        title_label = QLabel("Settings & Preferences", self)
        title_label.setProperty("class", "title")
        main_layout.addWidget(title_label)

        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(LAYOUT_SPACING_STD)

        # 1. Workspace Paths Card
        paths_card = QFrame(container)
        paths_card.setProperty("class", "card")
        paths_layout = QVBoxLayout(paths_card)

        paths_title = QLabel("Workspace Directories", paths_card)
        paths_title.setProperty("class", "header")
        paths_layout.addWidget(paths_title)

        paths_grid = QGridLayout()
        paths_grid.setSpacing(8)

        self._input_dir_edit = self._create_path_row(
            "Input Audio Folder:", 0, paths_grid, paths_card)
        self._db_dir_edit = self._create_path_row(
            "Database (.pklz) Folder:", 1, paths_grid, paths_card)
        self._output_dir_edit = self._create_path_row(
            "Output Results Folder:", 2, paths_grid, paths_card)
        self._logs_dir_edit = self._create_path_row(
            "Application Logs Folder:", 3, paths_grid, paths_card)
        self._bin_dir_edit = self._create_path_row(
            "Local Binaries Folder:", 4, paths_grid, paths_card)
        self._archive_dir_edit = self._create_path_row(
            "Harvested Audio Archive Folder:", 5, paths_grid, paths_card)

        paths_layout.addLayout(paths_grid)
        container_layout.addWidget(paths_card)

        # 2. System Diagnostics Card (Dependency Doctor)
        diag_card = QFrame(container)
        diag_card.setProperty("class", "card")
        diag_layout = QVBoxLayout(diag_card)

        diag_title = QLabel("System Diagnostics & External Tools", diag_card)
        diag_title.setProperty("class", "header")
        diag_layout.addWidget(diag_title)

        diag_desc = QLabel(
            "Inspect and repair external binary dependencies (FFmpeg, FFprobe, Chromaprint fpcalc) via winget.",
            diag_card,
        )
        diag_desc.setProperty("class", "muted")
        diag_layout.addWidget(diag_desc)

        diag_actions = QHBoxLayout()
        self._open_doctor_btn = QPushButton(
            "Open Dependency Doctor...", diag_card)
        self._open_doctor_btn.clicked.connect(self.open_dependency_doctor)
        diag_actions.addWidget(self._open_doctor_btn)
        diag_actions.addStretch()
        diag_layout.addLayout(diag_actions)

        container_layout.addWidget(diag_card)

        # 3. Pipeline & Interface Options Card
        opts_card = QFrame(container)
        opts_card.setProperty("class", "card")
        opts_layout = QVBoxLayout(opts_card)

        opts_title = QLabel("System & Appearance", opts_card)
        opts_title.setProperty("class", "header")
        opts_layout.addWidget(opts_title)

        opts_grid = QGridLayout()
        opts_grid.setSpacing(8)

        # Theme Selector
        opts_grid.addWidget(QLabel("Interface Theme:", opts_card), 0, 0)
        self._theme_combo = QComboBox(opts_card)
        self._theme_combo.addItems(
            [ThemeMode.DARK.value, ThemeMode.LIGHT.value, ThemeMode.SYSTEM.value])
        opts_grid.addWidget(self._theme_combo, 0, 1)

        # Scan Mode Selector
        opts_grid.addWidget(QLabel("Default Scan Mode:", opts_card), 0, 2)
        self._scan_combo = QComboBox(opts_card)
        self._scan_combo.addItems(
            [ScanMode.QUICK.value, ScanMode.LONG.value, ScanMode.BOTH.value])
        opts_grid.addWidget(self._scan_combo, 0, 3)

        opts_grid.addWidget(
            QLabel("Max Concurrent Downloads:", opts_card), 1, 0)
        self._downloads_spin = QSpinBox(opts_card)
        self._downloads_spin.setRange(MIN_POSITIVE_INT, 16)
        opts_grid.addWidget(self._downloads_spin, 1, 1)

        opts_grid.addWidget(
            QLabel("Fingerprint Worker Count:", opts_card), 1, 2)
        self._workers_spin = QSpinBox(opts_card)
        self._workers_spin.setRange(MIN_POSITIVE_INT, 32)
        opts_grid.addWidget(self._workers_spin, 1, 3)

        opts_layout.addLayout(opts_grid)
        container_layout.addWidget(opts_card)

        # 3. API Secrets Card
        secrets_card = QFrame(container)
        secrets_card.setProperty("class", "card")
        secrets_layout = QVBoxLayout(secrets_card)

        secrets_title = QLabel(
            "API Keys & Webhooks", secrets_card)
        secrets_title.setProperty("class", "header")
        secrets_layout.addWidget(secrets_title)

        sec_grid = QGridLayout()
        sec_grid.setSpacing(8)

        self._acoustid_edit = self._create_secret_row(
            "AcoustID API Key:", 0, sec_grid, secrets_card)
        self._audiotag_edit = self._create_secret_row(
            "AudioTag API Key:", 1, sec_grid, secrets_card)
        self._discord_edit = self._create_secret_row(
            "Discord Webhook URL:", 2, sec_grid, secrets_card)

        secrets_layout.addLayout(sec_grid)
        container_layout.addWidget(secrets_card)

        scroll_area.setWidget(container)
        main_layout.addWidget(scroll_area)

        # Bottom Actions
        bottom_layout = QHBoxLayout()
        self._save_btn = QPushButton("Save Settings", self)
        self._save_btn.setProperty("class", "primary")
        self._save_btn.clicked.connect(self.save_data)
        bottom_layout.addWidget(self._save_btn)

        self._reset_btn = QPushButton("Reset to Defaults", self)
        self._reset_btn.clicked.connect(self.reset_defaults)
        bottom_layout.addWidget(self._reset_btn)

        bottom_layout.addStretch()
        main_layout.addLayout(bottom_layout)

    def _create_path_row(self, label_text: str, row: int, grid: QGridLayout, parent: QWidget) -> QLineEdit:
        grid.addWidget(QLabel(label_text, parent), row, 0)
        edit = QLineEdit(parent)
        grid.addWidget(edit, row, 1)

        browse_btn = QPushButton("Browse...", parent)
        browse_btn.clicked.connect(lambda: self._browse_folder(edit))
        grid.addWidget(browse_btn, row, 2)
        return edit

    def _create_secret_row(self, label_text: str, row: int, grid: QGridLayout, parent: QWidget) -> QLineEdit:
        grid.addWidget(QLabel(label_text, parent), row, 0)
        edit = QLineEdit(parent)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        grid.addWidget(edit, row, 1)

        toggle_btn = QPushButton("Show", parent)
        toggle_btn.clicked.connect(lambda: self._toggle_echo(edit, toggle_btn))
        grid.addWidget(toggle_btn, row, 2)
        return edit

    def _browse_folder(self, target_edit: QLineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select Directory", target_edit.text())
        if chosen:
            target_edit.setText(chosen)

    def _toggle_echo(self, edit: QLineEdit, button: QPushButton) -> None:
        if edit.echoMode() == QLineEdit.EchoMode.Password:
            edit.setEchoMode(QLineEdit.EchoMode.Normal)
            button.setText("Hide")
        else:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            button.setText("Show")

    @Slot()
    def load_data(self) -> None:
        """Load settings from SQLite and secrets from .secrets.json into UI fields."""
        settings = self._settings_repo.load()
        secrets = self._secrets_store.load()

        self._input_dir_edit.setText(str(settings.paths.input_dir))
        self._db_dir_edit.setText(str(settings.paths.db_dir))
        self._output_dir_edit.setText(str(settings.paths.output_dir))
        self._logs_dir_edit.setText(str(settings.paths.logs_dir))
        self._bin_dir_edit.setText(str(settings.paths.bin_dir))
        self._archive_dir_edit.setText(str(settings.paths.archive_dir))

        self._theme_combo.setCurrentText(settings.theme.value)
        self._scan_combo.setCurrentText(settings.scan_mode.value)
        self._downloads_spin.setValue(settings.max_concurrent_downloads)
        self._workers_spin.setValue(settings.fp_worker_count)

        self._acoustid_edit.setText(secrets.acoustid_api_key)
        self._audiotag_edit.setText(secrets.audiotag_api_key)
        self._discord_edit.setText(secrets.discord_webhook_url)

    @Slot()
    def save_data(self) -> None:
        """Collect form values, validate, and persist to SQLite and .secrets.json."""
        current = self._settings_repo.load()
        paths = WorkspacePaths(
            input_dir=Path(self._input_dir_edit.text().strip()),
            db_dir=Path(self._db_dir_edit.text().strip()),
            output_dir=Path(self._output_dir_edit.text().strip()),
            logs_dir=Path(self._logs_dir_edit.text().strip()),
            bin_dir=Path(self._bin_dir_edit.text().strip()),
            archive_dir=Path(self._archive_dir_edit.text().strip()),
        )

        settings = replace(
            current,
            paths=paths,
            theme=ThemeMode(self._theme_combo.currentText()),
            scan_mode=ScanMode(self._scan_combo.currentText()),
            max_concurrent_downloads=self._downloads_spin.value(),
            fp_worker_count=self._workers_spin.value(),
        )

        secrets = SecretCredentials(
            acoustid_api_key=self._acoustid_edit.text().strip(),
            audiotag_api_key=self._audiotag_edit.text().strip(),
            discord_webhook_url=self._discord_edit.text().strip(),
        )

        # Persist
        self._settings_repo.save(settings)
        self._secrets_store.save(secrets)

        self.log_requested.emit(
            "Application settings and secret credentials saved successfully.")
        self.settings_saved.emit(settings)

    @Slot()
    def reset_defaults(self) -> None:
        """Reset inputs to standard base_dir defaults."""
        defaults = AppSettings.default(self._base_dir)
        self._input_dir_edit.setText(str(defaults.paths.input_dir))
        self._db_dir_edit.setText(str(defaults.paths.db_dir))
        self._output_dir_edit.setText(str(defaults.paths.output_dir))
        self._logs_dir_edit.setText(str(defaults.paths.logs_dir))
        self._bin_dir_edit.setText(str(defaults.paths.bin_dir))
        self._archive_dir_edit.setText(str(defaults.paths.archive_dir))

        self._theme_combo.setCurrentText(defaults.theme.value)
        self._scan_combo.setCurrentText(defaults.scan_mode.value)
        self._downloads_spin.setValue(defaults.max_concurrent_downloads)
        self._workers_spin.setValue(defaults.fp_worker_count)

    @Slot()
    def open_dependency_doctor(self) -> None:
        """Open the modal DependencyDoctorDialog on demand."""
        if self._doctor_service is None:
            self.log_requested.emit(
                "Dependency Doctor service is unavailable.")
            return

        from src.ui.views.dependency_doctor_view import DependencyDoctorDialog
        dialog = DependencyDoctorDialog(self._doctor_service, self)
        dialog.doctor_view.log_requested.connect(self.log_requested)
        dialog.exec()
