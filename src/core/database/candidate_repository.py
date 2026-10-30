"""Parameterized SQLite repository for candidate audio songs and scan matches."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Sequence

from src.core.config.models import (
    ScanCandidateRecord,
    ScanCandidateStatus,
    ScanMatchRecord,
)
from src.core.database.connection import DatabaseError, DatabaseManager


class CandidateRepository:
    """Repository managing local candidate audio files and acoustic matching results."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    # ------------------- Candidate CRUD -------------------

    def upsert_candidate(
        self,
        file_path: Path,
        file_name: str,
        relative_path: str,
        file_size_bytes: int,
        duration_seconds: float | None = None,
        status: ScanCandidateStatus = ScanCandidateStatus.PENDING,
    ) -> ScanCandidateRecord:
        """Insert or update a candidate audio record by its unique resolved file_path."""
        resolved_path = str(file_path.resolve())
        status_val = status.value if isinstance(
            status, ScanCandidateStatus) else str(status)

        sql = """
        INSERT INTO scan_candidates (
            file_path,
            file_name,
            relative_path,
            file_size_bytes,
            duration_seconds,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(file_path) DO UPDATE SET
            file_name = excluded.file_name,
            relative_path = excluded.relative_path,
            file_size_bytes = excluded.file_size_bytes,
            duration_seconds = COALESCE(excluded.duration_seconds, scan_candidates.duration_seconds),
            status = CASE
                WHEN scan_candidates.status = 'missing' THEN excluded.status
                ELSE scan_candidates.status
            END,
            updated_at = excluded.updated_at;
        """
        self._db.execute(
            sql,
            (
                resolved_path,
                file_name,
                relative_path,
                file_size_bytes,
                duration_seconds,
                status_val,
            ),
        )
        record = self.get_candidate_by_path(file_path)
        if record is None:
            raise DatabaseError(
                f"Failed to fetch upserted candidate record for '{resolved_path}'"
            )
        return record

    def get_candidate_by_path(self, file_path: Path) -> ScanCandidateRecord | None:
        """Fetch a candidate record by its file path."""
        resolved_path = str(file_path.resolve())
        sql = "SELECT * FROM scan_candidates WHERE file_path = ?;"
        row = self._db.query_one(sql, (resolved_path,))
        if row is None:
            return None
        return self._row_to_candidate(row)

    def get_candidate_by_id(self, candidate_id: int) -> ScanCandidateRecord | None:
        """Fetch a candidate record by its database primary key."""
        sql = "SELECT * FROM scan_candidates WHERE id = ?;"
        row = self._db.query_one(sql, (candidate_id,))
        if row is None:
            return None
        return self._row_to_candidate(row)

    def list_candidates(
        self, status: ScanCandidateStatus | None = None
    ) -> list[ScanCandidateRecord]:
        """List all candidate records, optionally filtered by status, ordered by relative_path."""
        if status is not None:
            status_val = status.value if isinstance(
                status, ScanCandidateStatus) else str(status)
            sql = "SELECT * FROM scan_candidates WHERE status = ? ORDER BY relative_path ASC;"
            rows = self._db.query_all(sql, (status_val,))
        else:
            sql = "SELECT * FROM scan_candidates ORDER BY relative_path ASC;"
            rows = self._db.query_all(sql)
        return [self._row_to_candidate(r) for r in rows]

    def update_candidate_status(
        self, candidate_id: int, status: ScanCandidateStatus
    ) -> bool:
        """Update candidate lifecycle status."""
        status_val = status.value if isinstance(
            status, ScanCandidateStatus) else str(status)
        sql = """
        UPDATE scan_candidates
        SET status = ?, updated_at = datetime('now')
        WHERE id = ?;
        """
        cursor = self._db.execute(sql, (status_val, candidate_id))
        return cursor.rowcount > 0

    def record_quick_scan(self, candidate_id: int) -> bool:
        """Timestamp quick scan completion and update status."""
        sql = """
        UPDATE scan_candidates
        SET quick_scanned_at = datetime('now'),
            status = 'scanned',
            updated_at = datetime('now')
        WHERE id = ?;
        """
        cursor = self._db.execute(sql, (candidate_id,))
        return cursor.rowcount > 0

    def record_long_scan(self, candidate_id: int) -> bool:
        """Timestamp long scan completion and update status."""
        sql = """
        UPDATE scan_candidates
        SET long_scanned_at = datetime('now'),
            status = 'scanned',
            updated_at = datetime('now')
        WHERE id = ?;
        """
        cursor = self._db.execute(sql, (candidate_id,))
        return cursor.rowcount > 0

    def mark_missing(self, candidate_ids: Sequence[int]) -> int:
        """Mark a collection of candidate IDs as missing when files are no longer on disk."""
        if not candidate_ids:
            return 0
        placeholders = ", ".join("?" for _ in candidate_ids)
        sql = f"""
        UPDATE scan_candidates
        SET status = 'missing', updated_at = datetime('now')
        WHERE id IN ({placeholders});
        """
        cursor = self._db.execute(sql, tuple(candidate_ids))
        return cursor.rowcount

    def delete_candidate(self, candidate_id: int) -> bool:
        """Delete a candidate record by ID (cascades to scan_matches)."""
        sql = "DELETE FROM scan_candidates WHERE id = ?;"
        cursor = self._db.execute(sql, (candidate_id,))
        return cursor.rowcount > 0

    # ------------------- Scan Match CRUD -------------------

    def add_match(
        self,
        candidate_id: int,
        matched_track: str,
        matched_database: str,
        engine: str = "audfprint",
        aligned_hashes: int = 0,
        raw_hashes: int = 0,
        offset_seconds: float = 0.0,
        confidence: float = 0.0,
        details_json: str = "{}",
    ) -> ScanMatchRecord:
        """Insert a detected identification match record."""
        sql = """
        INSERT INTO scan_matches (
            candidate_id,
            engine,
            matched_track,
            matched_database,
            aligned_hashes,
            raw_hashes,
            offset_seconds,
            confidence,
            details_json,
            matched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'));
        """
        cursor = self._db.execute(
            sql,
            (
                candidate_id,
                engine,
                matched_track,
                matched_database,
                aligned_hashes,
                raw_hashes,
                offset_seconds,
                confidence,
                details_json,
            ),
        )
        match_id = cursor.lastrowid
        row = self._db.query_one(
            "SELECT * FROM scan_matches WHERE id = ?;", (match_id,))
        if row is None:
            raise DatabaseError(
                f"Failed to fetch inserted match with id {match_id}")
        return self._row_to_match(row)

    def get_matches_for_candidate(self, candidate_id: int) -> list[ScanMatchRecord]:
        """Fetch all match records for a given candidate, ordered by aligned_hashes descending."""
        sql = "SELECT * FROM scan_matches WHERE candidate_id = ? ORDER BY aligned_hashes DESC;"
        rows = self._db.query_all(sql, (candidate_id,))
        return [self._row_to_match(r) for r in rows]

    def delete_matches_for_candidate(self, candidate_id: int) -> int:
        """Delete all match records for a specific candidate."""
        sql = "DELETE FROM scan_matches WHERE candidate_id = ?;"
        cursor = self._db.execute(sql, (candidate_id,))
        return cursor.rowcount

    # ------------------- Private Row Mappers -------------------

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> ScanCandidateRecord:
        raw_status = str(row["status"])
        status_enum = (
            ScanCandidateStatus(raw_status)
            if raw_status in ScanCandidateStatus._value2member_map_
            else ScanCandidateStatus.PENDING
        )
        return ScanCandidateRecord(
            id=int(row["id"]),
            file_path=Path(str(row["file_path"])),
            file_name=str(row["file_name"]),
            relative_path=str(row["relative_path"]),
            file_size_bytes=int(row["file_size_bytes"]),
            duration_seconds=float(
                row["duration_seconds"]) if row["duration_seconds"] is not None else None,
            status=status_enum,
            quick_scanned_at=str(
                row["quick_scanned_at"]) if row["quick_scanned_at"] is not None else None,
            long_scanned_at=str(
                row["long_scanned_at"]) if row["long_scanned_at"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_match(row: sqlite3.Row) -> ScanMatchRecord:
        return ScanMatchRecord(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            engine=str(row["engine"]),
            matched_track=str(row["matched_track"]),
            matched_database=str(row["matched_database"]),
            aligned_hashes=int(row["aligned_hashes"]),
            raw_hashes=int(row["raw_hashes"]),
            offset_seconds=float(row["offset_seconds"]),
            confidence=float(row["confidence"]),
            details_json=str(row["details_json"]),
            matched_at=str(row["matched_at"]),
        )
