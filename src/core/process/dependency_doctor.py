"""Dependency Doctor Service.

Validates external dependencies (FFmpeg, FFprobe, fpcalc, audfprint, Python packages)
and provisions missing binaries:
- Tier 1: Microsoft `winget` CLI without executing arbitrary remote binaries.
- Tier 2: Extracting pre-verified zip archives into `bin/` with SHA-256 integrity checks.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Mapping

from src.core.config.constants import (
    AUDFPRINT_INTEGRITY_MARKERS,
    AUDFPRINT_REQUIRED_FILES,
    BIN_NAME_FFMPEG,
    BIN_NAME_FFPROBE,
    BIN_NAME_FPCALC,
    BIN_NAME_WINGET,
    DEP_KEY_AUDFPRINT,
    DEP_KEY_FFMPEG,
    DEP_KEY_FFPROBE,
    DEP_KEY_FPCALC,
    DEP_KEY_PYTHON,
    DEP_KEY_WINGET,
    REQUIRED_PYTHON_PACKAGES,
    UTF8_ENCODING,
    WINGET_CHROMAPRINT_PACKAGE_ID,
    WINGET_FFMPEG_PACKAGE_ID,
)
from src.core.config.models import DependencyState, DependencyStatus
from src.core.process.binary_locator import BinaryLocator
from src.core.process.runner import ProcessResult, SilentProcessRunner


class DependencyDoctorService:
    """Diagnostic and provisioning service for external Asterismo dependencies."""

    def __init__(
        self,
        binary_locator: BinaryLocator,
        process_runner: SilentProcessRunner,
        audfprint_dir: Path | None = None,
    ) -> None:
        self._locator = binary_locator
        self._runner = process_runner
        if audfprint_dir:
            self._audfprint_dir = audfprint_dir.resolve()
        else:
            self._audfprint_dir = (
                Path(__file__).resolve().parent.parent /
                "fingerprinter" / "audfprint"
            )

    @property
    def binary_locator(self) -> BinaryLocator:
        return self._locator

    @property
    def process_runner(self) -> SilentProcessRunner:
        return self._runner

    @property
    def audfprint_dir(self) -> Path:
        return self._audfprint_dir

    def check_binary(self, key: str, binary_name: str, version_arg: str = "-version") -> DependencyStatus:
        """Probe external CLI binary for availability and operational health."""
        path = self._locator.locate(binary_name)
        if not path or not path.is_file():
            return DependencyStatus(
                name=key,
                state=DependencyState.MISSING,
                details=f"Binary '{binary_name}' was not found in local bin or system PATH.",
            )

        try:
            result = self._runner.run([str(path), version_arg], timeout=10.0)
            if result.is_success or result.returncode == 0:
                # Extract first non-empty line as version info
                version_str = ""
                output = result.stdout or result.stderr
                lines = [line.strip()
                         for line in output.splitlines() if line.strip()]
                if lines:
                    version_str = lines[0]
                return DependencyStatus(
                    name=key,
                    state=DependencyState.OK,
                    path=path,
                    version=version_str,
                    details="Operational",
                )
            return DependencyStatus(
                name=key,
                state=DependencyState.BROKEN,
                path=path,
                details=f"Execution returned non-zero code {result.returncode}: {result.stderr.strip()}",
            )
        except Exception as exc:
            return DependencyStatus(
                name=key,
                state=DependencyState.BROKEN,
                path=path,
                details=f"Execution check failed: {exc}",
            )

    def check_audfprint(self) -> DependencyStatus:
        """Verify presence, integrity patches, and execution of vendored audfprint."""
        if not self._audfprint_dir.is_dir():
            return DependencyStatus(
                name=DEP_KEY_AUDFPRINT,
                state=DependencyState.MISSING,
                details=f"Audfprint directory missing at {self._audfprint_dir}",
            )

        # 1. Verify required files exist
        for fname in AUDFPRINT_REQUIRED_FILES:
            file_path = self._audfprint_dir / fname
            if not file_path.is_file():
                return DependencyStatus(
                    name=DEP_KEY_AUDFPRINT,
                    state=DependencyState.MISSING,
                    path=self._audfprint_dir,
                    details=f"Missing required audfprint file: {fname}",
                )

        # 2. Verify Asterismo patch markers for Windows & NumPy 2.x
        for fname, marker in AUDFPRINT_INTEGRITY_MARKERS.items():
            file_path = self._audfprint_dir / fname
            try:
                content = file_path.read_text(
                    encoding=UTF8_ENCODING, errors="replace")
                if marker not in content:
                    return DependencyStatus(
                        name=DEP_KEY_AUDFPRINT,
                        state=DependencyState.BROKEN,
                        path=self._audfprint_dir,
                        details=f"Audfprint file '{fname}' missing required integrity marker '{marker}'.",
                    )
            except OSError as err:
                return DependencyStatus(
                    name=DEP_KEY_AUDFPRINT,
                    state=DependencyState.BROKEN,
                    path=self._audfprint_dir,
                    details=f"Could not read '{fname}': {err}",
                )

        # 3. Verify probe execution via python -m
        audfprint_py = self._audfprint_dir / "audfprint.py"
        try:
            result = self._runner.run(
                [sys.executable, str(audfprint_py), "--version"],
                timeout=25.0,
            )
            # audfprint --version prints version or docopt string
            ver_text = (result.stdout or result.stderr).strip()
            first_line = ver_text.splitlines(
            )[0] if ver_text else "audfprint vendored"
            return DependencyStatus(
                name=DEP_KEY_AUDFPRINT,
                state=DependencyState.OK,
                path=self._audfprint_dir,
                version=first_line,
                details="Vendored and patched successfully",
            )
        except Exception as exc:
            return DependencyStatus(
                name=DEP_KEY_AUDFPRINT,
                state=DependencyState.BROKEN,
                path=self._audfprint_dir,
                details=f"Probe execution failed: {exc}",
            )

    def check_python_packages(self) -> DependencyStatus:
        """Ensure all required Python libraries are present in the environment."""
        missing: list[str] = []
        for pkg_name in REQUIRED_PYTHON_PACKAGES:
            spec = importlib.util.find_spec(pkg_name)
            if spec is None:
                missing.append(pkg_name)

        if missing:
            return DependencyStatus(
                name=DEP_KEY_PYTHON,
                state=DependencyState.MISSING,
                details=f"Missing required python packages: {', '.join(missing)}",
            )

        return DependencyStatus(
            name=DEP_KEY_PYTHON,
            state=DependencyState.OK,
            version=sys.version.split()[0],
            details="All required packages present",
        )

    def check_all(self) -> dict[str, DependencyStatus]:
        """Perform full diagnostic sweep across all dependencies."""
        results: dict[str, DependencyStatus] = {}

        results[DEP_KEY_FFMPEG] = self.check_binary(
            DEP_KEY_FFMPEG, BIN_NAME_FFMPEG, "-version")
        results[DEP_KEY_FFPROBE] = self.check_binary(
            DEP_KEY_FFPROBE, BIN_NAME_FFPROBE, "-version")
        results[DEP_KEY_FPCALC] = self.check_binary(
            DEP_KEY_FPCALC, BIN_NAME_FPCALC, "-v")
        results[DEP_KEY_AUDFPRINT] = self.check_audfprint()
        results[DEP_KEY_PYTHON] = self.check_python_packages()
        results[DEP_KEY_WINGET] = self.check_binary(
            DEP_KEY_WINGET, BIN_NAME_WINGET, "--version")

        return results

    def provision_via_winget(self, package_id: str) -> ProcessResult:
        """Install or update an external dependency using Microsoft winget CLI.

        This avoids downloading raw .exe files into temporary directories,
        greatly reducing false-positive antivirus blocks.
        """
        winget_path = self._locator.locate(BIN_NAME_WINGET)
        if not winget_path:
            raise FileNotFoundError(
                "Microsoft winget CLI is not available on this system.")

        cmd = [
            str(winget_path),
            "install",
            "--id",
            package_id,
            "-e",
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ]
        return self._runner.run(cmd, timeout=300.0)

    def extract_verified_zip(
        self,
        archive_path: Path,
        expected_sha256: str,
        destination_dir: Path,
    ) -> bool:
        """Safely extract a downloaded zip archive only after cryptographic hash verification.

        Guards against corrupted downloads and directory traversal (zip-slip).
        """
        if not archive_path.is_file():
            raise FileNotFoundError(f"Archive not found: {archive_path}")

        # 1. Compute SHA-256
        hasher = hashlib.sha256()
        with archive_path.open("rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)

        computed_sha = hasher.hexdigest().lower()
        if computed_sha != expected_sha256.lower().strip():
            raise ValueError(
                f"SHA-256 checksum mismatch for {archive_path.name}. "
                f"Expected: {expected_sha256.lower()}, Computed: {computed_sha}"
            )

        destination_dir.mkdir(parents=True, exist_ok=True)
        resolved_dest = destination_dir.resolve()

        # 2. Extract with Zip-Slip path sanitization
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.infolist():
                target_path = (resolved_dest / member.filename).resolve()
                if not str(target_path).startswith(str(resolved_dest)):
                    raise RuntimeError(
                        f"Zip slip security violation detected: {member.filename}")
            zf.extractall(resolved_dest)

        return True
