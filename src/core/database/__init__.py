"""Database package for Asterismo."""
from __future__ import annotations

from src.core.database.connection import DatabaseError, DatabaseManager
from src.core.database.migrations import (
    MIGRATION_V1,
    Migration,
    MigrationManager,
)
from src.core.database.settings_repository import SettingsRepository

__all__ = [
    "DatabaseError",
    "DatabaseManager",
    "MIGRATION_V1",
    "Migration",
    "MigrationManager",
    "SettingsRepository",
]
