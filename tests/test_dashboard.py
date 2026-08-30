"""Tests for the public read-only evidence dashboard boundary."""

from __future__ import annotations

from datetime import UTC, datetime
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
from patchproof.storage import SqliteVerificationRunStore, StoredEvidence
from patchproof.workflow import PullRequestEvent

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
    assert 'id="analyze-form"' in page.text
    assert 'id="pr-url"' in page.text
    assert client.get("/dashboard/assets/styles.css").status_code == 200
    script = client.get("/dashboard/assets/dashboard.js")
    assert script.status_code == 200
    assert 'fetch("/api/analyze"' in script.text
    assert "fetch(`/api/runs/${encodeURIComponent(runId)}`" in script.text
    assert 'candidate.source || "Candidate source unavailable."' in script.text
    for durable_status in (
        "QUEUED",
        "SELECTING_CLAIM",
        "GENERATING_CANDIDATE",
        "RUNNING_BASE_HEAD",
        "ASSESSING_SEMANTICS",
        "COMPLETE",
        "FAILED",
        "ABSTAINED",
    ):
        assert durable_status in script.text


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
    with pytest.raises(ValueError, match="cannot exceed"):
        StoreDashboardSnapshotProvider(
            store=store,
            run_ids=(_SUCCESS_RUN_ID, _ABSTENTION_RUN_ID),
            max_runs=1,
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


def test_store_provider_auto_discovers_bounded_recent_runs_newest_first(
    writable_test_directory,
) -> None:
    clock_values = iter(
        (
            datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 30, 10, 1, tzinfo=UTC),
        )
    )
    store = SqliteVerificationRunStore(
        writable_test_directory / "recent.db",
        clock=lambda: next(clock_values),
    )

    def accept(delivery: str, pr_number: int, head: str, updated: datetime):
        return store.accept_pull_request(
            PullRequestEvent(
                delivery_id=delivery,
                action="product_analyze",
                repository="owner/repo",
                pr_number=pr_number,
                base_sha="a" * 40,
                head_sha=head * 40,
                head_updated_at=updated,
                title=f"PR {pr_number}",
            )
        ).run

    older = accept("one", 1, "b", datetime(2026, 8, 29, tzinfo=UTC))
    newer = accept("two", 2, "c", datetime(2026, 8, 30, tzinfo=UTC))

    snapshot = StoreDashboardSnapshotProvider(store=store, max_runs=1).snapshot()

    assert [run.run_id for run in snapshot.runs] == [newer.run_id]
    assert snapshot.runs[0].status == "ACCEPTED"
    assert older.run_id != newer.run_id


def test_store_projection_rejects_corrupted_evidence_hash(writable_test_directory) -> None:
    store = SqliteVerificationRunStore(writable_test_directory / "corrupt.db")
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id="corrupt",
            action="patchproof_analyze",
            repository="owner/repo",
            pr_number=3,
            base_sha="a" * 40,
            head_sha="b" * 40,
            head_updated_at=datetime(2026, 8, 30, tzinfo=UTC),
            title="Corrupt fixture",
        )
    ).run

    class CorruptingStore:
        def get_run(self, run_id):
            return store.get_run(run_id)

        def get_evidence(self, run_id):
            return StoredEvidence(
                run_id=run_id,
                document_json='{"not":"the declared hash"}',
                sha256="0" * 64,
                created_at=datetime(2026, 8, 30, tzinfo=UTC),
            )

        def get_publication(self, run_id):
            del run_id
            return None

        def get_failure(self, run_id):
            del run_id
            return None

        def list_recent_runs(self, *, limit):
            del limit
            return [run]

    provider = StoreDashboardSnapshotProvider(store=CorruptingStore())

    with pytest.raises(RuntimeError, match="hash does not match"):
        provider.snapshot()


def test_cloud_settings_parse_only_explicit_dashboard_run_ids(monkeypatch) -> None:
    values = {
        "GOOGLE_CLOUD_PROJECT": "patchproof-demo",
        "GOOGLE_CLOUD_LOCATION": "global",
        "PATCHPROOF_GEMINI_PROVIDER": "VERTEX_AI",
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
    assert settings.gemini_provider.provider_surface == "VERTEX_AI"
    assert settings.gemini_provider.project == "patchproof-demo"
    assert settings.gemini_provider.location == "global"
