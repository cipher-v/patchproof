"""Unit tests for independent workflow dimensions and transition rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from patchproof.models import ClaimOutcome, MechanicalEvidenceStatus
from patchproof.workflow import (
    InvalidRunTransition,
    PublicationState,
    RevisionState,
    RunLifecycle,
    RunPhase,
    RunTransition,
    TerminalReason,
    VerificationRun,
    apply_run_transition,
)

_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _run(**updates) -> VerificationRun:
    values = {
        "run_id": UUID("11111111-1111-4111-8111-111111111111"),
        "delivery_id": "delivery-1",
        "event_action": "opened",
        "repository": "owner/repository",
        "pr_number": 7,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "head_updated_at": _NOW,
        "lifecycle": RunLifecycle.ACCEPTED,
        "phase": RunPhase.CONTEXT,
        "terminal_reason": None,
        "mechanical_evidence_status": None,
        "claim_outcome": None,
        "publication_state": PublicationState.NOT_STARTED,
        "revision_state": RevisionState.CURRENT,
        "superseded_by_run_id": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "version": 1,
    }
    values.update(updates)
    return VerificationRun.model_validate(values)


def test_run_model_requires_terminal_reason_only_for_terminal_lifecycle() -> None:
    with pytest.raises(ValidationError, match="terminal lifecycle requires"):
        _run(lifecycle=RunLifecycle.TERMINAL)

    with pytest.raises(ValidationError, match="non-terminal lifecycle"):
        _run(terminal_reason=TerminalReason.FAILED)


def test_lifecycle_phase_and_evidence_advance_without_collapsing_dimensions() -> None:
    queued = apply_run_transition(
        _run(),
        RunTransition(lifecycle=RunLifecycle.QUEUED),
        updated_at=_NOW + timedelta(seconds=1),
    )
    running = apply_run_transition(
        queued,
        RunTransition(lifecycle=RunLifecycle.RUNNING, phase=RunPhase.BASE_EXECUTION),
        updated_at=_NOW + timedelta(seconds=2),
    )
    terminal = apply_run_transition(
        running,
        RunTransition(
            lifecycle=RunLifecycle.TERMINAL,
            phase=RunPhase.ASSESSMENT,
            terminal_reason=TerminalReason.COMPLETED,
            mechanical_evidence_status=MechanicalEvidenceStatus.NON_DISCRIMINATING,
            claim_outcome=ClaimOutcome.INSUFFICIENT_EVIDENCE,
        ),
        updated_at=_NOW + timedelta(seconds=3),
    )

    assert terminal.lifecycle is RunLifecycle.TERMINAL
    assert terminal.phase is RunPhase.ASSESSMENT
    assert terminal.terminal_reason is TerminalReason.COMPLETED
    assert terminal.mechanical_evidence_status is MechanicalEvidenceStatus.NON_DISCRIMINATING
    assert terminal.claim_outcome is ClaimOutcome.INSUFFICIENT_EVIDENCE
    assert terminal.publication_state is PublicationState.NOT_STARTED
    assert terminal.version == 4


def test_publication_can_retry_after_execution_is_terminal() -> None:
    terminal = apply_run_transition(
        _run(),
        RunTransition(
            lifecycle=RunLifecycle.TERMINAL,
            phase=RunPhase.PUBLICATION,
            terminal_reason=TerminalReason.COMPLETED,
            publication_state=PublicationState.PENDING,
        ),
        updated_at=_NOW + timedelta(seconds=1),
    )

    failed = apply_run_transition(
        terminal,
        RunTransition(publication_state=PublicationState.RETRYABLE_FAILURE),
        updated_at=_NOW + timedelta(seconds=2),
    )
    retried = apply_run_transition(
        failed,
        RunTransition(publication_state=PublicationState.PENDING),
        updated_at=_NOW + timedelta(seconds=3),
    )

    assert failed.lifecycle is retried.lifecycle is RunLifecycle.TERMINAL
    assert retried.publication_state is PublicationState.PENDING


def test_invalid_lifecycle_phase_and_outcome_rewrites_are_rejected() -> None:
    with pytest.raises(InvalidRunTransition, match="lifecycle cannot move"):
        apply_run_transition(
            _run(),
            RunTransition(lifecycle=RunLifecycle.RUNNING),
            updated_at=_NOW,
        )

    advanced = _run(phase=RunPhase.HEAD_EXECUTION)
    with pytest.raises(InvalidRunTransition, match="phase cannot move backward"):
        apply_run_transition(
            advanced,
            RunTransition(phase=RunPhase.CLAIM),
            updated_at=_NOW,
        )

    with_evidence = _run(mechanical_evidence_status=MechanicalEvidenceStatus.NON_DISCRIMINATING)
    with pytest.raises(InvalidRunTransition, match="cannot be overwritten"):
        apply_run_transition(
            with_evidence,
            RunTransition(mechanical_evidence_status=MechanicalEvidenceStatus.DISCRIMINATING),
            updated_at=_NOW,
        )


def test_superseded_run_cannot_resume_or_publish() -> None:
    superseded = _run(
        lifecycle=RunLifecycle.TERMINAL,
        terminal_reason=TerminalReason.SUPERSEDED,
        revision_state=RevisionState.SUPERSEDED,
        superseded_by_run_id=UUID("22222222-2222-4222-8222-222222222222"),
    )

    with pytest.raises(InvalidRunTransition, match="superseded"):
        apply_run_transition(
            superseded,
            RunTransition(publication_state=PublicationState.PENDING),
            updated_at=_NOW,
        )


def test_transition_timestamp_cannot_move_backward() -> None:
    current = _run(updated_at=_NOW + timedelta(seconds=1))

    with pytest.raises(InvalidRunTransition, match="timestamp cannot move backward"):
        apply_run_transition(
            current,
            RunTransition(lifecycle=RunLifecycle.QUEUED),
            updated_at=_NOW,
        )
