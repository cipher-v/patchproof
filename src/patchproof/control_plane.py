"""FastAPI control plane for authenticated GitHub pull-request events."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, ValidationError

from patchproof.github_webhook import (
    SUPPORTED_PULL_REQUEST_ACTIONS,
    GitHubPullRequestWebhook,
    validate_delivery_id,
    verify_github_signature,
)
from patchproof.storage import AcceptanceKind, SqliteVerificationRunStore, VerificationRunStore
from patchproof.workflow import RevisionState, RunLifecycle, normalize_repository_name


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    """Explicit runtime boundary configuration for the local control plane."""

    webhook_secret: bytes
    allowed_repositories: frozenset[str]
    database_path: Path
    max_payload_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not self.webhook_secret:
            raise ValueError("GitHub webhook secret must not be empty")
        canonical_repositories = frozenset(
            normalize_repository_name(repository)
            for repository in self.allowed_repositories
            if repository.strip()
        )
        if not canonical_repositories:
            raise ValueError("at least one allowlisted repository is required")
        if self.max_payload_bytes <= 0:
            raise ValueError("maximum webhook payload size must be positive")
        object.__setattr__(self, "allowed_repositories", canonical_repositories)
        object.__setattr__(self, "database_path", self.database_path.resolve())

    @classmethod
    def from_environment(cls) -> ControlPlaneSettings:
        """Load required local/deployment values without creating module-level state."""
        secret = os.environ.get("PATCHPROOF_WEBHOOK_SECRET", "")
        repositories = frozenset(
            value for value in os.environ.get("PATCHPROOF_ALLOWED_REPOSITORIES", "").split(",")
        )
        database_path = Path(
            os.environ.get("PATCHPROOF_DATABASE_PATH", "data/patchproof-control-plane.db")
        )
        return cls(
            webhook_secret=secret.encode("utf-8"),
            allowed_repositories=repositories,
            database_path=database_path,
        )


class WebhookDisposition(StrEnum):
    """Public acknowledgement outcome for one authenticated webhook delivery."""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    IGNORED = "IGNORED"


class WebhookResponse(BaseModel):
    """Small response that does not expose the full workflow record."""

    model_config = ConfigDict(frozen=True)

    disposition: WebhookDisposition
    detail: str
    run_id: UUID | None = None
    lifecycle: RunLifecycle | None = None
    revision_state: RevisionState | None = None


class VerificationRunDispatcher(Protocol):
    """Idempotently hand an accepted durable run identity to an execution worker."""

    def dispatch(self, run_id: UUID) -> None: ...


async def _read_bounded_body(request: Request, *, maximum_bytes: int) -> bytes:
    """Read an ASGI body without buffering more than the configured limit."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid Content-Length header") from error
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length header")
        if declared_length > maximum_bytes:
            raise HTTPException(status_code=413, detail="webhook payload is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        body.extend(chunk)
    return bytes(body)


def create_app(
    *,
    settings: ControlPlaneSettings | None = None,
    store: VerificationRunStore | None = None,
    dispatcher: VerificationRunDispatcher | None = None,
) -> FastAPI:
    """Create an injectable control-plane application without hidden global resources."""
    resolved_settings = settings or ControlPlaneSettings.from_environment()
    resolved_store = store or SqliteVerificationRunStore(resolved_settings.database_path)
    app = FastAPI(title="PatchProof Control Plane", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.run_store = resolved_store
    app.state.run_dispatcher = dispatcher

    @app.get("/healthz")
    @app.get("/livez")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/webhooks/github",
        response_model=WebhookResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def github_webhook(request: Request, response: Response) -> WebhookResponse:
        body = await _read_bounded_body(request, maximum_bytes=resolved_settings.max_payload_bytes)
        if not verify_github_signature(
            secret=resolved_settings.webhook_secret,
            body=body,
            signature_header=request.headers.get("x-hub-signature-256"),
        ):
            raise HTTPException(status_code=401, detail="invalid GitHub webhook signature")

        try:
            delivery_id = validate_delivery_id(request.headers.get("x-github-delivery"))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        event_name = request.headers.get("x-github-event", "").strip().lower()
        if not event_name:
            raise HTTPException(status_code=400, detail="X-GitHub-Event is missing")
        if event_name != "pull_request":
            return WebhookResponse(
                disposition=WebhookDisposition.IGNORED,
                detail=f"unsupported GitHub event: {event_name or 'missing'}",
            )

        try:
            payload = GitHubPullRequestWebhook.model_validate_json(body)
        except ValidationError as error:
            raise HTTPException(status_code=400, detail="malformed pull-request payload") from error

        if payload.action not in SUPPORTED_PULL_REQUEST_ACTIONS:
            return WebhookResponse(
                disposition=WebhookDisposition.IGNORED,
                detail=f"unsupported pull-request action: {payload.action}",
            )
        repository = payload.repository.full_name.strip().lower()
        if payload.repository.private:
            raise HTTPException(status_code=403, detail="private repositories are not supported")
        if repository not in resolved_settings.allowed_repositories:
            raise HTTPException(status_code=403, detail="repository is not allowlisted")

        try:
            event = payload.to_event(delivery_id)
        except ValidationError as error:
            raise HTTPException(status_code=400, detail="invalid pull-request identity") from error
        acceptance = resolved_store.accept_pull_request(event)
        if (
            dispatcher is not None
            and acceptance.run.revision_state is RevisionState.CURRENT
            and acceptance.run.lifecycle is not RunLifecycle.TERMINAL
        ):
            try:
                dispatcher.dispatch(acceptance.run.run_id)
            except Exception as error:
                raise HTTPException(
                    status_code=503,
                    detail="verification dispatch is temporarily unavailable",
                ) from error
        if acceptance.kind is AcceptanceKind.CREATED:
            disposition = WebhookDisposition.ACCEPTED
            detail = "verification run accepted"
        elif acceptance.kind is AcceptanceKind.STALE_CREATED:
            disposition = WebhookDisposition.STALE
            detail = "delivery recorded as stale; newer HEAD remains current"
        else:
            response.status_code = status.HTTP_200_OK
            disposition = WebhookDisposition.DUPLICATE
            detail = "delivery resolved to an existing verification run"

        return WebhookResponse(
            disposition=disposition,
            detail=detail,
            run_id=acceptance.run.run_id,
            lifecycle=acceptance.run.lifecycle,
            revision_state=acceptance.run.revision_state,
        )

    return app
