"""Phase 5 integration tests for bounded orchestration and publication-only retry."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import ContextRepositoryHistory

from patchproof.challenge import BaseHeadChallenge
from patchproof.claim_agent import (
    AffectedSymbolRef,
    BehavioralClaim,
    BehavioralClaimAgent,
    BehavioralClaimDraft,
    ClaimSelection,
    ClaimSelectionDisposition,
    ClaimSelectionDraft,
    ClaimTestability,
    ModelUsage,
    RawClaimModelResponse,
    SupportingContextRef,
)
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.evidence_workflow import (
    AssertionRelation,
    EvidenceWorkflow,
    SemanticAssessmentResult,
    SemanticEvidenceDecision,
)
from patchproof.execution_contract import ExecutionContractLoader
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.github_checks import (
    GitHubAppInstallationTokenProvider,
    GitHubCheckPublisher,
    GitHubCheckRetryableError,
    GitHubChecksClient,
    StaticInstallationTokenProvider,
    format_github_check,
)
from patchproof.models import (
    ClaimOutcome,
    DifferentialPattern,
    MechanicalEvidenceStatus,
    TestExecutionStatus,
)
from patchproof.pytest_runner import PytestRunner
from patchproof.storage import SqliteVerificationRunStore, StoredEvidenceConflictError
from patchproof.test_generation import (
    CandidateAttemptStatus,
    CandidateTestProposal,
    RawCandidateModelResponse,
)
from patchproof.workflow import PublicationState, PullRequestEvent, RunLifecycle


def _usage() -> ModelUsage:
    return ModelUsage(
        model_name="fake-gemini-3.6-flash",
        model_version="fake-v1",
        prompt_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cached_tokens=0,
        duration_seconds=0.01,
    )


class FakeClaimModel:
    def __init__(self, selection: ClaimSelection) -> None:
        self.selection = selection
        self.calls = 0

    async def invoke(self, request) -> RawClaimModelResponse:
        del request
        self.calls += 1
        claim = self.selection.claim
        draft = ClaimSelectionDraft(
            disposition=self.selection.disposition,
            claim=(
                BehavioralClaimDraft(
                    summary=claim.summary,
                    preconditions=claim.preconditions,
                    action=claim.action,
                    expected_behavior=claim.expected_behavior,
                    affected_symbols=claim.affected_symbols,
                    supporting_context=claim.supporting_context,
                    confidence=claim.confidence,
                )
                if claim is not None
                else None
            ),
            explanation=self.selection.explanation,
        )
        return RawClaimModelResponse(text=draft.model_dump_json(), usage=_usage())


class FakeCandidateModel:
    def __init__(self, *proposals: CandidateTestProposal) -> None:
        self.responses = deque(
            proposal.model_dump_json(include={"source", "rationale"}) for proposal in proposals
        )
        self.calls = 0

    async def invoke(self, request) -> RawCandidateModelResponse:
        del request
        self.calls += 1
        return RawCandidateModelResponse(text=self.responses.popleft(), usage=_usage())


class FakeAssessor:
    def __init__(self) -> None:
        self.calls = 0

    async def assess(self, *, claim, candidate_source, challenge) -> SemanticAssessmentResult:
        del claim, candidate_source, challenge
        self.calls += 1
        decision = SemanticEvidenceDecision(
            outcome=ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO,
            assertion_relation=AssertionRelation.RELATED,
            explanation="The assertion directly exercises candidate specificity.",
            confidence=0.94,
        )
        return SemanticAssessmentResult(
            decision=decision,
            usage=_usage(),
            raw_response_sha256="a" * 64,
        )


class CountingChallenge:
    def __init__(self, challenge: BaseHeadChallenge) -> None:
        self.inner = challenge
        self.runner = challenge.runner
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return self.inner.run(**kwargs)


def _claim_selection(retriever: DeterministicContextRetriever, history) -> ClaimSelection:
    context = retriever.retrieve(base_sha=history.base_sha, head_sha=history.head_sha)
    symbol = next(
        item
        for item in context.changed_symbols
        if item.qualified_name == "WorkspaceResolver.choose_workspace"
    )
    snippet = next(
        item
        for item in context.snippets
        if item.path == symbol.path and item.start_line <= symbol.start_line <= item.end_line
    )
    return ClaimSelection(
        disposition=ClaimSelectionDisposition.SELECTED,
        claim=BehavioralClaim(
            claim_id="claim-selected-behavior",
            summary="Workspace resolution prefers the most-specific candidate path.",
            preconditions=("Two candidate workspace paths are available.",),
            action="Resolve one workspace from the candidates.",
            expected_behavior="The deepest and longest path is returned.",
            affected_symbols=(
                AffectedSymbolRef(path=symbol.path, qualified_name=symbol.qualified_name),
            ),
            supporting_context=(
                SupportingContextRef(
                    path=snippet.path,
                    start_line=symbol.start_line,
                    end_line=min(symbol.end_line, snippet.end_line),
                    relevance="The changed implementation ranks candidate specificity.",
                ),
            ),
            confidence=0.91,
            testability=ClaimTestability.TESTABLE,
            reasoning_summary="One grounded and testable behavior is available.",
        ),
        explanation="One grounded and testable behavior is available.",
    )


def _proposal(candidate_id: str, candidates: list[str]) -> CandidateTestProposal:
    return CandidateTestProposal(
        candidate_id=candidate_id,
        target_path="tests/patchproof_generated/test_workspace_specificity.py",
        test_function="test_workspace_specificity",
        source=(
            "from workspace import WorkspaceResolver\n\n\n"
            "def test_patchproof_generated_behavior() -> None:\n"
            f"    candidates = {candidates!r}\n"
            "    assert WorkspaceResolver.choose_workspace(candidates) == "
            "'org/team/project'\n"
        ),
        rationale="Exercise observable choice ordering with overlapping paths.",
    )


def _execution_feedback(
    *,
    base_status: TestExecutionStatus,
    head_status: TestExecutionStatus,
    base_detail: str | None,
    head_detail: str | None,
    source: str = "def test_patchproof_generated_behavior():\n    call_api()\n",
):
    proposal = SimpleNamespace(source=source)
    attempt = SimpleNamespace(
        status=CandidateAttemptStatus.VALIDATED,
        issues=(),
        proposal=proposal,
        validated=SimpleNamespace(
            artifact=SimpleNamespace(
                relative_path="tests/patchproof_generated/test_patchproof_generated_initial.py"
            )
        ),
    )
    base = SimpleNamespace(
        status=base_status,
        detail=base_detail,
        stdout="WITHHELD_ORACLE_STDOUT",
        stderr="WITHHELD_ORACLE_STDERR",
    )
    head = SimpleNamespace(
        status=head_status,
        detail=head_detail,
        stdout="WITHHELD_ORACLE_STDOUT",
        stderr="WITHHELD_ORACLE_STDERR",
    )
    challenge = SimpleNamespace(
        base=base,
        head=head,
        assessment=SimpleNamespace(
            mechanical_status=MechanicalEvidenceStatus.ENVIRONMENTAL,
            pattern=DifferentialPattern.NOT_COMPARABLE,
        ),
    )
    return EvidenceWorkflow._repair_feedback(attempt=attempt, challenge=challenge)


def test_execution_error_feedback_contains_bounded_exception_and_generated_line() -> None:
    detail = (
        "TypeError: FunctionDef.__init__() missing required keyword-only arguments: "
        "'end_lineno' and 'end_col_offset'\n"
        "tests/patchproof_generated/test_patchproof_generated_initial.py:2: in test\n"
    )

    feedback = _execution_feedback(
        base_status=TestExecutionStatus.TEST_ERROR,
        head_status=TestExecutionStatus.TEST_ERROR,
        base_detail=detail,
        head_detail=detail,
    )

    joined = "\n".join(feedback.observations)
    assert "exception_type=TypeError" in joined
    assert "end_lineno" in joined
    assert "generated_line=2:call_api()" in joined
    assert "same_exception_on_base_and_head=true" in joined
    assert len(feedback.observations) == 4
    assert all(len(item) <= 500 for item in feedback.observations)


def test_execution_feedback_truncates_and_redacts_untrusted_values_without_logs() -> None:
    google_key = "AI" + "za" + "012345678901234567890123456789"
    detail = (
        "RuntimeError: token=super-secret-token-value "
        f"API_KEY={google_key} "
        "C:\\secure\\private\\file.py "
        + "repeated detail " * 80
        + "\n"
        + "tests/patchproof_generated/test_patchproof_generated_initial.py:2: in test\n"
    )
    source = (
        "def test_patchproof_generated_behavior():\n"
        "    secret = 'abcdefghijklmnopqrstuvwxyz0123456789'\n"
    )

    feedback = _execution_feedback(
        base_status=TestExecutionStatus.TEST_ERROR,
        head_status=TestExecutionStatus.TEST_ERROR,
        base_detail=detail,
        head_detail=detail,
        source=source,
    )

    joined = "\n".join(feedback.observations)
    assert "...[truncated]" in joined
    assert "super-secret-token-value" not in joined
    assert google_key not in joined
    assert "C:\\secure" not in joined
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in joined
    assert "credential=<redacted>" in joined
    assert "WITHHELD_ORACLE" not in joined
    assert all(len(item) <= 500 for item in feedback.observations)


def test_one_sided_domain_exception_feedback_preserves_conservative_classification() -> None:
    feedback = _execution_feedback(
        base_status=TestExecutionStatus.TEST_ERROR,
        head_status=TestExecutionStatus.PASSED,
        base_detail="package.errors.DomainValidationError: invalid nested value",
        head_detail=None,
    )

    joined = "\n".join(feedback.observations)
    assert "BASE status=TEST_ERROR" in joined
    assert "HEAD status=PASSED" in joined
    assert "DomainValidationError" in joined
    assert "explicit deterministic pytest assertion/failure" in joined
    assert "mechanical_status=ENVIRONMENTAL" in joined


def test_non_discriminating_candidate_repairs_then_persists_and_publication_retries_only(
    context_repository_history: ContextRepositoryHistory,
    writable_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_CACHE_DIR", str((Path.cwd() / ".uv-cache").resolve()))
    history = context_repository_history
    retriever = DeterministicContextRetriever(source_repository=history.path)
    claim_model = FakeClaimModel(_claim_selection(retriever, history))
    candidate_model = FakeCandidateModel(
        _proposal("candidate-non-discriminating", ["org/team/project", "org"]),
        _proposal("candidate-repaired-order", ["org", "org/team/project"]),
    )
    assessor = FakeAssessor()
    contract = ExecutionContractLoader().load(history.path)
    challenge = CountingChallenge(
        BaseHeadChallenge(
            workspaces=GitWorkspaceManager(
                source_repository=history.path,
                workspace_root=writable_test_directory / "workspaces",
            ),
            runner=PytestRunner(
                contract=contract,
                python_executable=Path(sys.executable),
                install_dependencies=True,
            ),
        )
    )
    store = SqliteVerificationRunStore(writable_test_directory / "workflow.db")
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id="phase-5-delivery",
            action="synchronize",
            repository="owner/repository",
            pr_number=17,
            base_sha=history.base_sha,
            head_sha=history.head_sha,
            head_updated_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
            title="Prefer the most-specific workspace",
            body="Resolve overlapping paths deterministically.",
            installation_id=12345,
        )
    ).run
    workflow = EvidenceWorkflow(
        store=store,
        context_retriever=retriever,
        claim_agent=BehavioralClaimAgent(model=claim_model),
        candidate_model=candidate_model,
        challenge=challenge,
        assessor=assessor,
    )

    report = asyncio.run(workflow.execute(run_id=run.run_id))

    assert claim_model.calls == 1
    assert candidate_model.calls == 2
    assert challenge.calls == 2
    assert assessor.calls == 1
    assert report.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING
    assert report.claim_outcome is ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO
    assert report.base_execution.status == "ASSERTION_FAILED"
    assert report.head_execution.status == "PASSED"
    assert report.candidate_attempts[1].parent_candidate_id == "candidate-initial"
    assert len(report.candidate_evaluations) == 2
    assert (
        report.candidate_evaluations[0].mechanical_status
        is MechanicalEvidenceStatus.NON_DISCRIMINATING
    )
    assert store.get_evidence(run.run_id).sha256 == report.sha256
    with pytest.raises(StoredEvidenceConflictError):
        replacement = '{"replacement":true}'
        store.save_evidence(
            run_id=run.run_id,
            document_json=replacement,
            sha256=hashlib.sha256(replacement.encode()).hexdigest(),
        )
    durable_run = store.get_run(run.run_id)
    assert durable_run.lifecycle is RunLifecycle.TERMINAL
    assert durable_run.publication_state is PublicationState.PENDING

    replay = asyncio.run(workflow.execute(run_id=run.run_id))
    assert replay == report
    assert (claim_model.calls, candidate_model.calls, challenge.calls, assessor.calls) == (
        1,
        2,
        2,
        1,
    )

    requests: list[httpx.Request] = []

    def github(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"check_runs": []})
        if len(requests) == 2:
            return httpx.Response(503, json={"message": "temporary"})
        if len(requests) == 3:
            return httpx.Response(
                200,
                json={"check_runs": [{"id": 77, "external_id": str(run.run_id)}]},
            )
        return httpx.Response(200, json={"id": 77})

    checks = GitHubChecksClient(
        tokens=StaticInstallationTokenProvider("test-installation-token"),
        client=httpx.Client(transport=httpx.MockTransport(github)),
        api_base_url="https://api.github.test",
    )
    publisher = GitHubCheckPublisher(store=store, client=checks)
    with pytest.raises(GitHubCheckRetryableError):
        publisher.publish(run.run_id)
    assert store.get_run(run.run_id).publication_state is PublicationState.RETRYABLE_FAILURE
    assert (claim_model.calls, candidate_model.calls, challenge.calls, assessor.calls) == (
        1,
        2,
        2,
        1,
    )

    publication = publisher.publish(run.run_id)
    assert publication.check_run_id == 77
    assert publication.attempt_count == 2
    assert store.get_run(run.run_id).publication_state is PublicationState.PUBLISHED
    assert [request.method for request in requests] == ["GET", "POST", "GET", "PATCH"]
    assert all(request.headers["x-github-api-version"] == "2026-03-10" for request in requests)
    assert all(
        request.headers["authorization"] == "Bearer test-installation-token" for request in requests
    )
    assert requests[1].url.path == "/repos/owner/repository/check-runs"
    assert requests[3].url.path == "/repos/owner/repository/check-runs/77"
    assert (claim_model.calls, candidate_model.calls, challenge.calls, assessor.calls) == (
        1,
        2,
        2,
        1,
    )

    no_op = publisher.publish(run.run_id)
    assert no_op.attempt_count == 2
    assert len(requests) == 4

    check = format_github_check(report)
    assert report.base_sha in check.output.text
    assert report.head_sha in check.output.text
    assert report.selected_artifact_sha256 in check.output.text
    assert "PR verified" not in check.output.text


def test_claim_abstention_is_terminal_without_candidate_model_or_pytest(
    context_repository_history: ContextRepositoryHistory,
    writable_test_directory: Path,
) -> None:
    history = context_repository_history
    retriever = DeterministicContextRetriever(source_repository=history.path)
    claim_model = FakeClaimModel(
        ClaimSelection(
            disposition=ClaimSelectionDisposition.INSUFFICIENT_EVIDENCE,
            claim=None,
            explanation="The bounded context does not support a sufficiently reliable claim.",
        )
    )
    candidate_model = FakeCandidateModel()
    assessor = FakeAssessor()
    contract = ExecutionContractLoader().load(history.path)
    challenge = CountingChallenge(
        BaseHeadChallenge(
            workspaces=GitWorkspaceManager(
                source_repository=history.path,
                workspace_root=writable_test_directory / "unused-workspaces",
            ),
            runner=PytestRunner(
                contract=contract,
                python_executable=Path(sys.executable),
                install_dependencies=True,
            ),
        )
    )
    store = SqliteVerificationRunStore(writable_test_directory / "abstention.db")
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id="phase-5-abstention",
            action="opened",
            repository="owner/repository",
            pr_number=18,
            base_sha=history.base_sha,
            head_sha=history.head_sha,
            head_updated_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        )
    ).run
    workflow = EvidenceWorkflow(
        store=store,
        context_retriever=retriever,
        claim_agent=BehavioralClaimAgent(model=claim_model),
        candidate_model=candidate_model,
        challenge=challenge,
        assessor=assessor,
    )

    report = asyncio.run(workflow.execute(run_id=run.run_id))

    assert report.claim_outcome is ClaimOutcome.INSUFFICIENT_EVIDENCE
    assert report.candidate_attempts == ()
    assert report.candidate_evaluations == ()
    assert report.base_execution is report.head_execution is None
    assert (claim_model.calls, candidate_model.calls, challenge.calls, assessor.calls) == (
        1,
        0,
        0,
        0,
    )
    assert store.get_run(run.run_id).publication_state is PublicationState.PENDING


def test_github_app_provider_mints_and_caches_short_lived_installation_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import patchproof.github_checks as checks_module

    monkeypatch.setattr(checks_module.crypt.RSASigner, "from_string", lambda _key: object())
    monkeypatch.setattr(
        checks_module.jwt,
        "encode",
        lambda _signer, _payload: b"header.payload.signature",
    )
    requests: list[httpx.Request] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "token": "ghs_short_lived_test_token",
                "expires_at": "2026-08-24T11:00:00Z",
            },
        )

    provider = GitHubAppInstallationTokenProvider(
        app_id=99,
        private_key_pem="test-private-key",
        client=httpx.Client(transport=httpx.MockTransport(token_endpoint)),
        api_base_url="https://api.github.test",
        clock=lambda: datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
    )

    first = provider.token_for(installation_id=4242, repository="owner/repository")
    second = provider.token_for(installation_id=4242, repository="owner/repository")

    assert first == second == "ghs_short_lived_test_token"
    assert len(requests) == 1
    assert requests[0].url.path == "/app/installations/4242/access_tokens"
    assert requests[0].headers["authorization"] == "Bearer header.payload.signature"
