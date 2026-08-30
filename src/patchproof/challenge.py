"""Phase 1 orchestration for one deterministic BASE/HEAD test challenge."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from patchproof.environment_introspection import installed_import_roots
from patchproof.evidence import MechanicalEvidenceClassifier
from patchproof.git_workspace import GitWorkspaceManager, RevisionWorkspaces
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


class ChallengeSession:
    """One pair of live worktrees reused for readiness and every candidate attempt.

    Creating a fresh worktree pair per operation forced the repository-declared install
    commands to run again each time -- for a three-attempt run that is eight cold
    installs, each bounded by the same timeout as the test itself. A session creates the
    pair once, installs once per revision, and runs every artifact against the same two
    checkouts.

    Reuse is safe because each artifact is written to a distinct path with exclusive
    creation, executed by exact node ID, and removed again afterwards, so no attempt can
    observe or collect another attempt's file.
    """

    def __init__(
        self,
        *,
        pair: RevisionWorkspaces,
        runner: PytestRunner,
        classifier: MechanicalEvidenceClassifier,
    ) -> None:
        self.pair = pair
        self.runner = runner
        self.classifier = classifier

    def prepare_environment(self) -> EnvironmentReadiness:
        """Install on both revisions, then prove an injected pytest node can run."""
        for role, workspace, failed_status in (
            ("BASE", self.pair.base_path, EnvironmentReadinessStatus.BASE_SETUP_FAILED),
            ("HEAD", self.pair.head_path, EnvironmentReadinessStatus.HEAD_SETUP_FAILED),
        ):
            error = self.runner.prepare_environment(workspace=workspace)
            if error is not None:
                reason = error if isinstance(error, str) else error.reason
                return EnvironmentReadiness(
                    status=failed_status,
                    reason=f"{role} repository environment setup failed: {reason}",
                    setup_diagnostic=error if not isinstance(error, str) else None,
                )

        probe = self._probe_artifact()
        for role, workspace, revision, failed_status in (
            (
                "BASE",
                self.pair.base_path,
                self.pair.base_revision,
                EnvironmentReadinessStatus.BASE_SETUP_FAILED,
            ),
            (
                "HEAD",
                self.pair.head_path,
                self.pair.head_revision,
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

    def run(
        self,
        *,
        artifact: TestArtifact,
        on_base_complete: Callable[[ExecutionResult], None] | None = None,
    ) -> ChallengeResult:
        """Execute identical bytes on both live worktrees and classify the pair."""
        base_result = self.runner.run(
            workspace=self.pair.base_path,
            revision=self.pair.base_revision,
            artifact=artifact,
        )
        if on_base_complete is not None:
            on_base_complete(base_result)
        head_result = self.runner.run(
            workspace=self.pair.head_path,
            revision=self.pair.head_revision,
            artifact=artifact,
        )
        assessment = self.classifier.classify(artifact=artifact, base=base_result, head=head_result)
        return ChallengeResult(
            artifact=artifact,
            base=base_result,
            head=head_result,
            assessment=assessment,
        )

    def installed_import_roots(self) -> frozenset[str]:
        """Top-level names importable on both revisions of this session.

        Intersecting BASE and HEAD keeps grounding symmetric: a candidate may only
        import something that is present on both sides of the comparison, so a
        dependency added by the pull request itself cannot make a test that is
        structurally unrunnable on BASE look like a discriminating result.
        """
        base = installed_import_roots(self.pair.base_path)
        head = installed_import_roots(self.pair.head_path)
        if not base or not head:
            return frozenset()
        return base & head

    def _probe_artifact(self) -> TestArtifact:
        """Build the readiness probe at the contract's first allowed test path."""
        directory = self.runner.contract.allowed_test_paths[0]
        relative_path = f"{directory}test_patchproof_environment_probe.py"
        return TestArtifact.from_text(
            relative_path=relative_path,
            node_id=f"{relative_path}::{READINESS_PROBE_FUNCTION}",
            content=READINESS_PROBE_SOURCE,
        )


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

    @contextmanager
    def session(self, *, base_ref: str, head_ref: str) -> Iterator[ChallengeSession]:
        """Open one worktree pair reused for readiness and every candidate attempt."""
        with self.workspaces.create_pair(base_ref=base_ref, head_ref=head_ref) as pair:
            yield ChallengeSession(pair=pair, runner=self.runner, classifier=self.classifier)

    def prepare_environment(self, *, base_ref: str, head_ref: str) -> EnvironmentReadiness:
        """Validate setup and injected-node execution in a single-use session."""
        with self.session(base_ref=base_ref, head_ref=head_ref) as session:
            return session.prepare_environment()

    def run(
        self,
        *,
        base_ref: str,
        head_ref: str,
        artifact: TestArtifact,
        on_base_complete: Callable[[ExecutionResult], None] | None = None,
    ) -> ChallengeResult:
        """Resolve refs, execute identical bytes on each worktree, and clean all workspaces."""
        with self.session(base_ref=base_ref, head_ref=head_ref) as session:
            return session.run(artifact=artifact, on_base_complete=on_base_complete)
