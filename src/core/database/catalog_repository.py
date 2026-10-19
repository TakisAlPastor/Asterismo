"""Parameterized SQLite repository for pklz_catalog operations."""
from __future__ import annotations

from pathlib import Path
import sqlite3

from src.core.config.models import PklzCatalogEntry
from src.core.database.connection import DatabaseError, DatabaseManager


class CatalogRepository:
    """Repository managing local acoustic database (.pklz) metadata records."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert_entry(
        self,
        file_path: Path,
        prefix_label: str,
        batch_index: int = 1,
        track_count: int = 0,
        total_hashes: int = 0,
        file_size_bytes: int = 0,
        sha256_hash: str = "",
    ) -> PklzCatalogEntry:
        """Insert or update a .pklz catalog entry by its unique file_path."""
        resolved_path = file_path.resolve()
        file_path_str = str(resolved_path)
        file_name = resolved_path.name

        sql = """
        INSERT INTO pklz_catalog (
            file_path,
            file_name,
            prefix_label,
            batch_index,
            track_count,
            total_hashes,
            file_size_bytes,
            sha256_hash,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(file_path) DO UPDATE SET
            file_name = excluded.file_name,
            prefix_label = excluded.prefix_label,
            batch_index = excluded.batch_index,
            track_count = excluded.track_count,
            total_hashes = excluded.total_hashes,
            file_size_bytes = excluded.file_size_bytes,
            sha256_hash = excluded.sha256_hash,
            updated_at = excluded.updated_at;
        """
        self._db.execute(
            sql,
            (
                file_path_str,
                file_name,
                prefix_label,
                batch_index,
                track_count,
                total_hashes,
                file_size_bytes,
                sha256_hash,
            ),
        )
        entry = self.get_by_path(resolved_path)
        if entry is None:
            raise DatabaseError(
                f"Failed to fetch upserted catalog entry for '{file_path_str}'")
        return entry

    def get_by_path(self, file_path: Path) -> PklzCatalogEntry | None:
        """Fetch a catalog entry by its file path."""
        resolved_path = str(file_path.resolve())
        sql = "SELECT * FROM pklz_catalog WHERE file_path = ?;"
        row = self._db.query_one(sql, (resolved_path,))
        if row is None:
            return None
        return self._row_to_entry(row)

    def list_entries(self, prefix_label: str | None = None) -> list[PklzCatalogEntry]:
        """List catalog entries, optionally filtered by prefix label, ordered alphabetically."""
        if prefix_label is not None:
            sql = "SELECT * FROM pklz_catalog WHERE prefix_label = ? ORDER BY file_name ASC;"
            rows = self._db.query_all(sql, (prefix_label,))
        else:
            sql = "SELECT * FROM pklz_catalog ORDER BY file_name ASC;"
            rows = self._db.query_all(sql)
        return [self._row_to_entry(r) for r in rows]

    def delete_by_path(self, file_path: Path) -> bool:
        """Delete an entry by its file path. Returns True if an entry was removed."""
        resolved_path = str(file_path.resolve())
        sql = "DELETE FROM pklz_catalog WHERE file_path = ?;"
        cursor = self._db.execute(sql, (resolved_path,))
        return cursor.rowcount > 0

    # ------------------- Private Row Mapping Helpers -------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> PklzCatalogEntry:
        return PklzCatalogEntry(
            id=int(row["id"]),
            file_path=Path(str(row["file_path"])),
            file_name=str(row["file_name"]),
            prefix_label=str(row["prefix_label"]),
            batch_index=int(row["batch_index"]),
            track_count=int(row["track_count"]),
            total_hashes=int(row["total_hashes"]),
            file_size_bytes=int(row["file_size_bytes"]),
            sha256_hash=str(row["sha256_hash"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
