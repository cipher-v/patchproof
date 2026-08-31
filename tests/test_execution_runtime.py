"""Security and reliability tests for the repository child-process boundary."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from patchproof.execution_runtime import (
    BoundedSubprocessRunner,
    ChildProcessEnvironmentPolicy,
    sanitize_process_output,
)


def test_child_environment_uses_an_allowlist_and_isolated_runtime_paths(
    writable_test_directory: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    monkeypatch.setenv("GOOGLE_API_KEY", "must-not-reach-repository-code")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-repository-code")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "secret-file.json")
    monkeypatch.setenv("PATCHPROOF_WEBHOOK_SECRET", "must-not-reach-repository-code")
    runtime_root = writable_test_directory / "runtime"

    environment = ChildProcessEnvironmentPolicy().build(runtime_root=runtime_root)

    assert environment["PATH"] == str(Path(sys.executable).parent)
    assert environment["HOME"].startswith(str(runtime_root.resolve()))
    assert environment["UV_CACHE_DIR"].startswith(str(runtime_root.resolve()))
    # Autoload must stay enabled: a repository's own conftest.py imports its declared
    # plugins, and its addopts reference their options. Interfering plugins are
    # disabled by name in pytest_runner.DISABLED_PYTEST_PLUGINS instead.
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in environment
    assert (
        not {
            "GOOGLE_API_KEY",
            "GITHUB_TOKEN",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "PATCHPROOF_WEBHOOK_SECRET",
        }
        & environment.keys()
    )


def test_process_output_is_streamed_into_a_hard_bounded_buffer(
    writable_test_directory: Path,
) -> None:
    runner = BoundedSubprocessRunner(max_output_chars=1_000)
    environment = ChildProcessEnvironmentPolicy().build(
        runtime_root=writable_test_directory / "runtime"
    )

    result = runner.run(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('o'*50000); sys.stderr.write('e'*50000)",
        ),
        cwd=writable_test_directory,
        environment=environment,
        timeout_seconds=5,
    )

    assert result.returncode == 0
    assert len(result.stdout) <= 1_000
    assert len(result.stderr) <= 1_000
    assert "middle output omitted by PatchProof" in result.stdout
    assert "middle output omitted by PatchProof" in result.stderr
    assert result.stdout.endswith("o" * 20)
    assert result.stderr.endswith("e" * 20)


def test_process_timeout_terminates_promptly_and_returns_only_bounded_facts(
    writable_test_directory: Path,
) -> None:
    runner = BoundedSubprocessRunner(max_output_chars=1_000)
    environment = ChildProcessEnvironmentPolicy().build(
        runtime_root=writable_test_directory / "runtime"
    )
    started = time.monotonic()

    result = runner.run(
        (sys.executable, "-c", "import time; print('started', flush=True); time.sleep(10)"),
        cwd=writable_test_directory,
        environment=environment,
        timeout_seconds=0.2,
    )

    assert result.timed_out is True
    assert result.returncode is None
    assert "started" in result.stdout
    assert time.monotonic() - started < 6


def test_process_start_failure_does_not_persist_os_error_details(
    writable_test_directory: Path,
) -> None:
    runner = BoundedSubprocessRunner(max_output_chars=1_000)
    environment = ChildProcessEnvironmentPolicy().build(
        runtime_root=writable_test_directory / "runtime"
    )

    result = runner.run(
        ("patchproof-command-that-does-not-exist",),
        cwd=writable_test_directory,
        environment=environment,
        timeout_seconds=1,
    )

    assert result.returncode is None
    assert result.start_error == "process could not start"
    assert result.stdout == result.stderr == ""


def test_setup_output_sanitization_redacts_credentials_and_stays_bounded() -> None:
    value = (
        "token=super-secret-value\nBearer signed-value\n"
        + ("x" * 10_000)
        + "\nterminal build cause"
    )

    sanitized = sanitize_process_output(value, maximum_chars=500)

    assert "super-secret-value" not in sanitized
    assert "signed-value" not in sanitized
    assert "credential=<redacted>" in sanitized
    assert "Bearer <redacted>" in sanitized
    assert len(sanitized) <= 500
    assert "middle output omitted by PatchProof" in sanitized
    assert sanitized.endswith("terminal build cause")
