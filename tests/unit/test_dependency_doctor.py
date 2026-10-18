"""Unit tests for DependencyDoctorService, BinaryLocator, and audfprint integrity."""

from __future__ import annotations

import hashlib
import io
import pickle
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config.constants import (
    AUDFPRINT_INTEGRITY_MARKERS,
    AUDFPRINT_REQUIRED_FILES,
    BIN_NAME_FFMPEG,
    BIN_NAME_FPCALC,
    BIN_NAME_WINGET,
    DEP_KEY_AUDFPRINT,
    DEP_KEY_FFMPEG,
    DEP_KEY_FPCALC,
    DEP_KEY_PYTHON,
    DEP_KEY_WINGET,
)
from src.core.config.models import DependencyState, DependencyStatus
from src.core.process.binary_locator import BinaryLocator
from src.core.process.dependency_doctor import DependencyDoctorService
from src.core.process.runner import ProcessResult, SilentProcessRunner


class TestBinaryLocator:
    """Test suite for BinaryLocator search priority."""

    def test_local_bin_has_priority_over_system_path(self, tmp_path: Path) -> None:
        # Arrange
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_ffmpeg = bin_dir / "ffmpeg.exe"
        fake_ffmpeg.write_text("fake binary")

        locator = BinaryLocator(bin_dir=bin_dir)

        # Act
        found = locator.locate("ffmpeg")

        # Assert
        assert found is not None
        assert found == fake_ffmpeg.resolve()

    def test_falls_back_to_shutil_which_when_not_in_bin_dir(self, tmp_path: Path) -> None:
        # Arrange
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        locator = BinaryLocator(bin_dir=bin_dir)

        # Act
        with patch("shutil.which", return_value=r"C:\Windows\System32\cmd.exe"):
            found = locator.locate("cmd")

        # Assert
        assert found is not None
        assert found == Path(r"C:\Windows\System32\cmd.exe").resolve()

    def test_returns_none_when_binary_not_found(self, tmp_path: Path) -> None:
        # Arrange
        locator = BinaryLocator(bin_dir=tmp_path / "bin")

        # Act
        with patch("shutil.which", return_value=None):
            found = locator.locate("completely_unknown_binary_12345")

        # Assert
        assert found is None


class TestAudfprintIntegrityAndCompatibility:
    """Validate vendored audfprint modules, patches, and unpickler."""

    def test_audfprint_files_exist_and_contain_integrity_markers(self) -> None:
        # Arrange
        audfprint_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "core"
            / "fingerprinter"
            / "audfprint"
        )

        # Assert required files
        for fname in AUDFPRINT_REQUIRED_FILES:
            assert (audfprint_dir / fname).is_file(), f"Missing {fname}"

        # Assert Asterismo patches
        for fname, marker in AUDFPRINT_INTEGRITY_MARKERS.items():
            content = (audfprint_dir / fname).read_text(encoding="utf-8")
            assert marker in content, f"Marker '{marker}' missing in {fname}"

    def test_numpy_core_compat_unpickler_rewrites_module_cleanly(self) -> None:
        # Arrange
        from src.core.fingerprinter.audfprint.hash_table import _rewrite_numpy_core_module, _NumpyCoreCompatUnpickler

        # Act & Assert rewrite helper
        assert _rewrite_numpy_core_module("numpy._core.multiarray") == "numpy.core.multiarray"
        assert _rewrite_numpy_core_module("numpy._core") == "numpy.core"
        assert _rewrite_numpy_core_module("other_module") == "other_module"

        # Test unpickler instantiation
        bio = io.BytesIO(pickle.dumps({"key": "val"}))
        unpickler = _NumpyCoreCompatUnpickler(bio)
        loaded = unpickler.load()
        assert loaded == {"key": "val"}


class TestDependencyDoctorService:
    """Test suite for DependencyDoctorService health checks and provisioning."""

    def test_check_python_packages_returns_ok_in_active_env(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        # Act
        status = doctor.check_python_packages()

        # Assert
        assert status.name == DEP_KEY_PYTHON
        assert status.state == DependencyState.OK
        assert status.is_ok is True

    def test_check_python_packages_reports_missing_package(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        # Act
        with patch("importlib.util.find_spec", return_value=None):
            status = doctor.check_python_packages()

        # Assert
        assert status.state == DependencyState.MISSING
        assert "Missing required python packages" in status.details

    def test_check_audfprint_real_environment(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        # Act
        status = doctor.check_audfprint()

        # Assert
        assert status.name == DEP_KEY_AUDFPRINT
        assert status.state == DependencyState.OK
        assert status.is_ok is True

    def test_check_binary_reports_missing_when_not_found(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        # Act
        with patch.object(locator, "locate", return_value=None):
            status = doctor.check_binary("custom_bin", "custom_bin.exe")

        # Assert
        assert status.state == DependencyState.MISSING

    def test_check_binary_reports_broken_when_runner_errors(self, tmp_path: Path) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)
        dummy_exe = tmp_path / "dummy.exe"
        dummy_exe.write_text("executable")

        # Act
        with patch.object(locator, "locate", return_value=dummy_exe):
            with patch.object(
                runner,
                "run",
                return_value=ProcessResult(returncode=1, stdout="", stderr="Crash", command=("cmd",)),
            ):
                status = doctor.check_binary("custom_bin", "custom_bin.exe")

        # Assert
        assert status.state == DependencyState.BROKEN
        assert "Crash" in status.details


    def test_extract_verified_zip_succeeds_with_matching_sha256(self, tmp_path: Path) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        # Create a real zip file
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("test_file.txt", "Asterismo Payload")

        # Compute its actual SHA-256
        sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        dest_dir = tmp_path / "extracted"

        # Act
        extracted = doctor.extract_verified_zip(zip_path, sha256, dest_dir)

        # Assert
        assert extracted is True
        assert (dest_dir / "test_file.txt").is_file()
        assert (dest_dir / "test_file.txt").read_text() == "Asterismo Payload"

    def test_extract_verified_zip_rejects_checksum_mismatch(self, tmp_path: Path) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        zip_path = tmp_path / "corrupted.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("payload.bin", "data")

        # Act & Assert
        with pytest.raises(ValueError, match="SHA-256 checksum mismatch"):
            doctor.extract_verified_zip(zip_path, "bad_sha256_hash", tmp_path / "dest")

    def test_extract_verified_zip_rejects_zip_slip_traversal(self, tmp_path: Path) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        zip_path = tmp_path / "malicious.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../escape.txt", "malicious payload")

        sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()

        # Act & Assert
        with pytest.raises(RuntimeError, match="Zip slip security violation"):
            doctor.extract_verified_zip(zip_path, sha256, tmp_path / "dest")

    def test_provision_via_winget_raises_if_winget_missing(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)

        # Act & Assert
        with patch.object(locator, "locate", return_value=None):
            with pytest.raises(FileNotFoundError, match="Microsoft winget CLI is not available"):
                doctor.provision_via_winget("Dummy.Package")

    def test_provision_via_winget_invokes_silent_runner_with_proper_flags(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        locator = BinaryLocator()
        doctor = DependencyDoctorService(binary_locator=locator, process_runner=runner)
        mock_winget = Path(r"C:\Users\User\AppData\Local\Microsoft\WindowsApps\winget.exe")

        # Act
        with patch.object(locator, "locate", return_value=mock_winget):
            with patch.object(
                runner,
                "run",
                return_value=ProcessResult(returncode=0, stdout="Success", stderr="", command=("winget",)),
            ) as mock_run:
                result = doctor.provision_via_winget("Gyan.FFmpeg")

        # Assert
        assert result.is_success is True
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == str(mock_winget)
        assert "Gyan.FFmpeg" in cmd
        assert "--silent" in cmd
        assert "--accept-source-agreements" in cmd
