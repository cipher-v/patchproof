"""FastAPI boundary tests for authenticated GitHub webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient as ApiClient

from patchproof.control_plane import ControlPlaneSettings, create_app
from patchproof.storage import SqliteVerificationRunStore
from patchproof.workflow import RevisionState, RunLifecycle

_SECRET = b"github-webhook-test-secret"


def _payload(
    *,
    action: str = "opened",
    repository: str = "Owner/Repository",
    private: bool = False,
    base_sha: str = "a" * 40,
    head_sha: str = "b" * 40,
    updated_at: str = "2026-08-24T10:00:00Z",
):
    return {
        "action": action,
        "number": 23,
        "repository": {"full_name": repository, "private": private},
        "pull_request": {
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
            "updated_at": updated_at,
            "title": "Bounded PR title",
            "body": "Bounded PR body",
        },
        "installation": {"id": 4242},
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _headers(
    body: bytes,
    *,
    delivery_id: str = "delivery-1",
    event: str = "pull_request",
    secret: bytes = _SECRET,
) -> dict[str, str]:
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-github-delivery": delivery_id,
        "x-github-event": event,
        "x-hub-signature-256": f"sha256={signature}",
    }


@pytest.fixture
def api_parts(writable_test_directory: Path):
    settings = ControlPlaneSettings(
        webhook_secret=_SECRET,
        allowed_repositories=frozenset({"Owner/Repository"}),
        database_path=writable_test_directory / "control-plane.db",
    )
    store = SqliteVerificationRunStore(settings.database_path)
    client = ApiClient(create_app(settings=settings, store=store))
    return client, store


def test_health_endpoint_is_independent_of_webhook_authentication(api_parts) -> None:
    client, _ = api_parts

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_valid_pull_request_delivery_creates_a_durable_run(api_parts) -> None:
    client, store = api_parts
    body = _body(_payload())

    response = client.post("/webhooks/github", content=body, headers=_headers(body))
    response_body = response.json()
    runs = store.list_runs(repository="owner/repository", pr_number=23)

    assert response.status_code == 202
    assert response_body["disposition"] == "ACCEPTED"
    assert response_body["lifecycle"] == "ACCEPTED"
    assert response_body["revision_state"] == "CURRENT"
    assert len(runs) == 1
    assert str(runs[0].run_id) == response_body["run_id"]
    assert runs[0].base_sha == "a" * 40
    assert runs[0].head_sha == "b" * 40
    assert runs[0].title == "Bounded PR title"
    assert runs[0].body == "Bounded PR body"
    assert runs[0].installation_id == 4242


def test_authenticated_current_run_is_dispatched_by_durable_identity(
    writable_test_directory: Path,
) -> None:
    class RecordingDispatcher:
        def __init__(self) -> None:
            self.run_ids = []

        def dispatch(self, run_id) -> None:
            self.run_ids.append(run_id)

    settings = ControlPlaneSettings(
        webhook_secret=_SECRET,
        allowed_repositories=frozenset({"Owner/Repository"}),
        database_path=writable_test_directory / "dispatch.db",
    )
    store = SqliteVerificationRunStore(settings.database_path)
    dispatcher = RecordingDispatcher()
    client = ApiClient(create_app(settings=settings, store=store, dispatcher=dispatcher))
    body = _body(_payload())

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 202
    assert [str(run_id) for run_id in dispatcher.run_ids] == [response.json()["run_id"]]


def test_duplicate_delivery_is_acknowledged_without_a_second_run(api_parts) -> None:
    client, store = api_parts
    body = _body(_payload())
    headers = _headers(body)

    first = client.post("/webhooks/github", content=body, headers=headers)
    duplicate = client.post("/webhooks/github", content=body, headers=headers)

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert duplicate.json()["disposition"] == "DUPLICATE"
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    assert len(store.list_runs(repository="owner/repository", pr_number=23)) == 1


def test_distinct_delivery_for_same_revision_is_revision_idempotent(api_parts) -> None:
    client, store = api_parts
    body = _body(_payload())

    first = client.post(
        "/webhooks/github", content=body, headers=_headers(body, delivery_id="delivery-1")
    )
    duplicate = client.post(
        "/webhooks/github", content=body, headers=_headers(body, delivery_id="delivery-2")
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["disposition"] == "DUPLICATE"
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    assert len(store.list_runs(repository="owner/repository", pr_number=23)) == 1


@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize", "ready_for_review"])
def test_supported_pull_request_actions_are_accepted(api_parts, action: str) -> None:
    client, _ = api_parts
    body = _body(_payload(action=action))

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 202
    assert response.json()["disposition"] == "ACCEPTED"


@pytest.mark.parametrize(
    ("body", "headers", "expected_status"),
    [
        (_body(_payload()), {}, 401),
        (_body(_payload()), {"x-hub-signature-256": "sha256=" + "0" * 64}, 401),
    ],
)
def test_missing_or_invalid_signature_is_rejected_before_persistence(
    api_parts,
    body: bytes,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    client, store = api_parts

    response = client.post("/webhooks/github", content=body, headers=headers)

    assert response.status_code == expected_status
    assert store.list_runs(repository="owner/repository", pr_number=23) == []


def test_malformed_authenticated_payload_is_rejected(api_parts) -> None:
    client, store = api_parts
    body = b"not-json"

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 400
    assert response.json()["detail"] == "malformed pull-request payload"
    assert store.list_runs(repository="owner/repository", pr_number=23) == []


def test_malformed_unauthenticated_payload_fails_authentication_before_parsing(api_parts) -> None:
    client, store = api_parts
    body = b"not-json"

    response = client.post(
        "/webhooks/github",
        content=body,
        headers=_headers(body, secret=b"incorrect-secret"),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid GitHub webhook signature"
    assert store.list_runs(repository="owner/repository", pr_number=23) == []


def test_missing_event_header_is_rejected_after_authentication(api_parts) -> None:
    client, _ = api_parts
    body = _body(_payload())
    headers = _headers(body)
    headers.pop("x-github-event")

    response = client.post("/webhooks/github", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "X-GitHub-Event is missing"


def test_missing_delivery_identifier_is_rejected_after_authentication(api_parts) -> None:
    client, _ = api_parts
    body = _body(_payload())
    headers = _headers(body)
    headers.pop("x-github-delivery")

    response = client.post("/webhooks/github", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "X-GitHub-Delivery is missing or malformed"


def test_non_pull_request_event_and_unsupported_action_are_ignored(api_parts) -> None:
    client, store = api_parts
    ping_body = b"{}"
    ping = client.post(
        "/webhooks/github",
        content=ping_body,
        headers=_headers(ping_body, delivery_id="ping-1", event="ping"),
    )
    closed_body = _body(_payload(action="closed"))
    closed = client.post(
        "/webhooks/github",
        content=closed_body,
        headers=_headers(closed_body, delivery_id="closed-1"),
    )

    assert ping.status_code == closed.status_code == 202
    assert ping.json()["disposition"] == closed.json()["disposition"] == "IGNORED"
    assert store.list_runs(repository="owner/repository", pr_number=23) == []


@pytest.mark.parametrize(
    ("repository", "private", "detail"),
    [
        ("other/repository", False, "repository is not allowlisted"),
        ("owner/repository", True, "private repositories are not supported"),
    ],
)
def test_repository_boundary_rejects_nonallowlisted_or_private_repositories(
    api_parts,
    repository: str,
    private: bool,
    detail: str,
) -> None:
    client, store = api_parts
    body = _body(_payload(repository=repository, private=private))

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 403
    assert response.json()["detail"] == detail
    assert store.list_runs(repository="owner/repository", pr_number=23) == []


def test_invalid_revision_sha_is_rejected_as_identity_input(api_parts) -> None:
    client, store = api_parts
    body = _body(_payload(head_sha="not-a-full-sha"))

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid pull-request identity"
    assert store.list_runs(repository="owner/repository", pr_number=23) == []


def test_out_of_order_delivery_returns_stale_without_replacing_current(api_parts) -> None:
    client, store = api_parts
    current_body = _body(_payload(head_sha="c" * 40, updated_at="2026-08-24T10:02:00Z"))
    current_response = client.post(
        "/webhooks/github",
        content=current_body,
        headers=_headers(current_body, delivery_id="delivery-current"),
    )
    stale_body = _body(_payload(head_sha="b" * 40, updated_at="2026-08-24T10:01:00Z"))
    stale_response = client.post(
        "/webhooks/github",
        content=stale_body,
        headers=_headers(stale_body, delivery_id="delivery-stale"),
    )

    runs = store.list_runs(repository="owner/repository", pr_number=23)
    current_runs = [run for run in runs if run.revision_state is RevisionState.CURRENT]

    assert current_response.status_code == 202
    assert stale_response.status_code == 202
    assert stale_response.json()["disposition"] == "STALE"
    assert stale_response.json()["lifecycle"] == RunLifecycle.TERMINAL
    assert len(current_runs) == 1
    assert current_runs[0].head_sha == "c" * 40


def test_newer_head_supersedes_the_previous_current_run(api_parts) -> None:
    client, store = api_parts
    old_body = _body(_payload(head_sha="b" * 40, updated_at="2026-08-24T10:01:00Z"))
    old_response = client.post(
        "/webhooks/github",
        content=old_body,
        headers=_headers(old_body, delivery_id="delivery-old"),
    )
    new_body = _body(_payload(head_sha="c" * 40, updated_at="2026-08-24T10:02:00Z"))
    new_response = client.post(
        "/webhooks/github",
        content=new_body,
        headers=_headers(new_body, delivery_id="delivery-new"),
    )

    runs = store.list_runs(repository="owner/repository", pr_number=23)
    old_run = next(run for run in runs if str(run.run_id) == old_response.json()["run_id"])
    new_run = next(run for run in runs if str(run.run_id) == new_response.json()["run_id"])

    assert old_run.revision_state is RevisionState.SUPERSEDED
    assert old_run.lifecycle is RunLifecycle.TERMINAL
    assert old_run.superseded_by_run_id == new_run.run_id
    assert new_run.revision_state is RevisionState.CURRENT


def test_payload_size_limit_rejects_before_json_parsing(writable_test_directory: Path) -> None:
    settings = ControlPlaneSettings(
        webhook_secret=_SECRET,
        allowed_repositories=frozenset({"owner/repository"}),
        database_path=writable_test_directory / "small-payload.db",
        max_payload_bytes=16,
    )
    client = ApiClient(create_app(settings=settings))
    body = _body(_payload())

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 413
