"""Tests for the public read-only evidence dashboard boundary."""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from patchproof.cloud_control import CloudControlSettings
from patchproof.dashboard import (
    StaticDashboardSnapshotProvider,
    StoreDashboardSnapshotProvider,
    install_dashboard,
    load_demo_snapshot,
)
from patchproof.storage import SqliteVerificationRunStore

_SUCCESS_RUN_ID = UUID("695eaa20-7db3-492f-a57e-9819ebb54087")
_ABSTENTION_RUN_ID = UUID("2386649f-56b9-44c8-833e-ddf440a05483")


def dashboard_client() -> TestClient:
    app = FastAPI()
    install_dashboard(app, provider=StaticDashboardSnapshotProvider(load_demo_snapshot()))
    return TestClient(app)


def test_checked_demo_snapshot_preserves_success_and_abstention() -> None:
    snapshot = load_demo_snapshot()

    assert tuple(run.run_id for run in snapshot.runs) == (
        _SUCCESS_RUN_ID,
        _ABSTENTION_RUN_ID,
    )
    success, abstention = snapshot.runs
    assert success.base_execution.status == "ASSERTION_FAILED"
    assert success.head_execution.status == "PASSED"
    assert success.mechanical_status == "DISCRIMINATING"
    assert success.claim_outcome == "CLAIM_SUPPORTED_FOR_SCENARIO"
    assert success.evidence_hash_verified
    assert success.check_run_id == 97764451438
    assert len(success.candidates) == 1
    assert abstention.mechanical_status == "ENVIRONMENTAL"
    assert abstention.claim_outcome == "INSUFFICIENT_EVIDENCE"
    assert len(abstention.candidates) == 2
    assert abstention.candidates[1].parent_candidate_id == "candidate-1"


def test_dashboard_page_has_strict_headers_and_external_assets() -> None:
    client = dashboard_client()

    root = client.get("/", follow_redirects=False)
    page = client.get("/dashboard")

    assert root.status_code == 307
    assert root.headers["location"] == "/dashboard"
    assert page.status_code == 200
    assert "Proof for the claim" in page.text
    assert '<script src="/dashboard/assets/dashboard.js" defer></script>' in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert client.get("/dashboard/assets/styles.css").status_code == 200
    assert client.get("/dashboard/assets/dashboard.js").status_code == 200


def test_dashboard_api_exposes_only_the_sanitized_projection() -> None:
    response = dashboard_client().get("/dashboard/api/runs")

    assert response.status_code == 200
    document = response.json()
    serialized = response.text
    assert len(document["runs"]) == 2
    assert document["runs"][0]["run_id"] == str(_SUCCESS_RUN_ID)
    for forbidden in (
        "raw_response_sha256",
        '"stdout"',
        '"stderr"',
        '"detail"',
        '"body"',
        "installation_id",
        "github_private_key",
    ):
        assert forbidden not in serialized


def test_store_provider_requires_a_small_unique_explicit_run_list(
    writable_test_directory,
) -> None:
    store = SqliteVerificationRunStore(writable_test_directory / "dashboard.db")

    with pytest.raises(ValueError, match="unique"):
        StoreDashboardSnapshotProvider(store=store, run_ids=(_SUCCESS_RUN_ID,) * 2)
    with pytest.raises(ValueError, match="at most eight"):
        StoreDashboardSnapshotProvider(
            store=store,
            run_ids=tuple(UUID(int=value) for value in range(1, 10)),
        )


def test_missing_featured_run_fails_closed_without_leaking_identity(
    writable_test_directory,
) -> None:
    app = FastAPI()
    store = SqliteVerificationRunStore(writable_test_directory / "missing.db")
    install_dashboard(
        app,
        provider=StoreDashboardSnapshotProvider(store=store, run_ids=(_SUCCESS_RUN_ID,)),
    )

    response = TestClient(app).get("/dashboard/api/runs")

    assert response.status_code == 503
    assert response.json() == {"detail": "featured evidence is temporarily unavailable"}
    assert str(_SUCCESS_RUN_ID) not in response.text


def test_cloud_settings_parse_only_explicit_dashboard_run_ids(monkeypatch) -> None:
    values = {
        "GOOGLE_CLOUD_PROJECT": "patchproof-demo",
        "PATCHPROOF_REGION": "asia-south1",
        "PATCHPROOF_TASK_QUEUE": "verification-runs",
        "PATCHPROOF_CONTROL_URL": "https://control.example",
        "PATCHPROOF_EXECUTOR_URL": "https://executor.example",
        "PATCHPROOF_TASK_INVOKER_EMAIL": "tasks@example.iam.gserviceaccount.com",
        "PATCHPROOF_WEBHOOK_SECRET": "secret",
        "PATCHPROOF_ALLOWED_REPOSITORIES": "cipher-v/patchproof",
        "PATCHPROOF_GITHUB_APP_ID": "4711074",
        "PATCHPROOF_GITHUB_PRIVATE_KEY": "fixture-only",
        "PATCHPROOF_DASHBOARD_RUN_IDS": f"{_SUCCESS_RUN_ID},{_ABSTENTION_RUN_ID}",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = CloudControlSettings.from_environment()

    assert settings.dashboard_run_ids == (_SUCCESS_RUN_ID, _ABSTENTION_RUN_ID)
