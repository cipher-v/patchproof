"""Tests for deterministic, credential-free Cloud Tasks payloads."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

from google.api_core.exceptions import AlreadyExists

from patchproof.cloud_tasks import CloudTasksDispatcherSettings, CloudTasksRunDispatcher


class FakeTasksClient:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.requests = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, *, request):
        self.requests.append(request)
        if self.duplicate:
            raise AlreadyExists("already dispatched")
        return request["task"]


def settings() -> CloudTasksDispatcherSettings:
    return CloudTasksDispatcherSettings(
        project_id="patchproof-demo",
        location="asia-south1",
        queue="verification-runs",
        target_url="https://control.example/tasks/verify",
        service_account_email="tasks@patchproof-demo.iam.gserviceaccount.com",
        audience="https://control.example",
    )


def test_dispatch_uses_deterministic_name_oidc_and_run_id_only() -> None:
    client = FakeTasksClient()
    run_id = UUID("dffcf1fe-f9eb-4bb7-b72b-26d18ef5dd60")

    CloudTasksRunDispatcher(settings=settings(), client=client).dispatch(run_id)

    request = client.requests[0]
    task = request["task"]
    assert task.name.endswith("/tasks/run-dffcf1fef9eb4bb7b72b26d18ef5dd60")
    assert json.loads(task.http_request.body) == {"run_id": str(run_id)}
    assert task.http_request.oidc_token.service_account_email.startswith("tasks@")
    assert task.http_request.oidc_token.audience == "https://control.example"
    assert task.dispatch_deadline.seconds == 900


def test_duplicate_task_name_is_an_idempotent_success() -> None:
    client = FakeTasksClient(duplicate=True)
    CloudTasksRunDispatcher(settings=settings(), client=client).dispatch(UUID(int=1))
    assert len(client.requests) == 1


def test_dispatch_settings_reject_non_https_target() -> None:
    try:
        replace(settings(), target_url="http://control.example/tasks/verify")
    except ValueError as error:
        assert "HTTPS" in str(error)
    else:
        raise AssertionError("insecure task target was accepted")
