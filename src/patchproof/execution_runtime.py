"""Credential-minimized child environments and bounded subprocess execution."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_TRUNCATION_MARKER = "\n... [output truncated by PatchProof]"


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """Bounded process facts without retaining an unbounded output buffer."""

    returncode: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False
    start_error: str | None = None


class ChildProcessEnvironmentPolicy:
    """Build an explicit environment that excludes control-plane credentials."""

    inherited_names = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    )

    def build(self, *, runtime_root: Path) -> dict[str, str]:
        """Return only operational values plus isolated HOME/cache/temp directories."""
        runtime_root = runtime_root.resolve()
        home = runtime_root / "home"
        temporary = runtime_root / "tmp"
        cache = runtime_root / "uv-cache"
        for path in (home, temporary, cache):
            path.mkdir(parents=True, exist_ok=True)
        environment = {
            name: os.environ[name] for name in self.inherited_names if os.environ.get(name)
        }
        if "PATH" not in environment:
            raise RuntimeError("child process PATH is unavailable")
        environment.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "TMPDIR": str(temporary),
                "UV_CACHE_DIR": str(cache),
                "UV_NO_CACHE": "1",
                "UV_NO_PROGRESS": "1",
                "UV_PYTHON_DOWNLOADS": "never",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "NO_COLOR": "1",
            }
        )
        return environment


class _BoundedCollector:
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self.data = bytearray()
        self.total_bytes = 0

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(8_192):
                self.total_bytes += len(chunk)
                remaining = self.maximum_bytes - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
        finally:
            stream.close()

    def text(self, maximum_chars: int) -> str:
        value = bytes(self.data).decode("utf-8", errors="replace")
        if self.total_bytes <= self.maximum_bytes and len(value) <= maximum_chars:
            return value
        retained = max(0, maximum_chars - len(_TRUNCATION_MARKER))
        return value[:retained] + _TRUNCATION_MARKER


class BoundedSubprocessRunner:
    """Run an argv without a shell, drain output concurrently, and terminate its process tree."""

    def __init__(self, *, max_output_chars: int = 12_000) -> None:
        if max_output_chars < len(_TRUNCATION_MARKER) + 1:
            raise ValueError("process output budget is too small")
        self.max_output_chars = max_output_chars
        self.max_output_bytes = max_output_chars * 4

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
    ) -> BoundedProcessResult:
        """Execute one validated argv and return bounded UTF-8 replacement-decoded output."""
        if not command or timeout_seconds <= 0:
            raise ValueError("process command and timeout must be configured")
        started = time.monotonic()
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        except OSError:
            return BoundedProcessResult(
                returncode=None,
                duration_seconds=time.monotonic() - started,
                stdout="",
                stderr="",
                start_error="process could not start",
            )

        assert process.stdout is not None and process.stderr is not None
        stdout = _BoundedCollector(self.max_output_bytes)
        stderr = _BoundedCollector(self.max_output_bytes)
        threads = (
            threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
            threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
        )
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_tree(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            for thread in threads:
                thread.join(timeout=5)
        return BoundedProcessResult(
            returncode=None if timed_out else process.returncode,
            duration_seconds=time.monotonic() - started,
            stdout=stdout.text(self.max_output_chars),
            stderr=stderr.text(self.max_output_chars),
            timed_out=timed_out,
        )

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            with suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()


def remove_runtime_directory(path: Path) -> None:
    """Best-effort cleanup with bounded retries for transient Windows file handles."""
    for attempt in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        if attempt < 19:
            time.sleep(0.1)
