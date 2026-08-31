"""Google Cloud control-plane composition, task authentication, and remote execution."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import patchproof.hard_mode as hard_mode
from patchproof.adk_claim_agent import DEFAULT_CLAIM_MODEL, AdkGeminiClaimModel
from patchproof.adk_evidence_assessor import AdkGeminiEvidenceAssessor
from patchproof.adk_test_agent import AdkGeminiCandidateModel
from patchproof.claim_agent import BehavioralClaimAgent
from patchproof.cloud_executor import (
    ExecutionContractOrigin,
    ExecutorChallengeRequest,
    ExecutorChallengeResponse,
    ExecutorEnvironmentRequest,
    ExecutorEnvironmentResponse,
)
from patchproof.cloud_tasks import CloudTasksDispatcherSettings, CloudTasksRunDispatcher
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.control_plane import ControlPlaneSettings, _read_bounded_body, create_app
from patchproof.dashboard import DashboardRun, StoreDashboardSnapshotProvider, install_dashboard
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
from patchproof.models import ChallengeResult, EnvironmentReadiness, ExecutionResult, TestArtifact
from patchproof.pr_analyze import PrAnalyzeError, find_known_pr, find_project_root, parse_pr_url
from patchproof.reliable_worker import EvidenceWorkerError, ReliableEvidenceWorker
from patchproof.storage import RunNotFoundError, VerificationRunStore
from patchproof.workflow import (
    PublicationState,
    PullRequestEvent,
    RevisionState,
    RunLifecycle,
    RunTransition,
    normalize_repository_name,
)


class RunTaskRequest(BaseModel):
    """The only user data persisted in a Cloud Task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID


class AnalyzeRequest(BaseModel):
    """Bounded public request for one reproducible onboarded pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_url: str = Field(min_length=1, max_length=300)


class AnalyzeResponse(BaseModel):
    """Fast acknowledgement for one durable asynchronous cloud run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str
    pr_url: str
    dashboard_url: str
    result_url: str


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


@dataclass(frozen=True, slots=True)
class _RemoteChallengeSession:
    """Bind immutable revision refs while retaining the remote executor boundary."""

    challenge: RemoteBaseHeadChallenge
    base_ref: str
    head_ref: str

    def prepare_environment(self) -> EnvironmentReadiness:
        return self.challenge.prepare_environment(base_ref=self.base_ref, head_ref=self.head_ref)

    def run(
        self,
        *,
        artifact: TestArtifact,
        on_base_complete: Callable[[ExecutionResult], None] | None = None,
    ) -> ChallengeResult:
        return self.challenge.run(
            base_ref=self.base_ref,
            head_ref=self.head_ref,
            artifact=artifact,
            on_base_complete=on_base_complete,
        )

    def installed_import_roots(self) -> frozenset[str]:
        # The credentialed control plane does not inspect the executor's environment.
        # Empty preserves the validator's context-derived import grounding.
        return frozenset()


class TrustedHistoricalContextRetriever(DeterministicContextRetriever):
    """Expose one probed contract without pretending it was committed upstream."""

    def __init__(
        self,
        *,
        source_repository: Path,
        excluded_paths: frozenset[str],
        contract: ExecutionContract,
        revisions: frozenset[str],
    ) -> None:
        super().__init__(
            source_repository=source_repository,
            excluded_paths=excluded_paths,
        )
        if not contract.synthesized or len(revisions) != 2:
            raise ValueError("historical context requires one synthesized two-revision contract")
        self._trusted_contract = contract
        self._trusted_revisions = revisions

    def read_committed_file(
        self,
        *,
        revision_sha: str,
        path: str,
        max_bytes: int = 8_192,
    ) -> bytes:
        if path != ExecutionContractLoader.filename:
            return super().read_committed_file(
                revision_sha=revision_sha,
                path=path,
                max_bytes=max_bytes,
            )
        if revision_sha not in self._trusted_revisions:
            raise ValueError("trusted contract requested for an unexpected revision")
        content = self._trusted_contract.model_dump_json().encode("utf-8")
        if len(content) > max_bytes:
            raise ValueError("trusted execution contract exceeds its byte budget")
        return content


class RemoteBaseHeadChallenge:
    """EvidenceWorkflow-compatible adapter for the credentialless private executor."""

    def __init__(
        self,
        *,
        repository: str,
        pull_request_number: int,
        contract: ExecutionContract,
        contract_origin: ExecutionContractOrigin = ExecutionContractOrigin.REPOSITORY,
        repository_python_paths: tuple[str, ...] = (),
        executor_url: str,
        tokens: ExecutorIdentityTokenProvider,
        client: httpx.Client | None = None,
    ) -> None:
        self.repository = normalize_repository_name(repository)
        self.pull_request_number = pull_request_number
        self.contract_origin = contract_origin
        self.repository_python_paths = repository_python_paths
        self.executor_url = executor_url.rstrip("/")
        if not self.executor_url.startswith("https://"):
            raise ValueError("executor URL must use HTTPS")
        self.tokens = tokens
        self.client = client or httpx.Client(timeout=950.0)
        self.runner = _RemoteRunnerContract(contract=contract)

    @contextmanager
    def session(self, *, base_ref: str, head_ref: str) -> Iterator[_RemoteChallengeSession]:
        """Expose the session interface without moving execution into the control plane."""
        yield _RemoteChallengeSession(self, base_ref, head_ref)

    def prepare_environment(self, *, base_ref: str, head_ref: str) -> EnvironmentReadiness:
        request = ExecutorEnvironmentRequest(
            repository=self.repository,
            pull_request_number=self.pull_request_number,
            base_sha=base_ref,
            head_sha=head_ref,
            contract=self.runner.contract,
            contract_origin=self.contract_origin,
            repository_python_paths=self.repository_python_paths,
        )
        token = self.tokens.token(self.executor_url)
        try:
            response = self.client.post(
                f"{self.executor_url}/internal/environment-readiness",
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
        return ExecutorEnvironmentResponse.model_validate_json(response.content).to_domain()

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
            contract_origin=self.contract_origin,
            repository_python_paths=self.repository_python_paths,
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
        project_root: Path,
        model_name: str = DEFAULT_CLAIM_MODEL,
        provider_config: GeminiProviderConfig | None = None,
        executor_tokens: ExecutorIdentityTokenProvider | None = None,
    ) -> None:
        self.store = store
        self.executor_url = executor_url
        self.project_root = project_root.resolve()
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
            if run.installation_id is not None:
                self.publisher.publish(run_id)
            else:
                self._finish_without_github_check(run_id)
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
            contract_origin = ExecutionContractOrigin.REPOSITORY
            repository_python_paths: tuple[str, ...] = ()
            if run.event_action == "patchproof_analyze":
                try:
                    known = find_known_pr(
                        parse_pr_url(f"https://github.com/{run.repository}/pull/{run.pr_number}"),
                        project_root=self.project_root,
                    )
                except PrAnalyzeError as error:
                    raise RuntimeError(
                        "durable product run is not a committed known case"
                    ) from error
                case = known.case
                if (case.base_sha, case.head_sha) != (run.base_sha, run.head_sha):
                    raise RuntimeError("durable product run revisions differ from known metadata")
                execution_plan = hard_mode._execution_plan(repository, case)
                contract = execution_plan.contract
                contract_origin = ExecutionContractOrigin.PATCHPROOF_MANIFEST
                repository_python_paths = case.repository_python_paths
                context = TrustedHistoricalContextRetriever(
                    source_repository=repository,
                    excluded_paths=frozenset(case.excluded_paths),
                    contract=contract,
                    revisions=frozenset({run.base_sha, run.head_sha}),
                )
            else:
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
                contract_origin=contract_origin,
                repository_python_paths=repository_python_paths,
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
        if run.installation_id is not None:
            self.publisher.publish(run_id)
        else:
            self._finish_without_github_check(run_id)

    def _finish_without_github_check(self, run_id: UUID) -> None:
        run = self.store.get_run(run_id)
        if run.publication_state is PublicationState.PENDING:
            self.store.transition_run(
                run_id=run_id,
                expected_version=run.version,
                transition=RunTransition(publication_state=PublicationState.NOT_APPLICABLE),
            )

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
    dashboard_recent_limit: int = 8
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
            dashboard_recent_limit=int(os.environ.get("PATCHPROOF_DASHBOARD_RECENT_LIMIT", "8")),
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
    project_root: Path | None = None,
) -> FastAPI:
    """Create the public webhook plus OIDC-protected task callback application."""
    root = (project_root or find_project_root()).resolve()
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
        project_root=root,
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
    dashboard_provider = StoreDashboardSnapshotProvider(
        store=resolved_store,
        run_ids=settings.dashboard_run_ids,
        max_runs=settings.dashboard_recent_limit,
    )
    install_dashboard(
        app,
        provider=dashboard_provider,
    )

    @app.post(
        "/api/analyze",
        response_model=AnalyzeResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def analyze(request: Request) -> AnalyzeResponse:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
            "application/json"
        ):
            raise HTTPException(status_code=415, detail="Content-Type must be application/json")
        body = await _read_bounded_body(request, maximum_bytes=1_024)
        try:
            payload = AnalyzeRequest.model_validate_json(body)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail="invalid cloud analyze request") from error
        try:
            parsed = parse_pr_url(payload.pr_url)
        except PrAnalyzeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            known = find_known_pr(parsed, project_root=root)
        except PrAnalyzeError as error:
            raise HTTPException(
                status_code=422,
                detail=(
                    "This PR is not in the currently onboarded reproducible case set. "
                    "Arbitrary public PR analysis is not enabled yet."
                ),
            ) from error
        case = known.case
        if case.repository.lower() not in settings.allowed_repositories:
            raise HTTPException(
                status_code=503,
                detail="This onboarded repository is not enabled in the current deployment.",
            )
        assert case.pull_request_number is not None and case.merged_at is not None
        identity = f"{case.repository}:{case.pull_request_number}:{case.base_sha}:{case.head_sha}"
        delivery_id = "product-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        acceptance = resolved_store.accept_pull_request(
            PullRequestEvent(
                delivery_id=delivery_id,
                action="patchproof_analyze",
                repository=case.repository,
                pr_number=case.pull_request_number,
                base_sha=case.base_sha,
                head_sha=case.head_sha,
                head_updated_at=case.merged_at.astimezone(UTC),
                title=case.title,
                body=case.body,
                installation_id=None,
            )
        )
        run = acceptance.run
        if (
            run.revision_state is RevisionState.CURRENT
            and run.lifecycle is not RunLifecycle.TERMINAL
        ):
            try:
                resolved_dispatcher.dispatch(run.run_id)
            except Exception as error:
                raise HTTPException(
                    status_code=503,
                    detail="Verification dispatch is temporarily unavailable.",
                ) from error
        return AnalyzeResponse(
            run_id=run.run_id,
            status=str(run.lifecycle),
            pr_url=parsed.url,
            dashboard_url=f"{settings.control_url}/dashboard?run={run.run_id}",
            result_url=f"{settings.control_url}/api/runs/{run.run_id}",
        )

    @app.get("/api/runs/{run_id}", response_model=DashboardRun)
    async def run_status(run_id: UUID) -> DashboardRun:
        try:
            return dashboard_provider.project_run(run_id)
        except RunNotFoundError as error:
            raise HTTPException(status_code=404, detail="verification run not found") from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(
                status_code=503,
                detail="verification evidence is temporarily unavailable",
            ) from error

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
        except Exception as error:
            failure = ReliableEvidenceWorker.classify(error)
            run = resolved_store.get_run(payload.run_id)
            if (
                run.revision_state is RevisionState.CURRENT
                and run.lifecycle is not RunLifecycle.TERMINAL
            ):
                resolved_store.fail_run(
                    run_id=run.run_id,
                    error_code=failure.code,
                    summary=failure.summary,
                    retryable=failure.retryable,
                    model_usage=failure.model_usage,
                    raw_response_sha256=failure.raw_response_sha256,
                )
            return {"status": "terminal"}
        return {"status": "completed"}

    return app
