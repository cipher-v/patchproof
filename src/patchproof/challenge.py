"""Phase 1 orchestration for one deterministic BASE/HEAD test challenge."""

from __future__ import annotations

from collections.abc import Callable

from patchproof.evidence import MechanicalEvidenceClassifier
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.models import ChallengeResult, ExecutionResult, TestArtifact
from patchproof.pytest_runner import PytestRunner


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
