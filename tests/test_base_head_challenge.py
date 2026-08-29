"""End-to-end Phase 1 tests using a real two-commit fixture repository."""

from __future__ import annotations

import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest
from conftest import RepositoryHistory

from patchproof.challenge import BaseHeadChallenge
from patchproof.execution_contract import ExecutionContract
from patchproof.execution_runtime import BoundedProcessResult
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.models import (
    DifferentialPattern,
    EnvironmentReadinessStatus,
    MechanicalEvidenceStatus,
    Revision,
    RevisionRole,
    TestArtifact,
    TestExecutionStatus,
)
from patchproof.pytest_runner import PytestRunner

_ARTIFACT_PATH = "tests/patchproof_generated/test_patchproof_generated.py"

_CONTRACT_DATA = {
    "version": 1,
    "python": "3.12",
    "install": [["uv", "sync", "--frozen"]],
    "test": {"command": ["python", "-m", "pytest"]},
    "allowed_test_paths": ["tests/patchproof_generated/"],
    "timeout_seconds": 5,
}


def _artifact(test_name: str, body: str) -> TestArtifact:
    return TestArtifact.from_text(
        relative_path=_ARTIFACT_PATH,
        node_id=f"{_ARTIFACT_PATH}::{test_name}",
        content=body,
    )


def _challenge(
    repository_history: RepositoryHistory, *, timeout_seconds: float = 5.0
) -> BaseHeadChallenge:
    return BaseHeadChallenge(
        workspaces=GitWorkspaceManager(
            source_repository=repository_history.path,
            workspace_root=repository_history.workspace_root,
        ),
        runner=PytestRunner(
            contract=ExecutionContract.model_validate(
                {**_CONTRACT_DATA, "timeout_seconds": timeout_seconds}
            ),
            python_executable=Path(sys.executable),
        ),
    )


def _run(
    repository_history: RepositoryHistory,
    artifact: TestArtifact,
    *,
    timeout_seconds: float = 5.0,
):
    result = _challenge(repository_history, timeout_seconds=timeout_seconds).run(
        base_ref=repository_history.base_sha,
        head_ref=repository_history.head_sha,
        artifact=artifact,
    )
    assert list(repository_history.workspace_root.iterdir()) == []
    return result


class _SetupRunner:
    def __init__(self, *results: str | None) -> None:
        self.results = deque(results)
        self.calls = 0

    def prepare_environment(self, *, workspace: Path) -> str | None:
        assert workspace.is_dir()
        self.calls += 1
        return self.results.popleft()


def test_environment_readiness_distinguishes_base_setup_failure(
    repository_history: RepositoryHistory,
) -> None:
    runner = _SetupRunner("dependency installation failed")
    challenge = BaseHeadChallenge(
        workspaces=GitWorkspaceManager(
            source_repository=repository_history.path,
            workspace_root=repository_history.workspace_root,
        ),
        runner=runner,  # type: ignore[arg-type]
    )

    readiness = challenge.prepare_environment(
        base_ref=repository_history.base_sha,
        head_ref=repository_history.head_sha,
    )

    assert readiness.status is EnvironmentReadinessStatus.BASE_SETUP_FAILED
    assert "BASE" in readiness.reason
    assert runner.calls == 1
    assert list(repository_history.workspace_root.iterdir()) == []


def test_environment_readiness_distinguishes_head_setup_failure(
    repository_history: RepositoryHistory,
) -> None:
    runner = _SetupRunner(None, "dependency installation timed out")
    challenge = BaseHeadChallenge(
        workspaces=GitWorkspaceManager(
            source_repository=repository_history.path,
            workspace_root=repository_history.workspace_root,
        ),
        runner=runner,  # type: ignore[arg-type]
    )

    readiness = challenge.prepare_environment(
        base_ref=repository_history.base_sha,
        head_ref=repository_history.head_sha,
    )

    assert readiness.status is EnvironmentReadinessStatus.HEAD_SETUP_FAILED
    assert "HEAD" in readiness.reason
    assert runner.calls == 2
    assert list(repository_history.workspace_root.iterdir()) == []


def test_install_failure_has_a_distinct_status_and_uses_only_contract_argv(
    writable_test_directory: Path,
) -> None:
    class FailingProcesses:
        def __init__(self) -> None:
            self.commands = []

        def run(self, command, **kwargs):
            del kwargs
            self.commands.append(command)
            return BoundedProcessResult(
                returncode=2,
                duration_seconds=0.01,
                stdout="resolver failed",
                stderr="",
            )

    workspace = writable_test_directory / "failed-install-workspace"
    workspace.mkdir()
    processes = FailingProcesses()
    runner = PytestRunner(
        contract=ExecutionContract.model_validate(_CONTRACT_DATA),
        python_executable=Path(sys.executable),
        install_dependencies=True,
        processes=processes,  # type: ignore[arg-type]
    )
    artifact = _artifact("test_never_runs", "def test_never_runs():\n    assert True\n")

    result = runner.run(
        workspace=workspace,
        revision=Revision(role=RevisionRole.BASE, sha="a" * 40),
        artifact=artifact,
    )

    assert result.status is TestExecutionStatus.ENVIRONMENT_SETUP_FAILED
    assert processes.commands == [("uv", "sync", "--frozen")]
    assert not (workspace / _ARTIFACT_PATH).exists()


def test_base_assertion_failure_and_head_pass_are_mechanically_discriminating(
    repository_history: RepositoryHistory,
) -> None:
    artifact = _artifact(
        "test_add",
        "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )

    result = _run(repository_history, artifact)

    assert result.base.revision.sha == repository_history.base_sha
    assert result.head.revision.sha == repository_history.head_sha
    assert result.base.status is TestExecutionStatus.ASSERTION_FAILED
    assert result.head.status is TestExecutionStatus.PASSED
    assert result.base.exit_code == 1
    assert result.head.exit_code == 0
    assert result.base.collected_count == result.head.collected_count == 1
    assert result.base.artifact_was_unchanged
    assert result.head.artifact_was_unchanged
    assert result.base.expected_artifact_sha256 == result.head.expected_artifact_sha256
    assert result.base.test_node_id == result.head.test_node_id == artifact.node_id
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING
    assert result.assessment.pattern is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
    assert result.assessment.claim_outcome is None


def test_runner_rejects_artifact_outside_execution_contract_path(
    repository_history: RepositoryHistory,
) -> None:
    artifact = TestArtifact.from_text(
        relative_path="tests/test_outside_contract.py",
        node_id="tests/test_outside_contract.py::test_outside",
        content="def test_outside() -> None:\n    assert True\n",
    )

    result = _run(repository_history, artifact)

    assert result.base.status is TestExecutionStatus.INVALID_ARTIFACT
    assert result.head.status is TestExecutionStatus.INVALID_ARTIFACT
    assert "outside the execution contract" in (result.base.detail or "")


def test_installed_python_contract_uses_repository_virtual_environment(
    writable_test_directory: Path,
) -> None:
    workspace = writable_test_directory / "workspace"
    workspace.mkdir()
    runner = PytestRunner(
        contract=ExecutionContract.model_validate(_CONTRACT_DATA),
        python_executable=Path(sys.executable),
        install_dependencies=True,
    )

    command = runner._test_command(workspace)

    expected = (
        workspace / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    assert command == (str(expected.absolute()), "-m", "pytest")


@pytest.mark.parametrize(
    ("test_name", "body", "base_status", "head_status", "evidence_status", "pattern"),
    [
        (
            "test_result_is_integer",
            "from calculator import add\n\n\n"
            "def test_result_is_integer():\n"
            "    assert isinstance(add(2, 3), int)\n",
            TestExecutionStatus.PASSED,
            TestExecutionStatus.PASSED,
            MechanicalEvidenceStatus.NON_DISCRIMINATING,
            DifferentialPattern.BOTH_PASSED,
        ),
        (
            "test_impossible_result",
            "from calculator import add\n\n\n"
            "def test_impossible_result():\n"
            "    assert add(2, 3) == 999\n",
            TestExecutionStatus.ASSERTION_FAILED,
            TestExecutionStatus.ASSERTION_FAILED,
            MechanicalEvidenceStatus.NON_DISCRIMINATING,
            DifferentialPattern.BOTH_ASSERTION_FAILED,
        ),
        (
            "test_old_buggy_result",
            "from calculator import add\n\n\n"
            "def test_old_buggy_result():\n"
            "    assert add(2, 3) == -1\n",
            TestExecutionStatus.PASSED,
            TestExecutionStatus.ASSERTION_FAILED,
            MechanicalEvidenceStatus.DISCRIMINATING,
            DifferentialPattern.BASE_PASSED_HEAD_ASSERTION_FAILED,
        ),
    ],
)
def test_challenge_classifies_other_comparable_patterns(
    repository_history: RepositoryHistory,
    test_name: str,
    body: str,
    base_status: TestExecutionStatus,
    head_status: TestExecutionStatus,
    evidence_status: MechanicalEvidenceStatus,
    pattern: DifferentialPattern,
) -> None:
    result = _run(repository_history, _artifact(test_name, body))

    assert result.base.status is base_status
    assert result.head.status is head_status
    assert result.assessment.mechanical_status is evidence_status
    assert result.assessment.pattern is pattern


def test_invalid_python_is_rejected_without_starting_pytest(
    repository_history: RepositoryHistory,
) -> None:
    artifact = _artifact("test_invalid", "def test_invalid(:\n    pass\n")

    result = _run(repository_history, artifact)

    assert result.base.status is TestExecutionStatus.INVALID_ARTIFACT
    assert result.head.status is TestExecutionStatus.INVALID_ARTIFACT
    assert result.base.exit_code is None
    assert result.head.exit_code is None
    assert result.base.artifact_was_unchanged
    assert result.head.artifact_was_unchanged
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.INVALID_TEST


def test_collection_error_is_environmental_not_discriminating(
    repository_history: RepositoryHistory,
) -> None:
    artifact = _artifact(
        "test_missing_import",
        "from package_that_does_not_exist import value\n\n\n"
        "def test_missing_import():\n"
        "    assert value\n",
    )

    result = _run(repository_history, artifact)

    assert result.base.status is TestExecutionStatus.COLLECTION_ERROR
    assert result.head.status is TestExecutionStatus.COLLECTION_ERROR
    assert result.base.collected_count == result.head.collected_count == 0
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.ENVIRONMENTAL
    assert result.assessment.pattern is DifferentialPattern.NOT_COMPARABLE


def test_missing_selected_node_is_invalid_evidence(
    repository_history: RepositoryHistory,
) -> None:
    artifact = _artifact("test_missing", "def helper():\n    return True\n")

    result = _run(repository_history, artifact)

    assert result.base.status is TestExecutionStatus.NOT_COLLECTED
    assert result.head.status is TestExecutionStatus.NOT_COLLECTED
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.INVALID_TEST


def test_runtime_exception_is_environmental_not_an_assertion_failure(
    repository_history: RepositoryHistory,
) -> None:
    artifact = _artifact(
        "test_runtime_error",
        "def test_runtime_error():\n    raise RuntimeError('unrelated execution failure')\n",
    )

    result = _run(repository_history, artifact)

    assert result.base.status is TestExecutionStatus.TEST_ERROR
    assert result.head.status is TestExecutionStatus.TEST_ERROR
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.ENVIRONMENTAL


def test_timeout_is_captured_and_classified_as_environmental(
    repository_history: RepositoryHistory,
) -> None:
    artifact = _artifact(
        "test_timeout",
        "import time\n\n\ndef test_timeout():\n    time.sleep(2)\n",
    )

    result = _run(repository_history, artifact, timeout_seconds=0.25)

    assert result.base.status is TestExecutionStatus.TIMED_OUT
    assert result.head.status is TestExecutionStatus.TIMED_OUT
    assert result.base.exit_code is None
    assert result.head.exit_code is None
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.ENVIRONMENTAL


def test_candidate_that_mutates_its_own_bytes_is_invalidated(
    repository_history: RepositoryHistory,
) -> None:
    artifact = _artifact(
        "test_mutates_itself",
        "from pathlib import Path\n\n\n"
        "def test_mutates_itself():\n"
        "    Path(__file__).write_text('# modified by test\\n', encoding='utf-8')\n"
        "    assert True\n",
    )

    result = _run(repository_history, artifact)

    assert result.base.status is TestExecutionStatus.PASSED
    assert result.head.status is TestExecutionStatus.PASSED
    assert not result.base.artifact_was_unchanged
    assert not result.head.artifact_was_unchanged
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.INVALID_TEST

    source_status = subprocess.run(
        ("git", "-C", str(repository_history.path), "status", "--porcelain"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert source_status == ""
