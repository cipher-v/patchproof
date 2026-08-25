"""GitHub webhook authentication and payload-boundary models."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from patchproof.workflow import PullRequestEvent

_SIGNATURE_PATTERN = re.compile(r"sha256=([0-9a-f]{64})")
_DELIVERY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")

SUPPORTED_PULL_REQUEST_ACTIONS = frozenset(
    {"opened", "reopened", "synchronize", "ready_for_review"}
)


class GitHubRepositoryPayload(BaseModel):
    """Repository fields used by the control plane."""

    model_config = ConfigDict(extra="ignore")

    full_name: str = Field(min_length=3, max_length=256)
    private: bool


class GitHubRevisionPayload(BaseModel):
    """One Git reference from a pull-request payload."""

    model_config = ConfigDict(extra="ignore")

    sha: str


class GitHubPullRequestPayload(BaseModel):
    """Pull-request fields required for immutable run identity."""

    model_config = ConfigDict(extra="ignore")

    base: GitHubRevisionPayload
    head: GitHubRevisionPayload
    updated_at: datetime
    title: str = "Untitled pull request"
    body: str | None = None

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pull request updated_at must include a timezone")
        return value.astimezone(UTC)


class GitHubInstallationPayload(BaseModel):
    """GitHub App installation identity used for repository-scoped API tokens."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)


class GitHubPullRequestWebhook(BaseModel):
    """Minimal validated projection of GitHub's pull-request webhook schema."""

    model_config = ConfigDict(extra="ignore")

    action: str = Field(min_length=1, max_length=64)
    number: int = Field(gt=0)
    repository: GitHubRepositoryPayload
    pull_request: GitHubPullRequestPayload
    installation: GitHubInstallationPayload | None = None

    def to_event(self, delivery_id: str) -> PullRequestEvent:
        """Convert authenticated GitHub input to the storage boundary model."""
        return PullRequestEvent(
            delivery_id=delivery_id,
            action=self.action,
            repository=self.repository.full_name,
            pr_number=self.number,
            base_sha=self.pull_request.base.sha,
            head_sha=self.pull_request.head.sha,
            head_updated_at=self.pull_request.updated_at,
            title=(" ".join(self.pull_request.title.split()) or "Untitled pull request")[:300],
            body=(self.pull_request.body or "")[:8_000],
            installation_id=self.installation.id if self.installation else None,
        )


def verify_github_signature(*, secret: bytes, body: bytes, signature_header: str | None) -> bool:
    """Verify GitHub's HMAC-SHA256 header using constant-time comparison."""
    if not secret or signature_header is None:
        return False
    match = _SIGNATURE_PATTERN.fullmatch(signature_header.strip().lower())
    if match is None:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1))


def validate_delivery_id(value: str | None) -> str:
    """Return a bounded delivery identifier or raise a boundary validation error."""
    normalized = value.strip() if value else ""
    if _DELIVERY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("X-GitHub-Delivery is missing or malformed")
    return normalized
