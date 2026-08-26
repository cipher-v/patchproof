"""Tests for deterministic candidate validation and the two-call repair budget."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque

import pytest
from conftest import ContextRepositoryHistory
from pydantic import ValidationError

from patchproof.claim_agent import (
    AffectedSymbolRef,
    BehavioralClaim,
    ClaimTestability,
    ModelUsage,
    SupportingContextRef,
)
from patchproof.context_retrieval import DeterministicContextRetriever, PullRequestContext
from patchproof.evidence_workflow import EvidenceWorkflow
from patchproof.execution_contract import ExecutionContract
from patchproof.test_generation import (
    BoundedCandidateTestGenerator,
    CandidateAttemptStatus,
    CandidateBudgetExceeded,
    CandidateFeedback,
    CandidateGenerationStateError,
    CandidateIssueCode,
    CandidateOrigin,
    CandidateTestDraft,
    CandidateTestProposal,
    CandidateTestValidator,
    CandidateValidationError,
    RawCandidateModelResponse,
)


class FakeCandidateModel:
    def __init__(self, *responses: str) -> None:
        self.responses = deque(responses)
        self.requests = []

    async def invoke(self, request) -> RawCandidateModelResponse:
        self.requests.append(request)
        return RawCandidateModelResponse(
            text=self.responses.popleft(),
            usage=ModelUsage(
                model_name="fake-gemini-3.6",
                model_version="fake-001",
                prompt_tokens=100,
                output_tokens=50,
                total_tokens=150,
                cached_tokens=0,
                duration_seconds=0.01,
            ),
        )


def _contract() -> ExecutionContract:
    return ExecutionContract.model_validate(
        {
            "version": 1,
            "python": "3.12",
            "install": [["uv", "sync", "--frozen"]],
            "test": {"command": ["python", "-m", "pytest"]},
            "allowed_test_paths": ["tests/patchproof_generated/"],
            "timeout_seconds": 120,
        }
    )


def _context(history: ContextRepositoryHistory) -> PullRequestContext:
    return DeterministicContextRetriever(source_repository=history.path).retrieve(
        base_sha=history.base_sha,
        head_sha=history.head_sha,
    )


def _claim(context: PullRequestContext) -> BehavioralClaim:
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
    return BehavioralClaim(
        claim_id="claim-most-specific-workspace",
        summary="Workspace resolution prefers the most-specific matching path.",
        preconditions=("At least two matching workspace paths are available.",),
        action="Resolve a workspace from the candidate paths.",
        expected_behavior="The deepest and longest matching path is returned.",
        affected_symbols=(
            AffectedSymbolRef(path=symbol.path, qualified_name=symbol.qualified_name),
        ),
        supporting_context=(
            SupportingContextRef(
                path=snippet.path,
                start_line=symbol.start_line,
                end_line=min(symbol.end_line, snippet.end_line),
                relevance="The changed expression ranks candidates by specificity.",
            ),
        ),
        confidence=0.91,
        testability=ClaimTestability.TESTABLE,
        reasoning_summary="The changed method has deterministic input and output behavior.",
    )


def _proposal(
    *,
    candidate_id: str = "candidate-most-specific-workspace",
    target_path: str = "tests/patchproof_generated/test_workspace_specificity.py",
    test_function: str = "test_most_specific_workspace_wins",
    source: str | None = None,
) -> CandidateTestProposal:
    return CandidateTestProposal(
        candidate_id=candidate_id,
        target_path=target_path,
        test_function=test_function,
        source=source
        or (
            "from workspace import WorkspaceResolver\n\n\n"
            "def test_most_specific_workspace_wins() -> None:\n"
            "    candidates = ['org', 'org/team', 'org/team/project']\n"
            "    assert WorkspaceResolver.choose_workspace(candidates) == 'org/team/project'\n"
        ),
        rationale="Exercises the observable selection behavior with competing path depths.",
    )


def _draft(
    *,
    source: str | None = None,
    rationale: str = "Exercises the observable selection behavior with competing path depths.",
) -> CandidateTestDraft:
    return CandidateTestDraft(
        source=source
        or (
            "from workspace import WorkspaceResolver\n\n\n"
            "def test_patchproof_generated_behavior() -> None:\n"
            "    candidates = ['org', 'org/team', 'org/team/project']\n"
            "    assert WorkspaceResolver.choose_workspace(candidates) == 'org/team/project'\n"
        ),
        rationale=rationale,
    )


def _generator(
    *,
    model: FakeCandidateModel,
    context: PullRequestContext,
    existing_paths: frozenset[str] = frozenset(),
    max_input_json_chars: int = 64_000,
    max_response_chars: int = 20_000,
) -> BoundedCandidateTestGenerator:
    return BoundedCandidateTestGenerator(
        model=model,
        validator=CandidateTestValidator(),
        claim=_claim(context),
        context=context,
        contract=_contract(),
        existing_paths=existing_paths,
        max_input_json_chars=max_input_json_chars,
        max_response_chars=max_response_chars,
    )


def test_valid_candidate_becomes_one_immutable_hashed_replay_artifact(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    context = _context(context_repository_history)
    draft = _draft()
    model = FakeCandidateModel(draft.model_dump_json())
    generator = _generator(model=model, context=context)

    attempt = asyncio.run(generator.generate_initial())

    assert attempt.status is CandidateAttemptStatus.VALIDATED
    assert attempt.origin is CandidateOrigin.INITIAL
    assert attempt.validated is not None
    artifact = attempt.validated.artifact
    assert artifact.content == draft.source.encode("utf-8")
    assert artifact.relative_path == (
        "tests/patchproof_generated/test_patchproof_generated_initial.py"
    )
    assert artifact.node_id.endswith("::test_patchproof_generated_behavior")
    assert attempt.proposal is not None
    assert attempt.proposal.candidate_id == "candidate-initial"
    assert len(artifact.sha256) == 64
    assert attempt.validated.imported_roots == ("workspace",)
    assert generator.snapshot.latest_validated is attempt.validated
    assert generator.snapshot.model_calls == 1
    assert len(model.requests) == 1
    assert "uv sync" not in model.requests[0].model_dump_json()


@pytest.mark.parametrize(
    ("proposal", "existing_paths", "expected_code"),
    [
        (
            _proposal(target_path="tests/test_outside_allowlist.py"),
            frozenset(),
            CandidateIssueCode.PATH_NOT_ALLOWED,
        ),
        (
            _proposal(),
            frozenset({"tests/patchproof_generated/test_workspace_specificity.py"}),
            CandidateIssueCode.PATH_ALREADY_EXISTS,
        ),
        (
            _proposal(source="def test_most_specific_workspace_wins(:\n    pass\n"),
            frozenset(),
            CandidateIssueCode.SYNTAX_ERROR,
        ),
        (
            _proposal(source="def test_different_name() -> None:\n    pass\n"),
            frozenset(),
            CandidateIssueCode.TEST_FUNCTION_MISMATCH,
        ),
        (
            _proposal(
                source=(
                    "def test_most_specific_workspace_wins() -> None:\n    pass\n\n"
                    "def test_second() -> None:\n    pass\n"
                )
            ),
            frozenset(),
            CandidateIssueCode.MULTIPLE_TESTS,
        ),
        (
            _proposal(
                source=(
                    "import mystery_package\n\n\n"
                    "def test_most_specific_workspace_wins() -> None:\n    pass\n"
                )
            ),
            frozenset(),
            CandidateIssueCode.UNGROUNDED_IMPORT,
        ),
        (
            _proposal(
                source=(
                    "import subprocess\n\n\n"
                    "def test_most_specific_workspace_wins() -> None:\n    pass\n"
                )
            ),
            frozenset(),
            CandidateIssueCode.BLOCKED_IMPORT,
        ),
        (
            _proposal(
                source=(
                    "import os\n\n\n"
                    "def test_most_specific_workspace_wins() -> None:\n"
                    "    os.system('echo unsafe')\n"
                )
            ),
            frozenset(),
            CandidateIssueCode.FORBIDDEN_CALL,
        ),
    ],
)
def test_validator_rejects_invalid_paths_source_imports_and_calls(
    context_repository_history: ContextRepositoryHistory,
    proposal: CandidateTestProposal,
    existing_paths: frozenset[str],
    expected_code: CandidateIssueCode,
) -> None:
    with pytest.raises(CandidateValidationError) as captured:
        CandidateTestValidator().validate(
            proposal=proposal,
            context=_context(context_repository_history),
            contract=_contract(),
            existing_paths=existing_paths,
        )

    assert captured.value.issues[0].code is expected_code


def test_invalid_structured_output_is_recorded_without_an_artifact(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    model = FakeCandidateModel('{"source":"not enough"}')
    generator = _generator(model=model, context=_context(context_repository_history))

    attempt = asyncio.run(generator.generate_initial())

    assert attempt.status is CandidateAttemptStatus.INVALID_MODEL_OUTPUT
    assert attempt.proposal is None
    assert attempt.validated is None
    assert attempt.issues[0].code is CandidateIssueCode.MALFORMED_OUTPUT
    assert len(attempt.raw_response_sha256) == 64
    assert attempt.malformed_output_diagnostic is not None
    assert attempt.malformed_output_diagnostic.expected_fields_missing == ("rationale",)
    assert attempt.malformed_output_diagnostic.raw_body_retained is False


def test_candidate_schema_rejects_a_model_generated_command_field() -> None:
    document = _proposal().model_dump()
    document["command"] = "uv run pytest"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CandidateTestProposal.model_validate(document)


def test_draft_rejects_control_plane_fields_and_records_only_sanitized_shape(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    source = _draft().source
    response = CandidateTestProposal(
        candidate_id="candidate-model-controlled",
        target_path="tests/patchproof_generated/test_model_controlled.py",
        test_function="test_patchproof_generated_behavior",
        source=source,
        rationale="Attempts to return redundant control-plane fields.",
    ).model_dump_json()
    attempt = asyncio.run(
        _generator(
            model=FakeCandidateModel(response),
            context=_context(context_repository_history),
        ).generate_initial()
    )

    assert attempt.status is CandidateAttemptStatus.INVALID_MODEL_OUTPUT
    diagnostic = attempt.malformed_output_diagnostic
    assert diagnostic is not None
    assert diagnostic.expected_fields_missing == ()
    assert diagnostic.unexpected_field_count == 3
    assert diagnostic.unexpected_fields == ("candidate_id", "target_path", "test_function")
    assert diagnostic.source_chars == len(source)
    assert diagnostic.source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert source not in diagnostic.model_dump_json()
    published = EvidenceWorkflow._attempt_evidence(attempt).model_dump(mode="json")
    assert "malformed_output_diagnostic" not in published
    assert "local_malformed_output_diagnostic" not in published


def test_malformed_json_diagnostic_records_location_without_raw_body(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    response = '{"source":\n'
    attempt = asyncio.run(
        _generator(
            model=FakeCandidateModel(response),
            context=_context(context_repository_history),
        ).generate_initial()
    )

    diagnostic = attempt.malformed_output_diagnostic
    assert diagnostic is not None
    assert diagnostic.json_parse_status == "INVALID"
    assert diagnostic.json_error_line == 2
    assert diagnostic.json_error_column == 1
    assert diagnostic.top_level_kind == "unknown"
    assert diagnostic.validation_errors == ("$:json_invalid",)
    assert response not in diagnostic.model_dump_json()


def test_candidate_schema_rejects_extra_dot_that_breaks_pytest_module_import() -> None:
    document = _proposal().model_dump()
    document["target_path"] = "tests/patchproof_generated/test_workspace_registry.com.py"

    with pytest.raises(ValidationError, match="normalized relative test"):
        CandidateTestProposal.model_validate(document)


def test_one_repair_records_parent_lineage_and_exhausts_two_call_budget(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    initial_draft = _draft()
    repaired_draft = _draft(
        rationale="Repairs the prior assertion while preserving the same observable behavior.",
    )
    model = FakeCandidateModel(
        initial_draft.model_dump_json(),
        repaired_draft.model_dump_json(),
    )
    generator = _generator(model=model, context=_context(context_repository_history))
    initial = asyncio.run(generator.generate_initial())
    feedback = CandidateFeedback(
        category="EXECUTION",
        summary="The assertion did not distinguish the revisions.",
        observations=("BASE and HEAD both passed.",),
    )

    repaired = asyncio.run(generator.repair(feedback=feedback))

    assert initial.validated is not None
    assert repaired.status is CandidateAttemptStatus.VALIDATED
    assert repaired.origin is CandidateOrigin.REPAIR
    assert repaired.parent_candidate_id == "candidate-initial"
    assert repaired.parent_artifact_sha256 == initial.validated.artifact.sha256
    assert generator.snapshot.model_calls == 2
    assert generator.snapshot.repair_used
    assert len(generator.snapshot.attempts) == 2
    assert model.requests[1].previous_candidate == initial.proposal
    assert repaired.proposal is not None
    assert repaired.proposal.candidate_id == "candidate-repair"
    assert repaired.proposal.target_path.endswith("test_patchproof_generated_repair.py")
    assert model.requests[1].feedback == feedback
    with pytest.raises(CandidateBudgetExceeded):
        asyncio.run(generator.repair(feedback=feedback))


def test_repair_before_initial_and_duplicate_initial_are_rejected_without_model_calls(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    context = _context(context_repository_history)
    model = FakeCandidateModel(_draft().model_dump_json())
    generator = _generator(model=model, context=context)
    feedback = CandidateFeedback(category="VALIDATION", summary="Try again.")

    with pytest.raises(CandidateGenerationStateError):
        asyncio.run(generator.repair(feedback=feedback))
    initial = asyncio.run(generator.generate_initial())
    assert initial.status is CandidateAttemptStatus.VALIDATED
    with pytest.raises(CandidateGenerationStateError):
        asyncio.run(generator.generate_initial())
    assert len(model.requests) == 1


def test_response_budget_rejects_candidate_after_exactly_one_model_call(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    draft_json = _draft().model_dump_json()
    model = FakeCandidateModel(draft_json)
    generator = _generator(
        model=model,
        context=_context(context_repository_history),
        max_response_chars=len(draft_json) - 1,
    )

    attempt = asyncio.run(generator.generate_initial())

    assert attempt.status is CandidateAttemptStatus.INVALID_MODEL_OUTPUT
    assert attempt.issues[0].code is CandidateIssueCode.RESPONSE_TOO_LARGE
    assert attempt.malformed_output_diagnostic is not None
    assert attempt.malformed_output_diagnostic.response_budget_exceeded
    assert attempt.malformed_output_diagnostic.json_parse_status == "NOT_ATTEMPTED"
    assert attempt.malformed_output_diagnostic.top_level_kind == "unknown"
    assert len(model.requests) == 1


def test_input_budget_fails_without_a_model_call(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    model = FakeCandidateModel(_draft().model_dump_json())
    generator = _generator(
        model=model,
        context=_context(context_repository_history),
        max_input_json_chars=10,
    )

    with pytest.raises(ValueError, match="input exceeds"):
        asyncio.run(generator.generate_initial())
    assert model.requests == []
