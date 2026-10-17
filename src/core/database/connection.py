"""SQLite database connection and PRAGMA management.

Enforces WAL mode, foreign keys, and secure transactional boundaries.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence

from src.core.config.constants import (
    MEMORY_DB_TARGET,
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_CACHE_SIZE_KB,
    SQLITE_FOREIGN_KEYS_ON,
    SQLITE_JOURNAL_MODE,
    SQLITE_SYNCHRONOUS,
    SQLITE_TEMP_STORE,
)


class DatabaseError(RuntimeError):
    """Domain exception raised when database operations fail."""


class DatabaseManager:
    """Manages SQLite connections and PRAGMA settings."""

    def __init__(self, db_target: Path | str) -> None:
        if isinstance(db_target, Path):
            self._target: Path | str = db_target.resolve()
        else:
            self._target = db_target

        self._conn: sqlite3.Connection | None = None

    @property
    def target(self) -> Path | str:
        """Return the configured database path or memory token."""
        return self._target

    @property
    def is_connected(self) -> bool:
        """Check if an active connection exists."""
        return self._conn is not None

    def connect(self) -> sqlite3.Connection:
        """Open or reuse connection and enforce performance and security PRAGMAs."""
        if self._conn is not None:
            return self._conn

        target_str: str
        if isinstance(self._target, Path):
            self._target.parent.mkdir(parents=True, exist_ok=True)
            target_str = str(self._target)
        else:
            target_str = self._target

        try:
            # Use autocommit=True so connection starts outside an implicit transaction
            conn = sqlite3.connect(target_str, autocommit=True)
            conn.row_factory = sqlite3.Row

            # Apply performance, integrity, and concurrency PRAGMAs outside transactions
            conn.execute(f"PRAGMA foreign_keys = {SQLITE_FOREIGN_KEYS_ON};")
            conn.execute(f"PRAGMA synchronous = {SQLITE_SYNCHRONOUS};")
            conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS};")
            conn.execute(f"PRAGMA temp_store = {SQLITE_TEMP_STORE};")
            conn.execute(f"PRAGMA cache_size = {SQLITE_CACHE_SIZE_KB};")
            if target_str != MEMORY_DB_TARGET:
                conn.execute(f"PRAGMA journal_mode = {SQLITE_JOURNAL_MODE};")

            # Switch autocommit to False for explicit transaction management
            conn.autocommit = False

            self._conn = conn
            return conn
        except sqlite3.Error as err:
            raise DatabaseError(
                f"Failed to initialize SQLite connection to {self._target}: {err}") from err

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Provide atomic transaction scope committing on success or rolling back on error."""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception as err:
            conn.rollback()
            if isinstance(err, DatabaseError):
                raise
            raise DatabaseError(f"Transaction failed: {err}") from err

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        """Execute a parameterized query inside an immediate transaction."""
        with self.transaction() as conn:
            try:
                return conn.execute(sql, params)
            except sqlite3.Error as err:
                raise DatabaseError(
                    f"Database execution error: {err}") from err

    def query_one(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        """Query a single row using parameterized values."""
        conn = self.connect()
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchone()
        except sqlite3.Error as err:
            raise DatabaseError(f"Database query error: {err}") from err

    def query_all(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Query multiple rows using parameterized values."""
        conn = self.connect()
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()
        except sqlite3.Error as err:
            raise DatabaseError(f"Database query error: {err}") from err

    def close(self) -> None:
        """Close connection if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            finally:
                self._conn = None
