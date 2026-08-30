"""Sanitized public client for one durable PatchProof cloud run."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from patchproof.dashboard import DashboardRun


class CloudClientError(RuntimeError):
    """Raised when the configured cloud product cannot serve a valid response."""


class CloudAnalyzeReceipt(BaseModel):
    """Asynchronous cloud-run identity returned by the control service."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str
    pr_url: str
    dashboard_url: str
    result_url: str


class PatchProofCloudClient:
    """Submit and observe runs without access to private executor credentials."""

    TERMINAL_STATUSES = frozenset({"COMPLETE", "ABSTAINED", "FAILED"})

    def __init__(
        self,
        *,
        control_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.control_url = control_url.strip().rstrip("/")
        if not self.control_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise CloudClientError("PATCHPROOF_CONTROL_URL must be HTTPS or local development HTTP")
        self.client = client or httpx.Client(timeout=30.0)

    @property
    def dashboard_url(self) -> str:
        return f"{self.control_url}/dashboard"

    def submit(self, pr_url: str) -> CloudAnalyzeReceipt:
        try:
            response = self.client.post(
                f"{self.control_url}/api/analyze",
                json={"pr_url": pr_url},
                headers={"Accept": "application/json"},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise CloudClientError(
                "cloud analyze request could not reach the control service"
            ) from error
        self._require_success(response, operation="cloud analyze")
        try:
            return CloudAnalyzeReceipt.model_validate_json(response.content)
        except ValueError as error:
            raise CloudClientError("cloud analyze returned an invalid response") from error

    def get_run(self, run_id: UUID) -> DashboardRun:
        try:
            response = self.client.get(
                f"{self.control_url}/api/runs/{run_id}",
                headers={"Accept": "application/json"},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise CloudClientError(
                "cloud run status could not reach the control service"
            ) from error
        self._require_success(response, operation="cloud run status")
        try:
            return DashboardRun.model_validate_json(response.content)
        except ValueError as error:
            raise CloudClientError("cloud run status returned an invalid response") from error

    def wait_for_terminal(
        self,
        run_id: UUID,
        *,
        poll_interval_seconds: float = 3.0,
        timeout_seconds: float = 1_800.0,
        on_status: Callable[[str], None] | None = None,
    ) -> DashboardRun:
        if poll_interval_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("cloud polling intervals must be positive")
        deadline = time.monotonic() + timeout_seconds
        previous: str | None = None
        while True:
            run = self.get_run(run_id)
            if run.status != previous and on_status is not None:
                on_status(run.status)
            previous = run.status
            if run.status in self.TERMINAL_STATUSES:
                return run
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloudClientError(
                    f"cloud run {run_id} did not finish within the polling limit"
                )
            time.sleep(min(poll_interval_seconds, remaining))

    @staticmethod
    def _require_success(response: httpx.Response, *, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = ""
        try:
            document: Any = response.json()
            if isinstance(document, dict) and isinstance(document.get("detail"), str):
                detail = document["detail"].strip()
        except ValueError:
            pass
        suffix = f": {detail[:500]}" if detail else ""
        raise CloudClientError(f"{operation} failed with HTTP {response.status_code}{suffix}")


def print_cloud_run(run: DashboardRun) -> None:
    """Render the same sanitized evidence exposed by the Evidence Console."""
    print("\n" + "=" * 72)
    print("PatchProof")
    print("Mode: CLOUD")
    print("=" * 72)
    print(f"PR:    {run.repository} #{run.pr_number}")
    print(f"URL:   {run.pr_url}")
    print(f"Title: {run.title}")
    print(f"Run:   {run.run_id}")
    print(f"BASE:  {run.base_sha}")
    print(f"HEAD:  {run.head_sha}")
    print(f"Status: {run.status}")

    if run.claim is not None:
        print("\nGemini claim selection: SELECTED")
        print(run.claim.summary)
        print(f"Action:   {run.claim.action}")
        print(f"Expected: {run.claim.expected_behavior}")
    else:
        print(f"\nGemini claim selection: {run.claim_disposition or 'NOT YET AVAILABLE'}")

    for candidate in run.candidates:
        print("\n" + "-" * 72)
        print(f"Gemini candidate #{candidate.sequence} ({candidate.origin.lower()})")
        print("-" * 72)
        print(candidate.source or "<no valid source>")
        evaluation = next(
            (
                item
                for item in run.evaluations
                if item.attempt_sequence == candidate.sequence
            ),
            None,
        )
        if evaluation is not None:
            print(f"\nBASE:       {evaluation.base_status}")
            print(f"HEAD:       {evaluation.head_status}")
            print(f"Mechanical: {evaluation.mechanical_status}")
        else:
            print(f"\nValidation: {candidate.status}")
            for issue in candidate.issues:
                print(f"Issue: {issue}")

    if not run.candidates:
        print("\nCandidate generation: NOT RUN")
    if run.semantic_assessment is not None:
        print(f"\nSemantic: {run.semantic_assessment.assertion_relation}")
    if run.failure is not None:
        print(f"\nFailure: {run.failure.error_code}: {run.failure.summary}")

    print("\n" + "=" * 72)
    print("FINAL:")
    print(run.claim_outcome or run.terminal_reason or run.status)
    print(f"Mechanical: {run.mechanical_status or 'NOT AVAILABLE'}")
    print(f"Evidence SHA-256: {run.evidence_sha256 or 'NOT AVAILABLE'}")
    print("=" * 72)
