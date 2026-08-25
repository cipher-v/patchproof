"""Conservative GitHub Check formatting and retry-safe Checks API publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import httpx
from google.auth import crypt, jwt
from pydantic import BaseModel, ConfigDict, Field

from patchproof.evidence_workflow import EvidenceReport
from patchproof.models import ClaimOutcome
from patchproof.storage import CheckPublication, VerificationRunStore
from patchproof.workflow import (
    PublicationState,
    RevisionState,
    RunLifecycle,
    RunTransition,
    TerminalReason,
)

CHECK_NAME = "PatchProof claim evidence"
GITHUB_API_VERSION = "2026-03-10"


class GitHubCheckOutput(BaseModel):
    """Bounded output object accepted by GitHub's Checks API."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=65_535)
    text: str = Field(min_length=1, max_length=65_535)


class GitHubCheckPayload(BaseModel):
    """One completed Check payload with a durable external idempotency key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = CHECK_NAME
    head_sha: str
    status: str = "completed"
    conclusion: str
    external_id: str
    output: GitHubCheckOutput

    @property
    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def format_github_check(report: EvidenceReport) -> GitHubCheckPayload:
    """Build claim-scoped Check prose exclusively from the stored evidence document."""
    if report.claim_outcome is ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO:
        conclusion = "success"
        title = "Generated scenario supports the selected claim"
    elif report.claim_outcome is ClaimOutcome.POTENTIAL_REGRESSION:
        conclusion = "failure"
        title = "Generated scenario indicates a potential regression"
    else:
        conclusion = "neutral"
        title = "Insufficient claim-scoped evidence"

    claim = (
        f"`{report.claim.claim_id}` — {report.claim.summary}"
        if report.claim
        else "No sufficiently grounded behavioral claim was selected."
    )
    selected = next(
        (
            attempt
            for attempt in reversed(report.candidate_attempts)
            if attempt.artifact_sha256 == report.selected_artifact_sha256
        ),
        None,
    )
    candidate = (
        f"`{selected.candidate_id}` at `{selected.target_path}::{selected.test_function}`\n\n"
        f"Artifact SHA-256: `{selected.artifact_sha256}`"
        if selected
        else "No candidate produced publishable evidence."
    )
    executions = "No comparable BASE/HEAD execution pair was retained."
    logs = "No execution logs were produced."
    if report.base_execution and report.head_execution:
        executions = (
            f"- BASE `{report.base_sha}`: **{report.base_execution.status}**\n"
            f"- HEAD `{report.head_sha}`: **{report.head_execution.status}**\n"
            f"- Mechanical result: **{report.mechanical_status}** / "
            f"**{report.differential_pattern}**\n"
            f"- Reason: {report.mechanical_reason}"
        )
        logs = (
            "#### BASE stdout\n```text\n"
            f"{_fenced(report.base_execution.stdout[:6_000])}\n```\n"
            "#### BASE stderr\n```text\n"
            f"{_fenced(report.base_execution.stderr[:6_000])}\n```\n"
            "#### HEAD stdout\n```text\n"
            f"{_fenced(report.head_execution.stdout[:6_000])}\n```\n"
            "#### HEAD stderr\n```text\n"
            f"{_fenced(report.head_execution.stderr[:6_000])}\n```"
        )
    source = f"```python\n{_fenced(selected.source or '')}\n```" if selected else "Not available."
    text = (
        "PatchProof reports evidence for one generated scenario. It does not approve, certify, "
        "or establish correctness of the pull request as a whole.\n\n"
        f"### Selected claim\n{claim}\n\n"
        f"### Candidate artifact\n{candidate}\n\n{source}\n\n"
        f"### Immutable comparison\n{executions}\n\n"
        f"### Conservative conclusion\n{report.conclusion}\n\n"
        f"### Captured logs and artifact provenance\n{logs}\n\n"
        f"Evidence document SHA-256: `{report.sha256}`"
    )
    return GitHubCheckPayload(
        head_sha=report.head_sha,
        conclusion=conclusion,
        external_id=str(report.run_id),
        output=GitHubCheckOutput(
            title=title,
            summary=report.conclusion,
            text=text[:65_535],
        ),
    )


def _fenced(value: str) -> str:
    return value.replace("```", "` ` `") or "(empty)"


class InstallationTokenProvider(Protocol):
    """Provide a short-lived token for one GitHub App installation."""

    def token_for(self, *, installation_id: int, repository: str) -> str: ...


@dataclass(frozen=True, slots=True)
class StaticInstallationTokenProvider:
    """Explicit local/test provider; production should mint installation access tokens."""

    token: str

    def token_for(self, *, installation_id: int, repository: str) -> str:
        del installation_id, repository
        if not self.token:
            raise GitHubCheckTerminalError("GitHub installation token is unavailable")
        return self.token


@dataclass(frozen=True, slots=True)
class _CachedInstallationToken:
    token: str
    expires_at: datetime


class GitHubAppInstallationTokenProvider:
    """Mint and cache short-lived repository tokens from GitHub App credentials."""

    def __init__(
        self,
        *,
        app_id: int,
        private_key_pem: str,
        client: httpx.Client | None = None,
        api_base_url: str = "https://api.github.com",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if app_id <= 0 or not private_key_pem.strip():
            raise ValueError("GitHub App ID and private key are required")
        try:
            self.signer = crypt.RSASigner.from_string(private_key_pem)
        except ValueError as error:
            raise ValueError("GitHub App private key is not valid PEM") from error
        self.app_id = app_id
        self.client = client or httpx.Client(timeout=20.0)
        self.api_base_url = api_base_url.rstrip("/")
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cache: dict[int, _CachedInstallationToken] = {}

    def token_for(self, *, installation_id: int, repository: str) -> str:
        del repository
        now = self._now()
        cached = self._cache.get(installation_id)
        if cached is not None and cached.expires_at - now > timedelta(minutes=2):
            return cached.token
        issued_at = int(now.timestamp()) - 60
        encoded = jwt.encode(
            self.signer,
            {"iat": issued_at, "exp": issued_at + 600, "iss": str(self.app_id)},
        )
        app_jwt = encoded.decode("ascii") if isinstance(encoded, bytes) else encoded
        try:
            response = self.client.post(
                f"{self.api_base_url}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise GitHubCheckRetryableError(
                "GitHub App token endpoint is temporarily unavailable"
            ) from error
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            raise GitHubCheckRetryableError(
                f"GitHub App token endpoint transient HTTP {response.status_code}"
            )
        if not 200 <= response.status_code < 300:
            raise GitHubCheckTerminalError(
                f"GitHub App token endpoint rejected credentials with HTTP {response.status_code}"
            )
        try:
            document = response.json()
            token = document["token"]
            expires_at = datetime.fromisoformat(document["expires_at"].replace("Z", "+00:00"))
            if (
                not isinstance(token, str)
                or not token
                or expires_at.tzinfo is None
                or expires_at.utcoffset() is None
            ):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GitHubCheckRetryableError(
                "GitHub App token endpoint returned an unusable response"
            ) from error
        cached = _CachedInstallationToken(token=token, expires_at=expires_at.astimezone(UTC))
        self._cache[installation_id] = cached
        return cached.token

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("GitHub App token clock must return an aware timestamp")
        return value.astimezone(UTC)


class GitHubCheckRetryableError(RuntimeError):
    """A transient publication failure for which the same stored payload may be retried."""


class GitHubCheckTerminalError(RuntimeError):
    """A permanent or invalid publication failure that should not be retried automatically."""


class GitHubChecksClient:
    """Small official REST boundary for finding, creating, and updating one Check run."""

    def __init__(
        self,
        *,
        tokens: InstallationTokenProvider,
        client: httpx.Client | None = None,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("GitHub API timeout must be positive")
        self.tokens = tokens
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self.api_base_url = api_base_url.rstrip("/")

    def upsert_completed(
        self,
        *,
        repository: str,
        installation_id: int | None,
        payload: GitHubCheckPayload,
        known_check_run_id: int | None,
    ) -> int:
        """Update a known Check, discover it by external_id, or create it exactly once."""
        if installation_id is None:
            raise GitHubCheckTerminalError("webhook did not include a GitHub App installation ID")
        token = self.tokens.token_for(installation_id=installation_id, repository=repository)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        check_run_id = known_check_run_id or self._find_existing(
            repository=repository,
            head_sha=payload.head_sha,
            external_id=payload.external_id,
            headers=headers,
        )
        if check_run_id is not None:
            response = self._request(
                "PATCH",
                f"/repos/{repository}/check-runs/{check_run_id}",
                headers=headers,
                json=payload.model_dump(exclude={"head_sha", "external_id"}),
            )
        else:
            response = self._request(
                "POST",
                f"/repos/{repository}/check-runs",
                headers=headers,
                json=payload.model_dump(),
            )
        try:
            remote_id = int(response.json()["id"])
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GitHubCheckRetryableError("GitHub returned an unusable Check response") from error
        if remote_id <= 0:
            raise GitHubCheckRetryableError("GitHub returned an invalid Check ID")
        return remote_id

    def _find_existing(
        self,
        *,
        repository: str,
        head_sha: str,
        external_id: str,
        headers: dict[str, str],
    ) -> int | None:
        response = self._request(
            "GET",
            f"/repos/{repository}/commits/{head_sha}/check-runs",
            headers=headers,
            params={"check_name": CHECK_NAME, "filter": "all", "per_page": "100"},
        )
        try:
            runs = response.json()["check_runs"]
            for item in runs:
                if item.get("external_id") == external_id:
                    return int(item["id"])
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GitHubCheckRetryableError("GitHub returned an unusable Check listing") from error
        return None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.client.request(method, f"{self.api_base_url}{path}", **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise GitHubCheckRetryableError(
                "GitHub Checks API is temporarily unavailable"
            ) from error
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            raise GitHubCheckRetryableError(
                f"GitHub Checks API transient HTTP {response.status_code}"
            )
        if not 200 <= response.status_code < 300:
            raise GitHubCheckTerminalError(
                f"GitHub Checks API rejected publication with HTTP {response.status_code}"
            )
        return response


class GitHubCheckPublisher:
    """Publish only stored evidence and persist retry/idempotency state separately."""

    def __init__(self, *, store: VerificationRunStore, client: GitHubChecksClient) -> None:
        self.store = store
        self.client = client

    def publish(self, run_id: UUID) -> CheckPublication:
        """Publish or retry a Check without access to Gemini, repositories, or pytest."""
        run = self.store.get_run(run_id)
        if run.revision_state is not RevisionState.CURRENT:
            raise RuntimeError("superseded verification run cannot publish")
        if (
            run.lifecycle is not RunLifecycle.TERMINAL
            or run.terminal_reason is not TerminalReason.COMPLETED
        ):
            raise RuntimeError("publication requires a completed verification run")
        evidence = self.store.get_evidence(run_id)
        if evidence is None:
            raise RuntimeError("publication requires a stored evidence document")
        report = EvidenceReport.model_validate_json(evidence.document_json)
        payload = format_github_check(report)
        if run.publication_state is PublicationState.PUBLISHED:
            publication = self.store.get_publication(run_id)
            if publication is None:
                raise RuntimeError("published run is missing GitHub Check metadata")
            if publication.payload_sha256 != payload.sha256:
                raise RuntimeError("published GitHub Check payload does not match stored evidence")
            return publication
        if run.publication_state is PublicationState.TERMINAL_FAILURE:
            raise RuntimeError("terminal GitHub publication failure cannot be retried")

        publication = self.store.begin_publication(run_id=run_id, payload_sha256=payload.sha256)
        run = self.store.get_run(run_id)
        if run.publication_state is PublicationState.RETRYABLE_FAILURE:
            run = self.store.transition_run(
                run_id=run_id,
                expected_version=run.version,
                transition=RunTransition(publication_state=PublicationState.PENDING),
            )
        try:
            check_run_id = self.client.upsert_completed(
                repository=run.repository,
                installation_id=run.installation_id,
                payload=payload,
                known_check_run_id=publication.check_run_id,
            )
            publication = self.store.set_check_run_id(run_id=run_id, check_run_id=check_run_id)
        except GitHubCheckRetryableError as error:
            self.store.record_publication_error(run_id=run_id, error=str(error))
            latest = self.store.get_run(run_id)
            self.store.transition_run(
                run_id=run_id,
                expected_version=latest.version,
                transition=RunTransition(publication_state=PublicationState.RETRYABLE_FAILURE),
            )
            raise
        except GitHubCheckTerminalError as error:
            self.store.record_publication_error(run_id=run_id, error=str(error))
            latest = self.store.get_run(run_id)
            self.store.transition_run(
                run_id=run_id,
                expected_version=latest.version,
                transition=RunTransition(publication_state=PublicationState.TERMINAL_FAILURE),
            )
            raise
        latest = self.store.get_run(run_id)
        self.store.transition_run(
            run_id=run_id,
            expected_version=latest.version,
            transition=RunTransition(publication_state=PublicationState.PUBLISHED),
        )
        return publication
