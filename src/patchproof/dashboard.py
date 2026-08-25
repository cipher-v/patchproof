"""Read-only evidence dashboard projection and FastAPI routes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from typing import Protocol
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from patchproof.claim_agent import ModelUsage
from patchproof.evidence_workflow import EvidenceReport, ExecutionEvidence
from patchproof.storage import RunNotFoundError, VerificationRunStore


class DashboardUsage(BaseModel):
    """Small model-accounting projection suitable for public display."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str
    total_tokens: int | None
    duration_seconds: float
    provider_attempts: int


class DashboardClaim(BaseModel):
    """Selected claim without source snippets or untrusted PR prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    summary: str
    preconditions: tuple[str, ...]
    action: str
    expected_behavior: str
    confidence: float
    reasoning_summary: str


class DashboardCandidate(BaseModel):
    """One bounded candidate and its lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    origin: str
    status: str
    parent_candidate_id: str | None
    candidate_id: str | None
    target_path: str | None
    test_function: str | None
    source: str | None
    rationale: str | None
    artifact_sha256: str | None
    issues: tuple[str, ...]
    feedback_summary: str | None
    feedback_observations: tuple[str, ...]
    usage: DashboardUsage


class DashboardEvaluation(BaseModel):
    """Mechanical outcome retained for every executed candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_sequence: int
    mechanical_status: str
    differential_pattern: str
    mechanical_reason: str
    base_status: str
    head_status: str


class DashboardExecution(BaseModel):
    """Bounded immutable execution facts without process logs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    revision_sha: str
    status: str
    collected_count: int
    exit_code: int | None
    duration_seconds: float
    artifact_unchanged: bool


class DashboardSemanticAssessment(BaseModel):
    """Conservative semantic narrowing of mechanical evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: str
    assertion_relation: str
    explanation: str
    confidence: float
    usage: DashboardUsage


class DashboardFailure(BaseModel):
    """Sanitized terminal failure, when no evidence document exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str
    error_code: str
    summary: str
    retryable: bool


class DashboardRun(BaseModel):
    """Public, read-only projection of one explicitly featured durable run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    repository: str
    pr_number: int
    pr_url: str
    title: str
    event_action: str
    lifecycle: str
    phase: str
    terminal_reason: str | None
    publication_state: str
    revision_state: str
    base_sha: str
    head_sha: str
    created_at: str
    updated_at: str
    claim_disposition: str | None = None
    claim: DashboardClaim | None = None
    claim_usage: DashboardUsage | None = None
    candidates: tuple[DashboardCandidate, ...] = ()
    evaluations: tuple[DashboardEvaluation, ...] = ()
    selected_artifact_sha256: str | None = None
    base_execution: DashboardExecution | None = None
    head_execution: DashboardExecution | None = None
    mechanical_status: str | None = None
    differential_pattern: str | None = None
    mechanical_reason: str | None = None
    semantic_assessment: DashboardSemanticAssessment | None = None
    claim_outcome: str | None = None
    conclusion: str | None = None
    evidence_sha256: str | None = None
    evidence_hash_verified: bool = False
    check_run_id: int | None = None
    check_url: str | None = None
    publication_attempts: int = 0
    publication_retries: int = 0
    publication_last_error: str | None = None
    failure: DashboardFailure | None = None


class DashboardSnapshot(BaseModel):
    """Complete response consumed by the static dashboard client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    source: str = "live durable evidence"
    scope_notice: str = (
        "PatchProof reports claim-scoped evidence for tested scenarios, not whole-PR correctness."
    )
    runs: tuple[DashboardRun, ...] = Field(max_length=8)


class DashboardSnapshotProvider(Protocol):
    """Supply an already-sanitized dashboard snapshot."""

    def snapshot(self) -> DashboardSnapshot: ...


@dataclass(frozen=True, slots=True)
class StaticDashboardSnapshotProvider:
    """Deterministic preview/test provider with no cloud dependency."""

    value: DashboardSnapshot

    def snapshot(self) -> DashboardSnapshot:
        return self.value


@dataclass(frozen=True, slots=True)
class StoreDashboardSnapshotProvider:
    """Project only explicitly configured run UUIDs from the durable store."""

    store: VerificationRunStore
    run_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if len(self.run_ids) > 8:
            raise ValueError("dashboard can feature at most eight run IDs")
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("dashboard run IDs must be unique")

    def snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(runs=tuple(self._run(run_id) for run_id in self.run_ids))

    def _run(self, run_id: UUID) -> DashboardRun:
        run = self.store.get_run(run_id)
        stored = self.store.get_evidence(run_id)
        publication = self.store.get_publication(run_id)
        failure = self.store.get_failure(run_id)
        report: EvidenceReport | None = None
        evidence_hash_verified = False
        if stored is not None:
            computed = hashlib.sha256(stored.document_json.encode("utf-8")).hexdigest()
            if computed != stored.sha256:
                raise RuntimeError("featured evidence hash does not match stored JSON")
            report = EvidenceReport.model_validate_json(stored.document_json)
            if (
                report.run_id != run.run_id
                or report.repository != run.repository
                or report.pr_number != run.pr_number
                or report.base_sha != run.base_sha
                or report.head_sha != run.head_sha
            ):
                raise RuntimeError("featured evidence identity differs from durable run")
            evidence_hash_verified = True

        check_run_id = publication.check_run_id if publication else None
        return DashboardRun(
            run_id=run.run_id,
            repository=run.repository,
            pr_number=run.pr_number,
            pr_url=f"https://github.com/{run.repository}/pull/{run.pr_number}",
            title=run.title,
            event_action=run.event_action,
            lifecycle=run.lifecycle,
            phase=run.phase,
            terminal_reason=run.terminal_reason,
            publication_state=run.publication_state,
            revision_state=run.revision_state,
            base_sha=run.base_sha,
            head_sha=run.head_sha,
            created_at=run.created_at.isoformat(),
            updated_at=run.updated_at.isoformat(),
            claim_disposition=report.claim_disposition if report else None,
            claim=_claim(report) if report and report.claim else None,
            claim_usage=_usage(report.claim_usage) if report else None,
            candidates=tuple(_candidate(item) for item in report.candidate_attempts)
            if report
            else (),
            evaluations=tuple(_evaluation(item) for item in report.candidate_evaluations)
            if report
            else (),
            selected_artifact_sha256=report.selected_artifact_sha256 if report else None,
            base_execution=_execution(report.base_execution)
            if report and report.base_execution
            else None,
            head_execution=_execution(report.head_execution)
            if report and report.head_execution
            else None,
            mechanical_status=report.mechanical_status if report else None,
            differential_pattern=report.differential_pattern if report else None,
            mechanical_reason=report.mechanical_reason if report else None,
            semantic_assessment=_semantic(report)
            if report and report.semantic_assessment
            else None,
            claim_outcome=report.claim_outcome if report else None,
            conclusion=report.conclusion if report else None,
            evidence_sha256=stored.sha256 if stored else None,
            evidence_hash_verified=evidence_hash_verified,
            check_run_id=check_run_id,
            check_url=(
                f"https://github.com/{run.repository}/runs/{check_run_id}" if check_run_id else None
            ),
            publication_attempts=publication.attempt_count if publication else 0,
            publication_retries=max(0, publication.attempt_count - 1) if publication else 0,
            publication_last_error=publication.last_error if publication else None,
            failure=(
                DashboardFailure(
                    phase=failure.phase,
                    error_code=failure.error_code,
                    summary=failure.summary,
                    retryable=failure.retryable,
                )
                if failure
                else None
            ),
        )


def load_demo_snapshot() -> DashboardSnapshot:
    """Load the checked, sanitized live-proof fixture used for local visual QA."""
    resource = files("patchproof").joinpath("dashboard_assets", "demo.json")
    return DashboardSnapshot.model_validate_json(resource.read_text(encoding="utf-8"))


def install_dashboard(app: FastAPI, *, provider: DashboardSnapshotProvider) -> None:
    """Install the public read-only dashboard and its bounded JSON projection."""
    assets = files("patchproof").joinpath("dashboard_assets")
    app.state.dashboard_provider = provider

    @app.get("/", include_in_schema=False)
    async def dashboard_root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=307)

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page() -> HTMLResponse:
        content = assets.joinpath("index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            content,
            headers={
                "Cache-Control": "public, max-age=300",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    @app.get("/dashboard/api/runs", response_model=DashboardSnapshot)
    async def dashboard_runs() -> DashboardSnapshot:
        try:
            return provider.snapshot()
        except (RunNotFoundError, RuntimeError, ValueError) as error:
            raise HTTPException(
                status_code=503, detail="featured evidence is temporarily unavailable"
            ) from error

    app.mount(
        "/dashboard/assets",
        StaticFiles(directory=str(assets)),
        name="dashboard-assets",
    )


def _usage(value: ModelUsage) -> DashboardUsage:
    return DashboardUsage(
        model_name=value.model_name,
        total_tokens=value.total_tokens,
        duration_seconds=value.duration_seconds,
        provider_attempts=value.provider_attempts,
    )


def _claim(report: EvidenceReport) -> DashboardClaim:
    assert report.claim is not None
    return DashboardClaim(
        claim_id=report.claim.claim_id,
        summary=report.claim.summary,
        preconditions=report.claim.preconditions,
        action=report.claim.action,
        expected_behavior=report.claim.expected_behavior,
        confidence=report.claim.confidence,
        reasoning_summary=report.claim.reasoning_summary,
    )


def _candidate(value) -> DashboardCandidate:
    feedback = value.feedback
    return DashboardCandidate(
        sequence=value.sequence,
        origin=value.origin,
        status=value.status,
        parent_candidate_id=value.parent_candidate_id,
        candidate_id=value.candidate_id,
        target_path=value.target_path,
        test_function=value.test_function,
        source=value.source,
        rationale=value.rationale,
        artifact_sha256=value.artifact_sha256,
        issues=value.issues,
        feedback_summary=feedback.summary if feedback else None,
        feedback_observations=feedback.observations if feedback else (),
        usage=_usage(value.usage),
    )


def _evaluation(value) -> DashboardEvaluation:
    return DashboardEvaluation(
        attempt_sequence=value.attempt_sequence,
        mechanical_status=value.mechanical_status,
        differential_pattern=value.differential_pattern,
        mechanical_reason=value.mechanical_reason,
        base_status=value.base_execution.status,
        head_status=value.head_execution.status,
    )


def _execution(value: ExecutionEvidence) -> DashboardExecution:
    return DashboardExecution(
        role=value.role,
        revision_sha=value.revision_sha,
        status=value.status,
        collected_count=value.collected_count,
        exit_code=value.exit_code,
        duration_seconds=value.duration_seconds,
        artifact_unchanged=(
            value.expected_artifact_sha256 == value.artifact_sha256_before
            and value.expected_artifact_sha256 == value.artifact_sha256_after
        ),
    )


def _semantic(report: EvidenceReport) -> DashboardSemanticAssessment:
    assert report.semantic_assessment is not None
    value = report.semantic_assessment
    return DashboardSemanticAssessment(
        outcome=value.decision.outcome,
        assertion_relation=value.decision.assertion_relation,
        explanation=value.decision.explanation,
        confidence=value.decision.confidence,
        usage=_usage(value.usage),
    )
