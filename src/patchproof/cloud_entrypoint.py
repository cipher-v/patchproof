"""Role-selected ASGI entrypoint shared by the two Cloud Run images."""

from __future__ import annotations

import os

import uvicorn

from patchproof.cloud_control import CloudControlSettings, create_cloud_control_app
from patchproof.cloud_executor import EphemeralChallengeExecutor, create_executor_app


def build_app():
    """Build exactly one service role so executor instances never load control secrets."""
    role = os.environ.get("PATCHPROOF_SERVICE_ROLE", "").strip().lower()
    if role == "control":
        return create_cloud_control_app(settings=CloudControlSettings.from_environment())
    if role == "executor":
        repositories = frozenset(
            item.strip()
            for item in os.environ.get("PATCHPROOF_ALLOWED_REPOSITORIES", "").split(",")
            if item.strip()
        )
        return create_executor_app(
            executor=EphemeralChallengeExecutor(allowed_repositories=repositories)
        )
    raise RuntimeError("PATCHPROOF_SERVICE_ROLE must be 'control' or 'executor'")


app = build_app()


def main() -> None:
    """Honor Cloud Run's injected PORT and listen on every container interface."""
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
