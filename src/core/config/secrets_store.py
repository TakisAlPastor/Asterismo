"""Atomic persistence for sensitive credentials (.secrets.json).

Uses atomic file replacement (os.replace) to prevent state corruption.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from src.core.config.constants import SECRETS_TEMP_SUFFIX, UTF8_ENCODING
from src.core.config.models import ConfigurationError, SecretCredentials


class SecretsStore:
    """Manages loading and atomic saving of SecretCredentials."""

    def __init__(self, secrets_path: Path) -> None:
        self._secrets_path = secrets_path.resolve()

    @property
    def path(self) -> Path:
        """Return the target file path."""
        return self._secrets_path

    def load(self) -> SecretCredentials:
        """Load SecretCredentials from disk, or return empty defaults if missing."""
        if not self._secrets_path.is_file():
            return SecretCredentials()

        try:
            content = self._secrets_path.read_text(encoding=UTF8_ENCODING)
            data = json.loads(content)
            if not isinstance(data, Mapping):
                raise ConfigurationError(
                    f"Corrupted secrets file at {self._secrets_path}: expected JSON object"
                )
            return SecretCredentials.from_dict(data)
        except json.JSONDecodeError as err:
            raise ConfigurationError(
                f"Invalid JSON in secrets file at {self._secrets_path}: {err}"
            ) from err
        except OSError as err:
            raise ConfigurationError(
                f"Failed to read secrets file at {self._secrets_path}: {err}"
            ) from err

    def save(self, credentials: SecretCredentials) -> None:
        """Atomically persist SecretCredentials using a temporary sibling file."""
        parent_dir = self._secrets_path.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        temp_path = parent_dir / \
            f"{self._secrets_path.name}{SECRETS_TEMP_SUFFIX}"
        data = credentials.to_dict()

        try:
            with open(temp_path, "w", encoding=UTF8_ENCODING) as file:
                json.dump(data, file, indent=2)
                file.flush()
                os.fsync(file.fileno())

            os.replace(temp_path, self._secrets_path)
        except OSError as err:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise ConfigurationError(
                f"Failed to save secrets file at {self._secrets_path}: {err}"
            ) from err
