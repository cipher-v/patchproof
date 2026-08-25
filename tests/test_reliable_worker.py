"""Tests for fail-closed durable evidence-worker behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from patchproof.claim_agent import InvalidClaimAgentOutput
from patchproof.reliable_worker import EvidenceWorkerError, ReliableEvidenceWorker
from patchproof.storage import SqliteVerificationRunStore
from patchproof.workflow import PullRequestEvent, RunLifecycle, TerminalReason


class FailingWorkflow:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def execute(self, *, run_id):
        del run_id
        self.calls += 1
        raise self.error


def test_worker_persists_sanitized_terminal_failure_without_provider_text(
    writable_test_directory,
) -> None:
    store = SqliteVerificationRunStore(writable_test_directory / "failures.db")
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id="failure-delivery",
            action="opened",
            repository="owner/repository",
            pr_number=91,
            base_sha="a" * 40,
            head_sha="b" * 40,
            head_updated_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        )
    ).run
    sensitive = "invalid response containing secret-provider-payload"
    workflow = FailingWorkflow(InvalidClaimAgentOutput(sensitive))
    worker = ReliableEvidenceWorker(workflow=workflow, store=store)

    with pytest.raises(EvidenceWorkerError) as captured:
        asyncio.run(worker.run(run.run_id))

    durable = store.get_run(run.run_id)
    failure = store.get_failure(run.run_id)
    assert durable.lifecycle is RunLifecycle.TERMINAL
    assert durable.terminal_reason is TerminalReason.FAILED
    assert failure.error_code == "MODEL_OUTPUT_INVALID"
    assert failure.retryable is False
    assert sensitive not in failure.summary
    assert sensitive not in str(captured.value)
    assert store.get_evidence(run.run_id) is None


def test_failure_write_is_idempotent_and_cannot_replace_the_first_record(
    writable_test_directory,
) -> None:
    store = SqliteVerificationRunStore(writable_test_directory / "idempotent-failure.db")
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id="failure-idempotency",
            action="opened",
            repository="owner/repository",
            pr_number=92,
            base_sha="a" * 40,
            head_sha="b" * 40,
            head_updated_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        )
    ).run

    first = store.fail_run(
        run_id=run.run_id,
        error_code="INTERNAL_WORKER_FAILURE",
        summary="The worker failed closed.",
        retryable=False,
    )
    repeated = store.fail_run(
        run_id=run.run_id,
        error_code="DIFFERENT_FAILURE",
        summary="This replacement must be ignored.",
        retryable=True,
    )

    assert repeated == first
    assert store.get_failure(run.run_id) == first
