"""Durable workflow-state models and deterministic transition rules."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchproof.models import ClaimOutcome, MechanicalEvidenceStatus

_GIT_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REPOSITORY_PATTERN = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+")


def normalize_repository_name(value: str) -> str:
    """Return a canonical GitHub owner/name or reject the value."""
    normalized = value.strip().lower()
    if _REPOSITORY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("repository must use the owner/name form")
    return normalized


class RunLifecycle(StrEnum):
    """Coarse scheduling/execution lifecycle, independent of workflow phase."""

    ACCEPTED = "ACCEPTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    TERMINAL = "TERMINAL"


class RunPhase(StrEnum):
    """Current business phase within a verification run."""

    CONTEXT = "CONTEXT"
    CLAIM = "CLAIM"
    TEST_GENERATION = "TEST_GENERATION"
    BASE_EXECUTION = "BASE_EXECUTION"
    HEAD_EXECUTION = "HEAD_EXECUTION"
    ASSESSMENT = "ASSESSMENT"
    PUBLICATION = "PUBLICATION"


class TerminalReason(StrEnum):
    """Why work ended, separate from an evidence or claim outcome."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class PublicationState(StrEnum):
    """Retryable publication state, independent of execution lifecycle."""

    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"


class RevisionState(StrEnum):
    """Whether this run still represents the pull request's newest known HEAD."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"


class PullRequestEvent(BaseModel):
    """Validated GitHub facts accepted by the persistence boundary."""

    model_config = ConfigDict(frozen=True)

    delivery_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    repository: str
    pr_number: int = Field(gt=0)
    base_sha: str
    head_sha: str
    head_updated_at: datetime

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        return normalize_repository_name(value)

    @field_validator("base_sha", "head_sha")
    @classmethod
    def validate_full_sha(cls, value: str) -> str:
        normalized = value.lower()
        if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
            raise ValueError("revision must be a full 40- or 64-character hexadecimal SHA")
        return normalized

    @field_validator("head_updated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("head update timestamp must include a timezone")
        return value.astimezone(UTC)


class VerificationRun(BaseModel):
    """A durable verification-run record with deliberately separate state dimensions."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    delivery_id: str
    event_action: str
    repository: str
    pr_number: int = Field(gt=0)
    base_sha: str
    head_sha: str
    head_updated_at: datetime
    lifecycle: RunLifecycle
    phase: RunPhase
    terminal_reason: TerminalReason | None = None
    mechanical_evidence_status: MechanicalEvidenceStatus | None = None
    claim_outcome: ClaimOutcome | None = None
    publication_state: PublicationState
    revision_state: RevisionState
    superseded_by_run_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if _REPOSITORY_PATTERN.fullmatch(value) is None or value != value.lower():
            raise ValueError("stored repository must be a canonical owner/name")
        return value

    @field_validator("base_sha", "head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        if _GIT_SHA_PATTERN.fullmatch(value) is None:
            raise ValueError("stored revision must be a full lowercase Git SHA")
        return value

    @field_validator("head_updated_at", "created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("workflow timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_dimensions(self) -> VerificationRun:
        if self.lifecycle is RunLifecycle.TERMINAL and self.terminal_reason is None:
            raise ValueError("terminal lifecycle requires a terminal reason")
        if self.lifecycle is not RunLifecycle.TERMINAL and self.terminal_reason is not None:
            raise ValueError("non-terminal lifecycle cannot have a terminal reason")
        if self.revision_state is RevisionState.CURRENT and self.superseded_by_run_id is not None:
            raise ValueError("current revision cannot reference a superseding run")
        if self.revision_state is RevisionState.SUPERSEDED and self.superseded_by_run_id is None:
            raise ValueError("superseded revision must reference its replacement run")
        if self.updated_at < self.created_at:
            raise ValueError("updated timestamp cannot precede creation")
        return self


class RunTransition(BaseModel):
    """Requested mutable dimensions for one optimistic state transition."""

    model_config = ConfigDict(frozen=True)

    lifecycle: RunLifecycle | None = None
    phase: RunPhase | None = None
    terminal_reason: TerminalReason | None = None
    mechanical_evidence_status: MechanicalEvidenceStatus | None = None
    claim_outcome: ClaimOutcome | None = None
    publication_state: PublicationState | None = None

    @model_validator(mode="after")
    def require_change(self) -> RunTransition:
        if all(
            value is None
            for value in (
                self.lifecycle,
                self.phase,
                self.terminal_reason,
                self.mechanical_evidence_status,
                self.claim_outcome,
                self.publication_state,
            )
        ):
            raise ValueError("transition must request at least one state change")
        return self


_LIFECYCLE_TRANSITIONS: dict[RunLifecycle, frozenset[RunLifecycle]] = {
    RunLifecycle.ACCEPTED: frozenset(
        {RunLifecycle.ACCEPTED, RunLifecycle.QUEUED, RunLifecycle.TERMINAL}
    ),
    RunLifecycle.QUEUED: frozenset(
        {RunLifecycle.QUEUED, RunLifecycle.RUNNING, RunLifecycle.TERMINAL}
    ),
    RunLifecycle.RUNNING: frozenset({RunLifecycle.RUNNING, RunLifecycle.TERMINAL}),
    RunLifecycle.TERMINAL: frozenset({RunLifecycle.TERMINAL}),
}
_PHASE_ORDER = {phase: index for index, phase in enumerate(RunPhase)}
_PUBLICATION_TRANSITIONS: dict[PublicationState, frozenset[PublicationState]] = {
    PublicationState.NOT_STARTED: frozenset(
        {PublicationState.NOT_STARTED, PublicationState.PENDING}
    ),
    PublicationState.PENDING: frozenset(
        {
            PublicationState.PENDING,
            PublicationState.PUBLISHED,
            PublicationState.RETRYABLE_FAILURE,
            PublicationState.TERMINAL_FAILURE,
        }
    ),
    PublicationState.RETRYABLE_FAILURE: frozenset(
        {
            PublicationState.RETRYABLE_FAILURE,
            PublicationState.PENDING,
            PublicationState.PUBLISHED,
            PublicationState.TERMINAL_FAILURE,
        }
    ),
    PublicationState.PUBLISHED: frozenset({PublicationState.PUBLISHED}),
    PublicationState.TERMINAL_FAILURE: frozenset({PublicationState.TERMINAL_FAILURE}),
}


class InvalidRunTransition(ValueError):
    """Raised when a workflow update violates deterministic state rules."""


def apply_run_transition(
    current: VerificationRun, transition: RunTransition, *, updated_at: datetime
) -> VerificationRun:
    """Validate and apply one transition without mutating the stored model."""
    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise InvalidRunTransition("transition timestamp must include a timezone")
    normalized_updated_at = updated_at.astimezone(UTC)
    if normalized_updated_at < current.updated_at:
        raise InvalidRunTransition("transition timestamp cannot move backward")
    if current.revision_state is RevisionState.SUPERSEDED:
        raise InvalidRunTransition("superseded runs cannot resume or publish")

    lifecycle = transition.lifecycle or current.lifecycle
    phase = transition.phase or current.phase
    publication_state = transition.publication_state or current.publication_state
    terminal_reason = transition.terminal_reason or current.terminal_reason

    if lifecycle not in _LIFECYCLE_TRANSITIONS[current.lifecycle]:
        raise InvalidRunTransition(f"lifecycle cannot move from {current.lifecycle} to {lifecycle}")
    if _PHASE_ORDER[phase] < _PHASE_ORDER[current.phase]:
        raise InvalidRunTransition(f"phase cannot move backward from {current.phase} to {phase}")
    if publication_state not in _PUBLICATION_TRANSITIONS[current.publication_state]:
        raise InvalidRunTransition(
            f"publication cannot move from {current.publication_state} to {publication_state}"
        )
    if lifecycle is RunLifecycle.TERMINAL and terminal_reason is None:
        raise InvalidRunTransition("terminal lifecycle requires a terminal reason")
    if lifecycle is not RunLifecycle.TERMINAL and terminal_reason is not None:
        raise InvalidRunTransition("terminal reason requires terminal lifecycle")
    if current.terminal_reason is not None and terminal_reason is not current.terminal_reason:
        raise InvalidRunTransition("terminal reason is immutable once recorded")

    mechanical_status = transition.mechanical_evidence_status or current.mechanical_evidence_status
    claim_outcome = transition.claim_outcome or current.claim_outcome
    if (
        current.mechanical_evidence_status is not None
        and mechanical_status is not current.mechanical_evidence_status
    ):
        raise InvalidRunTransition("mechanical evidence status cannot be overwritten")
    if current.claim_outcome is not None and claim_outcome is not current.claim_outcome:
        raise InvalidRunTransition("claim outcome cannot be overwritten")

    changes = {
        "lifecycle": lifecycle,
        "phase": phase,
        "terminal_reason": terminal_reason,
        "mechanical_evidence_status": mechanical_status,
        "claim_outcome": claim_outcome,
        "publication_state": publication_state,
    }
    if all(getattr(current, field) == value for field, value in changes.items()):
        raise InvalidRunTransition("transition does not change persisted state")

    return VerificationRun.model_validate(
        {
            **current.model_dump(),
            **changes,
            "updated_at": normalized_updated_at,
            "version": current.version + 1,
        }
    )
