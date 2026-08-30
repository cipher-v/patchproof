"""Tests for OIDC task ingress and the remote executor adapter."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from patchproof.cloud_control import (
    CloudControlSettings,
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
    def dispatch(self, run_id):
        del run_id


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
