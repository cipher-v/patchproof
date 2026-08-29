"""Phase 1 orchestration for one deterministic BASE/HEAD test challenge."""

from __future__ import annotations

from collections.abc import Callable

from patchproof.evidence import MechanicalEvidenceClassifier
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.models import (
    ChallengeResult,
    EnvironmentReadiness,
    EnvironmentReadinessStatus,
    ExecutionResult,
    TestArtifact,
    TestExecutionStatus,
)
from patchproof.pytest_runner import PytestRunner

READINESS_PROBE_FUNCTION = "test_patchproof_environment_probe"
#: The smallest possible collected test. It imports nothing from the repository, so a
#: failure here is unambiguously an environment or pytest-configuration problem rather
#: than anything about the pull request under examination.
READINESS_PROBE_SOURCE = f"def {READINESS_PROBE_FUNCTION}() -> None:\n    assert True\n"


class BaseHeadChallenge:
    """Execute one artifact on two immutable revisions and classify the paired facts."""

    def __init__(
        self,
        *,
        workspaces: GitWorkspaceManager,
        runner: PytestRunner,
        classifier: MechanicalEvidenceClassifier | None = None,
    ) -> None:
        self.workspaces = workspaces
        self.runner = runner
        self.classifier = classifier or MechanicalEvidenceClassifier()

    def prepare_environment(self, *, base_ref: str, head_ref: str) -> EnvironmentReadiness:
        """Prove both revisions can install *and* run one injected pytest node.

        Installing successfully is not evidence that a generated candidate can run.
        The sealed unseen holdout established the difference the hard way: its oracle
        tests were executed by absolute path from outside the checkout while generated
        candidates were injected into the checkout, so the oracle gate certified ten
        cases as runnable when eight of them could not execute a candidate at all
        (``docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md`` section B.1).

        This gate therefore ends by running :data:`READINESS_PROBE_SOURCE` -- a trivial
        always-passing test -- through the *identical* injection point, argument vector,
        and environment policy a real candidate uses. Only a PASS on both revisions
        counts as ready.
        """
        with self.workspaces.create_pair(base_ref=base_ref, head_ref=head_ref) as pair:
            for role, workspace, failed_status in (
                ("BASE", pair.base_path, EnvironmentReadinessStatus.BASE_SETUP_FAILED),
                ("HEAD", pair.head_path, EnvironmentReadinessStatus.HEAD_SETUP_FAILED),
            ):
                error = self.runner.prepare_environment(workspace=workspace)
                if error is not None:
                    return EnvironmentReadiness(
                        status=failed_status,
                        reason=f"{role} repository environment setup failed: {error}",
                    )

            probe = self._probe_artifact()
            for role, workspace, revision, failed_status in (
                (
                    "BASE",
                    pair.base_path,
                    pair.base_revision,
                    EnvironmentReadinessStatus.BASE_SETUP_FAILED,
                ),
                (
                    "HEAD",
                    pair.head_path,
                    pair.head_revision,
                    EnvironmentReadinessStatus.HEAD_SETUP_FAILED,
                ),
            ):
                result = self.runner.run(workspace=workspace, revision=revision, artifact=probe)
                if result.status is not TestExecutionStatus.PASSED:
                    detail = (result.detail or "")[:600]
                    return EnvironmentReadiness(
                        status=failed_status,
                        reason=(
                            f"{role} could not execute an injected pytest node "
                            f"(status={result.status}): {detail}"
                        )[:2_000],
                    )
        return EnvironmentReadiness(
            status=EnvironmentReadinessStatus.READY,
            reason=(
                "repository-declared setup completed and an injected pytest node "
                "executed successfully on BASE and HEAD"
            ),
        )

    def _probe_artifact(self) -> TestArtifact:
        """Build the readiness probe at the contract's first allowed test path."""
        directory = self.runner.contract.allowed_test_paths[0]
        relative_path = f"{directory}test_patchproof_environment_probe.py"
        return TestArtifact.from_text(
            relative_path=relative_path,
            node_id=f"{relative_path}::{READINESS_PROBE_FUNCTION}",
            content=READINESS_PROBE_SOURCE,
        )

    def run(
        self,
        *,
        base_ref: str,
        head_ref: str,
        artifact: TestArtifact,
        on_base_complete: Callable[[ExecutionResult], None] | None = None,
    ) -> ChallengeResult:
        """Resolve refs, execute identical bytes on each worktree, and clean all workspaces."""
        with self.workspaces.create_pair(base_ref=base_ref, head_ref=head_ref) as pair:
            base_result = self.runner.run(
                workspace=pair.base_path,
                revision=pair.base_revision,
                artifact=artifact,
            )
            if on_base_complete is not None:
                on_base_complete(base_result)
            head_result = self.runner.run(
                workspace=pair.head_path,
                revision=pair.head_revision,
                artifact=artifact,
            )

        assessment = self.classifier.classify(
            artifact=artifact,
            base=base_result,
            head=head_result,
        )
        return ChallengeResult(
            artifact=artifact,
            base=base_result,
            head=head_result,
            assessment=assessment,
        )
