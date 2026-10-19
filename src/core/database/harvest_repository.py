"""Parameterized SQLite repository for harvest_queue and harvest_items."""
from __future__ import annotations

import sqlite3
from typing import Sequence

from src.core.config.models import (
    HarvestItemRecord,
    HarvestItemStatus,
    HarvestSourceType,
    HarvestTask,
    HarvestTaskStatus,
)
from src.core.database.connection import DatabaseError, DatabaseManager


class DuplicateHarvestTaskError(DatabaseError):
    """Raised when an attempt is made to enqueue a URL that already exists."""


class HarvestRepository:
    """Repository managing harvest_queue and harvest_items operations."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def enqueue_task(
        self,
        raw_url: str,
        normalized_url: str,
        source_type: HarvestSourceType,
        prefix_label: str,
        title: str = "",
    ) -> HarvestTask:
        """Insert a new harvest task in PENDING status.

        Raises:
            DuplicateHarvestTaskError: If normalized_url is already present.
            DatabaseError: If insertion fails.
        """
        sql = """
        INSERT INTO harvest_queue (
            raw_url,
            normalized_url,
            source_type,
            prefix_label,
            title,
            status,
            total_items,
            completed_items,
            skipped_items,
            error_message,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, '', datetime('now'), datetime('now'));
        """
        try:
            cursor = self._db.execute(
                sql,
                (
                    raw_url,
                    normalized_url,
                    source_type.value,
                    prefix_label,
                    title,
                    HarvestTaskStatus.PENDING.value,
                ),
            )
            task_id = cursor.lastrowid
            task = self.get_task_by_id(task_id)
            if task is None:
                raise DatabaseError(
                    f"Failed to fetch newly created task id {task_id}")
            return task
        except sqlite3.IntegrityError as err:
            if "UNIQUE constraint failed" in str(err) and "normalized_url" in str(err):
                raise DuplicateHarvestTaskError(
                    f"A task with normalized URL '{normalized_url}' is already queued."
                ) from err
            raise DatabaseError(
                f"Integrity error enqueueing task: {err}") from err
        except DatabaseError as err:
            # Check original cause in case wrapped by DatabaseManager
            if err.__cause__ and isinstance(err.__cause__, sqlite3.IntegrityError):
                if "UNIQUE constraint failed" in str(err.__cause__) and "normalized_url" in str(err.__cause__):
                    raise DuplicateHarvestTaskError(
                        f"A task with normalized URL '{normalized_url}' is already queued."
                    ) from err
            raise

    def get_task_by_id(self, task_id: int) -> HarvestTask | None:
        """Fetch a harvest task by its primary key."""
        sql = "SELECT * FROM harvest_queue WHERE id = ?;"
        row = self._db.query_one(sql, (task_id,))
        if row is None:
            return None
        return self._row_to_task(row)

    def get_task_by_normalized_url(self, normalized_url: str) -> HarvestTask | None:
        """Fetch a harvest task by its unique normalized URL."""
        sql = "SELECT * FROM harvest_queue WHERE normalized_url = ?;"
        row = self._db.query_one(sql, (normalized_url,))
        if row is None:
            return None
        return self._row_to_task(row)

    def list_tasks(self, status: HarvestTaskStatus | None = None) -> list[HarvestTask]:
        """List tasks, optionally filtered by status, ordered chronologically."""
        if status is not None:
            sql = "SELECT * FROM harvest_queue WHERE status = ? ORDER BY id ASC;"
            rows = self._db.query_all(sql, (status.value,))
        else:
            sql = "SELECT * FROM harvest_queue ORDER BY id ASC;"
            rows = self._db.query_all(sql)
        return [self._row_to_task(r) for r in rows]

    def update_task_metadata(
        self,
        task_id: int,
        title: str,
        prefix_label: str,
        total_items: int,
    ) -> None:
        """Update discovery metadata for a task."""
        sql = """
        UPDATE harvest_queue
        SET title = ?,
            prefix_label = ?,
            total_items = ?,
            updated_at = datetime('now')
        WHERE id = ?;
        """
        self._db.execute(sql, (title, prefix_label, total_items, task_id))

    def update_task_status(
        self,
        task_id: int,
        status: HarvestTaskStatus,
        error_message: str = "",
    ) -> None:
        """Update task lifecycle status and optional error message."""
        sql = """
        UPDATE harvest_queue
        SET status = ?,
            error_message = ?,
            updated_at = datetime('now')
        WHERE id = ?;
        """
        self._db.execute(sql, (status.value, error_message, task_id))

    def update_task_progress(
        self,
        task_id: int,
        completed_items: int,
        skipped_items: int,
    ) -> None:
        """Update processed items progress counts."""
        sql = """
        UPDATE harvest_queue
        SET completed_items = ?,
            skipped_items = ?,
            updated_at = datetime('now')
        WHERE id = ?;
        """
        self._db.execute(sql, (completed_items, skipped_items, task_id))

    def delete_task(self, task_id: int) -> bool:
        """Delete task from queue. Automatically cascades to harvest_items.

        Returns True if a row was deleted.
        """
        sql = "DELETE FROM harvest_queue WHERE id = ?;"
        cursor = self._db.execute(sql, (task_id,))
        return cursor.rowcount > 0

    # ------------------- Item Deduplication & Tracking -------------------

    def upsert_item(
        self,
        task_id: int,
        item_key: str,
        title: str,
        source_url: str,
        duration_seconds: float | None = None,
        status: HarvestItemStatus = HarvestItemStatus.PENDING,
    ) -> HarvestItemRecord:
        """Upsert a discovered audio item associated with a harvest task."""
        sql = """
        INSERT INTO harvest_items (
            task_id,
            item_key,
            title,
            source_url,
            duration_seconds,
            status,
            pklz_filename,
            fingerprinted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, '', NULL)
        ON CONFLICT(item_key) DO UPDATE SET
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE harvest_items.title END,
            source_url = CASE WHEN excluded.source_url != '' THEN excluded.source_url ELSE harvest_items.source_url END,
            duration_seconds = COALESCE(excluded.duration_seconds, harvest_items.duration_seconds);
        """
        self._db.execute(
            sql,
            (
                task_id,
                item_key,
                title,
                source_url,
                duration_seconds,
                status.value,
            ),
        )
        row = self._db.query_one(
            "SELECT * FROM harvest_items WHERE item_key = ?;", (item_key,))
        if row is None:
            raise DatabaseError(
                f"Failed to retrieve upserted harvest item with key '{item_key}'")
        return self._row_to_item(row)

    def mark_items_fingerprinted(
        self,
        item_keys: Sequence[str],
        pklz_filename: str,
    ) -> int:
        """Batch-update items to FINGERPRINTED status with target pklz_filename."""
        if not item_keys:
            return 0

        # Execute inside a transaction
        sql = """
        UPDATE harvest_items
        SET status = ?,
            pklz_filename = ?,
            fingerprinted_at = datetime('now')
        WHERE item_key = ?;
        """
        count = 0
        with self._db.transaction() as conn:
            for key in item_keys:
                cursor = conn.execute(
                    sql,
                    (
                        HarvestItemStatus.FINGERPRINTED.value,
                        pklz_filename,
                        key,
                    ),
                )
                count += cursor.rowcount
        return count

    def is_item_fingerprinted(self, item_key: str) -> bool:
        """Check whether an item key has already been successfully fingerprinted."""
        sql = """
        SELECT 1 FROM harvest_items
        WHERE item_key = ? AND status = ?;
        """
        row = self._db.query_one(
            sql, (item_key, HarvestItemStatus.FINGERPRINTED.value))
        return row is not None

    def get_fingerprinted_item_keys(self, item_keys: Sequence[str]) -> set[str]:
        """Return the subset of provided item keys that are already fingerprinted."""
        if not item_keys:
            return set()

        # Batch query using chunks to prevent SQLite parameter limits
        chunk_size = 500
        found: set[str] = set()
        for i in range(0, len(item_keys), chunk_size):
            chunk = item_keys[i: i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            sql = f"""
            SELECT item_key FROM harvest_items
            WHERE status = ? AND item_key IN ({placeholders});
            """
            rows = self._db.query_all(
                sql, (HarvestItemStatus.FINGERPRINTED.value, *chunk))
            for r in rows:
                found.add(str(r["item_key"]))
        return found

    def list_items_for_task(self, task_id: int) -> list[HarvestItemRecord]:
        """List all items associated with a given harvest task."""
        sql = "SELECT * FROM harvest_items WHERE task_id = ? ORDER BY id ASC;"
        rows = self._db.query_all(sql, (task_id,))
        return [self._row_to_item(r) for r in rows]

    # ------------------- Private Row Mapping Helpers -------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> HarvestTask:
        return HarvestTask(
            id=int(row["id"]),
            raw_url=str(row["raw_url"]),
            normalized_url=str(row["normalized_url"]),
            source_type=HarvestSourceType(str(row["source_type"])),
            prefix_label=str(row["prefix_label"]),
            title=str(row["title"]),
            status=HarvestTaskStatus(str(row["status"])),
            total_items=int(row["total_items"]),
            completed_items=int(row["completed_items"]),
            skipped_items=int(row["skipped_items"]),
            error_message=str(row["error_message"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> HarvestItemRecord:
        dur = row["duration_seconds"]
        return HarvestItemRecord(
            id=int(row["id"]),
            task_id=int(row["task_id"]),
            item_key=str(row["item_key"]),
            title=str(row["title"]),
            source_url=str(row["source_url"]),
            duration_seconds=float(dur) if dur is not None else None,
            status=HarvestItemStatus(str(row["status"])),
            pklz_filename=str(row["pklz_filename"]),
            fingerprinted_at=str(row["fingerprinted_at"]
                                 ) if row["fingerprinted_at"] else None,
        )
