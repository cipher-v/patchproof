"""Conservative mechanical classification for paired execution results."""

from __future__ import annotations

from patchproof.models import (
    DifferentialPattern,
    EvidenceAssessment,
    ExecutionResult,
    MechanicalEvidenceStatus,
    RevisionRole,
    TestArtifact,
    TestExecutionStatus,
)

_INVALID_TEST_STATUSES = {
    TestExecutionStatus.INVALID_ARTIFACT,
    TestExecutionStatus.NOT_COLLECTED,
    TestExecutionStatus.MULTIPLE_TESTS_COLLECTED,
    TestExecutionStatus.SKIPPED,
    TestExecutionStatus.XFAILED,
    TestExecutionStatus.XPASSED,
}
_ENVIRONMENTAL_STATUSES = {
    TestExecutionStatus.COLLECTION_ERROR,
    TestExecutionStatus.TEST_ERROR,
    TestExecutionStatus.TIMED_OUT,
    TestExecutionStatus.ENVIRONMENT_SETUP_FAILED,
    TestExecutionStatus.PROCESS_ERROR,
}


class MechanicalEvidenceClassifier:
    """Classify observable BASE/HEAD facts without making semantic claim judgments."""

    def classify(
        self,
        *,
        artifact: TestArtifact,
        base: ExecutionResult,
        head: ExecutionResult,
    ) -> EvidenceAssessment:
        """Return a conservative evidence status for the paired executions."""
        if (
            base.revision.role is not RevisionRole.BASE
            or head.revision.role is not RevisionRole.HEAD
        ):
            raise ValueError("evidence classification requires BASE and HEAD execution roles")

        declared_identity_problem = self._declared_identity_problem(
            artifact=artifact, base=base, head=head
        )
        if declared_identity_problem is not None:
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.INVALID_TEST,
                pattern=DifferentialPattern.NOT_COMPARABLE,
                reason=declared_identity_problem,
            )

        statuses = {base.status, head.status}
        if statuses & _INVALID_TEST_STATUSES:
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.INVALID_TEST,
                pattern=DifferentialPattern.NOT_COMPARABLE,
                reason=(
                    "candidate did not execute as one ordinary collected test on both revisions: "
                    f"BASE={base.status}, HEAD={head.status}"
                ),
            )
        if statuses & _ENVIRONMENTAL_STATUSES:
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.ENVIRONMENTAL,
                pattern=DifferentialPattern.NOT_COMPARABLE,
                reason=(
                    "execution environments did not produce comparable assertion outcomes: "
                    f"BASE={base.status}, HEAD={head.status}"
                ),
            )

        integrity_problem = self._artifact_integrity_problem(base=base, head=head)
        if integrity_problem is not None:
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.INVALID_TEST,
                pattern=DifferentialPattern.NOT_COMPARABLE,
                reason=integrity_problem,
            )

        comparable_statuses = {
            TestExecutionStatus.PASSED,
            TestExecutionStatus.ASSERTION_FAILED,
        }
        if not statuses <= comparable_statuses:
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.ENVIRONMENTAL,
                pattern=DifferentialPattern.NOT_COMPARABLE,
                reason=f"unhandled non-comparable execution statuses: {sorted(statuses)}",
            )

        if base.status is TestExecutionStatus.PASSED and head.status is TestExecutionStatus.PASSED:
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.NON_DISCRIMINATING,
                pattern=DifferentialPattern.BOTH_PASSED,
                reason="the identical selected test passed on both BASE and HEAD",
            )
        if (
            base.status is TestExecutionStatus.ASSERTION_FAILED
            and head.status is TestExecutionStatus.ASSERTION_FAILED
        ):
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.NON_DISCRIMINATING,
                pattern=DifferentialPattern.BOTH_ASSERTION_FAILED,
                reason="the identical selected test asserted unsuccessfully on both revisions",
            )
        if (
            base.status is TestExecutionStatus.ASSERTION_FAILED
            and head.status is TestExecutionStatus.PASSED
        ):
            return EvidenceAssessment(
                mechanical_status=MechanicalEvidenceStatus.DISCRIMINATING,
                pattern=DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED,
                reason=(
                    "the identical selected test assertion failed on BASE and passed on HEAD; "
                    "semantic claim support has not been assessed"
                ),
            )
        return EvidenceAssessment(
            mechanical_status=MechanicalEvidenceStatus.DISCRIMINATING,
            pattern=DifferentialPattern.BASE_PASSED_HEAD_ASSERTION_FAILED,
            reason=(
                "the identical selected test passed on BASE and asserted unsuccessfully on HEAD; "
                "semantic regression relevance has not been assessed"
            ),
        )

    @staticmethod
    def _declared_identity_problem(
        *, artifact: TestArtifact, base: ExecutionResult, head: ExecutionResult
    ) -> str | None:
        if base.test_node_id != artifact.node_id or head.test_node_id != artifact.node_id:
            return "BASE and HEAD did not use the artifact's identical pytest node ID"
        if (
            base.expected_artifact_sha256 != artifact.sha256
            or head.expected_artifact_sha256 != artifact.sha256
        ):
            return "BASE and HEAD did not declare the same expected artifact hash"
        return None

    @staticmethod
    def _artifact_integrity_problem(*, base: ExecutionResult, head: ExecutionResult) -> str | None:
        if not base.artifact_was_unchanged or not head.artifact_was_unchanged:
            return "candidate bytes were missing, mismatched, or changed during execution"
        return None
