"""Private Cloud Run executor API for immutable BASE/HEAD challenges."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from patchproof.challenge import BaseHeadChallenge
from patchproof.execution_contract import ExecutionContract, ExecutionContractLoader
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.models import (
    ChallengeResult,
    DifferentialPattern,
    EnvironmentReadiness,
    EnvironmentReadinessStatus,
    EvidenceAssessment,
    ExecutionResult,
    MechanicalEvidenceStatus,
    Revision,
    RevisionRole,
    TestArtifact,
    TestExecutionStatus,
)
from patchproof.pytest_runner import PytestRunner
from patchproof.workflow import normalize_repository_name


class ExecutorEnvironmentRequest(BaseModel):
    """Immutable repository and setup contract crossing into the executor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    pull_request_number: int = Field(gt=0)
    base_sha: str = Field(pattern=r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
    head_sha: str = Field(pattern=r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
    contract: ExecutionContract

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        return normalize_repository_name(value)


class ExecutorChallengeRequest(ExecutorEnvironmentRequest):
    """Bounded candidate data crossing from the credentialed control plane."""

    artifact_path: str = Field(min_length=1, max_length=300)
    node_id: str = Field(min_length=1, max_length=500)
    artifact_source: str = Field(min_length=1, max_length=16_000)
    artifact_sha256: str = Field(pattern=r"[0-9a-f]{64}")

    def artifact(self) -> TestArtifact:
        artifact = TestArtifact.from_text(
            relative_path=self.artifact_path,
            node_id=self.node_id,
            content=self.artifact_source,
        )
        if artifact.sha256 != self.artifact_sha256:
            raise ValueError("candidate artifact hash does not match its source")
        return artifact


class ExecutionResultDocument(BaseModel):
    """JSON-safe execution facts returned without credentials or repository state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RevisionRole
    revision_sha: str
    test_node_id: str
    expected_artifact_sha256: str
    artifact_sha256_before: str | None
    artifact_sha256_after: str | None
    status: TestExecutionStatus
    collected_count: int
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    detail: str | None

    @classmethod
    def from_domain(cls, result: ExecutionResult) -> ExecutionResultDocument:
        return cls(
            role=result.revision.role,
            revision_sha=result.revision.sha,
            test_node_id=result.test_node_id,
            expected_artifact_sha256=result.expected_artifact_sha256,
            artifact_sha256_before=result.artifact_sha256_before,
            artifact_sha256_after=result.artifact_sha256_after,
            status=result.status,
            collected_count=result.collected_count,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            detail=result.detail,
        )

    def to_domain(self) -> ExecutionResult:
        return ExecutionResult(
            revision=Revision(role=self.role, sha=self.revision_sha),
            test_node_id=self.test_node_id,
            expected_artifact_sha256=self.expected_artifact_sha256,
            artifact_sha256_before=self.artifact_sha256_before,
            artifact_sha256_after=self.artifact_sha256_after,
            status=self.status,
            collected_count=self.collected_count,
            exit_code=self.exit_code,
            duration_seconds=self.duration_seconds,
            stdout=self.stdout,
            stderr=self.stderr,
            detail=self.detail,
        )


class ExecutorChallengeResponse(BaseModel):
    """Complete paired result produced inside one ephemeral executor instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_path: str
    node_id: str
    artifact_source: str
    artifact_sha256: str
    base: ExecutionResultDocument
    head: ExecutionResultDocument
    mechanical_status: MechanicalEvidenceStatus
    differential_pattern: DifferentialPattern
    mechanical_reason: str

    @classmethod
    def from_domain(cls, result: ChallengeResult) -> ExecutorChallengeResponse:
        return cls(
            artifact_path=result.artifact.relative_path,
            node_id=result.artifact.node_id,
            artifact_source=result.artifact.content.decode("utf-8"),
            artifact_sha256=result.artifact.sha256,
            base=ExecutionResultDocument.from_domain(result.base),
            head=ExecutionResultDocument.from_domain(result.head),
            mechanical_status=result.assessment.mechanical_status,
            differential_pattern=result.assessment.pattern,
            mechanical_reason=result.assessment.reason,
        )

    def to_domain(self) -> ChallengeResult:
        artifact = TestArtifact.from_text(
            relative_path=self.artifact_path,
            node_id=self.node_id,
            content=self.artifact_source,
        )
        if artifact.sha256 != self.artifact_sha256:
            raise ValueError("executor response artifact hash is inconsistent")
        return ChallengeResult(
            artifact=artifact,
            base=self.base.to_domain(),
            head=self.head.to_domain(),
            assessment=EvidenceAssessment(
                mechanical_status=self.mechanical_status,
                pattern=self.differential_pattern,
                reason=self.mechanical_reason,
            ),
        )


class ExecutorEnvironmentResponse(BaseModel):
    """Stable setup-readiness result without candidate or process-log content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnvironmentReadinessStatus
    reason: str = Field(min_length=1, max_length=2_000)

    @classmethod
    def from_domain(cls, result: EnvironmentReadiness) -> ExecutorEnvironmentResponse:
        return cls(status=result.status, reason=result.reason)

    def to_domain(self) -> EnvironmentReadiness:
        return EnvironmentReadiness(status=self.status, reason=self.reason)


class ChallengeExecutor(Protocol):
    """Injectable private execution boundary."""

    def prepare_environment(
        self, request: ExecutorEnvironmentRequest
    ) -> ExecutorEnvironmentResponse: ...

    def execute(self, request: ExecutorChallengeRequest) -> ExecutorChallengeResponse: ...


class EphemeralChallengeExecutor:
    """Fetch an allowlisted public PR and execute it in disposable worktrees."""

    def __init__(
        self,
        *,
        allowed_repositories: frozenset[str],
        git_timeout_seconds: float = 120.0,
    ) -> None:
        self.allowed_repositories = frozenset(
            normalize_repository_name(value) for value in allowed_repositories
        )
        if not self.allowed_repositories:
            raise ValueError("executor requires at least one allowlisted repository")
        if git_timeout_seconds <= 0:
            raise ValueError("Git timeout must be positive")
        self.git_timeout_seconds = git_timeout_seconds

    def execute(self, request: ExecutorChallengeRequest) -> ExecutorChallengeResponse:
        if request.repository not in self.allowed_repositories:
            raise PermissionError("repository is not allowlisted")
        artifact = request.artifact()
        if not request.contract.permits_test_path(artifact.relative_path):
            raise ValueError("candidate path is not permitted by the execution contract")

        with tempfile.TemporaryDirectory(prefix="patchproof-executor-") as temporary:
            root = Path(temporary)
            challenge = self._prepare_challenge(request=request, root=root)
            return ExecutorChallengeResponse.from_domain(
                challenge.run(
                    base_ref=request.base_sha,
                    head_ref=request.head_sha,
                    artifact=artifact,
                )
            )

    def prepare_environment(
        self, request: ExecutorEnvironmentRequest
    ) -> ExecutorEnvironmentResponse:
        if request.repository not in self.allowed_repositories:
            raise PermissionError("repository is not allowlisted")
        with tempfile.TemporaryDirectory(prefix="patchproof-executor-") as temporary:
            challenge = self._prepare_challenge(request=request, root=Path(temporary))
            return ExecutorEnvironmentResponse.from_domain(
                challenge.prepare_environment(
                    base_ref=request.base_sha,
                    head_ref=request.head_sha,
                )
            )

    def _prepare_challenge(
        self, *, request: ExecutorEnvironmentRequest, root: Path
    ) -> BaseHeadChallenge:
        repository = root / "repository.git"
        self._prepare_repository(request=request, destination=repository)
        loader = ExecutionContractLoader()
        base_contract = loader.load_bytes(
            self._git_output(repository, ("show", f"{request.base_sha}:{loader.filename}"))
        )
        head_contract = loader.load_bytes(
            self._git_output(repository, ("show", f"{request.head_sha}:{loader.filename}"))
        )
        if base_contract != head_contract or head_contract != request.contract:
            raise ValueError("immutable BASE/HEAD execution contracts do not match")
        return BaseHeadChallenge(
            workspaces=GitWorkspaceManager(
                source_repository=repository,
                workspace_root=root / "worktrees",
            ),
            runner=PytestRunner(
                contract=head_contract,
                python_executable=Path(sys.executable),
                install_dependencies=True,
            ),
        )

    def _prepare_repository(
        self, *, request: ExecutorEnvironmentRequest, destination: Path
    ) -> None:
        destination.mkdir()
        self._run(("git", "-C", str(destination), "init", "--bare"))
        url = f"https://github.com/{request.repository}.git"
        self._run(("git", "-C", str(destination), "remote", "add", "origin", url))
        pull_ref = f"+refs/pull/{request.pull_request_number}/head:refs/patchproof/head"
        self._run(("git", "-C", str(destination), "fetch", "--no-tags", "origin", pull_ref))
        self._run(
            (
                "git",
                "-C",
                str(destination),
                "fetch",
                "--no-tags",
                "origin",
                request.base_sha,
            )
        )
        resolved = self._git_output(destination, ("rev-parse", "refs/patchproof/head")).decode()
        if resolved.strip().lower() != request.head_sha:
            raise ValueError("fetched pull-request HEAD does not match the webhook SHA")
        self._git_output(destination, ("cat-file", "-e", f"{request.base_sha}^{{commit}}"))

    def _git_output(self, repository: Path, arguments: tuple[str, ...]) -> bytes:
        return self._run(("git", "-C", str(repository), *arguments)).stdout

    def _run(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=self.git_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("bounded Git preparation failed") from error
        if completed.returncode != 0:
            raise RuntimeError(
                "Git preparation failed: "
                + completed.stderr.decode("utf-8", errors="replace")[:1_000]
            )
        return completed


def create_executor_app(*, executor: ChallengeExecutor) -> FastAPI:
    """Create the private executor service; Cloud Run IAM is its authentication layer."""
    app = FastAPI(title="PatchProof Executor", version="0.1.0")
    app.state.executor = executor

    @app.get("/healthz")
    @app.get("/livez")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/challenge", response_model=ExecutorChallengeResponse)
    async def challenge(request: ExecutorChallengeRequest) -> ExecutorChallengeResponse:
        try:
            return executor.execute(request)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=503, detail="executor failed closed") from error

    @app.post("/internal/environment-readiness", response_model=ExecutorEnvironmentResponse)
    async def environment_readiness(
        request: ExecutorEnvironmentRequest,
    ) -> ExecutorEnvironmentResponse:
        try:
            return executor.prepare_environment(request)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=503, detail="executor failed closed") from error

    return app


def artifact_digest(source: str) -> str:
    """Expose the exact UTF-8 digest operation used by the wire contract."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
