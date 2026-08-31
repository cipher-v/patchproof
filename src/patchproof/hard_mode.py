"""Blind, exactly-once hard-mode evaluation for PatchProof's live Gemini workflow."""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from statistics import fmean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchproof.adk_claim_agent import AdkGeminiClaimModel
from patchproof.adk_evidence_assessor import AdkGeminiEvidenceAssessor
from patchproof.adk_test_agent import AdkGeminiCandidateModel
from patchproof.challenge import BaseHeadChallenge, ChallengeSession
from patchproof.claim_agent import (
    BehavioralClaimAgent,
    InvalidClaimAgentOutput,
    PullRequestNarrative,
)
from patchproof.claim_investigator import ClaimInvestigatorFactory
from patchproof.context_retrieval import DeterministicContextRetriever, PullRequestContext
from patchproof.evidence_workflow import EvidenceWorkflow
from patchproof.execution_contract import ExecutionContract, TestCommandContract
from patchproof.gemini_provider import (
    GeminiProviderConfig,
    GeminiProviderSurface,
    preflight_vertex_authentication,
)
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.install_strategy import (
    ContractSynthesisError,
    DependencyInstallProber,
    InstallPlan,
    resolve_contract_for_pair,
)
from patchproof.model_reliability import (
    BoundedRetryingEvidenceAssessor,
    BoundedRetryingModel,
    ModelInvocationFailure,
)
from patchproof.models import (
    ChallengeResult,
    ClaimOutcome,
    DifferentialPattern,
    MechanicalEvidenceStatus,
    TestArtifact,
    TestExecutionStatus,
)
from patchproof.pytest_runner import PytestRunner
from patchproof.reasoning_budget import AgentTask, budget_for
from patchproof.test_generation import (
    _MAX_CANDIDATE_MODEL_CALLS,
    _MAX_REPAIRS,
    BoundedCandidateTestGenerator,
    CandidateAttempt,
    CandidateTestValidator,
)

_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CASE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MAX_MANIFEST_BYTES = 256_000
_ORACLE_TARGET = "patchproof_generated_tests/test_hard_mode_oracle.py"


class HardModeConfigurationError(ValueError):
    """Raised when a frozen case, oracle, or gate fails deterministic validation."""


class HardModeInfrastructureError(RuntimeError):
    """Raised when immutable repository preparation or execution cannot complete."""


class HardModeCaseKind(StrEnum):
    HISTORICAL_PR = "HISTORICAL_PR"
    LOCAL_SYNTHETIC = "LOCAL_SYNTHETIC"


class HardModePacingPolicy(StrEnum):
    """Predeclared pacing applied uniformly between completed cases."""

    NONE = "NONE"
    BETWEEN_CASES = "BETWEEN_CASES"


class HardModeProtocol(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    declared_run_id: str = Field(min_length=1, max_length=200)
    model_name: str = Field(pattern=r"gemini-\d+\.\d+-[a-z0-9.-]+")
    provider_surface: GeminiProviderSurface | None = None
    temperature: float
    thinking_level: Literal["LOW"]
    claim_calls_per_case: Literal[1]
    candidate_calls_per_case: Literal[2, 3]
    assessment_calls_per_discriminating_case: Literal[1]
    transient_provider_retries_per_logical_call: Literal[0, 1]
    maximum_possible_logical_model_calls: int | None = Field(default=None, ge=1, le=1_000_000)
    maximum_possible_provider_calls: int | None = Field(default=None, ge=1, le=2_000_000)
    declared_available_provider_calls: int | None = Field(default=None, ge=1, le=1_000_000)
    model_call_budget_preflight_passed: bool | None = None
    narrative_policy: str = Field(min_length=1, max_length=1_000)
    withholding_policy: str = Field(min_length=1, max_length=1_000)
    pacing_policy: HardModePacingPolicy = HardModePacingPolicy.NONE
    inter_case_delay_seconds: float = Field(default=0.0, ge=0.0, le=300.0)

    @model_validator(mode="after")
    def validate_pacing(self) -> HardModeProtocol:
        if (
            self.pacing_policy is HardModePacingPolicy.NONE and self.inter_case_delay_seconds != 0
        ) or (
            self.pacing_policy is HardModePacingPolicy.BETWEEN_CASES
            and self.inter_case_delay_seconds <= 0
        ):
            raise ValueError("hard-mode pacing policy and delay are inconsistent")
        budget_values = (
            self.maximum_possible_logical_model_calls,
            self.maximum_possible_provider_calls,
            self.declared_available_provider_calls,
            self.model_call_budget_preflight_passed,
        )
        if any(value is not None for value in budget_values) and any(
            value is None for value in budget_values
        ):
            raise ValueError("hard-mode model-call budget fields must be declared together")
        if self.maximum_possible_provider_calls is not None:
            expected_pass = (
                self.declared_available_provider_calls >= self.maximum_possible_provider_calls
            )
            if self.model_call_budget_preflight_passed is not expected_pass:
                raise ValueError(
                    "hard-mode model-call budget pass/fail declaration is inconsistent"
                )
        return self

    def derive_maximum_possible_logical_model_calls(self, *, case_count: int) -> int:
        """Derive the maximum semantic PatchProof tasks for a complete run."""
        if case_count <= 0:
            raise ValueError("hard-mode model-call budget requires at least one case")
        calls_per_case = (
            self.claim_calls_per_case
            + self.candidate_calls_per_case
            + self.assessment_calls_per_discriminating_case
        )
        return case_count * calls_per_case

    def maximum_provider_attempts_per_logical_call(self) -> int:
        """Include the initial request and every permitted transient retry."""
        return 1 + self.transient_provider_retries_per_logical_call

    def derive_maximum_possible_provider_calls(self, *, case_count: int) -> int:
        """Derive actual provider-request capacity needed in the worst case."""
        logical_calls = self.derive_maximum_possible_logical_model_calls(case_count=case_count)
        return logical_calls * self.maximum_provider_attempts_per_logical_call()


class HardModeCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    kind: HardModeCaseKind
    repository: str = Field(pattern=r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
    source_url: str | None
    pull_request_number: int | None = Field(default=None, ge=1)
    pull_request_url: str | None
    merged_at: datetime | None
    base_sha: str
    head_sha: str
    category: str = Field(min_length=1, max_length=300)
    difficulty_rationale: str = Field(min_length=1, max_length=1_000)
    production_files_changed: tuple[str, ...] = Field(min_length=1, max_length=20)
    production_additions: int = Field(ge=0)
    production_deletions: int = Field(ge=0)
    interface_exists_on_both_revisions: bool
    environment_caveat: str = Field(min_length=1, max_length=1_000)
    expected_base_result: Literal["ASSERTION_FAILED"]
    expected_head_result: Literal["PASSED"]
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(max_length=8_000)
    excluded_paths: tuple[str, ...]
    expected_excluded_changed_files: int = Field(ge=0)
    repository_python_paths: tuple[str, ...] = Field(min_length=1, max_length=4)
    fixture_directory: str | None = None
    oracle_file: str
    oracle_sha256: str
    oracle_test_function: str = Field(pattern=r"test_[A-Za-z0-9_]+")

    @model_validator(mode="after")
    def validate_case(self) -> HardModeCase:
        if _CASE_ID.fullmatch(self.case_id) is None:
            raise ValueError("case_id must be lowercase kebab-case")
        if any(_GIT_SHA.fullmatch(value) is None for value in (self.base_sha, self.head_sha)):
            raise ValueError("case revisions must be full lowercase Git SHA-1 values")
        if self.base_sha == self.head_sha or _SHA256.fullmatch(self.oracle_sha256) is None:
            raise ValueError("case revisions must differ and oracle SHA-256 must be valid")
        paths = (*self.excluded_paths, *self.production_files_changed, self.oracle_file)
        if self.fixture_directory:
            paths = (*paths, self.fixture_directory)
        for value in paths:
            path = PurePosixPath(value)
            if (
                not value
                or path.is_absolute()
                or path.as_posix() != value
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("case paths must be normalized relative POSIX paths")
        if len(set(self.excluded_paths)) != len(self.excluded_paths):
            raise ValueError("excluded paths must be unique")
        if self.kind is HardModeCaseKind.HISTORICAL_PR:
            expected_source = f"https://github.com/{self.repository}.git"
            expected_pr = f"https://github.com/{self.repository}/pull/{self.pull_request_number}"
            if (
                self.source_url != expected_source
                or self.pull_request_url != expected_pr
                or self.pull_request_number is None
                or self.merged_at is None
                or self.fixture_directory is not None
            ):
                raise ValueError("historical case provenance is incomplete or inconsistent")
        elif (
            self.source_url is not None
            or self.pull_request_number is not None
            or self.pull_request_url is not None
            or self.merged_at is not None
            or self.fixture_directory is None
        ):
            raise ValueError("synthetic case must use only local fixture provenance")
        return self


class HardModeManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    protocol: HardModeProtocol
    cases: tuple[HardModeCase, ...] = Field(min_length=5, max_length=20)

    @model_validator(mode="after")
    def validate_coverage(self) -> HardModeManifest:
        historical = [case for case in self.cases if case.kind is HardModeCaseKind.HISTORICAL_PR]
        synthetic = [case for case in self.cases if case.kind is HardModeCaseKind.LOCAL_SYNTHETIC]
        if len(historical) < 4 or len({case.repository for case in historical}) < 2:
            raise ValueError("hard mode needs four historical cases across two repositories")
        if not synthetic:
            raise ValueError("hard mode needs at least one difficult local synthetic case")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("hard-mode case IDs must be unique")
        if self.protocol.maximum_possible_logical_model_calls is not None:
            derived_logical = self.protocol.derive_maximum_possible_logical_model_calls(
                case_count=len(self.cases)
            )
            derived_provider = self.protocol.derive_maximum_possible_provider_calls(
                case_count=len(self.cases)
            )
            if self.protocol.maximum_possible_logical_model_calls != derived_logical:
                raise ValueError(
                    "manifest maximum possible logical model calls does not match the protocol "
                    "formula"
                )
            if self.protocol.maximum_possible_provider_calls != derived_provider:
                raise ValueError(
                    "manifest maximum possible provider calls does not match the retry-aware "
                    "protocol formula"
                )
        return self


class HardModeCallBudgetPreflight(BaseModel):
    """Operator-declared capacity check performed before an exactly-once journal starts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    maximum_possible_logical_model_calls: int = Field(ge=1)
    maximum_possible_provider_calls: int = Field(ge=1)
    declared_available_provider_calls: int = Field(ge=1)
    passed: Literal[True] = True


def _model_call_budget_preflight(manifest: HardModeManifest) -> HardModeCallBudgetPreflight:
    """Refuse an underfunded run without claiming knowledge of live provider quota."""
    if manifest.protocol.candidate_calls_per_case != _MAX_CANDIDATE_MODEL_CALLS:
        raise HardModeConfigurationError(
            "new hard-mode live runs must declare the production candidate-call budget "
            f"of {_MAX_CANDIDATE_MODEL_CALLS}"
        )
    logical_required = manifest.protocol.derive_maximum_possible_logical_model_calls(
        case_count=len(manifest.cases)
    )
    provider_required = manifest.protocol.derive_maximum_possible_provider_calls(
        case_count=len(manifest.cases)
    )
    declared_logical_required = manifest.protocol.maximum_possible_logical_model_calls
    declared_provider_required = manifest.protocol.maximum_possible_provider_calls
    available = manifest.protocol.declared_available_provider_calls
    declared_passed = manifest.protocol.model_call_budget_preflight_passed
    if (
        declared_logical_required is None
        or declared_provider_required is None
        or available is None
        or declared_passed is None
    ):
        raise HardModeConfigurationError(
            "new hard-mode live runs must record maximum possible logical model calls, maximum "
            "possible provider calls, declared available provider calls, and preflight pass/fail "
            "in the manifest; PatchProof does not query the provider's live remaining quota"
        )
    if declared_logical_required != logical_required:
        raise HardModeConfigurationError(
            f"manifest records {declared_logical_required} maximum possible logical model calls "
            f"but the protocol formula derives {logical_required}"
        )
    if declared_provider_required != provider_required:
        raise HardModeConfigurationError(
            f"manifest records {declared_provider_required} maximum possible provider calls but "
            f"the retry-aware protocol formula derives {provider_required}"
        )
    passed = available >= provider_required
    if declared_passed is not passed:
        raise HardModeConfigurationError(
            "manifest model-call budget pass/fail does not match its declared call capacity"
        )
    if not passed:
        raise HardModeConfigurationError(
            f"operator-declared provider-call availability {available} is below the maximum "
            f"possible provider calls {provider_required}; refusing to start the live journal"
        )
    return HardModeCallBudgetPreflight(
        maximum_possible_logical_model_calls=logical_required,
        maximum_possible_provider_calls=provider_required,
        declared_available_provider_calls=available,
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json(document), encoding="utf-8", newline="\n")


def load_hard_mode_manifest(path: Path) -> tuple[HardModeManifest, str]:
    raw = path.resolve().read_bytes()
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise HardModeConfigurationError("hard-mode manifest is empty or oversized")
    try:
        manifest = HardModeManifest.model_validate_json(raw)
    except ValueError as error:
        raise HardModeConfigurationError("hard-mode manifest failed validation") from error
    root = path.resolve().parent
    for case in manifest.cases:
        oracle_path = (root / Path(*PurePosixPath(case.oracle_file).parts)).resolve()
        if not oracle_path.is_relative_to(root) or not oracle_path.is_file():
            raise HardModeConfigurationError(f"oracle is unavailable for {case.case_id}")
        content = oracle_path.read_bytes()
        if _sha256(content) != case.oracle_sha256:
            raise HardModeConfigurationError(f"oracle hash mismatch for {case.case_id}")
        try:
            tree = ast.parse(content, filename=case.oracle_file)
        except SyntaxError as error:
            raise HardModeConfigurationError(f"oracle syntax error for {case.case_id}") from error
        functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if functions != {case.oracle_test_function}:
            raise HardModeConfigurationError(
                f"oracle must contain exactly its declared top-level test for {case.case_id}"
            )
    return manifest, _sha256(raw)


class HardModeRepositoryCache:
    """Prepare exact public PR commits or a reproducible local synthetic repository."""

    def __init__(self, root: Path, manifest_root: Path) -> None:
        self.root = root.resolve()
        self.manifest_root = manifest_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, case: HardModeCase) -> Path:
        target = (self.root / case.repository.replace("/", "--")).resolve()
        if not target.is_relative_to(self.root):
            raise HardModeConfigurationError("repository cache target escapes cache root")
        if case.kind is HardModeCaseKind.HISTORICAL_PR:
            self._prepare_historical(case, target)
        else:
            self._prepare_synthetic(case, target)
        for revision in (case.base_sha, case.head_sha):
            self._git(target, "cat-file", "-e", f"{revision}^{{commit}}")
        return target

    def _prepare_historical(self, case: HardModeCase, target: Path) -> None:
        assert case.source_url is not None and case.pull_request_number is not None
        if not target.exists():
            self._run(
                "git",
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--no-checkout",
                case.source_url,
                str(target),
                cwd=self.root,
                timeout=180,
            )
        if not (target / ".git").is_dir():
            raise HardModeInfrastructureError("repository cache entry is not a Git repository")
        remote = self._git(target, "remote", "get-url", "origin").stdout.strip()
        if remote != case.source_url:
            raise HardModeInfrastructureError("repository cache origin differs from manifest")
        pr_ref = f"refs/patchproof/hard-mode-pr-{case.pull_request_number}"
        self._git(
            target,
            "fetch",
            "--quiet",
            "origin",
            f"+refs/pull/{case.pull_request_number}/head:{pr_ref}",
            timeout=180,
        )
        actual_head = self._git(
            target, "rev-parse", "--verify", f"{pr_ref}^{{commit}}"
        ).stdout.strip()
        if actual_head != case.head_sha:
            raise HardModeInfrastructureError("fetched PR HEAD differs from frozen manifest")

    def _prepare_synthetic(self, case: HardModeCase, target: Path) -> None:
        assert case.fixture_directory is not None
        fixture = (
            self.manifest_root / Path(*PurePosixPath(case.fixture_directory).parts)
        ).resolve()
        base_source = fixture / "base" / "workspace_registry.py"
        head_source = fixture / "head" / "workspace_registry.py"
        if not base_source.is_file() or not head_source.is_file():
            raise HardModeConfigurationError("synthetic fixture snapshots are unavailable")
        if not target.exists():
            target.mkdir(parents=True)
            self._git(target, "init", "--quiet")
            self._git(target, "config", "core.autocrlf", "false")
            self._git(target, "config", "user.name", "PatchProof Benchmark")
            self._git(target, "config", "user.email", "benchmark@patchproof.invalid")
            shutil.copyfile(base_source, target / "workspace_registry.py")
            self._git(target, "add", "workspace_registry.py")
            self._git(
                target,
                "commit",
                "--quiet",
                "-m",
                "synthetic base",
                extra_environment={
                    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
                },
            )
            shutil.copyfile(head_source, target / "workspace_registry.py")
            self._git(target, "add", "workspace_registry.py")
            self._git(
                target,
                "commit",
                "--quiet",
                "-m",
                "prefer deepest matching workspace",
                extra_environment={
                    "GIT_AUTHOR_DATE": "2026-01-02T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z",
                },
            )
        actual_base = self._git(target, "rev-parse", f"{case.head_sha}^").stdout.strip()
        actual_head = self._git(target, "rev-parse", "HEAD").stdout.strip()
        if (actual_base, actual_head) != (case.base_sha, case.head_sha):
            raise HardModeInfrastructureError("synthetic fixture commits differ from manifest")
        if (
            self._git(target, "show", f"{case.base_sha}:workspace_registry.py").stdout.encode()
            != base_source.read_bytes()
            or self._git(target, "show", f"{case.head_sha}:workspace_registry.py").stdout.encode()
            != head_source.read_bytes()
        ):
            raise HardModeInfrastructureError("synthetic committed bytes differ from snapshots")

    def changed_python_test_paths(self, case: HardModeCase, repository: Path) -> tuple[str, ...]:
        output = self._git(
            repository,
            "diff",
            "--name-only",
            "-z",
            case.base_sha,
            case.head_sha,
            "--",
            binary=True,
        ).stdout
        raw_paths = output.rstrip(b"\0").split(b"\0") if output else []
        paths: list[str] = []
        for raw_path in raw_paths:
            path = raw_path.decode("utf-8")
            pure = PurePosixPath(path)
            if path.endswith(".py") and (
                "tests" in pure.parts
                or pure.name.startswith("test_")
                or pure.name.endswith("_test.py")
            ):
                paths.append(path)
        return tuple(sorted(paths))

    def _git(
        self,
        repository: Path,
        *arguments: str,
        timeout: int = 60,
        extra_environment: dict[str, str] | None = None,
        binary: bool = False,
    ) -> subprocess.CompletedProcess[Any]:
        return self._run(
            "git",
            "-C",
            str(repository),
            *arguments,
            cwd=self.root,
            timeout=timeout,
            extra_environment=extra_environment,
            binary=binary,
        )

    @staticmethod
    def _run(
        *command: str,
        cwd: Path,
        timeout: int,
        extra_environment: dict[str, str] | None = None,
        binary: bool = False,
    ) -> subprocess.CompletedProcess[Any]:
        environment = os.environ.copy()
        environment.update(extra_environment or {})
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
                timeout=timeout,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HardModeInfrastructureError("bounded Git operation did not complete") from error
        if completed.returncode != 0:
            detail_value = completed.stderr
            detail = (
                detail_value.decode("utf-8", errors="replace")
                if isinstance(detail_value, bytes)
                else detail_value
            )
            raise HardModeInfrastructureError(
                f"Git operation failed with code {completed.returncode}: {detail[-500:]}"
            )
        return completed


def _contract() -> ExecutionContract:
    return ExecutionContract(
        version=1,
        python="3.12",
        install=(("uv", "sync", "--frozen"),),
        test=TestCommandContract(command=("python", "-m", "pytest")),
        allowed_test_paths=("patchproof_generated_tests/",),
        timeout_seconds=120,
    )


@dataclass(frozen=True, slots=True)
class _EvaluationExecutionPlan:
    """One deterministic BASE/HEAD environment plan for an evaluation case."""

    contract: ExecutionContract
    install_dependencies: bool
    base_install: InstallPlan | None = None
    head_install: InstallPlan | None = None


def _execution_plan(repository: Path, case: HardModeCase) -> _EvaluationExecutionPlan:
    """Resolve a symmetric install plan before any historical-case execution."""
    if case.kind is HardModeCaseKind.LOCAL_SYNTHETIC:
        return _EvaluationExecutionPlan(
            contract=_contract(),
            install_dependencies=False,
        )

    reader = DeterministicContextRetriever(source_repository=repository)
    try:
        contract, base_plan, head_plan = resolve_contract_for_pair(
            prober=DependencyInstallProber(reader=reader),
            base_sha=case.base_sha,
            head_sha=case.head_sha,
        )
    except ContractSynthesisError as error:
        raise HardModeConfigurationError(
            f"historical case {case.case_id} has no safe symmetric install plan: {error}"
        ) from error
    return _EvaluationExecutionPlan(
        contract=contract,
        install_dependencies=True,
        base_install=base_plan,
        head_install=head_plan,
    )


def _execution_plan_document(plan: _EvaluationExecutionPlan) -> dict[str, Any]:
    """Return bounded deterministic install provenance for evaluation output."""
    if plan.base_install is None or plan.head_install is None:
        return {
            "source": "LOCAL_SYNTHETIC",
            "install_dependencies": False,
            "base": None,
            "head": None,
            "equivalent": True,
        }

    def document(value: InstallPlan) -> dict[str, Any]:
        return {
            "strategy": value.strategy.value,
            "commands": [list(command) for command in value.commands],
            "rationale": value.rationale,
        }

    return {
        "source": "COMMITTED_METADATA_PROBE",
        "install_dependencies": True,
        "base": document(plan.base_install),
        "head": document(plan.head_install),
        "equivalent": plan.base_install.commands == plan.head_install.commands,
    }


def _challenge(
    repository: Path,
    workspace_root: Path,
    case: HardModeCase,
    *,
    plan: _EvaluationExecutionPlan | None = None,
) -> BaseHeadChallenge:
    resolved_plan = plan or _execution_plan(repository, case)
    return BaseHeadChallenge(
        workspaces=GitWorkspaceManager(
            source_repository=repository,
            workspace_root=workspace_root / case.case_id,
        ),
        runner=PytestRunner(
            contract=resolved_plan.contract,
            python_executable=Path(sys.executable),
            install_dependencies=resolved_plan.install_dependencies,
            repository_python_paths=case.repository_python_paths,
        ),
    )


def _execution_document(result: Any) -> dict[str, Any]:
    return {
        "role": str(result.revision.role),
        "sha": result.revision.sha,
        "test_node_id": result.test_node_id,
        "expected_artifact_sha256": result.expected_artifact_sha256,
        "artifact_sha256_before": result.artifact_sha256_before,
        "artifact_sha256_after": result.artifact_sha256_after,
        "status": str(result.status),
        "collected_count": result.collected_count,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "stdout": result.stdout[:12_000],
        "stderr": result.stderr[:12_000],
        "detail": result.detail[:2_000] if result.detail else None,
    }


def _readiness_document(readiness: Any) -> dict[str, Any]:
    document = {
        "status": readiness.status.value,
        "reason": readiness.reason,
    }
    if readiness.setup_diagnostic is not None:
        document["setup_diagnostic"] = asdict(readiness.setup_diagnostic)
    return document


def _context_leak_audit(
    *,
    case: HardModeCase,
    context: PullRequestContext,
    oracle_source: str,
) -> dict[str, Any]:
    serialized = context.model_dump_json()
    leaked_paths = [path for path in case.excluded_paths if path in serialized]
    oracle_lines = {
        line.strip()
        for line in oracle_source.splitlines()
        if len(line.strip()) >= 40
        and not line.lstrip().startswith(("import ", "from ", "sys.path.insert"))
    }
    leaked_oracle_lines = sorted(line for line in oracle_lines if line in serialized)
    passed = (
        not leaked_paths
        and not leaked_oracle_lines
        and context.stats.excluded_changed_files == case.expected_excluded_changed_files
    )
    return {
        "passed": passed,
        "excluded_paths_declared": list(case.excluded_paths),
        "excluded_changed_files_observed": context.stats.excluded_changed_files,
        "excluded_python_paths_observed": context.stats.excluded_python_paths,
        "leaked_excluded_paths": leaked_paths,
        "leaked_oracle_lines": leaked_oracle_lines,
        "context_sha256": _sha256(serialized.encode("utf-8")),
    }


def verify_oracles(
    *, manifest_path: Path, cache_root: Path, workspace_root: Path, gate_path: Path
) -> dict[str, Any]:
    """Prove every frozen oracle and every anti-leakage precondition before live inference."""
    if gate_path.exists():
        raise HardModeConfigurationError("oracle gate already exists; refusing to overwrite it")
    manifest, manifest_sha256 = load_hard_mode_manifest(manifest_path)
    root = manifest_path.resolve().parent
    repositories = HardModeRepositoryCache(cache_root, root)
    case_results: list[dict[str, Any]] = []
    for case in manifest.cases:
        repository = repositories.prepare(case)
        changed_test_paths = repositories.changed_python_test_paths(case, repository)
        if changed_test_paths != tuple(sorted(case.excluded_paths)):
            raise HardModeConfigurationError(
                f"declared changed-test exclusions are incomplete for {case.case_id}: "
                f"observed={changed_test_paths!r}"
            )
        oracle_path = root / Path(*PurePosixPath(case.oracle_file).parts)
        oracle_content = oracle_path.read_text(encoding="utf-8")
        artifact = TestArtifact.from_text(
            relative_path=_ORACLE_TARGET,
            node_id=f"{_ORACLE_TARGET}::{case.oracle_test_function}",
            content=oracle_content,
        )
        execution_plan = _execution_plan(repository, case)
        challenge_runner = _challenge(
            repository,
            workspace_root / "oracles",
            case,
            plan=execution_plan,
        )
        with challenge_runner.session(
            base_ref=case.base_sha,
            head_ref=case.head_sha,
        ) as session:
            readiness = (
                session.prepare_environment() if execution_plan.install_dependencies else None
            )
            if readiness is not None and not readiness.ready:
                raise HardModeConfigurationError(
                    f"oracle environment is not ready for {case.case_id}: {readiness.reason}"
                )
            challenge = session.run(artifact=artifact)
        context = DeterministicContextRetriever(
            source_repository=repository,
            excluded_paths=frozenset(case.excluded_paths),
        ).retrieve(base_sha=case.base_sha, head_sha=case.head_sha)
        leak_audit = _context_leak_audit(
            case=case,
            context=context,
            oracle_source=oracle_content,
        )
        accepted = (
            challenge.base.status is TestExecutionStatus.ASSERTION_FAILED
            and challenge.head.status is TestExecutionStatus.PASSED
            and challenge.base.artifact_was_unchanged
            and challenge.head.artifact_was_unchanged
            and challenge.assessment.pattern
            is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
            and leak_audit["passed"]
        )
        case_results.append(
            {
                "case_id": case.case_id,
                "accepted": accepted,
                "changed_python_test_paths": list(changed_test_paths),
                "oracle_file": case.oracle_file,
                "oracle_sha256": case.oracle_sha256,
                "execution_plan": _execution_plan_document(execution_plan),
                "environment_readiness": (
                    _readiness_document(readiness) if readiness is not None else None
                ),
                "base": _execution_document(challenge.base),
                "head": _execution_document(challenge.head),
                "mechanical_status": str(challenge.assessment.mechanical_status),
                "differential_pattern": str(challenge.assessment.pattern),
                "mechanical_reason": challenge.assessment.reason,
                "anti_leakage": leak_audit,
            }
        )
    gate = {
        "schema_version": 1,
        "created_at": _utc_now(),
        "manifest_sha256": manifest_sha256,
        "accepted": all(result["accepted"] for result in case_results),
        "case_count": len(case_results),
        "cases": case_results,
    }
    if not gate["accepted"]:
        rejection_path = gate_path.with_name("oracle_rejections.json")
        _write_json(rejection_path, gate)
        raise HardModeConfigurationError(
            "at least one case failed oracle or anti-leakage preflight; gate was not written; "
            f"diagnostics={rejection_path}"
        )
    _write_json(gate_path, gate)
    return gate


def _load_gate(path: Path, manifest_sha256: str, case_count: int) -> dict[str, Any]:
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HardModeConfigurationError("oracle gate is unavailable or invalid") from error
    if (
        gate.get("accepted") is not True
        or gate.get("manifest_sha256") != manifest_sha256
        or gate.get("case_count") != case_count
        or not all(case.get("accepted") is True for case in gate.get("cases", []))
    ):
        raise HardModeConfigurationError("oracle gate does not authorize this manifest")
    return gate


def _attempt_document(attempt: CandidateAttempt) -> dict[str, Any]:
    document = EvidenceWorkflow._attempt_evidence(attempt).model_dump(mode="json")
    if attempt.malformed_output_diagnostic is not None:
        document["local_malformed_output_diagnostic"] = (
            attempt.malformed_output_diagnostic.model_dump(mode="json")
        )
    return document


async def _bounded_candidate_challenges(
    *,
    generator: BoundedCandidateTestGenerator,
    session: ChallengeSession,
) -> AsyncIterator[tuple[CandidateAttempt, ChallengeResult | None]]:
    """Run the production-sized initial-plus-two-repair candidate policy."""
    attempt = await generator.generate_initial()
    challenge = None
    for attempt_number in range(_MAX_CANDIDATE_MODEL_CALLS):
        if attempt.validated is not None:
            challenge = session.run(artifact=attempt.validated.artifact)
        yield attempt, challenge
        if (
            challenge is not None
            and challenge.assessment.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING
        ):
            return
        if attempt_number >= _MAX_REPAIRS:
            return
        feedback = EvidenceWorkflow._repair_feedback(
            attempt=attempt,
            challenge=challenge,
        )
        attempt = await generator.repair(feedback=feedback)
        challenge = None


def _append_journal(path: Path, document: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as journal:
        journal.write(json.dumps(document, sort_keys=True, ensure_ascii=False) + "\n")
        journal.flush()
        os.fsync(journal.fileno())


def _pace_between_cases(
    *,
    protocol: HardModeProtocol,
    journal_path: Path,
    completed_case_id: str,
    next_case_id: str,
) -> None:
    if protocol.pacing_policy is HardModePacingPolicy.NONE:
        return
    delay = protocol.inter_case_delay_seconds
    _append_journal(
        journal_path,
        {
            "event": "INTER_CASE_PACING_STARTED",
            "at": _utc_now(),
            "completed_case_id": completed_case_id,
            "next_case_id": next_case_id,
            "declared_delay_seconds": delay,
        },
    )
    started = time.perf_counter()
    time.sleep(delay)
    actual = time.perf_counter() - started
    _append_journal(
        journal_path,
        {
            "event": "INTER_CASE_PACING_COMPLETED",
            "at": _utc_now(),
            "completed_case_id": completed_case_id,
            "next_case_id": next_case_id,
            "declared_delay_seconds": delay,
            "actual_delay_seconds": actual,
        },
    )


async def _run_live_case(
    *,
    case: HardModeCase,
    repository: Path,
    workspace_root: Path,
    model_name: str,
    provider_config: GeminiProviderConfig,
    expected_context_sha256: str,
    claim_investigator: ClaimInvestigatorFactory | None = None,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    oracle_source = ""  # The live path deliberately never loads developer-oracle bytes.
    context_retriever = DeterministicContextRetriever(
        source_repository=repository,
        excluded_paths=frozenset(case.excluded_paths),
    )
    narrative = PullRequestNarrative.from_untrusted(title=case.title, body=case.body)
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "kind": str(case.kind),
        "repository": case.repository,
        "pull_request_number": case.pull_request_number,
        "base_sha": case.base_sha,
        "head_sha": case.head_sha,
        "category": case.category,
        "difficulty_rationale": case.difficulty_rationale,
        "started_at": started.isoformat(),
        "context_sha256": None,
        "context": None,
        "narrative": narrative.model_dump(mode="json"),
        "oracle_loaded_during_live_run": bool(oracle_source),
        "execution_plan": None,
        "environment_readiness": None,
        "environment_setup_duration_seconds": None,
        "claim_result": None,
        "candidate_attempts": [],
        "candidate_evaluations": [],
        "semantic_assessment": None,
        "final_mechanical": None,
        "final_outcome": None,
        "terminal_status": None,
        "error": None,
    }
    execution_plan = _execution_plan(repository, case)
    result["execution_plan"] = _execution_plan_document(execution_plan)
    challenge_runner = _challenge(
        repository,
        workspace_root / "live",
        case,
        plan=execution_plan,
    )
    with challenge_runner.session(
        base_ref=case.base_sha,
        head_ref=case.head_sha,
    ) as session:
        if execution_plan.install_dependencies:
            setup_started = time.perf_counter()
            readiness = session.prepare_environment()
            result["environment_setup_duration_seconds"] = time.perf_counter() - setup_started
            result["environment_readiness"] = _readiness_document(readiness)
            if not readiness.ready:
                result["terminal_status"] = "ENVIRONMENT_NOT_READY"
                result["final_mechanical"] = str(MechanicalEvidenceStatus.ENVIRONMENTAL)
                result["final_outcome"] = str(ClaimOutcome.INSUFFICIENT_EVIDENCE)
                return _finish_case(result, started)
        context = context_retriever.retrieve(base_sha=case.base_sha, head_sha=case.head_sha)
        context_sha256 = _sha256(context.model_dump_json().encode("utf-8"))
        if context_sha256 != expected_context_sha256:
            raise HardModeConfigurationError(
                "live context differs from the preflight-gated context"
            )
        result["context_sha256"] = context_sha256
        result["context"] = context.model_dump(mode="json")
        return await _run_ready_live_case(
            case=case,
            context_retriever=context_retriever,
            context=context,
            narrative=narrative,
            execution_plan=execution_plan,
            session=session,
            model_name=model_name,
            provider_config=provider_config,
            result=result,
            started=started,
            claim_investigator=claim_investigator,
        )


async def _run_ready_live_case(
    *,
    case: HardModeCase,
    context_retriever: DeterministicContextRetriever,
    context: PullRequestContext,
    narrative: PullRequestNarrative,
    execution_plan: _EvaluationExecutionPlan,
    session: ChallengeSession,
    model_name: str,
    provider_config: GeminiProviderConfig,
    result: dict[str, Any],
    started: datetime,
    claim_investigator: ClaimInvestigatorFactory | None = None,
) -> dict[str, Any]:
    """Run inference only after the persistent BASE/HEAD session is ready."""
    try:
        if claim_investigator is None:
            claim_agent = BehavioralClaimAgent(
                model=BoundedRetryingModel(
                    AdkGeminiClaimModel(model_name=model_name, provider_config=provider_config)
                )
            )
            claim_result = await claim_agent.select_claim(context=context, narrative=narrative)
        else:
            investigation = await claim_investigator.build(
                base_sha=case.base_sha, head_sha=case.head_sha
            ).investigate(narrative=narrative, diff=context.diff)
            claim_result = investigation.agent_result
            result["investigation_transcript"] = investigation.transcript.model_dump(mode="json")
    except InvalidClaimAgentOutput as error:
        result["terminal_status"] = "CLAIM_INVALID_OUTPUT"
        result["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "usage": error.usage.model_dump(mode="json") if error.usage else None,
            "raw_response_sha256": error.raw_response_sha256,
            "local_malformed_output_diagnostic": (
                error.diagnostic.model_dump(mode="json") if error.diagnostic else None
            ),
        }
        return _finish_case(result, started)
    except ModelInvocationFailure as error:
        result["terminal_status"] = "CLAIM_INVOCATION_ERROR"
        result["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "retryable": error.retryable,
        }
        return _finish_case(result, started)
    result["claim_result"] = claim_result.model_dump(mode="json")
    claim = claim_result.selection.claim
    if claim is None:
        result["terminal_status"] = f"CLAIM_{claim_result.selection.disposition}"
        result["final_outcome"] = str(ClaimOutcome.INSUFFICIENT_EVIDENCE)
        return _finish_case(result, started)

    signature_context = context_retriever.retrieve_callable_signatures(
        head_sha=case.head_sha,
        context=context,
        affected_symbols=tuple(
            (symbol.path, symbol.qualified_name) for symbol in claim.affected_symbols
        ),
    )

    challenge = None
    generator = BoundedCandidateTestGenerator(
        model=BoundedRetryingModel(
            AdkGeminiCandidateModel(model_name=model_name, provider_config=provider_config)
        ),
        validator=CandidateTestValidator(
            installed_import_roots=session.installed_import_roots()
            if execution_plan.install_dependencies
            else frozenset()
        ),
        claim=claim,
        context=context,
        contract=execution_plan.contract,
        existing_paths=context_retriever.committed_paths(case.head_sha),
        repository_signatures=signature_context,
    )
    try:
        async for attempt, challenge in _bounded_candidate_challenges(
            generator=generator,
            session=session,
        ):
            result["candidate_attempts"].append(_attempt_document(attempt))
            if challenge is not None:
                evaluation = EvidenceWorkflow._evaluation_evidence(
                    attempt.sequence, challenge
                ).model_dump(mode="json")
                evaluation["hidden_oracle_pattern"] = str(
                    DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
                )
                evaluation["matches_hidden_oracle_direction"] = (
                    challenge.assessment.pattern
                    is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
                )
                result["candidate_evaluations"].append(evaluation)
                if (
                    challenge.assessment.mechanical_status
                    is MechanicalEvidenceStatus.DISCRIMINATING
                ):
                    semantic = await BoundedRetryingEvidenceAssessor(
                        AdkGeminiEvidenceAssessor(
                            model_name=model_name,
                            provider_config=provider_config,
                        )
                    ).assess(
                        claim=claim,
                        candidate_source=attempt.validated.proposal.source,
                        challenge=challenge,
                    )
                    EvidenceWorkflow._validate_semantic_decision(challenge, semantic.decision)
                    result["semantic_assessment"] = semantic.model_dump(mode="json")
                    result["final_mechanical"] = str(challenge.assessment.mechanical_status)
                    result["final_outcome"] = str(semantic.decision.outcome)
                    result["terminal_status"] = str(semantic.decision.outcome)
                    return _finish_case(result, started)
    except ModelInvocationFailure as error:
        result["terminal_status"] = "MODEL_INVOCATION_ERROR"
        result["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "retryable": error.retryable,
            "stage": (
                "ASSESSMENT"
                if challenge is not None
                and challenge.assessment.mechanical_status
                is MechanicalEvidenceStatus.DISCRIMINATING
                else "CANDIDATE"
            ),
            "logical_candidate_calls_started": generator.snapshot.model_calls,
        }
        return _finish_case(result, started)
    result["terminal_status"] = (
        str(challenge.assessment.mechanical_status) if challenge is not None else "INVALID_TEST"
    )
    result["final_mechanical"] = result["terminal_status"]
    result["final_outcome"] = str(ClaimOutcome.INSUFFICIENT_EVIDENCE)
    return _finish_case(result, started)


def _finish_case(result: dict[str, Any], started: datetime) -> dict[str, Any]:
    result["completed_at"] = _utc_now()
    result["wall_duration_seconds"] = (datetime.now(UTC) - started).total_seconds()
    return result


def run_live(
    *,
    manifest_path: Path,
    cache_root: Path,
    workspace_root: Path,
    gate_path: Path,
    journal_path: Path,
    raw_path: Path,
    provider_config: GeminiProviderConfig | None = None,
) -> dict[str, Any]:
    """Execute one declared run; any existing journal permanently blocks a rerun."""
    if journal_path.exists() or raw_path.exists():
        raise HardModeConfigurationError(
            "declared live run has already started; refusing retry or result replacement"
        )
    manifest, manifest_sha256 = load_hard_mode_manifest(manifest_path)
    call_budget_preflight = _model_call_budget_preflight(manifest)
    if manifest.protocol.provider_surface is None:
        raise HardModeConfigurationError(
            "new hard-mode live runs must declare an explicit Gemini provider surface"
        )
    resolved_provider = provider_config or GeminiProviderConfig.from_environment()
    configured_model = os.getenv("PATCHPROOF_GEMINI_MODEL")
    if configured_model and configured_model != manifest.protocol.model_name:
        raise HardModeConfigurationError(
            "configured Gemini model does not match the hard-mode manifest"
        )
    if resolved_provider.provider_surface is not manifest.protocol.provider_surface:
        raise HardModeConfigurationError(
            "configured Gemini provider surface does not match the hard-mode manifest"
        )
    if resolved_provider.provider_surface is GeminiProviderSurface.VERTEX_AI:
        preflight_vertex_authentication(resolved_provider)
    gate = _load_gate(gate_path, manifest_sha256, len(manifest.cases))
    gated_cases = {case["case_id"]: case for case in gate["cases"]}
    root = manifest_path.resolve().parent
    repositories = HardModeRepositoryCache(cache_root, root)
    prepared = {case.case_id: repositories.prepare(case) for case in manifest.cases}
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    _append_journal(
        journal_path,
        {
            "event": "RUN_STARTED",
            "at": _utc_now(),
            "declared_run_id": manifest.protocol.declared_run_id,
            "manifest_sha256": manifest_sha256,
            "model_name": manifest.protocol.model_name,
            "provider_surface": str(resolved_provider.provider_surface),
            "reasoning_levels": {
                task.value: budget_for(task).thinking_level.value for task in AgentTask
            },
            "case_ids": [case.case_id for case in manifest.cases],
            "maximum_possible_logical_model_calls": (
                call_budget_preflight.maximum_possible_logical_model_calls
            ),
            "maximum_possible_provider_calls": (
                call_budget_preflight.maximum_possible_provider_calls
            ),
            "declared_available_provider_calls": (
                call_budget_preflight.declared_available_provider_calls
            ),
            "model_call_budget_preflight_passed": call_budget_preflight.passed,
        },
    )
    started_at = _utc_now()
    results: list[dict[str, Any]] = []
    for case_index, case in enumerate(manifest.cases):
        if case_index:
            _pace_between_cases(
                protocol=manifest.protocol,
                journal_path=journal_path,
                completed_case_id=manifest.cases[case_index - 1].case_id,
                next_case_id=case.case_id,
            )
        _append_journal(
            journal_path,
            {"event": "CASE_STARTED", "at": _utc_now(), "case_id": case.case_id},
        )
        try:
            result = asyncio.run(
                _run_live_case(
                    case=case,
                    repository=prepared[case.case_id],
                    workspace_root=workspace_root,
                    model_name=manifest.protocol.model_name,
                    provider_config=resolved_provider,
                    expected_context_sha256=gated_cases[case.case_id]["anti_leakage"][
                        "context_sha256"
                    ],
                )
            )
        except Exception as error:  # Preserve the declared run and continue remaining cases.
            result = {
                "case_id": case.case_id,
                "kind": str(case.kind),
                "repository": case.repository,
                "pull_request_number": case.pull_request_number,
                "base_sha": case.base_sha,
                "head_sha": case.head_sha,
                "terminal_status": "HARNESS_OR_IMPLEMENTATION_ERROR",
                "error": {"type": type(error).__name__, "message": str(error)[:2_000]},
                "completed_at": _utc_now(),
            }
        results.append(result)
        _append_journal(
            journal_path,
            {"event": "CASE_COMPLETED", "at": _utc_now(), "result": result},
        )
    raw = {
        "schema_version": 1,
        "declared_run_id": manifest.protocol.declared_run_id,
        "manifest_sha256": manifest_sha256,
        "oracle_gate_sha256": _sha256(gate_path.read_bytes()),
        "model_name": manifest.protocol.model_name,
        "provider_surface": str(resolved_provider.provider_surface),
        "reasoning_levels": {
            task.value: budget_for(task).thinking_level.value for task in AgentTask
        },
        "pacing_policy": str(manifest.protocol.pacing_policy),
        "inter_case_delay_seconds": manifest.protocol.inter_case_delay_seconds,
        "maximum_possible_logical_model_calls": (
            call_budget_preflight.maximum_possible_logical_model_calls
        ),
        "maximum_possible_provider_calls": call_budget_preflight.maximum_possible_provider_calls,
        "declared_available_provider_calls": (
            call_budget_preflight.declared_available_provider_calls
        ),
        "model_call_budget_preflight_passed": call_budget_preflight.passed,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "started_at": started_at,
        "completed_at": _utc_now(),
        "exactly_once_journal": journal_path.name,
        "cases": results,
    }
    _write_json(raw_path, raw)
    _append_journal(
        journal_path,
        {"event": "RUN_COMPLETED", "at": _utc_now(), "raw_sha256": _sha256(raw_path.read_bytes())},
    )
    return raw


def summarize_live(raw: dict[str, Any]) -> dict[str, Any]:
    cases = raw["cases"]
    selected = [
        case for case in cases if (case.get("claim_result") or {}).get("selection", {}).get("claim")
    ]
    candidate_attempts = [
        attempt for case in cases for attempt in case.get("candidate_attempts", [])
    ]
    evaluations = [
        evaluation for case in cases for evaluation in case.get("candidate_evaluations", [])
    ]
    discriminating_cases = [
        case
        for case in cases
        if any(
            item.get("mechanical_status") == "DISCRIMINATING"
            for item in case.get("candidate_evaluations", [])
        )
    ]
    supported = [
        case
        for case in cases
        if case.get("terminal_status") == str(ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO)
    ]
    initial_attempts = [
        attempt
        for case in cases
        for attempt in case.get("candidate_attempts", [])
        if attempt.get("sequence") == 1
    ]
    initial_evaluations = [
        evaluation
        for case in cases
        for evaluation in case.get("candidate_evaluations", [])
        if evaluation.get("attempt_sequence") == 1
    ]
    repair_evaluations = [
        evaluation
        for case in cases
        for evaluation in case.get("candidate_evaluations", [])
        if evaluation.get("attempt_sequence") in {2, 3}
    ]
    environmental_evaluations = [
        evaluation
        for evaluation in evaluations
        if evaluation.get("mechanical_status") == "ENVIRONMENTAL"
    ]
    incorrect_supports = [
        case
        for case in supported
        if not case.get("candidate_evaluations", [])[-1].get(
            "matches_hidden_oracle_direction", False
        )
    ]
    usage_records: list[dict[str, Any]] = []
    for case in cases:
        claim_usage = (case.get("claim_result") or {}).get("usage")
        if claim_usage:
            usage_records.append(claim_usage)
        failed_claim_usage = (case.get("error") or {}).get("usage")
        if failed_claim_usage:
            usage_records.append(failed_claim_usage)
        usage_records.extend(
            attempt["usage"]
            for attempt in case.get("candidate_attempts", [])
            if attempt.get("usage")
        )
        semantic_usage = (case.get("semantic_assessment") or {}).get("usage")
        if semantic_usage:
            usage_records.append(semantic_usage)
    token_values = [
        item["total_tokens"] for item in usage_records if item.get("total_tokens") is not None
    ]
    prompt_values = [
        item["prompt_tokens"] for item in usage_records if item.get("prompt_tokens") is not None
    ]
    output_values = [
        item["output_tokens"] for item in usage_records if item.get("output_tokens") is not None
    ]
    reasoning_values = [
        item["reasoning_tokens"]
        for item in usage_records
        if item.get("reasoning_tokens") is not None
    ]
    durations = [item["duration_seconds"] for item in usage_records]
    summary = {
        "schema_version": 1,
        "declared_run_id": raw["declared_run_id"],
        "model_name": raw["model_name"],
        "case_count": len(cases),
        "historical_case_count": sum(case.get("kind") == "HISTORICAL_PR" for case in cases),
        "synthetic_case_count": sum(case.get("kind") == "LOCAL_SYNTHETIC" for case in cases),
        "claim_selected_count": len(selected),
        "grounded_valid_claim_count": len(selected),
        "claim_selection_rate": len(selected) / len(cases) if cases else 0.0,
        "claim_abstention_count": sum(
            case.get("final_outcome") == "INSUFFICIENT_EVIDENCE"
            and not (case.get("claim_result") or {}).get("selection", {}).get("claim")
            for case in cases
        ),
        "invalid_claim_output_count": sum(
            case.get("terminal_status") == "CLAIM_INVALID_OUTPUT" for case in cases
        ),
        "claim_invocation_error_count": sum(
            case.get("terminal_status") == "CLAIM_INVOCATION_ERROR" for case in cases
        ),
        "candidate_attempt_count": len(candidate_attempts),
        "repair_attempt_count": sum(
            attempt.get("origin") == "REPAIR" for attempt in candidate_attempts
        ),
        "validated_candidate_count": sum(
            attempt.get("status") == "VALIDATED" for attempt in candidate_attempts
        ),
        "invalid_candidate_model_output_count": sum(
            attempt.get("status") == "INVALID_MODEL_OUTPUT" for attempt in candidate_attempts
        ),
        "statically_valid_initial_candidate_count": sum(
            attempt.get("status") == "VALIDATED" for attempt in initial_attempts
        ),
        "executable_initial_candidate_count": len(initial_evaluations)
        - sum(
            evaluation.get("mechanical_status") in {"INVALID_TEST", "ENVIRONMENTAL"}
            for evaluation in initial_evaluations
        ),
        "discriminating_initial_candidate_count": sum(
            evaluation.get("mechanical_status") == "DISCRIMINATING"
            for evaluation in initial_evaluations
        ),
        "successful_repair_count": sum(
            evaluation.get("mechanical_status") == "DISCRIMINATING"
            for evaluation in repair_evaluations
        ),
        "environmental_evaluation_count": len(environmental_evaluations),
        "candidate_evaluation_count": len(evaluations),
        "discriminating_case_count": len(discriminating_cases),
        "discrimination_rate_all_cases": len(discriminating_cases) / len(cases) if cases else 0.0,
        "claim_supported_case_count": len(supported),
        "claim_supported_rate_all_cases": len(supported) / len(cases) if cases else 0.0,
        "insufficient_evidence_case_count": sum(
            case.get("final_outcome") == "INSUFFICIENT_EVIDENCE" for case in cases
        ),
        "potential_regression_case_count": sum(
            case.get("final_outcome") == "POTENTIAL_REGRESSION" for case in cases
        ),
        "incorrect_support_count": len(incorrect_supports),
        "terminal_status_counts": {
            status: sum(case.get("terminal_status") == status for case in cases)
            for status in sorted({case.get("terminal_status") for case in cases})
        },
        "logical_model_result_count": len(usage_records),
        "failed_logical_model_call_count": sum(
            case.get("terminal_status") in {"CLAIM_INVOCATION_ERROR", "MODEL_INVOCATION_ERROR"}
            for case in cases
        ),
        "provider_attempt_count_completed_results": sum(
            item.get("provider_attempts", 1) for item in usage_records
        ),
        "failed_provider_attempt_count": None,
        "prompt_tokens": sum(prompt_values) if prompt_values else None,
        "output_tokens": sum(output_values) if output_values else None,
        "total_tokens": sum(token_values) if token_values else None,
        "mean_tokens_per_completed_model_result": fmean(token_values) if token_values else None,
        "total_model_duration_seconds": sum(durations),
        "mean_model_duration_seconds": fmean(durations) if durations else None,
        "scope_note": (
            "This is a five-case adversarial diagnostic with fixed denominators, not a "
            "representative production accuracy estimate or proof of pull-request correctness."
        ),
    }
    if "provider_surface" in raw:
        summary["provider_surface"] = raw["provider_surface"]
    if any("reasoning_tokens" in item for item in usage_records):
        summary["reasoning_tokens"] = sum(reasoning_values) if reasoning_values else None
    if "maximum_possible_logical_model_calls" in raw:
        summary.update(
            {
                "maximum_possible_logical_model_calls": raw["maximum_possible_logical_model_calls"],
                "maximum_possible_provider_calls": raw["maximum_possible_provider_calls"],
                "declared_available_provider_calls": raw["declared_available_provider_calls"],
                "model_call_budget_preflight_passed": raw["model_call_budget_preflight_passed"],
            }
        )
    return summary


def render_summary_markdown(summary: dict[str, Any]) -> str:
    statuses = "\n".join(
        f"- `{status}`: {count}" for status, count in summary["terminal_status_counts"].items()
    )
    outcomes = "\n".join(
        (
            f"- Cases: {summary['case_count']} "
            f"({summary['historical_case_count']} historical, "
            f"{summary['synthetic_case_count']} synthetic)",
            f"- Claims selected: {summary['claim_selected_count']}/"
            f"{summary['case_count']} ({summary['claim_selection_rate']:.1%})",
            f"- Discriminating generated tests: {summary['discriminating_case_count']}/"
            f"{summary['case_count']} ({summary['discrimination_rate_all_cases']:.1%})",
            f"- Claim-supported scenarios: {summary['claim_supported_case_count']}/"
            f"{summary['case_count']} ({summary['claim_supported_rate_all_cases']:.1%})",
            f"- Candidate attempts: {summary['candidate_attempt_count']} "
            f"({summary['repair_attempt_count']} repairs)",
            f"- Validated candidates: {summary['validated_candidate_count']}",
            f"- Invalid candidate structured outputs: "
            f"{summary['invalid_candidate_model_output_count']}",
            f"- Environmental candidate evaluations: {summary['environmental_evaluation_count']}",
            f"- Incorrect supports versus oracle direction: {summary['incorrect_support_count']}",
        )
    )
    completed_provider_attempts = summary["provider_attempt_count_completed_results"]
    call_budget = ""
    if "maximum_possible_logical_model_calls" in summary:
        call_budget = f"""
## Model-call budget preflight

- Maximum possible logical model calls: {summary["maximum_possible_logical_model_calls"]}
- Maximum provider attempts per logical call: derived as 1 plus permitted transient retries
- Maximum possible provider calls: {summary["maximum_possible_provider_calls"]}
- Operator-declared available provider calls: {summary["declared_available_provider_calls"]}
- Preflight passed: {summary["model_call_budget_preflight_passed"]}

A logical model call is one semantic PatchProof task. A provider attempt is an actual provider
request, including any permitted transient retry. The available capacity is an operator declaration,
not a query of the provider's live remaining quota.
"""
    provider = ""
    if "provider_surface" in summary:
        provider = f"\nProvider surface: `{summary['provider_surface']}`\n"
    return f"""# PatchProof hard-mode result

Declared run: `{summary["declared_run_id"]}`
Model: `{summary["model_name"]}`
{provider}

## Outcomes

{outcomes}

## Terminal statuses

{statuses}

## Model accounting

- Completed logical model results: {summary["logical_model_result_count"]}
- Provider attempts represented by completed results: {completed_provider_attempts}
- Failed logical model calls: {summary["failed_logical_model_call_count"]}
- Failed provider attempts: unavailable from the adapter after terminal invocation failure
- Prompt tokens where reported: {summary["prompt_tokens"]}
- Output tokens where reported: {summary["output_tokens"]}
- Reasoning tokens where separately reported: {summary.get("reasoning_tokens")}
- Total tokens where reported: {summary["total_tokens"]}
- Total model duration: {summary["total_model_duration_seconds"]:.3f} seconds
{call_budget}

## Scope

{summary["scope_note"]}
"""


def write_summary(*, raw_path: Path, summary_path: Path, markdown_path: Path) -> dict[str, Any]:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    summary = summarize_live(raw)
    _write_json(summary_path, summary)
    markdown_path.write_text(render_summary_markdown(summary), encoding="utf-8", newline="\n")
    return summary


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify-oracles", "run-live", "summarize"))
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/hard_mode/manifest.json"))
    parser.add_argument("--runtime-root", type=Path, default=Path(".patchproof-hard-mode"))
    parser.add_argument("--output-root", type=Path, default=Path("benchmarks/hard_mode/results"))
    args = parser.parse_args(arguments)
    cache_root = args.runtime_root / "repositories"
    workspace_root = args.runtime_root / "workspaces"
    gate_path = args.runtime_root / "oracle_gate.json"
    journal_path = args.runtime_root / "live_journal.jsonl"
    raw_path = args.output_root / "raw.json"
    if args.command == "verify-oracles":
        verify_oracles(
            manifest_path=args.manifest,
            cache_root=cache_root,
            workspace_root=workspace_root,
            gate_path=gate_path,
        )
    elif args.command == "run-live":
        run_live(
            manifest_path=args.manifest,
            cache_root=cache_root,
            workspace_root=workspace_root,
            gate_path=gate_path,
            journal_path=journal_path,
            raw_path=raw_path,
        )
    else:
        write_summary(
            raw_path=raw_path,
            summary_path=args.output_root / "summary.json",
            markdown_path=args.output_root / "summary.md",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
