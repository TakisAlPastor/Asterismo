"""Background QObject worker for non-blocking catalog reconciliation and metadata inspection."""
from __future__ import annotations

import logging
from pathlib import Path
import threading
from PySide6.QtCore import QObject, Signal, Slot

from src.core.fingerprinter.catalog_service import (
    CatalogService,
    CatalogServiceCancelledError,
    CatalogServiceError,
    ReconciliationResult,
)

logger = logging.getLogger(__name__)


class CatalogWorker(QObject):
    """QThread worker executing CatalogService directory reconciliation without blocking the GUI."""

    progress_updated = Signal(int, int, str)
    status_changed = Signal(str)
    reconciliation_completed = Signal(object)  # ReconciliationResult
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(
        self,
        catalog_service: CatalogService,
        root_dir: Path,
        prune_missing: bool = True,
        force_reindex: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = catalog_service
        self._root_dir = root_dir
        self._prune_missing = prune_missing
        self._force_reindex = force_reindex
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Signal the worker to halt reconciliation gracefully."""
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        """Execute reconciliation on the dedicated background thread."""
        try:
            self.status_changed.emit(
                f"Scanning '{self._root_dir.name}' for .pklz databases...")
            result: ReconciliationResult = self._service.reconcile_directory(
                root_dir=self._root_dir,
                prune_missing=self._prune_missing,
                force_reindex=self._force_reindex,
                cancel_event=self._cancel_event,
                progress_callback=self.progress_updated.emit,
            )
            self.reconciliation_completed.emit(result)
        except CatalogServiceCancelledError:
            logger.info("CatalogWorker reconciliation cancelled.")
            self.status_changed.emit("Reconciliation cancelled.")
        except (CatalogServiceError, Exception) as exc:
            logger.exception("CatalogWorker error: %s", exc)
            self.error_occurred.emit(str(exc))
        finally:
            self.finished.emit()
