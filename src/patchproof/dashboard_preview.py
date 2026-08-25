"""Local-only entry point for deterministic dashboard visual verification."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from patchproof.dashboard import (
    StaticDashboardSnapshotProvider,
    install_dashboard,
    load_demo_snapshot,
)


def build_preview_app() -> FastAPI:
    app = FastAPI(title="PatchProof Evidence Console Preview")
    install_dashboard(app, provider=StaticDashboardSnapshotProvider(load_demo_snapshot()))
    return app


app = build_preview_app()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8092)
