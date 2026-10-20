"""Application Entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication

from src.core.config.constants import APP_NAME, SECRETS_FILENAME, STATE_DB_FILENAME
from src.core.config.models import WorkspacePaths

from src.core.config.secrets_store import SecretsStore
from src.core.database.connection import DatabaseManager
from src.core.database.harvest_repository import HarvestRepository
from src.core.database.catalog_repository import CatalogRepository
from src.core.database.migrations import MigrationManager
from src.core.database.settings_repository import SettingsRepository
from src.core.fingerprinter.builder import FingerprintBuilderService
from src.core.harvester.audio_splitter import AudioSplitterService
from src.core.harvester.service import HarvesterService
from src.core.harvester.web_crawler import WebDirectoryCrawler
from src.core.process.binary_locator import BinaryLocator
from src.core.process.dependency_doctor import DependencyDoctorService
from src.core.process.runner import SilentProcessRunner
from src.ui.main_window import MainWindow
from src.ui.theme import build_stylesheet


def bootstrap_application(base_dir: Path | None = None) -> tuple[QApplication, MainWindow]:
    """Initialize core databases, services, and MainWindow shell."""
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

    # 1. Initialize default workspace paths and ensure directories exist
    paths = WorkspacePaths.default(base_dir)
    for p in (paths.input_dir, paths.archive_dir, paths.db_dir, paths.output_dir, paths.logs_dir, paths.bin_dir):
        p.mkdir(parents=True, exist_ok=True)

    # 2. Initialize Database and run migrations (V1, V2)
    db_file = paths.db_dir / STATE_DB_FILENAME
    db_manager = DatabaseManager(db_file)
    migration_manager = MigrationManager(db_manager)
    migration_manager.apply_all()

    # 3. Instantiate repositories and services
    settings_repo = SettingsRepository(db=db_manager, base_dir=base_dir)
    harvest_repo = HarvestRepository(db=db_manager)
    catalog_repo = CatalogRepository(db=db_manager)
    secrets_store = SecretsStore(base_dir / SECRETS_FILENAME)
    app_settings = settings_repo.load()

    process_runner = SilentProcessRunner()
    binary_locator = BinaryLocator(bin_dir=paths.bin_dir)
    doctor_service = DependencyDoctorService(
        binary_locator=binary_locator,
        process_runner=process_runner,
    )

    audio_splitter = AudioSplitterService(
        runner=process_runner,
        locator=binary_locator,
    )
    builder_service = FingerprintBuilderService(
        runner=process_runner,
        locator=binary_locator,
        catalog_repo=catalog_repo,
    )
    web_crawler = WebDirectoryCrawler()

    harvester_service = HarvesterService(
        harvest_repo=harvest_repo,
        catalog_repo=catalog_repo,
        settings_repo=settings_repo,
        splitter=audio_splitter,
        builder=builder_service,
        crawler=web_crawler,
    )

    # 4. Initialize Qt Application
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(build_stylesheet(app_settings.theme))

    # 5. Build Main Window
    window = MainWindow(
        base_dir=base_dir,
        settings_repo=settings_repo,
        secrets_store=secrets_store,
        doctor_service=doctor_service,
        process_runner=process_runner,
        harvester_service=harvester_service,
    )

    return app, window


def main() -> int:
    """Run GUI desktop event loop."""
    app, window = bootstrap_application()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
