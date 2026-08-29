"""End-to-end checks for conservative pytest expectation normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from patchproof.execution_contract import ExecutionContract
from patchproof.models import Revision, RevisionRole, TestArtifact, TestExecutionStatus
from patchproof.pytest_runner import PytestRunner


def _run(writable_test_directory: Path, body: str):
    workspace = writable_test_directory / "workspace"
    workspace.mkdir()
    contract = ExecutionContract.model_validate(
        {
            "version": 1,
            "python": "3.12",
            "install": [["uv", "sync", "--frozen"]],
            "test": {"command": ["python", "-m", "pytest"]},
            "allowed_test_paths": ["generated/"],
            "timeout_seconds": 30,
        }
    )
    source = f"import pytest\n\ndef test_patchproof_generated_behavior():\n{body}"
    artifact = TestArtifact.from_text(
        relative_path="generated/test_expectation.py",
        node_id=("generated/test_expectation.py::test_patchproof_generated_behavior"),
        content=source,
    )
    return PytestRunner(
        contract=contract,
        python_executable=Path(sys.executable),
    ).run(
        workspace=workspace,
        revision=Revision(role=RevisionRole.HEAD, sha="a" * 40),
        artifact=artifact,
    )


@pytest.mark.parametrize(
    ("body", "expected_detail"),
    [
        (
            "    with pytest.warns(UserWarning):\n        pass\n",
            "PYTEST_EXPECTATION_NOT_MET: expected warning was not emitted",
        ),
        (
            "    with pytest.raises(ValueError):\n        pass\n",
            "PYTEST_EXPECTATION_NOT_MET: expected exception was not raised",
        ),
    ],
)
def test_real_pytest_expectation_non_occurrence_is_an_assertion_failure(
    writable_test_directory: Path,
    body: str,
    expected_detail: str,
) -> None:
    result = _run(writable_test_directory, body)

    assert result.status is TestExecutionStatus.ASSERTION_FAILED
    assert result.detail == expected_detail


@pytest.mark.parametrize(
    "body",
    [
        "    with pytest.warns(UserWarning):\n        raise TypeError('unexpected')\n",
        "    with pytest.raises(ValueError):\n        raise TypeError('unexpected')\n",
    ],
)
def test_real_unexpected_exception_inside_expectation_remains_test_error(
    writable_test_directory: Path,
    body: str,
) -> None:
    result = _run(writable_test_directory, body)

    assert result.status is TestExecutionStatus.TEST_ERROR
