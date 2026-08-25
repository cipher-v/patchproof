"""Idempotent Cloud Tasks dispatch for durable PatchProof run identities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2
from google.protobuf import duration_pb2


@dataclass(frozen=True, slots=True)
class CloudTasksDispatcherSettings:
    """Validated coordinates for one directed control-plane task queue."""

    project_id: str
    location: str
    queue: str
    target_url: str
    service_account_email: str
    audience: str
    dispatch_deadline_seconds: int = 900

    def __post_init__(self) -> None:
        strings = (
            self.project_id,
            self.location,
            self.queue,
            self.target_url,
            self.service_account_email,
            self.audience,
        )
        if any(not value.strip() for value in strings):
            raise ValueError("Cloud Tasks settings must not be empty")
        if not self.target_url.startswith("https://") or not self.audience.startswith("https://"):
            raise ValueError("Cloud Tasks target and audience must use HTTPS")
        if not 15 <= self.dispatch_deadline_seconds <= 1800:
            raise ValueError("Cloud Tasks deadline must be between 15 and 1800 seconds")


class CloudTasksRunDispatcher:
    """Create one deterministic OIDC-authenticated task per verification run."""

    def __init__(
        self,
        *,
        settings: CloudTasksDispatcherSettings,
        client: tasks_v2.CloudTasksClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or tasks_v2.CloudTasksClient()

    def dispatch(self, run_id: UUID) -> None:
        parent = self.client.queue_path(
            self.settings.project_id,
            self.settings.location,
            self.settings.queue,
        )
        task = tasks_v2.Task(
            name=f"{parent}/tasks/run-{run_id.hex}",
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=self.settings.target_url,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"run_id": str(run_id)}, separators=(",", ":")).encode("utf-8"),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self.settings.service_account_email,
                    audience=self.settings.audience,
                ),
            ),
            dispatch_deadline=duration_pb2.Duration(
                seconds=self.settings.dispatch_deadline_seconds
            ),
        )
        try:
            self.client.create_task(request={"parent": parent, "task": task})
        except AlreadyExists:
            # A deterministic task name is the Cloud Tasks idempotency key.
            return
