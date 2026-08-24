"""Unit tests for conservative mechanical evidence classification."""

from __future__ import annotations

from dataclasses import replace

import pytest

from patchproof.evidence import MechanicalEvidenceClassifier
from patchproof.models import (
    DifferentialPattern,
    ExecutionResult,
    MechanicalEvidenceStatus,
    Revision,
    RevisionRole,
    TestArtifact,
    TestExecutionStatus,
)

_ARTIFACT = TestArtifact.from_text(
    relative_path="tests/test_generated.py",
    node_id="tests/test_generated.py::test_behavior",
    content="def test_behavior():\n    assert True\n",
)


def _result(role: RevisionRole, status: TestExecutionStatus) -> ExecutionResult:
    return ExecutionResult(
        revision=Revision(role=role, sha=("a" if role is RevisionRole.BASE else "b") * 40),
        test_node_id=_ARTIFACT.node_id,
        expected_artifact_sha256=_ARTIFACT.sha256,
        artifact_sha256_before=_ARTIFACT.sha256,
        artifact_sha256_after=_ARTIFACT.sha256,
        status=status,
        collected_count=1,
        exit_code=0 if status is TestExecutionStatus.PASSED else 1,
        duration_seconds=0.1,
    )


@pytest.mark.parametrize(
    ("base_status", "head_status", "mechanical_status", "pattern"),
    [
        (
            TestExecutionStatus.ASSERTION_FAILED,
            TestExecutionStatus.PASSED,
            MechanicalEvidenceStatus.DISCRIMINATING,
            DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED,
        ),
        (
            TestExecutionStatus.PASSED,
            TestExecutionStatus.ASSERTION_FAILED,
            MechanicalEvidenceStatus.DISCRIMINATING,
            DifferentialPattern.BASE_PASSED_HEAD_ASSERTION_FAILED,
        ),
        (
            TestExecutionStatus.PASSED,
            TestExecutionStatus.PASSED,
            MechanicalEvidenceStatus.NON_DISCRIMINATING,
            DifferentialPattern.BOTH_PASSED,
        ),
        (
            TestExecutionStatus.ASSERTION_FAILED,
            TestExecutionStatus.ASSERTION_FAILED,
            MechanicalEvidenceStatus.NON_DISCRIMINATING,
            DifferentialPattern.BOTH_ASSERTION_FAILED,
        ),
    ],
)
def test_classifier_handles_comparable_outcome_patterns(
    base_status: TestExecutionStatus,
    head_status: TestExecutionStatus,
    mechanical_status: MechanicalEvidenceStatus,
    pattern: DifferentialPattern,
) -> None:
    assessment = MechanicalEvidenceClassifier().classify(
        artifact=_ARTIFACT,
        base=_result(RevisionRole.BASE, base_status),
        head=_result(RevisionRole.HEAD, head_status),
    )

    assert assessment.mechanical_status is mechanical_status
    assert assessment.pattern is pattern
    assert assessment.claim_outcome is None


@pytest.mark.parametrize(
    "status",
    [
        TestExecutionStatus.INVALID_ARTIFACT,
        TestExecutionStatus.NOT_COLLECTED,
        TestExecutionStatus.MULTIPLE_TESTS_COLLECTED,
        TestExecutionStatus.SKIPPED,
        TestExecutionStatus.XFAILED,
        TestExecutionStatus.XPASSED,
    ],
)
def test_classifier_rejects_invalid_or_nonexecuted_candidates(status: TestExecutionStatus) -> None:
    assessment = MechanicalEvidenceClassifier().classify(
        artifact=_ARTIFACT,
        base=_result(RevisionRole.BASE, status),
        head=_result(RevisionRole.HEAD, TestExecutionStatus.PASSED),
    )

    assert assessment.mechanical_status is MechanicalEvidenceStatus.INVALID_TEST
    assert assessment.pattern is DifferentialPattern.NOT_COMPARABLE


@pytest.mark.parametrize(
    "status",
    [
        TestExecutionStatus.COLLECTION_ERROR,
        TestExecutionStatus.TEST_ERROR,
        TestExecutionStatus.TIMED_OUT,
        TestExecutionStatus.PROCESS_ERROR,
    ],
)
def test_classifier_rejects_noncomparable_environmental_results(
    status: TestExecutionStatus,
) -> None:
    assessment = MechanicalEvidenceClassifier().classify(
        artifact=_ARTIFACT,
        base=_result(RevisionRole.BASE, status),
        head=_result(RevisionRole.HEAD, TestExecutionStatus.PASSED),
    )

    assert assessment.mechanical_status is MechanicalEvidenceStatus.ENVIRONMENTAL
    assert assessment.pattern is DifferentialPattern.NOT_COMPARABLE


def test_classifier_rejects_an_artifact_modified_during_execution() -> None:
    base = replace(
        _result(RevisionRole.BASE, TestExecutionStatus.PASSED),
        artifact_sha256_after="c" * 64,
    )

    assessment = MechanicalEvidenceClassifier().classify(
        artifact=_ARTIFACT,
        base=base,
        head=_result(RevisionRole.HEAD, TestExecutionStatus.PASSED),
    )

    assert assessment.mechanical_status is MechanicalEvidenceStatus.INVALID_TEST
    assert "changed" in assessment.reason


def test_process_failure_before_hashing_remains_environmental() -> None:
    base = replace(
        _result(RevisionRole.BASE, TestExecutionStatus.PROCESS_ERROR),
        artifact_sha256_before=None,
        artifact_sha256_after=None,
    )

    assessment = MechanicalEvidenceClassifier().classify(
        artifact=_ARTIFACT,
        base=base,
        head=_result(RevisionRole.HEAD, TestExecutionStatus.PASSED),
    )

    assert assessment.mechanical_status is MechanicalEvidenceStatus.ENVIRONMENTAL


def test_classifier_rejects_reversed_revision_roles() -> None:
    with pytest.raises(ValueError, match="BASE and HEAD"):
        MechanicalEvidenceClassifier().classify(
            artifact=_ARTIFACT,
            base=_result(RevisionRole.HEAD, TestExecutionStatus.PASSED),
            head=_result(RevisionRole.BASE, TestExecutionStatus.PASSED),
        )
