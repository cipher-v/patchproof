"""Failure classification tests for the isolated GitHub Checks boundary."""

from __future__ import annotations

import httpx
import pytest

from patchproof.github_checks import (
    GitHubCheckOutput,
    GitHubCheckPayload,
    GitHubCheckRetryableError,
    GitHubChecksClient,
    GitHubCheckTerminalError,
    StaticInstallationTokenProvider,
)


def _payload() -> GitHubCheckPayload:
    return GitHubCheckPayload(
        head_sha="b" * 40,
        conclusion="neutral",
        external_id="11111111-1111-4111-8111-111111111111",
        output=GitHubCheckOutput(title="Bounded result", summary="Summary", text="Evidence"),
    )


@pytest.mark.parametrize("status_code", [408, 429, 503])
def test_transient_github_failures_are_retryable_and_do_not_expose_credentials(
    status_code: int,
) -> None:
    secret = "ghs_must_never_appear_in_an_error"
    client = GitHubChecksClient(
        tokens=StaticInstallationTokenProvider(secret),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status_code, json={"message": secret})
            )
        ),
        api_base_url="https://api.github.test",
    )

    with pytest.raises(GitHubCheckRetryableError) as raised:
        client.upsert_completed(
            repository="owner/repository",
            installation_id=123,
            payload=_payload(),
            known_check_run_id=None,
        )

    assert secret not in str(raised.value)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_permanent_github_failures_are_terminal_and_do_not_expose_credentials(
    status_code: int,
) -> None:
    secret = "ghs_must_never_appear_in_an_error"
    client = GitHubChecksClient(
        tokens=StaticInstallationTokenProvider(secret),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status_code, json={"message": secret})
            )
        ),
        api_base_url="https://api.github.test",
    )

    with pytest.raises(GitHubCheckTerminalError) as raised:
        client.upsert_completed(
            repository="owner/repository",
            installation_id=123,
            payload=_payload(),
            known_check_run_id=None,
        )

    assert secret not in str(raised.value)


def test_missing_installation_id_fails_closed_before_any_request() -> None:
    requests: list[httpx.Request] = []
    client = GitHubChecksClient(
        tokens=StaticInstallationTokenProvider("unused"),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200)
            )
        ),
        api_base_url="https://api.github.test",
    )

    with pytest.raises(GitHubCheckTerminalError, match="installation ID"):
        client.upsert_completed(
            repository="owner/repository",
            installation_id=None,
            payload=_payload(),
            known_check_run_id=None,
        )

    assert requests == []
