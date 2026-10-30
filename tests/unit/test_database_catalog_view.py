"""Unit tests for DatabaseCatalogView and CatalogWorker."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication

from src.core.config.models import AppSettings, PklzCatalogEntry, WorkspacePaths
from src.core.fingerprinter.catalog_service import CatalogService, ReconciliationResult
from src.ui.views.database_catalog_view import DatabaseCatalogView
from src.ui.workers.catalog_worker import CatalogWorker


def test_database_catalog_view_init_and_refresh(qapp: QApplication, tmp_path: Path) -> None:
    """Verify DatabaseCatalogView initialization, summary display, and tree population."""
    mock_service = MagicMock(spec=CatalogService)
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(paths=WorkspacePaths.default(tmp_path))

    mock_service.list_catalog.return_value = [
        PklzCatalogEntry(
            file_path=tmp_path / "collection1.pklz",
            file_name="collection1.pklz",
            prefix_label="Rock80s",
            track_count=42,
            total_hashes=3500,
            file_size_bytes=1024 * 1024,
            sha256_hash="abcdef1234567890",
        )
    ]
    mock_service.get_catalog_summary.return_value = {
        "total_databases": 1,
        "total_tracks": 42,
        "total_hashes": 3500,
        "total_size_bytes": 1024 * 1024,
    }
    mock_service.get_database_track_names.return_value = ["Song A", "Song B"]

    view = DatabaseCatalogView(catalog_service=mock_service, settings_repo=mock_settings_repo)

    # Verify summary cards
    assert view._card_databases[1].text() == "1"
    assert view._card_tracks[1].text() == "42"
    assert view._card_hashes[1].text() == "3,500"
    assert "1.0 MB" in view._card_size[1].text()

    # Verify tree widget populated
    assert view._tree_widget.topLevelItemCount() == 1
    item = view._tree_widget.topLevelItem(0)
    assert item.text(0) == "collection1.pklz"
    assert item.text(1) == "Rock80s"

    # Select item and verify track drawer loads
    view._tree_widget.setCurrentItem(item)
    assert view._details_title.text() == "collection1.pklz"
    assert view._tracks_list.count() == 2
    assert view._tracks_list.item(0).text() == "Song A"


def test_database_catalog_view_filter(qapp: QApplication, tmp_path: Path) -> None:
    """Verify live search filtering."""
    mock_service = MagicMock(spec=CatalogService)
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(paths=WorkspacePaths.default(tmp_path))

    mock_service.list_catalog.return_value = [
        PklzCatalogEntry(
            file_path=tmp_path / "ambient.pklz",
            file_name="ambient.pklz",
            prefix_label="Chill",
            track_count=10,
        ),
        PklzCatalogEntry(
            file_path=tmp_path / "rock.pklz",
            file_name="rock.pklz",
            prefix_label="Metal",
            track_count=20,
        ),
    ]
    mock_service.get_catalog_summary.return_value = {
        "total_databases": 2,
        "total_tracks": 30,
        "total_hashes": 0,
        "total_size_bytes": 0,
    }

    view = DatabaseCatalogView(catalog_service=mock_service, settings_repo=mock_settings_repo)
    assert view._tree_widget.topLevelItemCount() == 2

    # Filter for 'ambient'
    view._search_input.setText("ambient")
    assert view._tree_widget.topLevelItemCount() == 1
    assert view._tree_widget.topLevelItem(0).text(0) == "ambient.pklz"

    # Filter for 'metal'
    view._search_input.setText("metal")
    assert view._tree_widget.topLevelItemCount() == 1
    assert view._tree_widget.topLevelItem(0).text(0) == "rock.pklz"


def test_catalog_worker_signals(tmp_path: Path) -> None:
    """Verify CatalogWorker signals and run execution."""
    mock_service = MagicMock(spec=CatalogService)
    expected_result = ReconciliationResult(
        discovered_files=3,
        new_or_updated=2,
        pruned_entries=1,
        failed_files=(),
        total_tracks=100,
        total_hashes=5000,
        total_size_bytes=2048,
    )
    mock_service.reconcile_directory.return_value = expected_result

    worker = CatalogWorker(catalog_service=mock_service, root_dir=tmp_path)

    completed_payload = []
    worker.reconciliation_completed.connect(completed_payload.append)

    worker.run()
    assert len(completed_payload) == 1
    assert completed_payload[0] == expected_result


def test_main_window_wires_database_catalog_view(qapp: QApplication, tmp_path: Path) -> None:
    """Verify MainWindow wires DatabaseCatalogView into index 1 and switches correctly."""
    from src.ui.main_window import MainWindow

    mock_catalog_service = MagicMock(spec=CatalogService)
    mock_catalog_service.list_catalog.return_value = []
    mock_catalog_service.get_catalog_summary.return_value = {
        "total_databases": 0,
        "total_tracks": 0,
        "total_hashes": 0,
        "total_size_bytes": 0,
    }
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(paths=WorkspacePaths.default(tmp_path))
    mock_secrets_store = MagicMock()
    from src.core.config.models import SecretCredentials
    mock_secrets_store.load.return_value = SecretCredentials()
    mock_doctor_service = MagicMock()
    mock_runner = MagicMock()

    window = MainWindow(
        base_dir=tmp_path,
        settings_repo=mock_settings_repo,
        secrets_store=mock_secrets_store,
        doctor_service=mock_doctor_service,
        process_runner=mock_runner,
        catalog_service=mock_catalog_service,
    )

    assert window.catalog_view is not None
    assert isinstance(window.catalog_view, DatabaseCatalogView)

    # Switch to Database Catalog (row 1)
    window._sidebar.setCurrentRow(1)
    assert window.view_stack.currentIndex() == 1
    assert window.view_stack.currentWidget() == window.catalog_view
