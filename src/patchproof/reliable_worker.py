"""Durable fail-closed worker boundary for one Phase 6 evidence run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from patchproof.claim_agent import InvalidClaimAgentOutput, ModelUsage
from patchproof.context_retrieval import ContextRetrievalError
from patchproof.evidence_workflow import EvidenceReport, EvidenceWorkflow
from patchproof.execution_contract import ExecutionContractError
from patchproof.git_workspace import GitWorkspaceError
from patchproof.model_reliability import ModelInvocationFailure
from patchproof.storage import VerificationRunStore
from patchproof.test_generation import CandidateBudgetExceeded
from patchproof.workflow import RevisionState, RunLifecycle


class WorkerFailureCode(StrEnum):
    """Stable sanitized failure categories safe for persistence and operator display."""

    MODEL_RETRY_EXHAUSTED = "MODEL_RETRY_EXHAUSTED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    REPOSITORY_CONTEXT_FAILED = "REPOSITORY_CONTEXT_FAILED"
    EXECUTION_CONTRACT_INVALID = "EXECUTION_CONTRACT_INVALID"
    WORKSPACE_FAILED = "WORKSPACE_FAILED"
    CANDIDATE_BUDGET_VIOLATION = "CANDIDATE_BUDGET_VIOLATION"
    INTERNAL_WORKER_FAILURE = "INTERNAL_WORKER_FAILURE"


@dataclass(frozen=True, slots=True)
class ClassifiedWorkerFailure:
    code: WorkerFailureCode
    summary: str
    retryable: bool
    model_usage: ModelUsage | None = None
    raw_response_sha256: str | None = None


class EvidenceWorkerError(RuntimeError):
    """Safe caller-facing worker failure without repository/provider exception text."""

    def __init__(self, failure: ClassifiedWorkerFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code}: {failure.summary}")


class ReliableEvidenceWorker:
    """Execute one workflow and atomically preserve sanitized terminal failure state."""

    def __init__(self, *, workflow: EvidenceWorkflow, store: VerificationRunStore) -> None:
        self.workflow = workflow
        self.store = store

    async def run(self, run_id: UUID) -> EvidenceReport:
        try:
            return await self.workflow.execute(run_id=run_id)
        except Exception as error:
            failure = self.classify(error)
            run = self.store.get_run(run_id)
            if (
                run.revision_state is RevisionState.CURRENT
                and run.lifecycle is not RunLifecycle.TERMINAL
            ):
                self.store.fail_run(
                    run_id=run_id,
                    error_code=failure.code,
                    summary=failure.summary,
                    retryable=failure.retryable,
                    model_usage=failure.model_usage,
                    raw_response_sha256=failure.raw_response_sha256,
                )
            raise EvidenceWorkerError(failure) from error

    @staticmethod
    def classify(error: Exception) -> ClassifiedWorkerFailure:
        if isinstance(error, ModelInvocationFailure):
            return ClassifiedWorkerFailure(
                code=WorkerFailureCode.MODEL_RETRY_EXHAUSTED,
                summary="The bounded semantic provider attempt budget was exhausted.",
                retryable=error.retryable,
            )
        if isinstance(error, InvalidClaimAgentOutput):
            return ClassifiedWorkerFailure(
                code=WorkerFailureCode.MODEL_OUTPUT_INVALID,
                summary="A semantic task returned invalid or ungrounded structured output.",
                retryable=False,
                model_usage=error.usage,
                raw_response_sha256=error.raw_response_sha256,
            )
        if isinstance(error, ContextRetrievalError):
            return ClassifiedWorkerFailure(
                code=WorkerFailureCode.REPOSITORY_CONTEXT_FAILED,
                summary="Immutable repository context could not be retrieved safely.",
                retryable=False,
            )
        if isinstance(error, ExecutionContractError):
            return ClassifiedWorkerFailure(
                code=WorkerFailureCode.EXECUTION_CONTRACT_INVALID,
                summary="The immutable repository execution contract is invalid.",
                retryable=False,
            )
        if isinstance(error, GitWorkspaceError):
            return ClassifiedWorkerFailure(
                code=WorkerFailureCode.WORKSPACE_FAILED,
                summary="Immutable revision workspaces could not be prepared or cleaned up.",
                retryable=True,
            )
        if isinstance(error, CandidateBudgetExceeded):
            return ClassifiedWorkerFailure(
                code=WorkerFailureCode.CANDIDATE_BUDGET_VIOLATION,
                summary="The bounded candidate-attempt policy was violated.",
                retryable=False,
            )
        return ClassifiedWorkerFailure(
            code=WorkerFailureCode.INTERNAL_WORKER_FAILURE,
            summary="The verification worker failed closed.",
            retryable=False,
        )
