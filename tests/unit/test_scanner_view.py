"""Unit tests for ScannerView and LocalMatcherWorker."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.core.config.models import (
    AppSettings,
    LocalMatchResult,
    MatchOptions,
    PklzCatalogEntry,
    ScanCandidateRecord,
    ScanCandidateStatus,
    ScanMatchRecord,
    SecretCredentials,
    WorkspacePaths,
)
from src.core.database.candidate_repository import CandidateRepository
from src.core.database.catalog_repository import CatalogRepository
from src.core.database.settings_repository import SettingsRepository
from src.core.matcher.candidate_reconciler import CandidateReconciler
from src.core.matcher.local_matcher import LocalMatcherService
from src.ui.views.scanner_view import ScannerView
from src.ui.workers.matcher_worker import LocalMatcherWorker


def _create_mock_repos(tmp_path: Path):
    mock_candidate_repo = MagicMock(spec=CandidateRepository)
    mock_catalog_repo = MagicMock(spec=CatalogRepository)
    mock_matcher_service = MagicMock(spec=LocalMatcherService)
    mock_reconciler = MagicMock(spec=CandidateReconciler)
    mock_settings_repo = MagicMock(spec=SettingsRepository)

    settings = AppSettings(paths=WorkspacePaths.default(tmp_path))
    mock_settings_repo.load.return_value = settings

    return (
        mock_candidate_repo,
        mock_catalog_repo,
        mock_matcher_service,
        mock_reconciler,
        mock_settings_repo,
    )


def test_scanner_view_init_and_population(qapp: QApplication, tmp_path: Path) -> None:
    """Verify ScannerView initialization, candidate table rendering, and database tree population."""
    (
        cand_repo,
        cat_repo,
        matcher_service,
        reconciler,
        settings_repo,
    ) = _create_mock_repos(tmp_path)

    cat_repo.list_entries.return_value = [
        PklzCatalogEntry(
            file_path=tmp_path / "eurobeat.pklz",
            file_name="eurobeat.pklz",
            prefix_label="Eurobeat",
            track_count=150,
        )
    ]

    cand_repo.list_candidates.return_value = [
        ScanCandidateRecord(
            id=1,
            file_path=tmp_path / "song1.mp3",
            file_name="song1.mp3",
            relative_path="song1.mp3",
            file_size_bytes=4096,
            duration_seconds=125.5,
            status=ScanCandidateStatus.PENDING,
        ),
        ScanCandidateRecord(
            id=2,
            file_path=tmp_path / "song2.wav",
            file_name="song2.wav",
            relative_path="sub/song2.wav",
            file_size_bytes=8192,
            duration_seconds=60.0,
            status=ScanCandidateStatus.SCANNED,
            quick_scanned_at="2026-10-29T12:00:00",
        ),
    ]

    cand_repo.get_matches_for_candidate.side_effect = lambda cid: (
        [
            ScanMatchRecord(
                id=10,
                candidate_id=2,
                engine="audfprint",
                matched_track="Running in the 90s",
                matched_database="eurobeat.pklz",
                aligned_hashes=25,
                confidence=0.88,
            )
        ]
        if cid == 2
        else []
    )

    view = ScannerView(
        candidate_repo=cand_repo,
        catalog_repo=cat_repo,
        matcher_service=matcher_service,
        reconciler=reconciler,
        settings_repo=settings_repo,
    )

    # Candidate table checks
    assert view._cand_table.rowCount() == 2
    assert view._cand_table.item(0, 2).text() == "song1.mp3"
    assert view._cand_table.item(0, 1).text() == "PENDING"
    assert view._cand_table.item(0, 3).text() == "02:06"
    assert view._cand_table.item(0, 5).text() == "0"

    assert view._cand_table.item(1, 2).text() == "song2.wav"
    assert view._cand_table.item(1, 1).text() == "SCANNED"
    assert view._cand_table.item(1, 5).text() == "1"

    # Database tree checks
    assert view._db_tree.topLevelItemCount() == 1
    parent_item = view._db_tree.topLevelItem(0)
    assert parent_item.text(0) == "Eurobeat"
    assert parent_item.childCount() == 1
    assert parent_item.child(0).text(0) == "eurobeat.pklz"

    # Check selected paths
    paths = view.get_selected_database_paths()
    assert len(paths) == 1
    assert paths[0] == tmp_path / "eurobeat.pklz"


def test_scanner_view_candidate_and_db_selection(qapp: QApplication, tmp_path: Path) -> None:
    """Verify select/deselect all buttons and candidate filtering."""
    (
        cand_repo,
        cat_repo,
        matcher_service,
        reconciler,
        settings_repo,
    ) = _create_mock_repos(tmp_path)

    cat_repo.list_entries.return_value = [
        PklzCatalogEntry(
            file_path=tmp_path / "db1.pklz",
            file_name="db1.pklz",
            prefix_label="Rock",
            track_count=10,
        ),
        PklzCatalogEntry(
            file_path=tmp_path / "db2.pklz",
            file_name="db2.pklz",
            prefix_label="Jazz",
            track_count=20,
        ),
    ]

    cand_repo.list_candidates.return_value = [
        ScanCandidateRecord(
            id=1,
            file_path=tmp_path / "alpha.mp3",
            file_name="alpha.mp3",
            relative_path="alpha.mp3",
            file_size_bytes=1000,
            status=ScanCandidateStatus.PENDING,
        ),
        ScanCandidateRecord(
            id=2,
            file_path=tmp_path / "beta.wav",
            file_name="beta.wav",
            relative_path="beta.wav",
            file_size_bytes=2000,
            status=ScanCandidateStatus.SCANNED,
        ),
    ]
    cand_repo.get_matches_for_candidate.return_value = []

    view = ScannerView(
        candidate_repo=cand_repo,
        catalog_repo=cat_repo,
        matcher_service=matcher_service,
        reconciler=reconciler,
        settings_repo=settings_repo,
    )

    # Test Database deselect / select
    assert len(view.get_selected_database_paths()) == 2
    view._deselect_all_databases()
    assert len(view.get_selected_database_paths()) == 0
    view._select_all_databases()
    assert len(view.get_selected_database_paths()) == 2

    # Test candidate select unscanned
    view._select_unscanned_candidates()
    selected = view.get_selected_candidates()
    assert len(selected) == 1
    assert selected[0].id == 1

    # Test candidate select all
    view._select_all_candidates()
    selected_all = view.get_selected_candidates()
    assert len(selected_all) == 2

    # Test candidate filter
    view._cand_filter_combo.setText("beta")
    assert view._cand_table.rowCount() == 1
    assert view._cand_table.item(0, 2).text() == "beta.wav"


def test_scanner_view_match_inspector(qapp: QApplication, tmp_path: Path) -> None:
    """Verify match hits table updates when candidate row is clicked."""
    (
        cand_repo,
        cat_repo,
        matcher_service,
        reconciler,
        settings_repo,
    ) = _create_mock_repos(tmp_path)

    cat_repo.list_entries.return_value = []
    cand = ScanCandidateRecord(
        id=42,
        file_path=tmp_path / "lost_tape.mp3",
        file_name="lost_tape.mp3",
        relative_path="lost_tape.mp3",
        file_size_bytes=5000,
        status=ScanCandidateStatus.SCANNED,
    )
    cand_repo.list_candidates.return_value = [cand]
    cand_repo.get_matches_for_candidate.return_value = [
        ScanMatchRecord(
            id=1,
            candidate_id=42,
            engine="audfprint",
            matched_track="Mysterious Song",
            matched_database="synthwave.pklz",
            aligned_hashes=45,
            offset_seconds=12.34,
            confidence=0.95,
        )
    ]

    view = ScannerView(
        candidate_repo=cand_repo,
        catalog_repo=cat_repo,
        matcher_service=matcher_service,
        reconciler=reconciler,
        settings_repo=settings_repo,
    )

    # Select row 0
    view._cand_table.selectRow(0)
    assert view._matches_table.rowCount() == 1
    assert view._matches_table.item(0, 0).text() == "Mysterious Song"
    assert view._matches_table.item(0, 1).text() == "synthwave.pklz"
    assert view._matches_table.item(0, 2).text() == "45"
    assert view._matches_table.item(0, 3).text() == "12.34"
    assert "95.0%" in view._matches_table.item(0, 4).text()


def test_matcher_worker_lifecycle(tmp_path: Path) -> None:
    """Verify LocalMatcherWorker signals and execution."""
    mock_service = MagicMock(spec=LocalMatcherService)
    cand = ScanCandidateRecord(
        id=1,
        file_path=tmp_path / "clip.mp3",
        file_name="clip.mp3",
        relative_path="clip.mp3",
        file_size_bytes=1000,
    )
    match_rec = ScanMatchRecord(
        id=1,
        candidate_id=1,
        engine="audfprint",
        matched_track="Song 1",
        matched_database="db.pklz",
        aligned_hashes=20,
    )
    expected_result = LocalMatchResult(
        candidate_path=cand.file_path,
        candidate_id=cand.id,
        matches=(match_rec,),
        databases_searched=1,
        elapsed_seconds=0.1,
    )
    mock_service.match_single_candidate.return_value = expected_result

    worker = LocalMatcherWorker(
        matcher_service=mock_service,
        candidates=[cand],
        target_databases=[tmp_path / "db.pklz"],
        options=MatchOptions(),
    )

    candidates_done = []
    matches_emitted = []
    scan_done = []

    worker.candidate_completed.connect(candidates_done.append)
    worker.match_detected.connect(matches_emitted.append)
    worker.scan_completed.connect(scan_done.append)

    worker.run()

    assert len(candidates_done) == 1
    assert candidates_done[0] == expected_result
    assert len(matches_emitted) == 1
    assert matches_emitted[0] == match_rec
    assert len(scan_done) == 1


def test_matcher_worker_cancellation(tmp_path: Path) -> None:
    """Verify LocalMatcherWorker gracefully handles cancellation."""
    mock_service = MagicMock(spec=LocalMatcherService)
    cand = ScanCandidateRecord(
        id=1,
        file_path=tmp_path / "clip.mp3",
        file_name="clip.mp3",
        relative_path="clip.mp3",
        file_size_bytes=1000,
    )

    worker = LocalMatcherWorker(
        matcher_service=mock_service,
        candidates=[cand],
        target_databases=[tmp_path / "db.pklz"],
    )

    status_emitted = []
    worker.status_changed.connect(status_emitted.append)

    worker.cancel()
    worker.run()

    assert any("cancelled" in s.lower() for s in status_emitted)
    mock_service.match_single_candidate.assert_not_called()


def test_main_window_wires_scanner_view(qapp: QApplication, tmp_path: Path) -> None:
    """Verify MainWindow integrates ScannerView into stack index 2."""
    from src.ui.main_window import MainWindow

    (
        cand_repo,
        cat_repo,
        matcher_service,
        reconciler,
        settings_repo,
    ) = _create_mock_repos(tmp_path)

    cand_repo.list_candidates.return_value = []
    cat_repo.list_entries.return_value = []

    mock_scanner_view = ScannerView(
        candidate_repo=cand_repo,
        catalog_repo=cat_repo,
        matcher_service=matcher_service,
        reconciler=reconciler,
        settings_repo=settings_repo,
    )

    mock_secrets_store = MagicMock()
    mock_secrets_store.load.return_value = SecretCredentials()
    mock_doctor_service = MagicMock()
    mock_runner = MagicMock()

    window = MainWindow(
        base_dir=tmp_path,
        settings_repo=settings_repo,
        secrets_store=mock_secrets_store,
        doctor_service=mock_doctor_service,
        process_runner=mock_runner,
        scanner_view=mock_scanner_view,
    )

    assert window.scanner_view is not None
    assert isinstance(window.scanner_view, ScannerView)

    # Switch to Scanner (row index 2)
    window._sidebar.setCurrentRow(2)
    assert window.view_stack.currentIndex() == 2
    assert window.view_stack.currentWidget() == mock_scanner_view
