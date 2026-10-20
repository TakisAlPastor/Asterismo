"""Silent Subprocess Runner.

Enforces stealth execution on Windows:
- Win32 CREATE_NO_WINDOW and STARTF_USESHOWWINDOW / SW_HIDE flags.
- Complete stdin/stdout/stderr pipe capture.
- Active PID registration with graceful termination and force-kill fallback.
- Standardized ProcessResult return structure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from src.core.config.constants import (
    DEFAULT_SUBPROCESS_TIMEOUT_SEC,
    ENCODING_ERROR_POLICY,
    UTF8_ENCODING,
    WIN32_CREATE_NO_WINDOW,
    WIN32_SW_HIDE,
)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Immutable result of a completed or failed subprocess execution."""

    returncode: int
    stdout: str
    stderr: str
    command: tuple[str, ...]

    @property
    def is_success(self) -> bool:
        """True if the subprocess exited cleanly with returncode 0."""
        return self.returncode == 0


class SilentProcessRunner:
    """Thread-safe runner executing CLI subprocesses without console windows."""

    def __init__(self) -> None:
        self._active_processes: dict[int, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def run(
        self,
        command: Sequence[str],
        timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> ProcessResult:
        """Execute a CLI command silently, returning captured output.

        Raises:
            FileNotFoundError: If the binary command does not exist.
            TimeoutExpired: If execution exceeds the specified timeout.
            PermissionError: If execution permissions are denied.
        """
        cmd_tuple = tuple(str(arg) for arg in command)
        creation_flags = 0
        startupinfo = None

        if sys.platform == "win32":
            creation_flags = WIN32_CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = WIN32_SW_HIDE

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        proc = subprocess.Popen(
            list(cmd_tuple),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
            startupinfo=startupinfo,
            env=run_env,
            cwd=cwd,
            text=True,
            encoding=UTF8_ENCODING,
            errors=ENCODING_ERROR_POLICY,
        )

        with self._lock:
            self._active_processes[proc.pid] = proc

        try:
            stdout_str, stderr_str = proc.communicate(timeout=timeout)
            return ProcessResult(
                returncode=proc.returncode,
                stdout=stdout_str,
                stderr=stderr_str,
                command=cmd_tuple,
            )
        except subprocess.TimeoutExpired as exc:
            self._kill_process(proc)
            proc.communicate()  # drain pipes
            raise exc
        finally:
            with self._lock:
                self._active_processes.pop(proc.pid, None)

    def run_streaming(
        self,
        command: Sequence[str],
        on_stdout_line: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
        timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> ProcessResult:
        """Execute a CLI command silently, streaming stdout lines in real-time.

        Args:
            command: Command and arguments sequence.
            on_stdout_line: Optional callback invoked for each line of stdout.
            cancel_event: Optional event monitored to trigger premature process termination.
            timeout: Subprocess timeout in seconds.
            env: Optional environment variables dictionary.
            cwd: Working directory.

        Returns:
            ProcessResult with complete captured stdout and stderr.
        """
        cmd_tuple = tuple(str(arg) for arg in command)
        creation_flags = 0
        startupinfo = None

        if sys.platform == "win32":
            creation_flags = WIN32_CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = WIN32_SW_HIDE

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        proc = subprocess.Popen(
            list(cmd_tuple),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
            startupinfo=startupinfo,
            env=run_env,
            cwd=cwd,
            text=True,
            bufsize=1,
            encoding=UTF8_ENCODING,
            errors=ENCODING_ERROR_POLICY,
        )

        with self._lock:
            self._active_processes[proc.pid] = proc

        stdout_lines: list[str] = []
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            if proc.stderr is not None:
                for line in proc.stderr:
                    stderr_chunks.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        try:
            if proc.stdout is not None:
                for raw_line in proc.stdout:
                    if cancel_event is not None and cancel_event.is_set():
                        self._kill_process(proc)
                        break
                    line = raw_line.rstrip("\r\n")
                    stdout_lines.append(line)
                    if on_stdout_line is not None:
                        on_stdout_line(line)

            proc.wait(timeout=timeout)
            stderr_thread.join(timeout=2.0)
            return ProcessResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout="\n".join(stdout_lines),
                stderr="".join(stderr_chunks),
                command=cmd_tuple,
            )
        except subprocess.TimeoutExpired as exc:
            self._kill_process(proc)
            stderr_thread.join(timeout=1.0)
            raise exc
        finally:
            with self._lock:
                self._active_processes.pop(proc.pid, None)

    def _kill_process(self, proc: subprocess.Popen[str]) -> None:
        """Attempt graceful termination before force killing."""
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    def terminate_all(self) -> None:
        """Terminate all currently active processes managed by this runner."""
        with self._lock:
            pids = list(self._active_processes.keys())
            for pid in pids:
                proc = self._active_processes.get(pid)
                if proc and proc.poll() is None:
                    self._kill_process(proc)
            self._active_processes.clear()
