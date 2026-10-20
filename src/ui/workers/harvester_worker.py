"""Background QObject worker for non-blocking Harvester queue and disk processing."""
from __future__ import annotations

import logging
from pathlib import Path
from PySide6.QtCore import QObject, Signal, Slot

from src.core.harvester.service import (
    HarvesterCallbacks,
    HarvesterService,
    HarvestRunSummary,
)

logger = logging.getLogger(__name__)


class HarvesterWorker(QObject):
    """QThread worker executing HarvesterService pipelines without blocking the UI."""

    log_emitted = Signal(str)
    status_changed = Signal(str)
    progress_updated = Signal(int, int, str)
    task_updated = Signal(int)
    backoff_triggered = Signal(float, str)
    queue_completed = Signal(object)  # HarvestRunSummary
    disk_completed = Signal(list)  # list[FingerprintBatchResult]
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(
        self,
        service: HarvesterService,
        mode: str = "queue",
        disk_folder: Path | None = None,
        disk_prefix: str = "",
        disk_split: bool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._mode = mode
        self._disk_folder = disk_folder
        self._disk_prefix = disk_prefix
        self._disk_split = disk_split

    @Slot()
    def run(self) -> None:
        """Execute the configured harvest operation on the dedicated worker thread."""
        callbacks = HarvesterCallbacks(
            on_log=self.log_emitted.emit,
            on_status=self.status_changed.emit,
            on_progress=self.progress_updated.emit,
            on_task_updated=self.task_updated.emit,
            on_backoff=self.backoff_triggered.emit,
        )

        try:
            if self._mode == "queue":
                summary: HarvestRunSummary = self._service.run_queue(
                    callbacks=callbacks)
                self.queue_completed.emit(summary)
            elif self._mode == "disk":
                if self._disk_folder is None or not self._disk_folder.is_dir():
                    raise ValueError(
                        f"Invalid disk source folder: {self._disk_folder}")

                results = self._service.run_local_disk_folder(
                    source_folder=self._disk_folder,
                    prefix_label=self._disk_prefix,
                    split_override=self._disk_split,
                    callbacks=callbacks,
                )
                self.disk_completed.emit(results)
            else:
                raise ValueError(
                    f"Unknown harvester worker mode: '{self._mode}'")

        except Exception as exc:
            logger.error("Unhandled error in HarvesterWorker: %s",
                         exc, exc_info=True)
            self.error_occurred.emit(str(exc))
        finally:
            self.finished.emit()
