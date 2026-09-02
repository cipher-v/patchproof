"""Bounded resolution of immutable metadata for public GitHub pull requests."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchproof.workflow import normalize_repository_name

_PR_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)/?$"
)
_GIT_SHA = re.compile(r"[0-9a-f]{40}")


class PullRequestUrlError(ValueError):
    """Raised when a client value is not one canonical GitHub pull-request URL."""


class PullRequestResolutionError(RuntimeError):
    """Raised when GitHub returns permanently invalid or mismatched PR metadata."""


class PullRequestUpstreamError(PullRequestResolutionError):
    """Raised when transient GitHub API unavailability prevents PR resolution."""


class ParsedPullRequest(BaseModel):
    """Canonical repository and number obtained only from a validated URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    number: int = Field(gt=0)
    url: str


class ResolvedPullRequest(BaseModel):
    """Minimal immutable product metadata independently resolved by the server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    number: int = Field(gt=0)
    url: str
    base_sha: str
    head_sha: str
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=8_000)
    updated_at: datetime

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        return normalize_repository_name(value)

    @field_validator("base_sha", "head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        normalized = value.lower()
        if _GIT_SHA.fullmatch(normalized) is None:
            raise ValueError("GitHub revision must be one full SHA-1")
        return normalized

    @field_validator("updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pull-request update timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_identity(self) -> ResolvedPullRequest:
        expected = f"https://github.com/{self.repository}/pull/{self.number}"
        if self.url != expected:
            raise ValueError("resolved pull-request URL differs from its identity")
        if self.base_sha == self.head_sha:
            raise ValueError("resolved BASE and HEAD revisions must differ")
        return self


def parse_github_pull_request_url(value: str) -> ParsedPullRequest:
    """Parse exactly one public github.com pull-request URL without network access."""
    match = _PR_URL.fullmatch(value.strip())
    if match is None:
        raise PullRequestUrlError(
            "expected a GitHub PR URL like https://github.com/owner/repo/pull/123"
        )
    repository = normalize_repository_name(f"{match.group('owner')}/{match.group('repo')}")
    number = int(match.group("number"))
    return ParsedPullRequest(
        repository=repository,
        number=number,
        url=f"https://github.com/{repository}/pull/{number}",
    )


class PullRequestResolver(Protocol):
    """Resolve server-trusted immutable facts for one already-validated PR URL."""

    async def resolve(self, parsed: ParsedPullRequest) -> ResolvedPullRequest: ...


class _GitHubRepository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str = Field(min_length=3, max_length=256)
    private: bool


class _GitHubBaseRevision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str
    repo: _GitHubRepository


class _GitHubHeadRevision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str


class _GitHubPullRequestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int = Field(gt=0)
    html_url: str
    title: str
    body: str | None = None
    updated_at: datetime
    base: _GitHubBaseRevision
    head: _GitHubHeadRevision


class GitHubRestPullRequestResolver:
    """Read one bounded response from GitHub's fixed public pull-request endpoint."""

    api_origin = "https://api.github.com"
    maximum_timeout_seconds = 15.0

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 524_288,
        token: str | None = None,
    ) -> None:
        if (
            not 0 < timeout_seconds <= self.maximum_timeout_seconds
            or not 1_024 <= max_response_bytes <= 1_048_576
        ):
            raise ValueError("GitHub resolver limits are invalid")
        if token is not None and (not token.strip() or any(char in token for char in "\r\n")):
            raise ValueError("GitHub resolver token is invalid")
        self.client = client
        self.deadline_seconds = timeout_seconds
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.token = token.strip() if token else None

    async def resolve(self, parsed: ParsedPullRequest) -> ResolvedPullRequest:
        try:
            # asyncio deadlines use the event loop's monotonic clock and cancel a
            # blocked connect/header/body operation when the whole budget expires.
            async with asyncio.timeout(self.deadline_seconds):
                return await self._resolve_within_deadline(parsed)
        except PullRequestResolutionError:
            raise
        except TimeoutError as error:
            raise PullRequestUpstreamError(
                "GitHub pull-request resolution exceeded its overall deadline"
            ) from error

    async def _resolve_within_deadline(self, parsed: ParsedPullRequest) -> ResolvedPullRequest:
        endpoint = f"{self.api_origin}/repos/{parsed.repository}/pulls/{parsed.number}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "PatchProof/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"

        if self.client is not None:
            raw = await self._read_response(self.client, endpoint=endpoint, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                raw = await self._read_response(client, endpoint=endpoint, headers=headers)

        try:
            response = _GitHubPullRequestResponse.model_validate_json(raw)
            response_repository = normalize_repository_name(response.base.repo.full_name)
            response_url = parse_github_pull_request_url(response.html_url)
        except (ValueError, PullRequestUrlError) as error:
            raise PullRequestResolutionError(
                "GitHub returned invalid pull-request metadata"
            ) from error
        if response.base.repo.private:
            raise PullRequestResolutionError("private repositories are not supported")
        if (
            response.number != parsed.number
            or response_repository != parsed.repository
            or response_url.repository != parsed.repository
            or response_url.number != parsed.number
        ):
            raise PullRequestResolutionError(
                "GitHub pull-request identity did not match the request"
            )

        try:
            return ResolvedPullRequest(
                repository=parsed.repository,
                number=parsed.number,
                url=parsed.url,
                base_sha=response.base.sha,
                head_sha=response.head.sha,
                title=(" ".join(response.title.split()) or "Untitled pull request")[:300],
                body=(response.body or "")[:8_000],
                updated_at=response.updated_at,
            )
        except ValueError as error:
            raise PullRequestResolutionError(
                "GitHub returned invalid pull-request metadata"
            ) from error

    async def _read_response(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint: str,
        headers: dict[str, str],
    ) -> bytes:
        try:
            async with client.stream(
                "GET",
                endpoint,
                headers=headers,
                timeout=self.timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    raise PullRequestResolutionError("GitHub pull request was not found")
                if response.status_code == 429 or response.status_code >= 500:
                    raise PullRequestUpstreamError(
                        "GitHub pull-request resolution was temporarily unavailable"
                    )
                if response.status_code == 401 or (
                    response.status_code == 403 and self._is_rate_limited(response)
                ):
                    raise PullRequestUpstreamError(
                        "GitHub pull-request resolution was temporarily unavailable"
                    )
                if response.status_code != 200:
                    raise PullRequestResolutionError("GitHub pull-request resolution failed")
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_bytes = int(declared)
                        if declared_bytes < 0:
                            raise ValueError
                        if declared_bytes > self.max_response_bytes:
                            raise PullRequestResolutionError(
                                "GitHub pull-request response exceeded its byte limit"
                            )
                    except ValueError as error:
                        raise PullRequestResolutionError(
                            "GitHub returned an invalid response length"
                        ) from error
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > self.max_response_bytes:
                        raise PullRequestResolutionError(
                            "GitHub pull-request response exceeded its byte limit"
                        )
                    content.extend(chunk)
                return bytes(content)
        except PullRequestResolutionError:
            raise
        except httpx.HTTPError as error:
            raise PullRequestUpstreamError(
                "GitHub pull-request resolution was unavailable"
            ) from error

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        """Recognize GitHub's header-signaled primary or secondary rate limiting."""
        return response.headers.get("x-ratelimit-remaining", "").strip() == "0" or bool(
            response.headers.get("retry-after", "").strip()
        )
