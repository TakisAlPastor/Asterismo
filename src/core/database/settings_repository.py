"""Parameterized SQLite repository for AppSettings persistence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from src.core.config.constants import APP_SETTINGS_ROW_KEY
from src.core.config.models import AppSettings, ConfigurationError
from src.core.database.connection import DatabaseManager


class SettingsRepository:
    """Repository managing AppSettings persistence inside the app_settings table."""

    def __init__(self, db: DatabaseManager, base_dir: Path) -> None:
        self._db = db
        self._base_dir = base_dir.resolve()

    def get_value(self, key: str) -> str | None:
        """Fetch raw JSON string for key using parameterized query."""
        row = self._db.query_one(
            "SELECT value_json FROM app_settings WHERE key = ?", (key,))
        if row is None:
            return None
        return str(row["value_json"])

    def set_value(self, key: str, value_json: str) -> None:
        """Upsert raw JSON string for key using parameterized query."""
        sql = """
        INSERT INTO app_settings (key, value_json, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at;
        """
        self._db.execute(sql, (key, value_json))

    def load(self) -> AppSettings:
        """Load AppSettings from database, or return default settings if not yet saved."""
        raw_json = self.get_value(APP_SETTINGS_ROW_KEY)
        if raw_json is None:
            return AppSettings.default(self._base_dir)

        try:
            data = json.loads(raw_json)
            if not isinstance(data, Mapping):
                raise ConfigurationError(
                    f"Corrupted settings payload in database for key '{APP_SETTINGS_ROW_KEY}': expected JSON object"
                )
            return AppSettings.from_dict(data, base_dir=self._base_dir)
        except json.JSONDecodeError as err:
            raise ConfigurationError(
                f"Failed to parse JSON settings from database for key '{APP_SETTINGS_ROW_KEY}': {err}"
            ) from err

    def save(self, settings: AppSettings) -> None:
        """Serialize AppSettings and persist via atomic upsert."""
        payload = json.dumps(settings.to_dict())
        self.set_value(APP_SETTINGS_ROW_KEY, payload)
