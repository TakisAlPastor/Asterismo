"""Database package."""
from __future__ import annotations

from src.core.database.catalog_repository import CatalogRepository
from src.core.database.connection import DatabaseError, DatabaseManager
from src.core.database.harvest_repository import (
    DuplicateHarvestTaskError,
    HarvestRepository,
)
from src.core.database.migrations import (
    ALL_MIGRATIONS,
    MIGRATION_V1,
    MIGRATION_V2,
    Migration,
    MigrationManager,
)
from src.core.database.settings_repository import SettingsRepository

__all__ = [
    "ALL_MIGRATIONS",
    "CatalogRepository",
    "DatabaseError",
    "DatabaseManager",
    "DuplicateHarvestTaskError",
    "HarvestRepository",
    "MIGRATION_V1",
    "MIGRATION_V2",
    "Migration",
    "MigrationManager",
    "SettingsRepository",
]
