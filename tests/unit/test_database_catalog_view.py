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
    mock_settings_repo.load.return_value = AppSettings(
        paths=WorkspacePaths.default(tmp_path))

    file1 = tmp_path / "collection1.pklz"
    file1.write_bytes(b"PKLZ")

    mock_service.list_catalog.return_value = [
        PklzCatalogEntry(
            file_path=file1,
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

    view = DatabaseCatalogView(
        catalog_service=mock_service, settings_repo=mock_settings_repo)

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
    mock_settings_repo.load.return_value = AppSettings(
        paths=WorkspacePaths.default(tmp_path))

    f1 = tmp_path / "ambient.pklz"
    f2 = tmp_path / "rock.pklz"
    f1.write_bytes(b"A")
    f2.write_bytes(b"R")

    mock_service.list_catalog.return_value = [
        PklzCatalogEntry(
            file_path=f1,
            file_name="ambient.pklz",
            prefix_label="Chill",
            track_count=10,
        ),
        PklzCatalogEntry(
            file_path=f2,
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

    view = DatabaseCatalogView(
        catalog_service=mock_service, settings_repo=mock_settings_repo)
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
    mock_settings_repo.load.return_value = AppSettings(
        paths=WorkspacePaths.default(tmp_path))
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


def test_main_window_cross_view_signal_wiring(qapp: QApplication, tmp_path: Path) -> None:
    """Verify MainWindow cross-wires catalog_updated, settings_saved, and tab switching."""
    from src.ui.main_window import MainWindow

    mock_catalog_view = MagicMock(spec=DatabaseCatalogView)
    mock_catalog_view.catalog_updated = MagicMock()
    # Replace with real Qt Signal mock wrapper if needed or use real view
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(
        paths=WorkspacePaths.default(tmp_path))
    mock_secrets_store = MagicMock()
    from src.core.config.models import SecretCredentials
    mock_secrets_store.load.return_value = SecretCredentials()

    mock_catalog_service = MagicMock(spec=CatalogService)
    mock_catalog_service.list_catalog.return_value = []
    mock_catalog_service.get_catalog_summary.return_value = {
        "total_databases": 0, "total_tracks": 0, "total_hashes": 0, "total_size_bytes": 0,
    }

    mock_candidate_repo = MagicMock()
    mock_candidate_repo.list_candidates.return_value = []
    mock_catalog_repo = MagicMock()
    mock_catalog_repo.list_entries.return_value = []
    mock_matcher_service = MagicMock()
    mock_reconciler = MagicMock()

    from src.ui.views.scanner_view import ScannerView
    real_scanner_view = ScannerView(
        candidate_repo=mock_candidate_repo,
        catalog_repo=mock_catalog_repo,
        matcher_service=mock_matcher_service,
        reconciler=mock_reconciler,
        settings_repo=mock_settings_repo,
    )

    window = MainWindow(
        base_dir=tmp_path,
        settings_repo=mock_settings_repo,
        secrets_store=mock_secrets_store,
        doctor_service=MagicMock(),
        process_runner=MagicMock(),
        catalog_service=mock_catalog_service,
        scanner_view=real_scanner_view,
    )

    real_catalog_view = window.catalog_view
    assert real_catalog_view is not None

    mock_catalog_repo.list_entries.reset_mock()

    # 1. catalog_updated triggers scanner reload
    real_catalog_view.catalog_updated.emit()
    assert mock_catalog_repo.list_entries.called

    # 2. settings_saved updates catalog archive dir and reloads scanner
    mock_catalog_repo.list_entries.reset_mock()
    new_db_dir = tmp_path / "new_db_dir"
    new_paths = WorkspacePaths(
        input_dir=tmp_path / "input",
        archive_dir=tmp_path / "archive",
        db_dir=new_db_dir,
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        bin_dir=tmp_path / "bin",
    )
    new_settings = AppSettings(paths=new_paths)
    window.on_settings_saved(new_settings)

    assert real_catalog_view._dir_input.text() == str(new_db_dir)
    assert mock_catalog_repo.list_entries.called

    # 3. Switching tabs to Scanner triggers reload
    mock_catalog_repo.list_entries.reset_mock()
    window._sidebar.setCurrentRow(2)
    assert mock_catalog_repo.list_entries.called


def test_database_catalog_view_missing_badge_and_clean(qapp: QApplication, tmp_path: Path) -> None:
    """Verify missing database files receive [Missing] badge and clean_missing_entries emits catalog_updated."""
    mock_service = MagicMock(spec=CatalogService)
    mock_settings_repo = MagicMock()
    mock_settings_repo.load.return_value = AppSettings(
        paths=WorkspacePaths.default(tmp_path))

    # Existing file
    exist_file = tmp_path / "exist.pklz"
    exist_file.write_bytes(b"DATA")

    # Missing file
    ghost_file = tmp_path / "ghost.pklz"

    mock_service.list_catalog.return_value = [
        PklzCatalogEntry(file_path=exist_file,
                         file_name="exist.pklz", prefix_label="lib"),
        PklzCatalogEntry(file_path=ghost_file,
                         file_name="ghost.pklz", prefix_label="lib"),
    ]
    mock_service.get_catalog_summary.return_value = {
        "total_databases": 2,
        "total_tracks": 10,
        "total_hashes": 100,
        "total_size_bytes": 1000,
    }
    mock_service.prune_nonexistent.return_value = 1

    view = DatabaseCatalogView(
        catalog_service=mock_service, settings_repo=mock_settings_repo)

    assert view._tree_widget.topLevelItemCount() == 2
    item0 = view._tree_widget.topLevelItem(0)
    item1 = view._tree_widget.topLevelItem(1)

    assert item0.text(0) == "exist.pklz"
    assert item1.text(0) == "[Missing] ghost.pklz"

    # Verify signal emission on clean_missing_entries
    signal_emitted = []
    view.catalog_updated.connect(lambda: signal_emitted.append(True))

    view.clean_missing_entries()
    mock_service.prune_nonexistent.assert_called_once_with(global_prune=True)
    assert len(signal_emitted) == 1
