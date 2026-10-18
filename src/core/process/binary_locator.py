"""Binary locator resolving local bundled tools before falling back to system PATH."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


class BinaryLocator:
    """Discovers binaries prioritizing workspace local directories over system PATH."""

    def __init__(self, bin_dir: Path | None = None) -> None:
        self._bin_dir = bin_dir.resolve() if bin_dir else None

    @property
    def bin_dir(self) -> Path | None:
        """Configured local binary directory."""
        return self._bin_dir

    def locate(self, binary_name: str) -> Path | None:
        """Find the absolute path to an executable binary.

        Search order:
        1. Local `bin_dir` (e.g. `bin/ffmpeg.exe`, `bin/ffmpeg`)
        2. System PATH via `shutil.which`

        Returns:
            Resolved absolute Path if executable is found and verified, None otherwise.
        """
        # Determine Windows executable variants if extension not specified
        names_to_try = [binary_name]
        if sys.platform == "win32" and not binary_name.lower().endswith(".exe"):
            names_to_try.insert(0, f"{binary_name}.exe")

        # 1. Check local bin_dir first
        if self._bin_dir and self._bin_dir.is_dir():
            for name in names_to_try:
                candidate = self._bin_dir / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate.resolve()

        # 2. Check system PATH
        for name in names_to_try:
            found_str = shutil.which(name)
            if found_str:
                found_path = Path(found_str)
                if found_path.is_file() and os.access(found_path, os.X_OK):
                    return found_path.resolve()

        return None
