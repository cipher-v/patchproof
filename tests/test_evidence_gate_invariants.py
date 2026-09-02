"""The evidence gate must survive every generalization change.

This file exists to be read by a skeptic. The changes on this branch widen what
PatchProof can *reach* -- more repositories, better feedback, better grounding. A
reviewer's first and correct question is whether any of that also widened what counts
as *support*.

Each test below pins one of the invariants declared in
`docs/19_GENERALIZATION_HARDENING_PLAN.md`. They are deliberately written against the
public mechanical surfaces rather than internals, so they keep their meaning if the
implementation is refactored again.
"""

from __future__ import annotations

import itertools

import pytest

from patchproof.evidence import MechanicalEvidenceClassifier
from patchproof.evidence_workflow import (
    AssertionRelation,
    EvidenceWorkflow,
    SemanticEvidenceDecision,
)
from patchproof.execution_contract import ExecutionContract
from patchproof.install_strategy import validate_probed_install_command
from patchproof.models import (
    ChallengeResult,
    ClaimOutcome,
    DifferentialPattern,
    EvidenceAssessment,
    ExecutionResult,
    MechanicalEvidenceStatus,
    Revision,
    RevisionRole,
    TestArtifact,
    TestExecutionStatus,
)

_ARTIFACT = TestArtifact.from_text(
    relative_path="tests/patchproof_generated/test_patchproof_generated_initial.py",
    node_id=(
        "tests/patchproof_generated/test_patchproof_generated_initial.py"
        "::test_patchproof_generated_behavior"
    ),
    content="def test_patchproof_generated_behavior():\n    assert True\n",
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
        exit_code=0,
        duration_seconds=0.1,
    )


def _classify(
    base_status: TestExecutionStatus, head_status: TestExecutionStatus
) -> EvidenceAssessment:
    return MechanicalEvidenceClassifier().classify(
        artifact=_ARTIFACT,
        base=_result(RevisionRole.BASE, base_status),
        head=_result(RevisionRole.HEAD, head_status),
    )


# --------------------------------------------------------------------------------------
# Invariant 1: exactly one status pair may ever support a claim.
# --------------------------------------------------------------------------------------


def test_only_base_assertion_failed_with_head_passed_can_support_a_claim() -> None:
    supporting = {
        (TestExecutionStatus.ASSERTION_FAILED, TestExecutionStatus.PASSED),
    }
    for base_status, head_status in itertools.product(TestExecutionStatus, repeat=2):
        assessment = _classify(base_status, head_status)
        is_support_pattern = (
            assessment.pattern is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
        )
        assert is_support_pattern == ((base_status, head_status) in supporting), (
            f"BASE={base_status} HEAD={head_status} produced {assessment.pattern}"
        )


def test_base_test_error_with_head_passed_is_never_support() -> None:
    """The single most important safety rule in the system."""
    assessment = _classify(TestExecutionStatus.TEST_ERROR, TestExecutionStatus.PASSED)

    assert assessment.pattern is DifferentialPattern.NOT_COMPARABLE
    assert assessment.mechanical_status is not MechanicalEvidenceStatus.DISCRIMINATING
    # Stage 2 renamed this case for honest accounting. The rename must not have promoted it.
    assert (
        assessment.mechanical_status is MechanicalEvidenceStatus.UNCAUGHT_EXCEPTION_ON_ONE_REVISION
    )


def test_the_new_status_cannot_reach_a_supporting_outcome() -> None:
    """UNCAUGHT_EXCEPTION_ON_ONE_REVISION is NOT_COMPARABLE, so the gate refuses it."""
    challenge = ChallengeResult(
        artifact=_ARTIFACT,
        base=_result(RevisionRole.BASE, TestExecutionStatus.TEST_ERROR),
        head=_result(RevisionRole.HEAD, TestExecutionStatus.PASSED),
        assessment=_classify(TestExecutionStatus.TEST_ERROR, TestExecutionStatus.PASSED),
    )
    decision = SemanticEvidenceDecision(
        outcome=ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO,
        assertion_relation=AssertionRelation.RELATED,
        explanation="the model attempts to call this support",
        confidence=1.0,
    )

    with pytest.raises(ValueError, match="contradicts mechanical"):
        EvidenceWorkflow._validate_semantic_decision(challenge, decision)


# --------------------------------------------------------------------------------------
# Invariant 2: the model cannot upgrade mechanical evidence.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_status", "head_status"),
    [
        (TestExecutionStatus.PASSED, TestExecutionStatus.PASSED),
        (TestExecutionStatus.ASSERTION_FAILED, TestExecutionStatus.ASSERTION_FAILED),
        (TestExecutionStatus.COLLECTION_ERROR, TestExecutionStatus.PASSED),
        (TestExecutionStatus.TIMED_OUT, TestExecutionStatus.PASSED),
        (TestExecutionStatus.TEST_ERROR, TestExecutionStatus.ASSERTION_FAILED),
    ],
)
def test_a_confident_model_cannot_manufacture_support(
    base_status: TestExecutionStatus, head_status: TestExecutionStatus
) -> None:
    challenge = ChallengeResult(
        artifact=_ARTIFACT,
        base=_result(RevisionRole.BASE, base_status),
        head=_result(RevisionRole.HEAD, head_status),
        assessment=_classify(base_status, head_status),
    )
    for outcome in (ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO, ClaimOutcome.POTENTIAL_REGRESSION):
        decision = SemanticEvidenceDecision(
            outcome=outcome,
            assertion_relation=AssertionRelation.RELATED,
            explanation="asserting an outcome the mechanical layer did not observe",
            confidence=1.0,
        )
        with pytest.raises(ValueError):
            EvidenceWorkflow._validate_semantic_decision(challenge, decision)


def test_an_uncertain_assertion_cannot_support_even_on_a_discriminating_pair() -> None:
    challenge = ChallengeResult(
        artifact=_ARTIFACT,
        base=_result(RevisionRole.BASE, TestExecutionStatus.ASSERTION_FAILED),
        head=_result(RevisionRole.HEAD, TestExecutionStatus.PASSED),
        assessment=_classify(TestExecutionStatus.ASSERTION_FAILED, TestExecutionStatus.PASSED),
    )
    decision = SemanticEvidenceDecision(
        outcome=ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO,
        assertion_relation=AssertionRelation.UNCERTAIN,
        explanation="the assertion may not relate to the claim",
        confidence=0.9,
    )

    with pytest.raises(ValueError, match="requires a related assertion"):
        EvidenceWorkflow._validate_semantic_decision(challenge, decision)


# --------------------------------------------------------------------------------------
# Invariant 3: no model-authored execution commands, on any path.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ("uv", "pip", "install", "attacker-controlled-package"),
        ("uv", "pip", "install", "-r", "https://example.invalid/req.txt"),
        ("uv", "pip", "install", "-e", ".[test] && curl example.invalid"),
        ("python", "-c", "import os; os.system('id')"),
        ("bash", "-lc", "echo pwned"),
        ("uv", "run", "python", "-c", "print(1)"),
    ],
)
def test_probed_install_allowlist_rejects_anything_not_templated(
    command: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        validate_probed_install_command(command)


def test_a_repository_declared_contract_still_uses_the_narrow_literal_allowlist() -> None:
    """Widening applies only to deterministically probed plans, not to hand-written YAML."""
    with pytest.raises(ValueError):
        ExecutionContract(
            version=1,
            python="3.12",
            install=(("uv", "venv"),),
            test={"command": ["python", "-m", "pytest"]},
            allowed_test_paths=("tests/generated/",),
            timeout_seconds=60,
        )


def test_test_command_allowlist_is_unchanged() -> None:
    for command in (("pytest",), ("python", "-c", "1"), ("uv", "run", "python")):
        with pytest.raises(ValueError):
            ExecutionContract(
                version=1,
                python="3.12",
                install=(("uv", "sync", "--frozen"),),
                test={"command": list(command)},
                allowed_test_paths=("tests/generated/",),
                timeout_seconds=60,
            )


# --------------------------------------------------------------------------------------
# Invariant 4: attempts stay bounded.
# --------------------------------------------------------------------------------------


def test_candidate_budget_is_still_one_initial_and_two_repairs() -> None:
    from patchproof.test_generation import _MAX_CANDIDATE_MODEL_CALLS, _MAX_REPAIRS

    assert _MAX_CANDIDATE_MODEL_CALLS == 3
    assert _MAX_REPAIRS == 2
