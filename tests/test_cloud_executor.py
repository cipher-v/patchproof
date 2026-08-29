"""Tests for the private executor's strict wire and API boundaries."""

from __future__ import annotations

from fastapi.testclient import TestClient

from patchproof.cloud_executor import (
    ExecutionResultDocument,
    ExecutorChallengeRequest,
    ExecutorChallengeResponse,
    ExecutorEnvironmentRequest,
    ExecutorEnvironmentResponse,
    artifact_digest,
    create_executor_app,
)
from patchproof.execution_contract import ExecutionContract
from patchproof.models import (
    DifferentialPattern,
    EnvironmentReadinessStatus,
    MechanicalEvidenceStatus,
    RevisionRole,
    TestExecutionStatus,
)


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
        repository="Owner/Repo",
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
            duration_seconds=0.25,
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
        mechanical_reason="same artifact failed by assertion on BASE and passed on HEAD",
    )


class StubExecutor:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.requests = []

    def prepare_environment(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return ExecutorEnvironmentResponse(
            status=EnvironmentReadinessStatus.READY,
            reason="repository-declared setup completed on BASE and HEAD",
        )

    def execute(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.result


def test_wire_response_round_trip_preserves_domain_facts() -> None:
    result = response_document().to_domain()
    assert result.artifact.sha256 == request_document().artifact_sha256
    assert result.base.revision.role is RevisionRole.BASE
    assert result.head.status is TestExecutionStatus.PASSED
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING


def test_executor_exposes_internal_and_public_liveness_paths() -> None:
    client = TestClient(create_executor_app(executor=StubExecutor(result=response_document())))

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/livez").json() == {"status": "ok"}


def test_executor_api_validates_and_delegates_bounded_request() -> None:
    executor = StubExecutor(result=response_document())
    response = TestClient(create_executor_app(executor=executor)).post(
        "/internal/challenge", json=request_document().model_dump(mode="json")
    )
    assert response.status_code == 200
    assert len(executor.requests) == 1
    assert executor.requests[0].repository == "owner/repo"


def test_executor_api_prepares_environment_without_candidate_data() -> None:
    executor = StubExecutor(result=response_document())
    request = ExecutorEnvironmentRequest(
        repository="Owner/Repo",
        pull_request_number=4,
        base_sha="a" * 40,
        head_sha="b" * 40,
        contract=contract(),
    )

    response = TestClient(create_executor_app(executor=executor)).post(
        "/internal/environment-readiness", json=request.model_dump(mode="json")
    )

    assert response.status_code == 200
    assert response.json()["status"] == "READY"
    assert len(executor.requests) == 1
    assert not hasattr(executor.requests[0], "artifact_source")


def test_executor_api_fails_closed_without_leaking_internal_error() -> None:
    response = TestClient(
        create_executor_app(executor=StubExecutor(error=RuntimeError("secret detail")))
    ).post("/internal/challenge", json=request_document().model_dump(mode="json"))
    assert response.status_code == 503
    assert "secret detail" not in response.text


def test_request_rejects_artifact_hash_mismatch() -> None:
    invalid = request_document().model_copy(update={"artifact_sha256": "0" * 64})
    try:
        invalid.artifact()
    except ValueError as error:
        assert "hash" in str(error)
    else:
        raise AssertionError("mismatched candidate bytes were accepted")
