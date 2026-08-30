"""Tests for OIDC task ingress and the remote executor adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

import patchproof.cloud_control as cloud_control_module
from patchproof.cloud_control import (
    CloudControlSettings,
    CloudRunTaskProcessor,
    GoogleTaskIdentityVerifier,
    RemoteBaseHeadChallenge,
    create_cloud_control_app,
)
from patchproof.cloud_executor import (
    ExecutionResultDocument,
    ExecutorChallengeRequest,
    ExecutorChallengeResponse,
    ExecutorEnvironmentResponse,
    artifact_digest,
)
from patchproof.execution_contract import ExecutionContract
from patchproof.models import (
    DifferentialPattern,
    EnvironmentReadinessStatus,
    MechanicalEvidenceStatus,
    RevisionRole,
    TestExecutionStatus,
)
from patchproof.storage import SqliteVerificationRunStore
from patchproof.workflow import PullRequestEvent


def contract() -> ExecutionContract:
    return ExecutionContract.model_validate(
        {
            "version": 1,
            "python": "3.12",
            "install": [["uv", "sync", "--frozen"]],
            "test": {"command": ["uv", "run", "pytest"]},
            "allowed_test_paths": ["tests/"],
            "timeout_seconds": 120,
        }
    )


def historical_contract() -> ExecutionContract:
    return ExecutionContract.model_validate(
        {
            "version": 1,
            "python": "3.12",
            "install": [
                ["uv", "venv"],
                ["uv", "pip", "install", "-e", "."],
                ["uv", "pip", "install", "pytest"],
            ],
            "test": {"command": ["python", "-m", "pytest"]},
            "allowed_test_paths": ["patchproof_generated_tests/"],
            "timeout_seconds": 300,
            "synthesized": True,
        }
    )


def request_document() -> ExecutorChallengeRequest:
    source = "def test_behavior():\n    assert True\n"
    return ExecutorChallengeRequest(
        repository="owner/repo",
        pull_request_number=4,
        base_sha="a" * 40,
        head_sha="b" * 40,
        contract=contract(),
        artifact_path="tests/test_patchproof_generated.py",
        node_id="tests/test_patchproof_generated.py::test_behavior",
        artifact_source=source,
        artifact_sha256=artifact_digest(source),
    )


def response_document() -> ExecutorChallengeResponse:
    request = request_document()

    def execution(role: RevisionRole, sha: str, status: TestExecutionStatus):
        return ExecutionResultDocument(
            role=role,
            revision_sha=sha,
            test_node_id=request.node_id,
            expected_artifact_sha256=request.artifact_sha256,
            artifact_sha256_before=request.artifact_sha256,
            artifact_sha256_after=request.artifact_sha256,
            status=status,
            collected_count=1,
            exit_code=1 if role is RevisionRole.BASE else 0,
            duration_seconds=0.1,
            stdout="",
            stderr="",
            detail=None,
        )

    return ExecutorChallengeResponse(
        artifact_path=request.artifact_path,
        node_id=request.node_id,
        artifact_source=request.artifact_source,
        artifact_sha256=request.artifact_sha256,
        base=execution(RevisionRole.BASE, request.base_sha, TestExecutionStatus.ASSERTION_FAILED),
        head=execution(RevisionRole.HEAD, request.head_sha, TestExecutionStatus.PASSED),
        mechanical_status=MechanicalEvidenceStatus.DISCRIMINATING,
        differential_pattern=DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED,
        mechanical_reason="same artifact failed on BASE and passed on HEAD",
    )


class StubDispatcher:
    def __init__(self) -> None:
        self.run_ids = []

    def dispatch(self, run_id):
        self.run_ids.append(run_id)


class StubVerifier:
    def __init__(self, *, accepted: bool) -> None:
        self.accepted = accepted
        self.headers = []

    def verify(self, authorization):
        self.headers.append(authorization)
        if not self.accepted:
            raise PermissionError("invalid task identity")


class StubProcessor:
    def __init__(self) -> None:
        self.run_ids = []

    async def process(self, run_id):
        self.run_ids.append(run_id)


class StubTokenProvider:
    def token(self, audience: str) -> str:
        assert audience == "https://executor.example"
        return "short-lived-id-token"


def settings() -> CloudControlSettings:
    return CloudControlSettings(
        project_id="patchproof-demo",
        region="asia-south1",
        queue="verification-runs",
        control_url="https://control.example",
        executor_url="https://executor.example",
        task_invoker_email="tasks@patchproof-demo.iam.gserviceaccount.com",
        webhook_secret=b"webhook-secret",
        allowed_repositories=frozenset({"owner/repo"}),
        github_app_id=123,
        github_private_key="injected processor avoids parsing this fixture",
    )


def test_task_route_rejects_missing_or_invalid_oidc_before_processing(
    writable_test_directory: Path,
) -> None:
    processor = StubProcessor()
    app = create_cloud_control_app(
        settings=settings(),
        store=SqliteVerificationRunStore(writable_test_directory / "control-reject.db"),
        dispatcher=StubDispatcher(),
        processor=processor,
        verifier=StubVerifier(accepted=False),
    )
    response = TestClient(app).post("/tasks/verify", json={"run_id": str(UUID(int=2))})
    assert response.status_code == 401
    assert processor.run_ids == []


def test_task_route_accepts_verified_cloud_task_identity(
    writable_test_directory: Path,
) -> None:
    processor = StubProcessor()
    verifier = StubVerifier(accepted=True)
    app = create_cloud_control_app(
        settings=settings(),
        store=SqliteVerificationRunStore(writable_test_directory / "control-accept.db"),
        dispatcher=StubDispatcher(),
        processor=processor,
        verifier=verifier,
    )
    run_id = UUID(int=3)
    response = TestClient(app).post(
        "/tasks/verify",
        headers={"Authorization": "Bearer signed-google-token"},
        json={"run_id": str(run_id)},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "completed"}
    assert processor.run_ids == [run_id]
    assert verifier.headers == ["Bearer signed-google-token"]


def test_remote_challenge_sends_oidc_and_rejects_artifact_substitution() -> None:
    request = request_document()
    expected = response_document()
    captured = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["authorization"] = http_request.headers["authorization"]
        captured["payload"] = http_request.read()
        return httpx.Response(200, content=expected.model_dump_json())

    challenge = RemoteBaseHeadChallenge(
        repository=request.repository,
        pull_request_number=request.pull_request_number,
        contract=contract(),
        executor_url="https://executor.example",
        tokens=StubTokenProvider(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    callbacks = []
    result = challenge.run(
        base_ref=request.base_sha,
        head_ref=request.head_sha,
        artifact=request.artifact(),
        on_base_complete=callbacks.append,
    )
    assert captured["authorization"] == "Bearer short-lived-id-token"
    assert result.artifact.sha256 == request.artifact_sha256
    assert callbacks == [result.base]


def test_remote_challenge_checks_environment_before_candidate_execution() -> None:
    captured = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["path"] = http_request.url.path
        captured["payload"] = http_request.read()
        return httpx.Response(
            200,
            content=ExecutorEnvironmentResponse(
                status=EnvironmentReadinessStatus.READY,
                reason="repository-declared setup completed on BASE and HEAD",
            ).model_dump_json(),
        )

    challenge = RemoteBaseHeadChallenge(
        repository="owner/repo",
        pull_request_number=4,
        contract=contract(),
        executor_url="https://executor.example",
        tokens=StubTokenProvider(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    readiness = challenge.prepare_environment(base_ref="a" * 40, head_ref="b" * 40)

    assert readiness.ready
    assert captured["path"] == "/internal/environment-readiness"
    assert b"artifact_source" not in captured["payload"]


def test_remote_challenge_session_binds_revisions_for_workflow_interface() -> None:
    request = request_document()
    expected = response_document()
    paths = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        paths.append(http_request.url.path)
        if http_request.url.path.endswith("environment-readiness"):
            return httpx.Response(
                200,
                content=ExecutorEnvironmentResponse(
                    status=EnvironmentReadinessStatus.READY,
                    reason="repository-declared setup completed on BASE and HEAD",
                ).model_dump_json(),
            )
        return httpx.Response(200, content=expected.model_dump_json())

    challenge = RemoteBaseHeadChallenge(
        repository=request.repository,
        pull_request_number=request.pull_request_number,
        contract=contract(),
        executor_url="https://executor.example",
        tokens=StubTokenProvider(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with challenge.session(base_ref=request.base_sha, head_ref=request.head_sha) as session:
        assert session.prepare_environment().ready
        assert session.installed_import_roots() == frozenset()
        result = session.run(artifact=request.artifact())

    assert result.artifact.sha256 == request.artifact_sha256
    assert paths == ["/internal/environment-readiness", "/internal/challenge"]


def test_google_task_verifier_pins_audience_issuer_and_email(monkeypatch) -> None:
    captured = {}

    def verify(token, request, *, audience):
        del request
        captured.update(token=token, audience=audience)
        return {
            "iss": "https://accounts.google.com",
            "email": "tasks@patchproof-demo.iam.gserviceaccount.com",
            "email_verified": True,
        }

    monkeypatch.setattr("patchproof.cloud_control.id_token.verify_oauth2_token", verify)
    verifier = GoogleTaskIdentityVerifier(
        audience="https://control.example",
        service_account_email="tasks@patchproof-demo.iam.gserviceaccount.com",
    )
    verifier.verify("Bearer signed-token")
    assert captured == {
        "token": "signed-token",
        "audience": "https://control.example",
    }


def test_cloud_analyze_accepts_known_case_persists_and_dispatches(
    writable_test_directory: Path,
) -> None:
    store = SqliteVerificationRunStore(writable_test_directory / "analyze.db")
    dispatcher = StubDispatcher()
    app = create_cloud_control_app(
        settings=replace(
            settings(),
            allowed_repositories=frozenset({"python-jsonschema/jsonschema"}),
        ),
        store=store,
        dispatcher=dispatcher,
        processor=StubProcessor(),
        verifier=StubVerifier(accepted=True),
        project_root=Path(__file__).resolve().parents[1],
    )
    client = TestClient(app)

    response = client.post(
        "/api/analyze",
        json={
            "pr_url": "https://github.com/python-jsonschema/jsonschema/pull/1208",
        },
    )

    assert response.status_code == 202
    document = response.json()
    run_id = UUID(document["run_id"])
    assert document["status"] == "ACCEPTED"
    assert document["dashboard_url"] == f"https://control.example/dashboard?run={run_id}"
    assert document["result_url"] == f"https://control.example/api/runs/{run_id}"
    assert dispatcher.run_ids == [run_id]
    run = store.get_run(run_id)
    assert run.event_action == "patchproof_analyze"
    assert run.repository == "python-jsonschema/jsonschema"
    assert run.installation_id is None

    status_response = client.get(f"/api/runs/{run_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "ACCEPTED"
    assert client.get("/dashboard/api/runs").json()["runs"][0]["run_id"] == str(run_id)


def test_cloud_analyze_duplicate_revision_returns_same_durable_run(
    writable_test_directory: Path,
) -> None:
    store = SqliteVerificationRunStore(writable_test_directory / "dedupe.db")
    dispatcher = StubDispatcher()
    app = create_cloud_control_app(
        settings=replace(
            settings(),
            allowed_repositories=frozenset({"python-jsonschema/jsonschema"}),
        ),
        store=store,
        dispatcher=dispatcher,
        processor=StubProcessor(),
        verifier=StubVerifier(accepted=True),
        project_root=Path(__file__).resolve().parents[1],
    )
    client = TestClient(app)
    payload = {"pr_url": "https://github.com/python-jsonschema/jsonschema/pull/1208"}

    first = client.post("/api/analyze", json=payload)
    duplicate = client.post("/api/analyze", json=payload)

    assert first.status_code == duplicate.status_code == 202
    assert first.json()["run_id"] == duplicate.json()["run_id"]
    assert dispatcher.run_ids == [UUID(first.json()["run_id"])] * 2


def test_cloud_analyze_rejects_malformed_and_unknown_pr_without_dispatch(
    writable_test_directory: Path,
) -> None:
    dispatcher = StubDispatcher()
    app = create_cloud_control_app(
        settings=replace(
            settings(),
            allowed_repositories=frozenset({"python-jsonschema/jsonschema"}),
        ),
        store=SqliteVerificationRunStore(writable_test_directory / "reject.db"),
        dispatcher=dispatcher,
        processor=StubProcessor(),
        verifier=StubVerifier(accepted=True),
        project_root=Path(__file__).resolve().parents[1],
    )
    client = TestClient(app)

    malformed = client.post("/api/analyze", json={"pr_url": "not-a-url"})
    unknown = client.post(
        "/api/analyze",
        json={"pr_url": "https://github.com/python/cpython/pull/1"},
    )

    assert malformed.status_code == 422
    assert unknown.status_code == 422
    assert "Arbitrary public PR analysis is not enabled" in unknown.json()["detail"]
    assert dispatcher.run_ids == []

    oversized = client.post(
        "/api/analyze",
        content=b'{"pr_url":"' + (b"x" * 2_000) + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413


def test_known_product_worker_uses_manifest_contract_and_skips_github_publication(
    monkeypatch, writable_test_directory: Path
) -> None:
    root = Path(__file__).resolve().parents[1]
    store = SqliteVerificationRunStore(writable_test_directory / "worker-known.db")
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id="product-known",
            action="patchproof_analyze",
            repository="python-jsonschema/jsonschema",
            pr_number=1208,
            base_sha="256dadd3539861ae696c03805923eb4097b871f9",
            head_sha="9a3d4a770813484be066ead99edf2e779b72362a",
            head_updated_at=datetime(2024, 1, 1, tzinfo=UTC),
            title="Use equal for enum",
        )
    ).run
    captured = {}
    resolved_contract = historical_contract()

    def prepare_repository(_repository, _pr, _base, _head, destination) -> None:
        destination.mkdir()

    def context_factory(**kwargs):
        captured["context"] = kwargs
        return object()

    def challenge_factory(**kwargs):
        captured["challenge"] = kwargs
        return object()

    class FakeWorker:
        def __init__(self, *, workflow, store) -> None:
            captured["workflow"] = workflow
            captured["store"] = store

        async def run(self, run_id) -> None:
            captured["run_id"] = run_id

    monkeypatch.setattr(
        cloud_control_module.hard_mode,
        "_execution_plan",
        lambda _repository, _case: SimpleNamespace(contract=resolved_contract),
    )
    monkeypatch.setattr(
        cloud_control_module,
        "TrustedHistoricalContextRetriever",
        context_factory,
    )
    monkeypatch.setattr(cloud_control_module, "RemoteBaseHeadChallenge", challenge_factory)
    monkeypatch.setattr(cloud_control_module, "BehavioralClaimAgent", lambda **_kwargs: object())
    monkeypatch.setattr(cloud_control_module, "AdkGeminiClaimModel", lambda **_kwargs: object())
    monkeypatch.setattr(cloud_control_module, "AdkGeminiCandidateModel", lambda **_kwargs: object())
    monkeypatch.setattr(
        cloud_control_module,
        "AdkGeminiEvidenceAssessor",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        cloud_control_module,
        "EvidenceWorkflow",
        lambda **kwargs: SimpleNamespace(arguments=kwargs),
    )
    monkeypatch.setattr(cloud_control_module, "ReliableEvidenceWorker", FakeWorker)
    published = []
    monkeypatch.setattr(
        cloud_control_module,
        "GitHubAppInstallationTokenProvider",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        cloud_control_module,
        "GitHubCheckPublisher",
        lambda **_kwargs: SimpleNamespace(publish=published.append),
    )
    processor = CloudRunTaskProcessor(
        store=store,
        executor_url="https://executor.example",
        allowed_repositories=frozenset({"python-jsonschema/jsonschema"}),
        github_app_id=123,
        github_private_key="fixture-key",
        project_root=root,
    )
    monkeypatch.setattr(processor, "_prepare_repository", prepare_repository)

    asyncio.run(processor.process(run.run_id))

    assert captured["run_id"] == run.run_id
    assert captured["context"]["excluded_paths"] == frozenset()
    assert captured["context"]["contract"] == resolved_contract
    assert captured["challenge"]["contract_origin"] == "PATCHPROOF_MANIFEST"
    assert captured["challenge"]["repository_python_paths"] == (".",)
    assert published == []


def test_unexpected_task_preparation_failure_becomes_sanitized_terminal_run(
    writable_test_directory: Path,
) -> None:
    class FailingProcessor:
        async def process(self, _run_id) -> None:
            raise RuntimeError("private machine path and secret detail")

    store = SqliteVerificationRunStore(writable_test_directory / "task-failure.db")
    run = store.accept_pull_request(
        PullRequestEvent(
            delivery_id="failure-run",
            action="patchproof_analyze",
            repository="owner/repo",
            pr_number=4,
            base_sha="a" * 40,
            head_sha="b" * 40,
            head_updated_at=datetime(2026, 8, 30, tzinfo=UTC),
            title="Failure fixture",
        )
    ).run
    app = create_cloud_control_app(
        settings=settings(),
        store=store,
        dispatcher=StubDispatcher(),
        processor=FailingProcessor(),
        verifier=StubVerifier(accepted=True),
        project_root=Path(__file__).resolve().parents[1],
    )

    response = TestClient(app).post(
        "/tasks/verify",
        headers={"Authorization": "Bearer signed-google-token"},
        json={"run_id": str(run.run_id)},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "terminal"}
    failure = store.get_failure(run.run_id)
    assert failure is not None
    assert failure.error_code == "INTERNAL_WORKER_FAILURE"
    assert "secret detail" not in failure.summary
