"""End-to-end claim-scoped evidence orchestration with durable replay boundaries."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchproof.challenge import BaseHeadChallenge
from patchproof.claim_agent import (
    BehavioralClaim,
    BehavioralClaimAgent,
    ClaimAgentResult,
    ClaimSelectionDisposition,
    ModelUsage,
    PullRequestNarrative,
)
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.execution_contract import ExecutionContractLoader
from patchproof.model_reliability import (
    BoundedRetryingEvidenceAssessor,
    BoundedRetryingModel,
)
from patchproof.models import (
    ChallengeResult,
    ClaimOutcome,
    DifferentialPattern,
    EnvironmentReadiness,
    EnvironmentReadinessStatus,
    ExecutionResult,
    MechanicalEvidenceStatus,
    RevisionRole,
    TestExecutionStatus,
)
from patchproof.storage import StoredEvidence, VerificationRunStore
from patchproof.structured_output import StrictGeminiOutputModel
from patchproof.test_generation import (
    BoundedCandidateTestGenerator,
    CandidateAttempt,
    CandidateAttemptStatus,
    CandidateFeedback,
    CandidateTestValidator,
    StructuredCandidateModel,
)
from patchproof.workflow import (
    PublicationState,
    RevisionState,
    RunLifecycle,
    RunPhase,
    RunTransition,
    TerminalReason,
    VerificationRun,
)

_EXCEPTION_LINE_PATTERN = re.compile(
    r"^(?:E\s+)?(?P<type>(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*"
    r"(?:Error|Exception|Warning|Exit|Interrupt)):\s*(?P<message>.*)$"
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
    r"\b\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN_PATTERN = re.compile(r"(?:AIza[A-Za-z0-9_-]{20,}|gh[opsu]_[A-Za-z0-9_]{20,})")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s'\"]+")
_UNIX_PATH_PATTERN = re.compile(r"(?<![\w.])/(?:[^/\s]+/)+[^:\s]+")
_OPAQUE_VALUE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")
_TRUNCATION_MARKER = "...[truncated]"


class AssertionRelation(StrEnum):
    """Semantic relation of the generated assertion to the selected claim."""

    RELATED = "RELATED"
    UNCERTAIN = "UNCERTAIN"


class SemanticEvidenceDecision(StrictGeminiOutputModel):
    """Bounded interpretation that may narrow, but never override, mechanical facts."""

    outcome: ClaimOutcome
    assertion_relation: AssertionRelation
    explanation: str = Field(min_length=1, max_length=1_000)
    confidence: float = Field(ge=0, le=1)


class SemanticAssessmentResult(BaseModel):
    """Validated semantic decision plus model provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: SemanticEvidenceDecision
    usage: ModelUsage
    raw_response_sha256: str = Field(pattern=r"[0-9a-f]{64}")


class SemanticEvidenceAssessor(Protocol):
    """One isolated semantic assessment boundary for a discriminating result."""

    async def assess(
        self,
        *,
        claim: BehavioralClaim,
        candidate_source: str,
        challenge: ChallengeResult,
    ) -> SemanticAssessmentResult: ...


class CandidateAttemptEvidence(BaseModel):
    """Durable, content-addressed provenance for one bounded candidate attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=1, le=3)
    origin: str
    status: str
    parent_candidate_id: str | None
    parent_artifact_sha256: str | None
    candidate_id: str | None
    target_path: str | None
    test_function: str | None
    source: str | None = Field(default=None, max_length=16_000)
    rationale: str | None
    artifact_sha256: str | None
    behavior_fingerprint: str | None = Field(default=None, pattern=r"[0-9a-f]{64}")
    signature_context_count: int = Field(ge=0, le=8)
    signature_context_truncated: bool
    signature_context_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    issues: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    feedback: CandidateFeedback | None
    usage: ModelUsage
    raw_response_sha256: str


class ExecutionEvidence(BaseModel):
    """Bounded replay facts for one immutable revision and candidate artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    revision_sha: str
    test_node_id: str
    expected_artifact_sha256: str
    artifact_sha256_before: str | None
    artifact_sha256_after: str | None
    status: str
    collected_count: int = Field(ge=0)
    exit_code: int | None
    duration_seconds: float = Field(ge=0)
    stdout: str = Field(max_length=12_000)
    stderr: str = Field(max_length=12_000)
    detail: str | None = Field(default=None, max_length=2_000)


class CandidateEvaluationEvidence(BaseModel):
    """Mechanical evidence retained even when a candidate self-rejects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_sequence: int = Field(ge=1, le=3)
    artifact_sha256: str
    base_execution: ExecutionEvidence
    head_execution: ExecutionEvidence
    mechanical_status: MechanicalEvidenceStatus
    differential_pattern: DifferentialPattern
    mechanical_reason: str = Field(min_length=1, max_length=2_000)


class EnvironmentReadinessEvidence(BaseModel):
    """Durable setup result established before candidate-generation calls."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnvironmentReadinessStatus
    reason: str = Field(min_length=1, max_length=2_000)

    @classmethod
    def from_domain(cls, result: EnvironmentReadiness) -> EnvironmentReadinessEvidence:
        return cls(status=result.status, reason=result.reason[:2_000])


class EvidenceReport(BaseModel):
    """Immutable result document from which every GitHub publication is rebuilt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    run_id: UUID
    repository: str
    pr_number: int = Field(gt=0)
    base_sha: str
    head_sha: str
    claim_disposition: ClaimSelectionDisposition
    claim: BehavioralClaim | None
    claim_usage: ModelUsage
    claim_response_sha256: str
    environment_readiness: EnvironmentReadinessEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    candidate_attempts: tuple[CandidateAttemptEvidence, ...] = Field(max_length=3)
    candidate_evaluations: tuple[CandidateEvaluationEvidence, ...] = Field(max_length=3)
    selected_artifact_sha256: str | None
    base_execution: ExecutionEvidence | None
    head_execution: ExecutionEvidence | None
    mechanical_status: MechanicalEvidenceStatus
    differential_pattern: DifferentialPattern
    mechanical_reason: str = Field(min_length=1, max_length=2_000)
    semantic_assessment: SemanticAssessmentResult | None
    claim_outcome: ClaimOutcome
    conclusion: str = Field(min_length=1, max_length=2_000)
    created_at: datetime

    @model_validator(mode="after")
    def validate_evidence_pair(self) -> EvidenceReport:
        if (self.base_execution is None) != (self.head_execution is None):
            raise ValueError("BASE and HEAD evidence must be present or absent together")
        if self.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING and (
            self.base_execution is None or self.semantic_assessment is None
        ):
            raise ValueError("discriminating evidence requires executions and interpretation")
        if self.claim is None and self.claim_outcome is not ClaimOutcome.INSUFFICIENT_EVIDENCE:
            raise ValueError("claim abstention can only conclude insufficient evidence")
        return self

    @property
    def canonical_json(self) -> str:
        return self.model_dump_json()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


class EvidenceWorkflow:
    """Run semantic and deterministic phases once, then persist a terminal result."""

    def __init__(
        self,
        *,
        store: VerificationRunStore,
        context_retriever: DeterministicContextRetriever,
        claim_agent: BehavioralClaimAgent,
        candidate_model: StructuredCandidateModel,
        challenge: BaseHeadChallenge,
        assessor: SemanticEvidenceAssessor,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.context_retriever = context_retriever
        self.claim_agent = BehavioralClaimAgent(
            model=BoundedRetryingModel(claim_agent.model),
            max_input_json_chars=claim_agent.max_input_json_chars,
            max_response_chars=claim_agent.max_response_chars,
        )
        self.candidate_model = BoundedRetryingModel(candidate_model)
        self.challenge = challenge
        self.assessor = BoundedRetryingEvidenceAssessor(assessor)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        run_id: UUID,
    ) -> EvidenceReport:
        """Compute one run, or replay its stored result without invoking model/pytest again."""
        prior = self.store.get_evidence(run_id)
        if prior is not None:
            report = EvidenceReport.model_validate_json(prior.document_json)
            durable_run = self.store.get_run(run_id)
            if (
                durable_run.revision_state is RevisionState.CURRENT
                and durable_run.lifecycle is not RunLifecycle.TERMINAL
            ):
                self._persist_terminal(durable_run, report)
            return report

        run = self.store.get_run(run_id)
        self._require_current(run)
        if run.lifecycle is RunLifecycle.TERMINAL:
            raise RuntimeError("terminal verification run cannot execute again")
        contract_loader = ExecutionContractLoader()
        base_contract = contract_loader.load_bytes(
            self.context_retriever.read_committed_file(
                revision_sha=run.base_sha, path=contract_loader.filename
            )
        )
        contract = contract_loader.load_bytes(
            self.context_retriever.read_committed_file(
                revision_sha=run.head_sha, path=contract_loader.filename
            )
        )
        if base_contract != contract:
            raise RuntimeError("BASE and HEAD execution contracts differ; comparison is unsafe")
        if self.challenge.runner.contract != contract:
            raise RuntimeError("executor contract does not match the immutable repository contract")
        if not self.challenge.runner.install_dependencies:
            raise RuntimeError("Phase 5 execution requires validated dependency installation")
        existing_paths = self.context_retriever.committed_paths(run.head_sha)
        if run.lifecycle is RunLifecycle.ACCEPTED:
            run = self._transition(run, RunTransition(lifecycle=RunLifecycle.QUEUED))
        if run.lifecycle is RunLifecycle.QUEUED:
            run = self._transition(run, RunTransition(lifecycle=RunLifecycle.RUNNING))

        context = self.context_retriever.retrieve(base_sha=run.base_sha, head_sha=run.head_sha)
        run = self._transition(run, RunTransition(phase=RunPhase.CLAIM))
        claim_result = await self.claim_agent.select_claim(
            context=context,
            narrative=PullRequestNarrative.from_untrusted(title=run.title, body=run.body),
        )
        selection = claim_result.selection
        if selection.claim is None:
            status = (
                MechanicalEvidenceStatus.COUNTERFACTUAL_NOT_APPLICABLE
                if selection.disposition is ClaimSelectionDisposition.COUNTERFACTUAL_NOT_APPLICABLE
                else MechanicalEvidenceStatus.NON_DISCRIMINATING
            )
            report = self._abstention_report(
                run=run,
                claim_result=claim_result,
                attempts=(),
                evaluations=(),
                mechanical_status=status,
                reason=selection.explanation,
            )
            self._persist_terminal(run, report)
            return report

        environment_readiness = self.challenge.prepare_environment(
            base_ref=run.base_sha,
            head_ref=run.head_sha,
        )
        readiness_evidence = EnvironmentReadinessEvidence.from_domain(environment_readiness)
        if not environment_readiness.ready:
            report = self._abstention_report(
                run=run,
                claim_result=claim_result,
                attempts=(),
                evaluations=(),
                mechanical_status=MechanicalEvidenceStatus.ENVIRONMENTAL,
                reason=environment_readiness.reason,
                environment_readiness=readiness_evidence,
            )
            self._persist_terminal(run, report)
            return report

        run = self._transition(run, RunTransition(phase=RunPhase.TEST_GENERATION))
        signature_context = self.context_retriever.retrieve_callable_signatures(
            head_sha=run.head_sha,
            context=context,
            affected_symbols=tuple(
                (symbol.path, symbol.qualified_name) for symbol in selection.claim.affected_symbols
            ),
        )
        generator = BoundedCandidateTestGenerator(
            model=self.candidate_model,
            validator=CandidateTestValidator(),
            claim=selection.claim,
            context=context,
            contract=contract,
            existing_paths=existing_paths,
            repository_signatures=signature_context,
        )
        attempts: list[CandidateAttempt] = []
        challenge: ChallengeResult | None = None
        evaluations: list[CandidateEvaluationEvidence] = []
        attempt = await generator.generate_initial()
        attempts.append(attempt)

        for attempt_number in range(3):
            if attempt.validated is not None:
                self._require_current(self.store.get_run(run_id))
                run = self._advance_for_execution(run)

                def mark_head_execution(_base_result: ExecutionResult) -> None:
                    nonlocal run
                    self._require_current(self.store.get_run(run_id))
                    if run.phase is RunPhase.BASE_EXECUTION:
                        run = self._transition(run, RunTransition(phase=RunPhase.HEAD_EXECUTION))

                challenge = self.challenge.run(
                    base_ref=run.base_sha,
                    head_ref=run.head_sha,
                    artifact=attempt.validated.artifact,
                    on_base_complete=mark_head_execution,
                )
                evaluations.append(self._evaluation_evidence(attempt.sequence, challenge))
                if run.phase is not RunPhase.ASSESSMENT:
                    run = self._transition(run, RunTransition(phase=RunPhase.ASSESSMENT))
                setup_failure = self._candidate_environment_failure(challenge)
                if setup_failure is not None:
                    readiness_evidence = EnvironmentReadinessEvidence.from_domain(setup_failure)
                    report = self._abstention_report(
                        run=run,
                        claim_result=claim_result,
                        attempts=tuple(attempts),
                        evaluations=tuple(evaluations),
                        mechanical_status=MechanicalEvidenceStatus.ENVIRONMENTAL,
                        reason=setup_failure.reason,
                        challenge=challenge,
                        environment_readiness=readiness_evidence,
                    )
                    self._persist_terminal(run, report)
                    return report
                if (
                    challenge.assessment.mechanical_status
                    is MechanicalEvidenceStatus.DISCRIMINATING
                ):
                    semantic_result = await self.assessor.assess(
                        claim=selection.claim,
                        candidate_source=attempt.validated.proposal.source,
                        challenge=challenge,
                    )
                    self._validate_semantic_decision(challenge, semantic_result.decision)
                    report = self._completed_report(
                        run=run,
                        claim_result=claim_result,
                        attempts=tuple(attempts),
                        evaluations=tuple(evaluations),
                        challenge=challenge,
                        semantic_result=semantic_result,
                        environment_readiness=readiness_evidence,
                    )
                    self._persist_terminal(run, report)
                    return report

            if attempt_number == 2:
                break
            feedback = self._repair_feedback(attempt=attempt, challenge=challenge)
            attempt = await generator.repair(feedback=feedback)
            attempts.append(attempt)
            challenge = None

        status = (
            challenge.assessment.mechanical_status
            if challenge is not None
            else MechanicalEvidenceStatus.INVALID_TEST
        )
        reason = (
            challenge.assessment.reason
            if challenge is not None
            else "All three bounded candidate attempts failed deterministic validation."
        )
        report = self._abstention_report(
            run=run,
            claim_result=claim_result,
            attempts=tuple(attempts),
            evaluations=tuple(evaluations),
            mechanical_status=status,
            reason=reason,
            challenge=challenge,
            environment_readiness=readiness_evidence,
        )
        self._persist_terminal(run, report)
        return report

    def _persist_terminal(self, run: VerificationRun, report: EvidenceReport) -> StoredEvidence:
        stored = self.store.save_evidence(
            run_id=run.run_id,
            document_json=report.canonical_json,
            sha256=report.sha256,
        )
        latest = self.store.get_run(run.run_id)
        self._transition(
            latest,
            RunTransition(
                lifecycle=RunLifecycle.TERMINAL,
                phase=RunPhase.PUBLICATION,
                terminal_reason=TerminalReason.COMPLETED,
                mechanical_evidence_status=report.mechanical_status,
                claim_outcome=report.claim_outcome,
                publication_state=PublicationState.PENDING,
            ),
        )
        return stored

    def _completed_report(
        self,
        *,
        run: VerificationRun,
        claim_result: ClaimAgentResult,
        attempts: tuple[CandidateAttempt, ...],
        evaluations: tuple[CandidateEvaluationEvidence, ...],
        challenge: ChallengeResult,
        semantic_result: SemanticAssessmentResult,
        environment_readiness: EnvironmentReadinessEvidence,
    ) -> EvidenceReport:
        claim = claim_result.selection.claim
        assert claim is not None
        decision = semantic_result.decision
        conclusion = self._conclusion(decision.outcome, claim)
        return EvidenceReport(
            run_id=run.run_id,
            repository=run.repository,
            pr_number=run.pr_number,
            base_sha=run.base_sha,
            head_sha=run.head_sha,
            claim_disposition=claim_result.selection.disposition,
            claim=claim,
            claim_usage=claim_result.usage,
            claim_response_sha256=claim_result.raw_response_sha256,
            environment_readiness=environment_readiness,
            candidate_attempts=tuple(self._attempt_evidence(item) for item in attempts),
            candidate_evaluations=evaluations,
            selected_artifact_sha256=challenge.artifact.sha256,
            base_execution=self._execution_evidence(challenge.base),
            head_execution=self._execution_evidence(challenge.head),
            mechanical_status=challenge.assessment.mechanical_status,
            differential_pattern=challenge.assessment.pattern,
            mechanical_reason=challenge.assessment.reason,
            semantic_assessment=semantic_result,
            claim_outcome=decision.outcome,
            conclusion=conclusion,
            created_at=self._now(),
        )

    def _abstention_report(
        self,
        *,
        run: VerificationRun,
        claim_result: ClaimAgentResult,
        attempts: tuple[CandidateAttempt, ...],
        evaluations: tuple[CandidateEvaluationEvidence, ...],
        mechanical_status: MechanicalEvidenceStatus,
        reason: str,
        challenge: ChallengeResult | None = None,
        environment_readiness: EnvironmentReadinessEvidence | None = None,
    ) -> EvidenceReport:
        return EvidenceReport(
            run_id=run.run_id,
            repository=run.repository,
            pr_number=run.pr_number,
            base_sha=run.base_sha,
            head_sha=run.head_sha,
            claim_disposition=claim_result.selection.disposition,
            claim=claim_result.selection.claim,
            claim_usage=claim_result.usage,
            claim_response_sha256=claim_result.raw_response_sha256,
            environment_readiness=environment_readiness,
            candidate_attempts=tuple(self._attempt_evidence(item) for item in attempts),
            candidate_evaluations=evaluations,
            selected_artifact_sha256=challenge.artifact.sha256 if challenge else None,
            base_execution=self._execution_evidence(challenge.base) if challenge else None,
            head_execution=self._execution_evidence(challenge.head) if challenge else None,
            mechanical_status=mechanical_status,
            differential_pattern=(
                challenge.assessment.pattern if challenge else DifferentialPattern.NOT_COMPARABLE
            ),
            mechanical_reason=reason[:2_000],
            semantic_assessment=None,
            claim_outcome=ClaimOutcome.INSUFFICIENT_EVIDENCE,
            conclusion=(
                "PatchProof found insufficient evidence for the selected claim and abstained."
                if claim_result.selection.claim
                else "PatchProof could not select a sufficiently grounded claim and abstained."
            ),
            created_at=self._now(),
        )

    @staticmethod
    def _candidate_environment_failure(
        challenge: ChallengeResult,
    ) -> EnvironmentReadiness | None:
        if challenge.base.status is TestExecutionStatus.ENVIRONMENT_SETUP_FAILED:
            detail = challenge.base.detail or "repository-declared setup failed"
            return EnvironmentReadiness(
                status=EnvironmentReadinessStatus.BASE_SETUP_FAILED,
                reason=f"BASE repository environment setup failed: {detail}"[:2_000],
            )
        if challenge.head.status is TestExecutionStatus.ENVIRONMENT_SETUP_FAILED:
            detail = challenge.head.detail or "repository-declared setup failed"
            return EnvironmentReadiness(
                status=EnvironmentReadinessStatus.HEAD_SETUP_FAILED,
                reason=f"HEAD repository environment setup failed: {detail}"[:2_000],
            )
        return None

    def _advance_for_execution(self, run: VerificationRun) -> VerificationRun:
        if run.phase is RunPhase.TEST_GENERATION:
            return self._transition(run, RunTransition(phase=RunPhase.BASE_EXECUTION))
        return run

    def _transition(self, run: VerificationRun, transition: RunTransition) -> VerificationRun:
        return self.store.transition_run(
            run_id=run.run_id, expected_version=run.version, transition=transition
        )

    @staticmethod
    def _require_current(run: VerificationRun) -> None:
        if run.revision_state is not RevisionState.CURRENT:
            raise RuntimeError("superseded verification run cannot execute or publish")

    @staticmethod
    def _repair_feedback(
        *, attempt: CandidateAttempt, challenge: ChallengeResult | None
    ) -> CandidateFeedback:
        if attempt.status is not CandidateAttemptStatus.VALIDATED:
            if attempt.issues:
                return CandidateFeedback.from_validation_issues(attempt.issues)
            return CandidateFeedback(
                category="VALIDATION",
                summary="The previous candidate did not produce a valid executable artifact.",
            )
        assert challenge is not None
        assert attempt.validated is not None
        base_exception = EvidenceWorkflow._execution_exception(challenge.base)
        head_exception = EvidenceWorkflow._execution_exception(challenge.head)
        observations = [
            EvidenceWorkflow._execution_observation(
                role=RevisionRole.BASE,
                result=challenge.base,
                attempt=attempt,
                exception=base_exception,
            ),
            EvidenceWorkflow._execution_observation(
                role=RevisionRole.HEAD,
                result=challenge.head,
                attempt=attempt,
                exception=head_exception,
            ),
            (
                f"mechanical_status={challenge.assessment.mechanical_status}; "
                f"pattern={challenge.assessment.pattern}"
            ),
        ]
        if (
            challenge.base.status is TestExecutionStatus.TEST_ERROR
            and challenge.head.status is TestExecutionStatus.TEST_ERROR
        ):
            same_exception = (
                base_exception is not None
                and head_exception is not None
                and base_exception == head_exception
            )
            observations.append(f"same_exception_on_base_and_head={str(same_exception).lower()}")
        elif {
            challenge.base.status,
            challenge.head.status,
        } == {TestExecutionStatus.TEST_ERROR, TestExecutionStatus.PASSED}:
            observations.append(
                "An exception escaping on one revision is mechanically TEST_ERROR. If it "
                "represents expected claim behavior and its exception import is grounded in "
                "the supplied context, convert it into an explicit deterministic pytest "
                "assertion/failure; do not catch unrelated exceptions."
            )
        return CandidateFeedback(
            category="EXECUTION_EVIDENCE",
            summary=(
                "The previous candidate did not produce discriminating BASE/HEAD evidence. "
                "Use only these bounded execution facts to repair the generated test."
            ),
            observations=tuple(observations),
        )

    @staticmethod
    def _execution_exception(result: ExecutionResult) -> tuple[str, str] | None:
        if result.status is not TestExecutionStatus.TEST_ERROR or not result.detail:
            return None
        for line in result.detail.splitlines():
            match = _EXCEPTION_LINE_PATTERN.match(line.strip())
            if match is not None:
                return (
                    EvidenceWorkflow._sanitize_feedback_text(match.group("type"), limit=120),
                    EvidenceWorkflow._sanitize_feedback_text(match.group("message"), limit=240),
                )
        return None

    @staticmethod
    def _execution_observation(
        *,
        role: RevisionRole,
        result: ExecutionResult,
        attempt: CandidateAttempt,
        exception: tuple[str, str] | None,
    ) -> str:
        parts = [f"{role} status={result.status}"]
        if exception is not None:
            exception_type, message = exception
            parts.append(f"exception_type={exception_type}")
            parts.append(f"message={message or '<empty>'}")
        generated_line = EvidenceWorkflow._generated_failing_line(result, attempt)
        if generated_line is not None:
            line_number, source = generated_line
            parts.append(f"generated_line={line_number}:{source}")
        return EvidenceWorkflow._sanitize_feedback_text("; ".join(parts), limit=500)

    @staticmethod
    def _generated_failing_line(
        result: ExecutionResult, attempt: CandidateAttempt
    ) -> tuple[int, str] | None:
        if not result.detail or attempt.proposal is None or attempt.validated is None:
            return None
        relative_path = attempt.validated.artifact.relative_path
        path_markers = {
            relative_path,
            relative_path.replace("/", "\\"),
            relative_path.rsplit("/", maxsplit=1)[-1],
        }
        source_lines = attempt.proposal.source.splitlines()
        for traceback_line in result.detail.splitlines():
            if not any(marker in traceback_line for marker in path_markers):
                continue
            matches = re.findall(r":(\d+)(?::|\s|$)", traceback_line)
            if not matches:
                matches = re.findall(r"\bline\s+(\d+)\b", traceback_line, flags=re.IGNORECASE)
            if not matches:
                continue
            line_number = int(matches[-1])
            if 1 <= line_number <= len(source_lines):
                source = EvidenceWorkflow._sanitize_feedback_text(
                    source_lines[line_number - 1].strip(), limit=160
                )
                return line_number, source or "<blank>"
        return None

    @staticmethod
    def _sanitize_feedback_text(value: str, *, limit: int) -> str:
        text = _CREDENTIAL_PATTERN.sub("credential=<redacted>", value)
        text = _BEARER_PATTERN.sub("Bearer <redacted>", text)
        text = _KNOWN_TOKEN_PATTERN.sub("<redacted-token>", text)
        text = _WINDOWS_PATH_PATTERN.sub("<path>", text)
        text = _UNIX_PATH_PATTERN.sub("<path>", text)
        text = _OPAQUE_VALUE_PATTERN.sub("<opaque>", text)
        text = _CONTROL_PATTERN.sub(" ", text)
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        keep = max(0, limit - len(_TRUNCATION_MARKER))
        return f"{text[:keep].rstrip()}{_TRUNCATION_MARKER}"

    @staticmethod
    def _validate_semantic_decision(
        challenge: ChallengeResult, decision: SemanticEvidenceDecision
    ) -> None:
        pattern = challenge.assessment.pattern
        allowed = {ClaimOutcome.INSUFFICIENT_EVIDENCE}
        if pattern is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED:
            allowed.add(ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO)
        elif pattern is DifferentialPattern.BASE_PASSED_HEAD_ASSERTION_FAILED:
            allowed.add(ClaimOutcome.POTENTIAL_REGRESSION)
        if decision.outcome not in allowed:
            raise ValueError("semantic assessment contradicts mechanical BASE/HEAD evidence")
        if (
            decision.outcome is not ClaimOutcome.INSUFFICIENT_EVIDENCE
            and decision.assertion_relation is not AssertionRelation.RELATED
        ):
            raise ValueError("claim conclusion requires a related assertion")

    @staticmethod
    def _conclusion(outcome: ClaimOutcome, claim: BehavioralClaim) -> str:
        if outcome is ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO:
            return (
                f"The generated scenario supports claim {claim.claim_id}: {claim.summary}. "
                "This is claim-scoped evidence, not verification of the pull request as a whole."
            )
        if outcome is ClaimOutcome.POTENTIAL_REGRESSION:
            return (
                f"The generated scenario indicates a potential regression for claim "
                f"{claim.claim_id}: {claim.summary}."
            )
        return f"PatchProof found insufficient evidence for claim {claim.claim_id}."

    @staticmethod
    def _attempt_evidence(attempt: CandidateAttempt) -> CandidateAttemptEvidence:
        proposal = attempt.proposal
        return CandidateAttemptEvidence(
            sequence=attempt.sequence,
            origin=attempt.origin,
            status=attempt.status,
            parent_candidate_id=attempt.parent_candidate_id,
            parent_artifact_sha256=attempt.parent_artifact_sha256,
            candidate_id=proposal.candidate_id if proposal else None,
            target_path=proposal.target_path if proposal else None,
            test_function=proposal.test_function if proposal else None,
            source=proposal.source if proposal else None,
            rationale=proposal.rationale if proposal else None,
            artifact_sha256=attempt.validated.artifact.sha256 if attempt.validated else None,
            behavior_fingerprint=attempt.behavior_fingerprint,
            signature_context_count=attempt.signature_context_count,
            signature_context_truncated=attempt.signature_context_truncated,
            signature_context_sha256=attempt.signature_context_sha256,
            issues=tuple(f"{issue.code}: {issue.message}" for issue in attempt.issues),
            feedback=attempt.feedback,
            usage=attempt.usage,
            raw_response_sha256=attempt.raw_response_sha256,
        )

    @staticmethod
    def _execution_evidence(result: ExecutionResult) -> ExecutionEvidence:
        return ExecutionEvidence(
            role=result.revision.role,
            revision_sha=result.revision.sha,
            test_node_id=result.test_node_id,
            expected_artifact_sha256=result.expected_artifact_sha256,
            artifact_sha256_before=result.artifact_sha256_before,
            artifact_sha256_after=result.artifact_sha256_after,
            status=result.status,
            collected_count=result.collected_count,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            stdout=result.stdout[:12_000],
            stderr=result.stderr[:12_000],
            detail=result.detail[:2_000] if result.detail else None,
        )

    @classmethod
    def _evaluation_evidence(
        cls, attempt_sequence: int, challenge: ChallengeResult
    ) -> CandidateEvaluationEvidence:
        return CandidateEvaluationEvidence(
            attempt_sequence=attempt_sequence,
            artifact_sha256=challenge.artifact.sha256,
            base_execution=cls._execution_evidence(challenge.base),
            head_execution=cls._execution_evidence(challenge.head),
            mechanical_status=challenge.assessment.mechanical_status,
            differential_pattern=challenge.assessment.pattern,
            mechanical_reason=challenge.assessment.reason,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("workflow clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)
