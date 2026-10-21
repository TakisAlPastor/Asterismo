"""SQLite database connection and PRAGMA management.

Enforces WAL mode, foreign keys, and secure transactional boundaries.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
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
    """Manages SQLite connections and PRAGMA settings with thread-safe connection pooling."""

    def __init__(self, db_target: Path | str) -> None:
        if isinstance(db_target, Path):
            self._target: Path | str = db_target.resolve()
        else:
            self._target = db_target

        self._is_memory = str(self._target) == MEMORY_DB_TARGET
        self._local = threading.local()
        self._lock = threading.RLock()
        self._open_connections: set[sqlite3.Connection] = set()
        self._memory_conn: sqlite3.Connection | None = None

    @property
    def target(self) -> Path | str:
        """Return the configured database path or memory token."""
        return self._target

    @property
    def is_connected(self) -> bool:
        """Check if an active connection exists for the current context."""
        if self._is_memory:
            return self._memory_conn is not None
        return getattr(self._local, "conn", None) is not None

    @property
    def _conn(self) -> sqlite3.Connection | None:
        """Compatibility accessor returning connection for current thread or memory DB."""
        if self._is_memory:
            return self._memory_conn
        return getattr(self._local, "conn", None)

    def _create_connection(self) -> sqlite3.Connection:
        """Instantiate a new SQLite connection with configured PRAGMAs applied."""
        target_str: str
        if isinstance(self._target, Path):
            self._target.parent.mkdir(parents=True, exist_ok=True)
            target_str = str(self._target)
        else:
            target_str = self._target

        try:
            # check_same_thread=False allows cross-thread teardown/cleanup without ProgrammingError
            conn = sqlite3.connect(
                target_str,
                autocommit=True,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row

            # Apply performance, integrity, and concurrency PRAGMAs outside transactions
            conn.execute(f"PRAGMA foreign_keys = {SQLITE_FOREIGN_KEYS_ON};")
            conn.execute(f"PRAGMA synchronous = {SQLITE_SYNCHRONOUS};")
            conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS};")
            conn.execute(f"PRAGMA temp_store = {SQLITE_TEMP_STORE};")
            conn.execute(f"PRAGMA cache_size = {SQLITE_CACHE_SIZE_KB};")
            if not self._is_memory:
                conn.execute(f"PRAGMA journal_mode = {SQLITE_JOURNAL_MODE};")

            # Switch autocommit to False for explicit transaction management
            conn.autocommit = False

            self._open_connections.add(conn)
            return conn
        except sqlite3.Error as err:
            raise DatabaseError(
                f"Failed to initialize SQLite connection to {self._target}: {err}") from err

    def connect(self) -> sqlite3.Connection:
        """Open or reuse connection for the calling thread and enforce PRAGMAs."""
        if self._is_memory:
            with self._lock:
                if self._memory_conn is not None:
                    return self._memory_conn
                conn = self._create_connection()
                self._memory_conn = conn
                return conn

        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        with self._lock:
            conn = self._create_connection()
            self._local.conn = conn
            return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Provide atomic transaction scope committing on success or rolling back on error."""
        if self._is_memory:
            with self._lock:
                conn = self.connect()
                try:
                    yield conn
                    conn.commit()
                except Exception as err:
                    conn.rollback()
                    if isinstance(err, DatabaseError):
                        raise
                    raise DatabaseError(f"Transaction failed: {err}") from err
        else:
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
        if self._is_memory:
            with self._lock:
                conn = self.connect()
                try:
                    cursor = conn.execute(sql, params)
                    row = cursor.fetchone()
                    conn.commit()
                    return row
                except sqlite3.Error as err:
                    conn.rollback()
                    raise DatabaseError(
                        f"Database query error: {err}") from err
        else:
            conn = self.connect()
            try:
                cursor = conn.execute(sql, params)
                row = cursor.fetchone()
                conn.commit()
                return row
            except sqlite3.Error as err:
                conn.rollback()
                raise DatabaseError(f"Database query error: {err}") from err

    def query_all(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Query multiple rows using parameterized values."""
        if self._is_memory:
            with self._lock:
                conn = self.connect()
                try:
                    cursor = conn.execute(sql, params)
                    rows = cursor.fetchall()
                    conn.commit()
                    return rows
                except sqlite3.Error as err:
                    conn.rollback()
                    raise DatabaseError(
                        f"Database query error: {err}") from err
        else:
            conn = self.connect()
            try:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()
                conn.commit()
                return rows
            except sqlite3.Error as err:
                conn.rollback()
                raise DatabaseError(f"Database query error: {err}") from err

    def close(self) -> None:
        """Close connection if open."""
        with self._lock:
            for conn in list(self._open_connections):
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._open_connections.clear()
            self._memory_conn = None
            self._local = threading.local()
