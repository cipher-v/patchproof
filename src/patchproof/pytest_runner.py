"""Bounded pytest execution and initial JUnit result parsing."""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import time
import uuid
import xml.etree.ElementTree as element_tree
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from patchproof.execution_contract import ExecutionContract
from patchproof.execution_runtime import (
    BoundedSubprocessRunner,
    ChildProcessEnvironmentPolicy,
    remove_runtime_directory,
)
from patchproof.models import ExecutionResult, Revision, TestArtifact, TestExecutionStatus


@dataclass(frozen=True, slots=True)
class ParsedPytestResult:
    """Normalized facts extracted from pytest's JUnit report."""

    status: TestExecutionStatus
    collected_count: int
    detail: str | None = None


class PytestJUnitParser:
    """Interpret one selected pytest node without relying on terminal-summary prose."""

    def parse(
        self, *, report_path: Path, exit_code: int, stdout: str, stderr: str
    ) -> ParsedPytestResult:
        """Parse pytest's machine-readable report and conservative process fallback signals."""
        if not report_path.is_file():
            return self._without_report(exit_code=exit_code, stdout=stdout, stderr=stderr)

        try:
            root = element_tree.parse(report_path).getroot()
        except (OSError, element_tree.ParseError) as error:
            return ParsedPytestResult(
                status=TestExecutionStatus.PROCESS_ERROR,
                collected_count=0,
                detail=f"pytest JUnit report could not be parsed: {error}",
            )

        test_cases = [
            element for element in root.iter() if self._local_name(element.tag) == "testcase"
        ]
        if not test_cases:
            return self._without_report(exit_code=exit_code, stdout=stdout, stderr=stderr)
        if len(test_cases) > 1:
            return ParsedPytestResult(
                status=TestExecutionStatus.MULTIPLE_TESTS_COLLECTED,
                collected_count=len(test_cases),
                detail=f"selected node collected {len(test_cases)} tests; exactly one is required",
            )

        test_case = test_cases[0]
        children = {self._local_name(child.tag): child for child in test_case}

        error = children.get("error")
        if error is not None:
            detail = self._element_detail(error)
            is_collection_error = (
                "collection failure" in detail.lower()
                or "error collecting" in detail.lower()
                or not test_case.attrib.get("classname")
            )
            return ParsedPytestResult(
                status=(
                    TestExecutionStatus.COLLECTION_ERROR
                    if is_collection_error
                    else TestExecutionStatus.TEST_ERROR
                ),
                collected_count=0 if is_collection_error else 1,
                detail=detail,
            )

        skipped = children.get("skipped")
        if skipped is not None:
            detail = self._element_detail(skipped)
            status = (
                TestExecutionStatus.XFAILED
                if "xfail" in skipped.attrib.get("type", "").lower() or "xfail" in detail.lower()
                else TestExecutionStatus.SKIPPED
            )
            return ParsedPytestResult(status=status, collected_count=1, detail=detail)

        failure = children.get("failure")
        if failure is not None:
            detail = self._element_detail(failure)
            if "xpass" in detail.lower():
                status = TestExecutionStatus.XPASSED
            elif "assertionerror" in detail.lower() or detail.lstrip().startswith("assert "):
                status = TestExecutionStatus.ASSERTION_FAILED
            else:
                status = TestExecutionStatus.TEST_ERROR
            return ParsedPytestResult(status=status, collected_count=1, detail=detail)

        if "XPASS" in stdout:
            return ParsedPytestResult(
                status=TestExecutionStatus.XPASSED,
                collected_count=1,
                detail="pytest reported an unexpected pass",
            )
        if exit_code == 0:
            return ParsedPytestResult(status=TestExecutionStatus.PASSED, collected_count=1)
        return ParsedPytestResult(
            status=TestExecutionStatus.PROCESS_ERROR,
            collected_count=1,
            detail=f"pytest exited with {exit_code} despite a result without an outcome element",
        )

    @staticmethod
    def _without_report(*, exit_code: int, stdout: str, stderr: str) -> ParsedPytestResult:
        combined_output = f"{stdout}\n{stderr}".strip()
        if exit_code == 5 or "not found:" in combined_output.lower():
            return ParsedPytestResult(
                status=TestExecutionStatus.NOT_COLLECTED,
                collected_count=0,
                detail=combined_output or "selected pytest node was not collected",
            )
        if (
            "error collecting" in combined_output.lower()
            or "collection error" in combined_output.lower()
        ):
            return ParsedPytestResult(
                status=TestExecutionStatus.COLLECTION_ERROR,
                collected_count=0,
                detail=combined_output,
            )
        return ParsedPytestResult(
            status=TestExecutionStatus.PROCESS_ERROR,
            collected_count=0,
            detail=combined_output or f"pytest exited with {exit_code} without a JUnit report",
        )

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", maxsplit=1)[-1]

    @staticmethod
    def _element_detail(element: element_tree.Element) -> str:
        message = element.attrib.get("message", "").strip()
        body = (element.text or "").strip()
        return "\n".join(part for part in (message, body) if part)


class PytestRunner:
    """Inject exact bytes and execute one fixed pytest node with a hard timeout."""

    def __init__(
        self,
        *,
        contract: ExecutionContract,
        python_executable: Path,
        parser: PytestJUnitParser | None = None,
        install_dependencies: bool = False,
        repository_python_paths: tuple[str, ...] = (),
        processes: BoundedSubprocessRunner | None = None,
        environment_policy: ChildProcessEnvironmentPolicy | None = None,
    ) -> None:
        self.python_executable = python_executable.resolve()
        self.contract = contract
        self.command_prefix = contract.resolved_test_command(
            python_executable=self.python_executable
        )
        self.timeout_seconds = contract.timeout_seconds
        self.parser = parser or PytestJUnitParser()
        self.install_dependencies = install_dependencies
        self.repository_python_paths = tuple(
            self._validate_python_path(path) for path in repository_python_paths
        )
        self.processes = processes or BoundedSubprocessRunner()
        self.environment_policy = environment_policy or ChildProcessEnvironmentPolicy()

    def run(
        self, *, workspace: Path, revision: Revision, artifact: TestArtifact
    ) -> ExecutionResult:
        """Write the artifact once, run its selected node, and hash it again afterward."""
        started_at = time.monotonic()
        workspace = workspace.resolve()
        if not workspace.is_dir():
            return self._early_result(
                revision=revision,
                artifact=artifact,
                status=TestExecutionStatus.PROCESS_ERROR,
                started_at=started_at,
                detail=f"revision workspace does not exist: {workspace}",
            )

        if not self.contract.permits_test_path(artifact.relative_path):
            return self._early_result(
                revision=revision,
                artifact=artifact,
                status=TestExecutionStatus.INVALID_ARTIFACT,
                started_at=started_at,
                detail="candidate test path is outside the execution contract allowlist",
            )

        try:
            artifact_path = self._safe_artifact_path(workspace, artifact.relative_path)
        except ValueError as error:
            return self._early_result(
                revision=revision,
                artifact=artifact,
                status=TestExecutionStatus.INVALID_ARTIFACT,
                started_at=started_at,
                detail=str(error),
            )

        if artifact_path.exists():
            return self._early_result(
                revision=revision,
                artifact=artifact,
                status=TestExecutionStatus.INVALID_ARTIFACT,
                started_at=started_at,
                detail=f"refusing to overwrite repository path: {artifact.relative_path}",
            )

        if self.install_dependencies:
            installation_error = self._install(workspace)
            if installation_error is not None:
                detail, stdout, stderr, exit_code = installation_error
                return self._early_result(
                    revision=revision,
                    artifact=artifact,
                    status=TestExecutionStatus.PROCESS_ERROR,
                    started_at=started_at,
                    detail=detail,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                )

        try:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            if not artifact_path.parent.resolve().is_relative_to(workspace):
                raise ValueError("artifact parent resolves outside the revision workspace")
            with artifact_path.open("xb") as artifact_file:
                artifact_file.write(artifact.content)
        except (OSError, ValueError) as error:
            return self._early_result(
                revision=revision,
                artifact=artifact,
                status=TestExecutionStatus.INVALID_ARTIFACT,
                started_at=started_at,
                detail=f"test artifact could not be injected safely: {error}",
            )

        hash_before = self._hash_file(artifact_path)
        syntax_error = self._syntax_error(artifact)
        if syntax_error is not None:
            return ExecutionResult(
                revision=revision,
                test_node_id=artifact.node_id,
                expected_artifact_sha256=artifact.sha256,
                artifact_sha256_before=hash_before,
                artifact_sha256_after=self._hash_file(artifact_path),
                status=TestExecutionStatus.INVALID_ARTIFACT,
                collected_count=0,
                exit_code=None,
                duration_seconds=time.monotonic() - started_at,
                detail=syntax_error,
            )

        result_directory = workspace.parent / f".patchproof-results-{uuid.uuid4().hex}"
        result_directory.mkdir()
        try:
            report_path = result_directory / "pytest.xml"
            command = (
                *self._test_command(workspace),
                "-p",
                "no:cacheprovider",
                "--color=no",
                "--tb=short",
                "--maxfail=1",
                "-q",
                "-rxX",
                "-o",
                "junit_family=xunit2",
                f"--junitxml={report_path}",
                artifact.node_id,
            )
            environment = self.environment_policy.build(runtime_root=result_directory / "runtime")
            if self.repository_python_paths:
                environment["PYTHONPATH"] = os.pathsep.join(
                    str(self._safe_python_path(workspace, path))
                    for path in self.repository_python_paths
                )
            completed = self.processes.run(
                command,
                cwd=workspace,
                environment=environment,
                timeout_seconds=self.timeout_seconds,
            )
            if completed.timed_out:
                return ExecutionResult(
                    revision=revision,
                    test_node_id=artifact.node_id,
                    expected_artifact_sha256=artifact.sha256,
                    artifact_sha256_before=hash_before,
                    artifact_sha256_after=self._hash_file(artifact_path),
                    status=TestExecutionStatus.TIMED_OUT,
                    collected_count=0,
                    exit_code=None,
                    duration_seconds=time.monotonic() - started_at,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    detail=f"pytest exceeded the {self.timeout_seconds:g}-second timeout",
                )
            if completed.start_error is not None:
                return ExecutionResult(
                    revision=revision,
                    test_node_id=artifact.node_id,
                    expected_artifact_sha256=artifact.sha256,
                    artifact_sha256_before=hash_before,
                    artifact_sha256_after=self._hash_file(artifact_path),
                    status=TestExecutionStatus.PROCESS_ERROR,
                    collected_count=0,
                    exit_code=None,
                    duration_seconds=time.monotonic() - started_at,
                    detail=completed.start_error,
                )

            parsed = self.parser.parse(
                report_path=report_path,
                exit_code=completed.returncode or 0,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            return ExecutionResult(
                revision=revision,
                test_node_id=artifact.node_id,
                expected_artifact_sha256=artifact.sha256,
                artifact_sha256_before=hash_before,
                artifact_sha256_after=self._hash_file(artifact_path),
                status=parsed.status,
                collected_count=parsed.collected_count,
                exit_code=completed.returncode,
                duration_seconds=time.monotonic() - started_at,
                stdout=completed.stdout,
                stderr=completed.stderr,
                detail=parsed.detail,
            )
        finally:
            shutil.rmtree(result_directory, ignore_errors=True)

    def _test_command(self, workspace: Path) -> tuple[str, ...]:
        """Use the repository environment after installation for `python -m pytest`."""
        if not self.install_dependencies or self.contract.test.command[0] != "python":
            return self.command_prefix
        executable = (
            workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        return (str(executable.absolute()), *self.contract.test.command[1:])

    @staticmethod
    def _safe_artifact_path(workspace: Path, relative_path: str) -> Path:
        artifact_path = (workspace / Path(*relative_path.split("/"))).resolve()
        if not artifact_path.is_relative_to(workspace):
            raise ValueError("artifact path resolves outside the revision workspace")
        return artifact_path

    @staticmethod
    def _validate_python_path(value: str) -> str:
        if value == ".":
            return value
        if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("repository Python path contains an unsupported character")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("repository Python path must be a normalized relative POSIX path")
        return value

    @staticmethod
    def _safe_python_path(workspace: Path, relative_path: str) -> Path:
        path = workspace if relative_path == "." else workspace / Path(*relative_path.split("/"))
        resolved = path.resolve()
        if not resolved.is_relative_to(workspace):
            raise ValueError("repository Python path resolves outside the revision workspace")
        return resolved

    def _install(self, workspace: Path) -> tuple[str, str, str, int | None] | None:
        """Run only validated contract argument arrays, without a command shell."""
        captured_stdout: list[str] = []
        captured_stderr: list[str] = []
        runtime_root = workspace.parent / f".patchproof-install-{uuid.uuid4().hex}"
        try:
            environment = self.environment_policy.build(runtime_root=runtime_root)
            for command in self.contract.install:
                completed = self.processes.run(
                    command,
                    cwd=workspace,
                    environment=environment,
                    timeout_seconds=self.timeout_seconds,
                )
                captured_stdout.append(completed.stdout)
                captured_stderr.append(completed.stderr)
                if completed.timed_out:
                    return (
                        "dependency installation exceeded the "
                        f"{self.timeout_seconds:g}-second timeout",
                        "".join(captured_stdout)[:12_000],
                        "".join(captured_stderr)[:12_000],
                        None,
                    )
                if completed.start_error is not None:
                    return ("dependency installation could not start", "", "", None)
                if completed.returncode != 0:
                    return (
                        f"dependency installation failed with exit code {completed.returncode}",
                        "".join(captured_stdout)[:12_000],
                        "".join(captured_stderr)[:12_000],
                        completed.returncode,
                    )
            return None
        finally:
            remove_runtime_directory(runtime_root)

    @staticmethod
    def _syntax_error(artifact: TestArtifact) -> str | None:
        try:
            source = artifact.content.decode("utf-8")
            ast.parse(source, filename=artifact.relative_path)
        except (UnicodeDecodeError, SyntaxError) as error:
            return f"candidate test is not valid UTF-8 Python source: {error}"
        return None

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    @staticmethod
    def _early_result(
        *,
        revision: Revision,
        artifact: TestArtifact,
        status: TestExecutionStatus,
        started_at: float,
        detail: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            revision=revision,
            test_node_id=artifact.node_id,
            expected_artifact_sha256=artifact.sha256,
            artifact_sha256_before=None,
            artifact_sha256_after=None,
            status=status,
            collected_count=0,
            exit_code=exit_code,
            duration_seconds=time.monotonic() - started_at,
            stdout=stdout,
            stderr=stderr,
            detail=detail,
        )
