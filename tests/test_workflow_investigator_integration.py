"""EvidenceWorkflow -> ClaimInvestigator -> ClaimAgentResult -> existing evidence path.

These run the real `EvidenceWorkflow` against a real two-commit Git fixture, with real
BASE/HEAD pytest execution and the real mechanical classifier. Only the two model
boundaries are scripted. No Gemini call, no historical PR.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import ContextRepositoryHistory

from patchproof.challenge import BaseHeadChallenge
from patchproof.claim_agent import BehavioralClaimAgent, ModelUsage
from patchproof.claim_investigation import DeterministicInvestigationPlanner
from patchproof.claim_investigator import (
    ClaimInvestigator,
    InvestigationTranscript,
    RawInvestigatorResponse,
)
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.evidence_workflow import EvidenceReport, EvidenceWorkflow
from patchproof.execution_contract import ExecutionContractLoader
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.investigation_tools import RepositoryInvestigator
from patchproof.models import ClaimOutcome, MechanicalEvidenceStatus, Revision, RevisionRole
from patchproof.pytest_runner import PytestRunner
from patchproof.repository_index import RepositoryIndex
from patchproof.storage import SqliteVerificationRunStore
from patchproof.test_generation import CandidateTestDraft, RawCandidateModelResponse
from patchproof.workflow import PullRequestEvent

# The fixture repository changes WorkspaceResolver.choose_workspace: BASE returns the
# first candidate, HEAD returns the most specific one.
_INTERFACE_PATH = "workspace.py"
_INTERFACE_NAME = "WorkspaceResolver.choose_workspace"

_CANDIDATE = (
    "from workspace import WorkspaceResolver\n\n\n"
    "def test_patchproof_generated_behavior():\n"
    "    assert WorkspaceResolver.choose_workspace(['a', 'a/b/c']) == 'a/b/c'\n"
)


class ScriptedInvestigatorModel:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def invoke(self, request):
        del request
        self.calls += 1
        return RawInvestigatorResponse(
            text=self.responses.pop(0),
            usage=ModelUsage(model_name="scripted-investigator", duration_seconds=0.01),
        )


class ScriptedCandidateModel:
    def __init__(self, source: str) -> None:
        self.source = source
        self.calls = 0

    async def invoke(self, request):
        del request
        self.calls += 1
        return RawCandidateModelResponse(
            text=CandidateTestDraft(
                source=self.source, rationale="exercises the claimed behavior"
            ).model_dump_json(),
            usage=ModelUsage(model_name="scripted-candidate", duration_seconds=0.01),
        )


class ScriptedAssessor:
    def __init__(self) -> None:
        self.calls = 0

    async def assess(self, *, claim, candidate_source, challenge):
        del claim, candidate_source, challenge
        from patchproof.evidence_workflow import (
            AssertionRelation,
            SemanticAssessmentResult,
            SemanticEvidenceDecision,
        )

        self.calls += 1
        return SemanticAssessmentResult(
            decision=SemanticEvidenceDecision(
                outcome=ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO,
                assertion_relation=AssertionRelation.RELATED,
                explanation="the assertion exercises the claimed workspace selection",
                confidence=0.95,
            ),
            usage=ModelUsage(model_name="scripted-assessor", duration_seconds=0.01),
            raw_response_sha256="c" * 64,
        )


class FixedInvestigatorFactory:
    """Build the investigator from in-memory indexes rather than Git, for speed."""

    def __init__(self, *, repository: Path, model) -> None:
        self.repository = repository
        self.model = model

    def build(self, *, base_sha: str, head_sha: str) -> ClaimInvestigator:
        base = RepositoryIndex.from_git(
            source_repository=self.repository,
            revision=Revision(role=RevisionRole.BASE, sha=base_sha),
        )
        head = RepositoryIndex.from_git(
            source_repository=self.repository,
            revision=Revision(role=RevisionRole.HEAD, sha=head_sha),
        )
        planner = DeterministicInvestigationPlanner(
            investigator=RepositoryInvestigator(base=base, head=head)
        )
        return ClaimInvestigator(model=self.model, planner=planner)


def _conclude(*, path: str = _INTERFACE_PATH, qualified_name: str = _INTERFACE_NAME) -> str:
    return json.dumps(
        {
            "action": "CONCLUDE",
            "reasoning": "choose_workspace is the shared observable whose behavior changed.",
            "disposition": "SELECTED",
            "claim": {
                "summary": "Workspace selection prefers the most specific candidate path.",
                "interface_path": path,
                "interface_qualified_name": qualified_name,
                "observable_operation": "WorkspaceResolver.choose_workspace(candidates)",
                "trigger_condition": "two candidates differ in depth",
                "expected_head_observation": "the deepest candidate is returned",
                "expected_base_hypothesis": "the first candidate is returned",
                "action": "resolve a workspace from two candidates",
                "expected_behavior": "the most specific path is returned",
                "preconditions": ["at least two candidates"],
                "confidence": 0.92,
            },
        }
    )


def _investigate(*calls: dict[str, object]) -> str:
    return json.dumps(
        {
            "action": "INVESTIGATE",
            "reasoning": "checking the shared symbol",
            "tool_calls": list(calls),
        }
    )


def _build_workflow(
    history: ContextRepositoryHistory,
    directory: Path,
    *,
    investigator_model,
    candidate_source: str = _CANDIDATE,
):
    store = SqliteVerificationRunStore(directory / "runs.sqlite3")
    retriever = DeterministicContextRetriever(source_repository=history.path)
    workspaces = directory / "workspaces"
    workspaces.mkdir(exist_ok=True)
    challenge = BaseHeadChallenge(
        workspaces=GitWorkspaceManager(source_repository=history.path, workspace_root=workspaces),
        runner=PytestRunner(
            contract=ExecutionContractLoader().load(history.path),
            python_executable=Path(sys.executable),
            install_dependencies=True,
        ),
    )
    candidate_model = ScriptedCandidateModel(candidate_source)
    assessor = ScriptedAssessor()
    workflow = EvidenceWorkflow(
        store=store,
        context_retriever=retriever,
        claim_agent=BehavioralClaimAgent(model=_NeverCalledClaimModel()),
        candidate_model=candidate_model,
        challenge=challenge,
        assessor=assessor,
        claim_investigator=FixedInvestigatorFactory(
            repository=history.path, model=investigator_model
        ),
    )
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id=f"phase-2-{uuid4().hex}",
            action="synchronize",
            repository="owner/repository",
            pr_number=42,
            base_sha=history.base_sha,
            head_sha=history.head_sha,
            head_updated_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
            title="Prefer the most-specific workspace",
            body="Resolve overlapping paths deterministically.",
            installation_id=12345,
        )
    ).run
    return workflow, store, run, candidate_model, assessor


class _NeverCalledClaimModel:
    """Proves the v1 claim agent is bypassed when an investigator is supplied."""

    async def invoke(self, request):  # pragma: no cover - must never run
        raise AssertionError("the v1 claim agent must not be used when wired to Phase 2")


@pytest.fixture
def wired(context_repository_history, writable_test_directory):
    return context_repository_history, writable_test_directory


# ---------------------------------------------------------------------------------
# The wiring itself
# ---------------------------------------------------------------------------------


def test_workflow_uses_the_investigator_and_reaches_the_existing_evidence_path(wired) -> None:
    """The full chain, end to end, with real BASE/HEAD execution."""
    history, directory = wired
    model = ScriptedInvestigatorModel(_conclude())
    workflow, _store, run, candidate_model, assessor = _build_workflow(
        history, directory, investigator_model=model
    )

    report = asyncio.run(workflow.execute(run_id=run.run_id))

    # 1. The investigator selected the claim (the v1 agent would have raised).
    assert model.calls == 1
    assert report.claim is not None
    assert report.claim.shared_interface == f"{_INTERFACE_PATH}::{_INTERFACE_NAME}"

    # 2. The existing candidate path ran on that claim.
    assert candidate_model.calls == 1
    assert report.candidate_attempts and report.candidate_attempts[0].source == _CANDIDATE

    # 3. Real BASE/HEAD execution produced real mechanical evidence.
    assert report.base_execution is not None and report.head_execution is not None
    assert report.base_execution.status == "ASSERTION_FAILED"
    assert report.head_execution.status == "PASSED"
    assert report.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING

    # 4. The unchanged semantic assessor produced the terminal outcome.
    assert assessor.calls == 1
    assert report.claim_outcome is ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO


def test_abstention_from_the_investigator_terminates_without_candidate_generation(
    wired,
) -> None:
    history, directory = wired
    model = ScriptedInvestigatorModel(
        json.dumps(
            {
                "action": "CONCLUDE",
                "reasoning": "no grounded shared observable",
                "disposition": "INSUFFICIENT_EVIDENCE",
            }
        )
    )
    workflow, _store, run, candidate_model, assessor = _build_workflow(
        history, directory, investigator_model=model
    )

    report = asyncio.run(workflow.execute(run_id=run.run_id))

    assert report.claim is None
    assert report.claim_outcome is ClaimOutcome.INSUFFICIENT_EVIDENCE
    assert candidate_model.calls == 0
    assert assessor.calls == 0


def test_the_v1_claim_agent_still_runs_when_no_investigator_is_supplied(wired) -> None:
    """Backward compatibility: the investigator is opt-in, not mandatory."""
    history, directory = wired
    store = SqliteVerificationRunStore(directory / "legacy.sqlite3")
    workflow = EvidenceWorkflow(
        store=store,
        context_retriever=DeterministicContextRetriever(source_repository=history.path),
        claim_agent=BehavioralClaimAgent(model=_NeverCalledClaimModel()),
        candidate_model=ScriptedCandidateModel(_CANDIDATE),
        challenge=BaseHeadChallenge(
            workspaces=GitWorkspaceManager(
                source_repository=history.path, workspace_root=directory
            ),
            runner=PytestRunner(
                contract=ExecutionContractLoader().load(history.path),
                python_executable=Path(sys.executable),
                install_dependencies=True,
            ),
        ),
        assessor=ScriptedAssessor(),
    )
    assert workflow.claim_investigator is None


# ---------------------------------------------------------------------------------
# Transcript persistence
# ---------------------------------------------------------------------------------


def test_investigation_transcript_survives_into_durable_evidence(wired) -> None:
    history, directory = wired
    model = ScriptedInvestigatorModel(
        _investigate(
            {
                "tool": "inspect_symbol",
                "revision": "HEAD",
                "qualified_name": _INTERFACE_NAME,
            }
        ),
        _conclude(),
    )
    workflow, store, run, _candidate, _assessor = _build_workflow(
        history, directory, investigator_model=model
    )

    report = asyncio.run(workflow.execute(run_id=run.run_id))

    transcript = report.investigation_transcript
    assert transcript is not None
    assert transcript.turns == 2
    assert len(transcript.tool_calls) == 1
    assert transcript.tool_calls[0].tool.value == "inspect_symbol"
    assert transcript.tool_calls[0].arguments["qualified_name"] == _INTERFACE_NAME
    assert len(transcript.starting_context_sha256) == 64
    assert all(len(item) == 64 for item in transcript.response_sha256)

    # It survives the durable round trip the dashboard and checks rebuild from.
    stored = store.get_evidence(run.run_id)
    assert stored is not None
    reloaded = EvidenceReport.model_validate_json(stored.document_json)
    assert reloaded.investigation_transcript == transcript


def test_persisted_transcript_contains_no_free_model_text(wired) -> None:
    """Only bounded audit facts: no reasoning, no chain-of-thought, no raw responses."""
    history, directory = wired
    model = ScriptedInvestigatorModel(
        _investigate(
            {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": _INTERFACE_NAME}
        ),
        _conclude(),
    )
    workflow, _store, run, _candidate, _assessor = _build_workflow(
        history, directory, investigator_model=model
    )

    report = asyncio.run(workflow.execute(run_id=run.run_id))
    encoded = report.investigation_transcript.model_dump_json()

    assert "checking the shared symbol" not in encoded
    assert "choose_workspace is the shared observable" not in encoded
    assert set(InvestigationTranscript.model_fields) == {
        "turns",
        "tool_calls",
        "starting_context_sha256",
        "response_sha256",
    }


def test_transcript_is_absent_when_the_investigator_is_not_used() -> None:
    """v1 stored evidence has no transcript field and must still validate."""
    assert EvidenceReport.model_fields["investigation_transcript"].default is None


def test_v1_stored_evidence_without_a_transcript_still_loads(wired) -> None:
    history, directory = wired
    model = ScriptedInvestigatorModel(_conclude())
    workflow, _store, run, _candidate, _assessor = _build_workflow(
        history, directory, investigator_model=model
    )
    report = asyncio.run(workflow.execute(run_id=run.run_id))

    document = json.loads(report.canonical_json)
    document.pop("investigation_transcript", None)

    legacy = EvidenceReport.model_validate_json(json.dumps(document))
    assert legacy.investigation_transcript is None
    assert legacy.claim_outcome is report.claim_outcome
