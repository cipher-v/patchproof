"""Google Cloud control-plane composition, task authentication, and remote execution."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict

from patchproof.adk_claim_agent import DEFAULT_CLAIM_MODEL, AdkGeminiClaimModel
from patchproof.adk_evidence_assessor import AdkGeminiEvidenceAssessor
from patchproof.adk_test_agent import AdkGeminiCandidateModel
from patchproof.claim_agent import BehavioralClaimAgent
from patchproof.cloud_executor import (
    ExecutorChallengeRequest,
    ExecutorChallengeResponse,
)
from patchproof.cloud_tasks import CloudTasksDispatcherSettings, CloudTasksRunDispatcher
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.control_plane import ControlPlaneSettings, create_app
from patchproof.dashboard import StoreDashboardSnapshotProvider, install_dashboard
from patchproof.evidence_workflow import EvidenceWorkflow
from patchproof.execution_contract import ExecutionContract, ExecutionContractLoader
from patchproof.firestore_store import FirestoreVerificationRunStore
from patchproof.gemini_provider import (
    GeminiProviderConfig,
    GeminiProviderSurface,
    preflight_vertex_authentication,
)
from patchproof.github_checks import (
    GitHubAppInstallationTokenProvider,
    GitHubCheckPublisher,
    GitHubCheckRetryableError,
    GitHubChecksClient,
    GitHubCheckTerminalError,
)
from patchproof.models import ChallengeResult, ExecutionResult, TestArtifact
from patchproof.reliable_worker import EvidenceWorkerError, ReliableEvidenceWorker
from patchproof.storage import VerificationRunStore
from patchproof.workflow import normalize_repository_name


class RunTaskRequest(BaseModel):
    """The only user data persisted in a Cloud Task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID


class TaskIdentityVerifier(Protocol):
    """Verify the application-layer identity on the public task callback route."""

    def verify(self, authorization: str | None) -> None: ...


class GoogleTaskIdentityVerifier:
    """Validate a Google-signed OIDC token and its dedicated service-account email."""

    def __init__(self, *, audience: str, service_account_email: str) -> None:
        if not audience.startswith("https://") or "@" not in service_account_email:
            raise ValueError("task OIDC audience and service-account email are required")
        self.audience = audience
        self.service_account_email = service_account_email

    def verify(self, authorization: str | None) -> None:
        prefix = "Bearer "
        if authorization is None or not authorization.startswith(prefix):
            raise PermissionError("missing Cloud Tasks bearer token")
        token = authorization[len(prefix) :].strip()
        if not token:
            raise PermissionError("missing Cloud Tasks bearer token")
        try:
            claims = id_token.verify_oauth2_token(
                token,
                GoogleAuthRequest(),
                audience=self.audience,
            )
        except (ValueError, TypeError) as error:
            raise PermissionError("invalid Cloud Tasks identity token") from error
        if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
            raise PermissionError("invalid Cloud Tasks token issuer")
        if claims.get("email") != self.service_account_email:
            raise PermissionError("unexpected Cloud Tasks service account")
        if claims.get("email_verified") not in {True, "true"}:
            raise PermissionError("Cloud Tasks service-account email is not verified")


class ExecutorIdentityTokenProvider(Protocol):
    """Mint a short-lived identity token for the private executor service."""

    def token(self, audience: str) -> str: ...


class GoogleExecutorIdentityTokenProvider:
    """Use Application Default Credentials to mint the control-plane caller token."""

    def token(self, audience: str) -> str:
        return id_token.fetch_id_token(GoogleAuthRequest(), audience)


@dataclass(frozen=True, slots=True)
class _RemoteRunnerContract:
    contract: ExecutionContract
    install_dependencies: bool = True


class RemoteBaseHeadChallenge:
    """EvidenceWorkflow-compatible adapter for the credentialless private executor."""

    def __init__(
        self,
        *,
        repository: str,
        pull_request_number: int,
        contract: ExecutionContract,
        executor_url: str,
        tokens: ExecutorIdentityTokenProvider,
        client: httpx.Client | None = None,
    ) -> None:
        self.repository = normalize_repository_name(repository)
        self.pull_request_number = pull_request_number
        self.executor_url = executor_url.rstrip("/")
        if not self.executor_url.startswith("https://"):
            raise ValueError("executor URL must use HTTPS")
        self.tokens = tokens
        self.client = client or httpx.Client(timeout=950.0)
        self.runner = _RemoteRunnerContract(contract=contract)

    def run(
        self,
        *,
        base_ref: str,
        head_ref: str,
        artifact: TestArtifact,
        on_base_complete: Callable[[ExecutionResult], None] | None = None,
    ) -> ChallengeResult:
        request = ExecutorChallengeRequest(
            repository=self.repository,
            pull_request_number=self.pull_request_number,
            base_sha=base_ref,
            head_sha=head_ref,
            contract=self.runner.contract,
            artifact_path=artifact.relative_path,
            node_id=artifact.node_id,
            artifact_source=artifact.content.decode("utf-8"),
            artifact_sha256=artifact.sha256,
        )
        token = self.tokens.token(self.executor_url)
        try:
            response = self.client.post(
                f"{self.executor_url}/internal/challenge",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                content=request.model_dump_json(),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise RuntimeError("private executor is temporarily unavailable") from error
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"private executor returned HTTP {response.status_code}")
        document = ExecutorChallengeResponse.model_validate_json(response.content)
        result = document.to_domain()
        if result.artifact.sha256 != artifact.sha256:
            raise RuntimeError("private executor returned a different artifact")
        if on_base_complete is not None:
            on_base_complete(result.base)
        return result


class RunTaskProcessor(Protocol):
    """Process one durable run and publish its stored result."""

    async def process(self, run_id: UUID) -> None: ...


class CloudRunTaskProcessor:
    """Compose the real ADK workflow while keeping execution in the private service."""

    def __init__(
        self,
        *,
        store: VerificationRunStore,
        executor_url: str,
        allowed_repositories: frozenset[str],
        github_app_id: int,
        github_private_key: str,
        model_name: str = DEFAULT_CLAIM_MODEL,
        provider_config: GeminiProviderConfig | None = None,
        executor_tokens: ExecutorIdentityTokenProvider | None = None,
    ) -> None:
        self.store = store
        self.executor_url = executor_url
        self.allowed_repositories = frozenset(
            normalize_repository_name(value) for value in allowed_repositories
        )
        self.model_name = model_name
        self.provider_config = provider_config or GeminiProviderConfig.developer_api()
        if self.provider_config.provider_surface is GeminiProviderSurface.VERTEX_AI:
            preflight_vertex_authentication(self.provider_config)
        self.executor_tokens = executor_tokens or GoogleExecutorIdentityTokenProvider()
        tokens = GitHubAppInstallationTokenProvider(
            app_id=github_app_id,
            private_key_pem=github_private_key,
        )
        self.publisher = GitHubCheckPublisher(
            store=store,
            client=GitHubChecksClient(tokens=tokens),
        )

    async def process(self, run_id: UUID) -> None:
        run = self.store.get_run(run_id)
        if run.repository not in self.allowed_repositories:
            raise PermissionError("durable run repository is not allowlisted")
        if self.store.get_evidence(run_id) is not None:
            self.publisher.publish(run_id)
            return
        with tempfile.TemporaryDirectory(prefix="patchproof-control-") as temporary:
            repository = Path(temporary) / "repository.git"
            self._prepare_repository(
                run.repository,
                run.pr_number,
                run.base_sha,
                run.head_sha,
                repository,
            )
            context = DeterministicContextRetriever(source_repository=repository)
            loader = ExecutionContractLoader()
            contract = loader.load_bytes(
                context.read_committed_file(
                    revision_sha=run.head_sha,
                    path=loader.filename,
                )
            )
            challenge = RemoteBaseHeadChallenge(
                repository=run.repository,
                pull_request_number=run.pr_number,
                contract=contract,
                executor_url=self.executor_url,
                tokens=self.executor_tokens,
            )
            workflow = EvidenceWorkflow(
                store=self.store,
                context_retriever=context,
                claim_agent=BehavioralClaimAgent(
                    model=AdkGeminiClaimModel(
                        model_name=self.model_name,
                        provider_config=self.provider_config,
                    )
                ),
                candidate_model=AdkGeminiCandidateModel(
                    model_name=self.model_name,
                    provider_config=self.provider_config,
                ),
                challenge=challenge,
                assessor=AdkGeminiEvidenceAssessor(
                    model_name=self.model_name,
                    provider_config=self.provider_config,
                ),
            )
            await ReliableEvidenceWorker(workflow=workflow, store=self.store).run(run_id)
        self.publisher.publish(run_id)

    @staticmethod
    def _prepare_repository(
        repository: str,
        pr_number: int,
        base_sha: str,
        expected_head: str,
        destination: Path,
    ) -> None:
        destination.mkdir()
        commands = (
            ("git", "-C", str(destination), "init", "--bare"),
            (
                "git",
                "-C",
                str(destination),
                "remote",
                "add",
                "origin",
                f"https://github.com/{repository}.git",
            ),
            (
                "git",
                "-C",
                str(destination),
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/pull/{pr_number}/head:refs/patchproof/head",
            ),
            (
                "git",
                "-C",
                str(destination),
                "fetch",
                "--no-tags",
                "origin",
                base_sha,
            ),
            ("git", "-C", str(destination), "rev-parse", "refs/patchproof/head"),
        )
        completed: subprocess.CompletedProcess[str] | None = None
        for command in commands:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("immutable public pull-request fetch failed")
        assert completed is not None
        if completed.stdout.strip().lower() != expected_head:
            raise RuntimeError("fetched pull-request HEAD differs from the durable run")


@dataclass(frozen=True, slots=True)
class CloudControlSettings:
    """Required environment configuration for the deployed control service."""

    project_id: str
    region: str
    queue: str
    control_url: str
    executor_url: str
    task_invoker_email: str
    webhook_secret: bytes
    allowed_repositories: frozenset[str]
    github_app_id: int
    github_private_key: str
    dashboard_run_ids: tuple[UUID, ...] = ()
    firestore_namespace: str = "patchproof"
    model_name: str = DEFAULT_CLAIM_MODEL
    gemini_provider: GeminiProviderConfig = field(
        default_factory=GeminiProviderConfig.developer_api
    )

    @classmethod
    def from_environment(cls) -> CloudControlSettings:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"required environment variable is missing: {name}")
            return value

        return cls(
            project_id=required("GOOGLE_CLOUD_PROJECT"),
            region=required("PATCHPROOF_REGION"),
            queue=required("PATCHPROOF_TASK_QUEUE"),
            control_url=required("PATCHPROOF_CONTROL_URL").rstrip("/"),
            executor_url=required("PATCHPROOF_EXECUTOR_URL").rstrip("/"),
            task_invoker_email=required("PATCHPROOF_TASK_INVOKER_EMAIL"),
            webhook_secret=required("PATCHPROOF_WEBHOOK_SECRET").encode(),
            allowed_repositories=frozenset(
                item.strip()
                for item in required("PATCHPROOF_ALLOWED_REPOSITORIES").split(",")
                if item.strip()
            ),
            github_app_id=int(required("PATCHPROOF_GITHUB_APP_ID")),
            github_private_key=required("PATCHPROOF_GITHUB_PRIVATE_KEY"),
            dashboard_run_ids=tuple(
                UUID(item.strip())
                for item in os.environ.get("PATCHPROOF_DASHBOARD_RUN_IDS", "").split(",")
                if item.strip()
            ),
            firestore_namespace=os.environ.get("PATCHPROOF_FIRESTORE_NAMESPACE", "patchproof"),
            model_name=os.environ.get("PATCHPROOF_GEMINI_MODEL", DEFAULT_CLAIM_MODEL),
            gemini_provider=GeminiProviderConfig.from_environment(),
        )


def create_cloud_control_app(
    *,
    settings: CloudControlSettings,
    store: VerificationRunStore | None = None,
    dispatcher: CloudTasksRunDispatcher | None = None,
    processor: RunTaskProcessor | None = None,
    verifier: TaskIdentityVerifier | None = None,
) -> FastAPI:
    """Create the public webhook plus OIDC-protected task callback application."""
    resolved_store = store or FirestoreVerificationRunStore(
        client=firestore.Client(project=settings.project_id),
        namespace=settings.firestore_namespace,
    )
    resolved_dispatcher = dispatcher or CloudTasksRunDispatcher(
        settings=CloudTasksDispatcherSettings(
            project_id=settings.project_id,
            location=settings.region,
            queue=settings.queue,
            target_url=f"{settings.control_url}/tasks/verify",
            service_account_email=settings.task_invoker_email,
            audience=settings.control_url,
        )
    )
    resolved_processor = processor or CloudRunTaskProcessor(
        store=resolved_store,
        executor_url=settings.executor_url,
        allowed_repositories=settings.allowed_repositories,
        github_app_id=settings.github_app_id,
        github_private_key=settings.github_private_key,
        model_name=settings.model_name,
        provider_config=settings.gemini_provider,
    )
    resolved_verifier = verifier or GoogleTaskIdentityVerifier(
        audience=settings.control_url,
        service_account_email=settings.task_invoker_email,
    )
    app = create_app(
        settings=ControlPlaneSettings(
            webhook_secret=settings.webhook_secret,
            allowed_repositories=settings.allowed_repositories,
            database_path=Path("/tmp/unused-cloud-control.db"),
        ),
        store=resolved_store,
        dispatcher=resolved_dispatcher,
    )
    install_dashboard(
        app,
        provider=StoreDashboardSnapshotProvider(
            store=resolved_store,
            run_ids=settings.dashboard_run_ids,
        ),
    )

    @app.post("/tasks/verify")
    async def verify_task(payload: RunTaskRequest, request: Request) -> dict[str, str]:
        try:
            resolved_verifier.verify(request.headers.get("authorization"))
        except PermissionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        try:
            await resolved_processor.process(payload.run_id)
        except GitHubCheckRetryableError as error:
            raise HTTPException(status_code=503, detail="publication retry requested") from error
        except (EvidenceWorkerError, GitHubCheckTerminalError, PermissionError):
            # These paths are already represented durably or cannot become valid by retrying.
            return {"status": "terminal"}
        return {"status": "completed"}

    return app
