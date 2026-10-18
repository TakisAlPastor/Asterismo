"""Unit tests for SilentProcessRunner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config.constants import (
    DEFAULT_SUBPROCESS_TIMEOUT_SEC,
    WIN32_CREATE_NO_WINDOW,
    WIN32_SW_HIDE,
)
from src.core.process.runner import ProcessResult, SilentProcessRunner


class TestSilentProcessRunner:
    """Test suite for SilentProcessRunner execution and process termination."""

    def test_run_simple_python_command_success(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        cmd = [sys.executable, "-c", "import sys; print('Hello Silent World')"]

        # Act
        result = runner.run(cmd, timeout=10.0)

        # Assert
        assert isinstance(result, ProcessResult)
        assert result.is_success is True
        assert result.returncode == 0
        assert "Hello Silent World" in result.stdout

    def test_run_preserves_unicode_arguments_and_output(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        test_unicode = "Canción_日本語_★_123"
        cmd = [sys.executable, "-c", f"import sys; print('{test_unicode}')"]

        # Act
        result = runner.run(cmd, timeout=10.0)

        # Assert
        assert result.is_success is True
        assert test_unicode in result.stdout

    def test_run_captures_stderr_on_nonzero_exit(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        cmd = [sys.executable, "-c",
               "import sys; sys.stderr.write('ErrorOccurred\\n'); sys.exit(42)"]

        # Act
        result = runner.run(cmd, timeout=10.0)

        # Assert
        assert result.is_success is False
        assert result.returncode == 42
        assert "ErrorOccurred" in result.stderr

    def test_run_raises_filenotfound_for_missing_binary(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        missing_bin = "non_existent_binary_xyz_123456"

        # Act & Assert
        with pytest.raises(FileNotFoundError):
            runner.run([missing_bin, "--version"], timeout=5.0)

    def test_run_raises_timeoutexpired_and_cleans_up(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        # Script that sleeps longer than timeout
        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]

        # Act & Assert
        with pytest.raises(subprocess.TimeoutExpired):
            runner.run(cmd, timeout=0.5)

        # Process registry must be clean
        assert len(runner._active_processes) == 0

    @patch("subprocess.Popen")
    def test_run_applies_win32_stealth_flags(self, mock_popen: MagicMock) -> None:
        # Arrange
        runner = SilentProcessRunner()
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.communicate.return_value = ("stdout_ok", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # Act
        with patch("sys.platform", "win32"):
            runner.run(["dummy_cmd"], timeout=5.0)

        # Assert
        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        assert kwargs["creationflags"] == WIN32_CREATE_NO_WINDOW
        startupinfo = kwargs["startupinfo"]
        assert startupinfo is not None
        assert startupinfo.wShowWindow == WIN32_SW_HIDE
        assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW

    def test_terminate_all_terminates_registered_processes(self) -> None:
        # Arrange
        runner = SilentProcessRunner()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        runner._active_processes[1001] = mock_proc

        # Act
        runner.terminate_all()

        # Assert
        mock_proc.terminate.assert_called_once()
        assert len(runner._active_processes) == 0
