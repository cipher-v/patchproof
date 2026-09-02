"""Tests for bounded server-side GitHub pull-request resolution."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from patchproof.pr_resolution import (
    GitHubRestPullRequestResolver,
    PullRequestResolutionError,
    PullRequestUpstreamError,
    PullRequestUrlError,
    parse_github_pull_request_url,
)


def _response(**overrides) -> dict:
    document = {
        "number": 37,
        "html_url": "https://github.com/Owner/Repo/pull/37",
        "title": "  Fix   public\nbehavior ",
        "body": "Details",
        "updated_at": "2026-09-01T12:30:00Z",
        "base": {
            "sha": "a" * 40,
            "repo": {"full_name": "Owner/Repo", "private": False},
        },
        "head": {"sha": "b" * 40},
    }
    document.update(overrides)
    return document


def _resolver(handler) -> GitHubRestPullRequestResolver:
    return GitHubRestPullRequestResolver(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def test_parse_valid_github_pr_url_normalizes_identity() -> None:
    parsed = parse_github_pull_request_url("https://github.com/Owner/Repo/pull/37/")

    assert parsed.repository == "owner/repo"
    assert parsed.number == 37
    assert parsed.url == "https://github.com/owner/repo/pull/37"


@pytest.mark.parametrize(
    "value",
    (
        "https://example.com/owner/repo/pull/37",
        "https://github.com/owner/repo/issues/37",
        "https://github.com/owner/repo/pull/0",
        "http://github.com/owner/repo/pull/37",
        "https://github.com/owner/repo/pull/37/files",
    ),
)
def test_parse_rejects_invalid_host_and_path(value: str) -> None:
    with pytest.raises(PullRequestUrlError):
        parse_github_pull_request_url(value)


def test_resolver_uses_fixed_endpoint_and_server_derived_revisions() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["accept"] = request.headers["accept"]
        return httpx.Response(200, json=_response())

    resolver = _resolver(handler)
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")
    resolved = asyncio.run(resolver.resolve(parsed))
    asyncio.run(resolver.client.aclose())

    assert captured["url"] == "https://api.github.com/repos/owner/repo/pulls/37"
    assert captured["accept"] == "application/vnd.github+json"
    assert resolved.base_sha == "a" * 40
    assert resolved.head_sha == "b" * 40
    assert resolved.title == "Fix public behavior"
    assert resolved.repository == parsed.repository


def test_unknown_pr_number_is_a_sanitized_resolution_failure() -> None:
    resolver = _resolver(lambda _request: httpx.Response(404, json={"message": "Not Found"}))
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/999")

    with pytest.raises(PullRequestResolutionError, match="not found") as captured:
        asyncio.run(resolver.resolve(parsed))
    assert not isinstance(captured.value, PullRequestUpstreamError)
    asyncio.run(resolver.client.aclose())


def test_malformed_response_is_a_permanent_resolution_failure() -> None:
    resolver = _resolver(lambda _request: httpx.Response(200, json={"number": 37}))
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestResolutionError, match="invalid") as captured:
        asyncio.run(resolver.resolve(parsed))
    assert not isinstance(captured.value, PullRequestUpstreamError)
    asyncio.run(resolver.client.aclose())


@pytest.mark.parametrize("status_code", (429, 500, 503))
def test_retryable_http_status_is_an_upstream_failure(status_code: int) -> None:
    resolver = _resolver(lambda _request: httpx.Response(status_code))
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestUpstreamError, match="temporarily unavailable"):
        asyncio.run(resolver.resolve(parsed))
    asyncio.run(resolver.client.aclose())


def test_header_signaled_403_rate_limit_is_an_upstream_failure() -> None:
    resolver = _resolver(
        lambda _request: httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    )
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestUpstreamError, match="temporarily unavailable"):
        asyncio.run(resolver.resolve(parsed))
    asyncio.run(resolver.client.aclose())


def test_ordinary_403_is_a_permanent_resolution_failure() -> None:
    resolver = _resolver(lambda _request: httpx.Response(403))
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestResolutionError, match="resolution failed") as captured:
        asyncio.run(resolver.resolve(parsed))
    assert not isinstance(captured.value, PullRequestUpstreamError)
    asyncio.run(resolver.client.aclose())


def test_network_connection_failure_is_an_upstream_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture connection failure", request=request)

    resolver = _resolver(handler)
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestUpstreamError, match="unavailable"):
        asyncio.run(resolver.resolve(parsed))
    asyncio.run(resolver.client.aclose())


def test_overall_deadline_cancels_a_blocked_body_without_waiting_15_seconds() -> None:
    class BlockedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"unreachable"

    resolver = GitHubRestPullRequestResolver(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, stream=BlockedBody())
            )
        ),
        timeout_seconds=0.01,
    )
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestUpstreamError, match="overall deadline"):
        asyncio.run(resolver.resolve(parsed))
    asyncio.run(resolver.client.aclose())


def test_resolver_rejects_an_overall_budget_above_15_seconds() -> None:
    with pytest.raises(ValueError, match="limits"):
        GitHubRestPullRequestResolver(timeout_seconds=15.01)


def test_private_or_mismatched_repository_is_rejected() -> None:
    private = _resolver(
        lambda _request: httpx.Response(
            200,
            json=_response(
                base={
                    "sha": "a" * 40,
                    "repo": {"full_name": "owner/repo", "private": True},
                }
            ),
        )
    )
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestResolutionError, match="private"):
        asyncio.run(private.resolve(parsed))
    asyncio.run(private.client.aclose())


def test_response_byte_limit_is_enforced_while_streaming() -> None:
    oversized = json.dumps({"padding": "x" * 2_000}).encode()
    resolver = GitHubRestPullRequestResolver(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=oversized))
        ),
        max_response_bytes=1_024,
    )
    parsed = parse_github_pull_request_url("https://github.com/owner/repo/pull/37")

    with pytest.raises(PullRequestResolutionError, match="byte limit"):
        asyncio.run(resolver.resolve(parsed))
    asyncio.run(resolver.client.aclose())
