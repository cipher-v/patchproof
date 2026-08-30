"""Reproducible historical-oracle benchmark and conservative policy comparison."""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import platform
import re
import sys
import xml.etree.ElementTree as element_tree
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchproof.challenge import BaseHeadChallenge
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.execution_runtime import (
    BoundedProcessResult,
    BoundedSubprocessRunner,
    ChildProcessEnvironmentPolicy,
)
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.install_strategy import (
    ContractSynthesisError,
    DependencyInstallProber,
    resolve_contract_for_pair,
)
from patchproof.models import (
    ChallengeResult,
    DifferentialPattern,
    MechanicalEvidenceStatus,
    TestArtifact,
    TestExecutionStatus,
)
from patchproof.pytest_runner import PytestRunner

_CASE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_TEST_FUNCTION = re.compile(r"test_[A-Za-z0-9_]+")
_MAX_MANIFEST_BYTES = 128_000
_MAX_ARTIFACT_BYTES = 20_000


class BenchmarkConfigurationError(ValueError):
    """Raised when benchmark provenance or an artifact is invalid."""


class BenchmarkInfrastructureError(RuntimeError):
    """Raised when a historical repository cannot be prepared reproducibly."""


class BenchmarkTruth(StrEnum):
    """Independent scenario truth derived from historical or controlled construction."""

    FIX_PRESENT = "FIX_PRESENT"
    FIX_ABSENT = "FIX_ABSENT"


class ArtifactKind(StrEnum):
    """Why an immutable benchmark artifact exists."""

    DEVELOPER_ORACLE = "DEVELOPER_ORACLE"
    CONTROLLED_WEAK_CANDIDATE = "CONTROLLED_WEAK_CANDIDATE"


class EvaluationStrategy(StrEnum):
    """The evidence policy applied to one observed execution."""

    HEAD_ONLY = "HEAD_ONLY"
    PATCHPROOF_BASE_HEAD = "PATCHPROOF_BASE_HEAD"


class EvaluationDecision(StrEnum):
    """Whether a strategy emits strong claim support."""

    SUPPORT = "SUPPORT"
    NO_SUPPORT = "NO_SUPPORT"


class HistoricalBenchmarkCase(BaseModel):
    """One immutable public bug-fix PR and its separately stored reference artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    repository: str
    source_url: str
    pull_request_number: int = Field(ge=1)
    pull_request_url: str
    title: str = Field(min_length=1, max_length=300)
    merged_at: datetime
    base_sha: str
    head_sha: str
    claim: str = Field(min_length=1, max_length=500)
    upstream_test_path: str
    oracle_file: str
    oracle_sha256: str
    oracle_test_function: str
    weak_candidate_file: str
    weak_candidate_sha256: str
    weak_test_function: str

    @model_validator(mode="after")
    def validate_provenance(self) -> HistoricalBenchmarkCase:
        if _CASE_ID.fullmatch(self.case_id) is None:
            raise ValueError("case_id must be lowercase kebab-case")
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("repository must be an owner/name identifier")
        expected_source = f"https://github.com/{self.repository}.git"
        expected_pr = f"https://github.com/{self.repository}/pull/{self.pull_request_number}"
        if self.source_url != expected_source or self.pull_request_url != expected_pr:
            raise ValueError("benchmark URLs must match the declared public GitHub PR")
        if any(_GIT_SHA.fullmatch(value) is None for value in (self.base_sha, self.head_sha)):
            raise ValueError("benchmark revisions must be full lowercase Git SHA-1 values")
        if self.base_sha == self.head_sha:
            raise ValueError("historical BASE and HEAD must differ")
        if any(
            _SHA256.fullmatch(value) is None
            for value in (self.oracle_sha256, self.weak_candidate_sha256)
        ):
            raise ValueError("benchmark artifacts require lowercase SHA-256 values")
        for value in (self.oracle_test_function, self.weak_test_function):
            if _TEST_FUNCTION.fullmatch(value) is None:
                raise ValueError("benchmark test functions must be simple pytest names")
        for value in (
            self.upstream_test_path,
            self.oracle_file,
            self.weak_candidate_file,
        ):
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or path.as_posix() != value
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("benchmark paths must be normalized relative POSIX paths")
        if self.merged_at.tzinfo is None or self.merged_at.utcoffset() is None:
            raise ValueError("merged_at must include a timezone")
        return self


class BenchmarkManifest(BaseModel):
    """Versioned benchmark definition with a cross-repository minimum."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    description: str = Field(min_length=1, max_length=1_000)
    controlled_checks: tuple[str, ...] = Field(min_length=6, max_length=20)
    cases: tuple[HistoricalBenchmarkCase, ...] = Field(min_length=4, max_length=20)

    @model_validator(mode="after")
    def validate_coverage(self) -> BenchmarkManifest:
        case_ids = [case.case_id for case in self.cases]
        pr_ids = [(case.repository, case.pull_request_number) for case in self.cases]
        if len(set(case_ids)) != len(case_ids) or len(set(pr_ids)) != len(pr_ids):
            raise ValueError("benchmark case and pull-request identities must be unique")
        if len({case.repository for case in self.cases}) < 2:
            raise ValueError("benchmark must cover at least two repositories")
        if len(set(self.controlled_checks)) != len(self.controlled_checks):
            raise ValueError("controlled-check node IDs must be unique")
        if any(
            not node.startswith("tests/")
            or "::test_" not in node
            or any(character in node for character in ("\x00", "\n", "\r"))
            for node in self.controlled_checks
        ):
            raise ValueError("controlled checks must be explicit local pytest node IDs")
        return self


class RevisionObservation(BaseModel):
    """Raw bounded process evidence for one immutable revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    status: TestExecutionStatus
    collected_count: int = Field(ge=0)
    exit_code: int | None
    duration_seconds: float = Field(ge=0)
    artifact_sha256_before: str | None
    artifact_sha256_after: str | None
    stdout: str
    stderr: str
    detail: str | None


class StrategyObservation(BaseModel):
    """One policy decision over shared raw execution facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: EvaluationStrategy
    decision: EvaluationDecision
    is_false_support: bool


class ScenarioObservation(BaseModel):
    """Raw result for a historical positive or controlled negative challenge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    case_id: str
    repository: str
    pull_request_number: int
    truth: BenchmarkTruth
    artifact_kind: ArtifactKind
    artifact_sha256: str
    base: RevisionObservation
    head: RevisionObservation
    mechanical_status: MechanicalEvidenceStatus
    differential_pattern: DifferentialPattern
    mechanical_reason: str
    strategies: tuple[StrategyObservation, ...]


class RawBenchmarkReport(BaseModel):
    """Machine-readable, non-cherry-picked output from one complete manifest run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    methodology: Literal["REFERENCE_ORACLE_POLICY_REPLAY"] = "REFERENCE_ORACLE_POLICY_REPLAY"
    manifest_sha256: str
    started_at: datetime
    completed_at: datetime
    python_version: str
    platform: str
    agent_candidate_generation: Literal["NOT_MEASURED_NO_CREDENTIALS"]
    scenarios: tuple[ScenarioObservation, ...]
    controlled_suite: ControlledSuiteObservation


class ControlledSuiteObservation(BaseModel):
    """Raw result from explicit reliability/security failure-path checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_ids: tuple[str, ...]
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str


class StrategyMetrics(BaseModel):
    """Auditable aggregates calculated only from raw strategy observations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: EvaluationStrategy
    evaluated_scenarios: int
    strong_supports: int
    true_supports: int
    false_supports: int
    no_supports: int
    false_support_rate: float | None
    negative_false_support_rate: float | None
    positive_support_rate: float | None


class BenchmarkSummary(BaseModel):
    """Machine-readable benchmark summary derived from a raw report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    methodology: str
    manifest_sha256: str
    historical_cases: int
    repositories: int
    scenarios: int
    historical_oracles_reproduced: int
    weak_candidates_rejected: int
    controlled_selections: int
    controlled_checks: int
    controlled_checks_passed: int
    controlled_recovery_rate: float | None
    mean_challenge_latency_seconds: float
    agent_candidate_generation: str
    unmeasured_comparisons: tuple[str, ...]
    strategy_metrics: tuple[StrategyMetrics, ...]


def load_manifest(path: Path) -> tuple[BenchmarkManifest, str]:
    """Load a bounded manifest and verify every local artifact before network access."""
    path = path.resolve()
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise BenchmarkConfigurationError("benchmark manifest could not be read") from error
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise BenchmarkConfigurationError("benchmark manifest is empty or oversized")
    try:
        manifest = BenchmarkManifest.model_validate_json(raw)
    except ValueError as error:
        raise BenchmarkConfigurationError("benchmark manifest failed validation") from error
    root = path.parent
    for case in manifest.cases:
        _load_artifact(root, case.oracle_file, case.oracle_sha256, case.oracle_test_function)
        _load_artifact(
            root,
            case.weak_candidate_file,
            case.weak_candidate_sha256,
            case.weak_test_function,
        )
    return manifest, hashlib.sha256(raw).hexdigest()


def _load_artifact(root: Path, relative: str, expected_sha256: str, function: str) -> bytes:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise BenchmarkConfigurationError("benchmark artifact resolves outside its root")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise BenchmarkConfigurationError("benchmark artifact could not be read") from error
    if not content or len(content) > _MAX_ARTIFACT_BYTES:
        raise BenchmarkConfigurationError("benchmark artifact is empty or oversized")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise BenchmarkConfigurationError("benchmark artifact hash mismatch")
    try:
        tree = ast.parse(content, filename=relative)
    except SyntaxError as error:
        raise BenchmarkConfigurationError("benchmark artifact is not valid Python") from error
    functions = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if functions != {function}:
        raise BenchmarkConfigurationError(
            "benchmark artifact must contain exactly its declared top-level test"
        )
    return content


class GitRepositoryCache:
    """Prepare public GitHub repositories and validate exact PR-head provenance."""

    def __init__(self, root: Path, *, timeout_seconds: float = 180.0) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.runner = BoundedSubprocessRunner(max_output_chars=8_000)
        self.environment = ChildProcessEnvironmentPolicy().build(
            runtime_root=self.root / "git-runtime"
        )
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            if os.environ.get(name):
                self.environment[name] = os.environ[name]

    def prepare(self, case: HistoricalBenchmarkCase) -> Path:
        """Clone once, fetch the PR ref, and prove both declared commits exist exactly."""
        target = (self.root / case.repository.replace("/", "--")).resolve()
        if not target.is_relative_to(self.root):
            raise BenchmarkConfigurationError("repository cache path escapes its root")
        if not target.exists():
            self._run(
                ("git", "clone", "--quiet", "--no-checkout", case.source_url, str(target)),
                cwd=self.root,
            )
        if not (target / ".git").is_dir():
            raise BenchmarkInfrastructureError("benchmark cache contains an invalid repository")
        remote = self._run(
            ("git", "-C", str(target), "remote", "get-url", "origin"), cwd=self.root
        ).stdout.strip()
        if remote != case.source_url:
            raise BenchmarkInfrastructureError("benchmark cache origin does not match manifest")
        pr_ref = f"refs/patchproof/pr-{case.pull_request_number}"
        self._run(
            (
                "git",
                "-C",
                str(target),
                "fetch",
                "--quiet",
                "origin",
                f"+refs/pull/{case.pull_request_number}/head:{pr_ref}",
            ),
            cwd=self.root,
        )
        resolved_head = self._run(
            ("git", "-C", str(target), "rev-parse", "--verify", f"{pr_ref}^{{commit}}"),
            cwd=self.root,
        ).stdout.strip()
        if resolved_head != case.head_sha:
            raise BenchmarkInfrastructureError("fetched PR HEAD does not match the manifest")
        for revision in (case.base_sha, case.head_sha):
            self._run(
                ("git", "-C", str(target), "cat-file", "-e", f"{revision}^{{commit}}"),
                cwd=self.root,
            )
        return target

    def _run(self, command: tuple[str, ...], *, cwd: Path) -> BoundedProcessResult:
        result = self.runner.run(
            command,
            cwd=cwd,
            environment=self.environment,
            timeout_seconds=self.timeout_seconds,
        )
        if result.timed_out:
            raise BenchmarkInfrastructureError("benchmark Git operation timed out")
        if result.start_error is not None:
            raise BenchmarkInfrastructureError("benchmark Git process could not start")
        if result.returncode != 0:
            detail = result.stderr.strip()[-500:] or "no bounded diagnostic"
            raise BenchmarkInfrastructureError(
                f"benchmark Git operation failed with code {result.returncode}: {detail}"
            )
        return result


def run_benchmark(
    *,
    manifest_path: Path,
    cache_root: Path,
    workspace_root: Path,
    python_executable: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RawBenchmarkReport:
    """Execute every historical oracle and controlled weak candidate without omission."""
    manifest, manifest_sha256 = load_manifest(manifest_path)
    root = manifest_path.resolve().parent
    now = clock or (lambda: datetime.now(UTC))
    started_at = _aware_utc(now())
    repositories = GitRepositoryCache(cache_root)
    executable = (python_executable or Path(sys.executable)).resolve()
    observations: list[ScenarioObservation] = []
    for case in manifest.cases:
        repository = repositories.prepare(case)
        observations.append(
            _run_scenario(
                case=case,
                repository=repository,
                workspace_root=workspace_root,
                executable=executable,
                manifest_root=root,
                truth=BenchmarkTruth.FIX_PRESENT,
                artifact_kind=ArtifactKind.DEVELOPER_ORACLE,
                artifact_file=case.oracle_file,
                artifact_sha256=case.oracle_sha256,
                function=case.oracle_test_function,
                base_sha=case.base_sha,
                head_sha=case.head_sha,
            )
        )
        observations.append(
            _run_scenario(
                case=case,
                repository=repository,
                workspace_root=workspace_root,
                executable=executable,
                manifest_root=root,
                truth=BenchmarkTruth.FIX_ABSENT,
                artifact_kind=ArtifactKind.CONTROLLED_WEAK_CANDIDATE,
                artifact_file=case.weak_candidate_file,
                artifact_sha256=case.weak_candidate_sha256,
                function=case.weak_test_function,
                base_sha=case.base_sha,
                head_sha=case.base_sha,
            )
        )
    controlled_suite = _run_controlled_suite(
        node_ids=manifest.controlled_checks,
        project_root=root.parent,
        runtime_root=cache_root.resolve().parent / "controlled-runtime",
        python_executable=executable,
    )
    return RawBenchmarkReport(
        manifest_sha256=manifest_sha256,
        started_at=started_at,
        completed_at=_aware_utc(now()),
        python_version=platform.python_version(),
        platform=platform.platform(),
        agent_candidate_generation="NOT_MEASURED_NO_CREDENTIALS",
        scenarios=tuple(observations),
        controlled_suite=controlled_suite,
    )


def _run_controlled_suite(
    *,
    node_ids: tuple[str, ...],
    project_root: Path,
    runtime_root: Path,
    python_executable: Path,
) -> ControlledSuiteObservation:
    result_path = runtime_root / "controlled-junit.xml"
    environment = ChildProcessEnvironmentPolicy().build(runtime_root=runtime_root / "process")
    result = BoundedSubprocessRunner(max_output_chars=12_000).run(
        (
            str(python_executable),
            "-m",
            "pytest",
            "-q",
            f"--junitxml={result_path}",
            *node_ids,
        ),
        cwd=project_root,
        environment=environment,
        timeout_seconds=120,
    )
    tests = failures = errors = skipped = 0
    if result_path.is_file():
        try:
            root = element_tree.parse(result_path).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
            tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
            failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
            errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
            skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
        except (OSError, ValueError, element_tree.ParseError):
            tests = failures = errors = skipped = 0
    return ControlledSuiteObservation(
        node_ids=node_ids,
        passed=max(0, tests - failures - errors - skipped),
        failed=failures,
        errors=errors,
        skipped=skipped,
        duration_seconds=result.duration_seconds,
        exit_code=result.returncode,
        timed_out=result.timed_out,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _run_scenario(
    *,
    case: HistoricalBenchmarkCase,
    repository: Path,
    workspace_root: Path,
    executable: Path,
    manifest_root: Path,
    truth: BenchmarkTruth,
    artifact_kind: ArtifactKind,
    artifact_file: str,
    artifact_sha256: str,
    function: str,
    base_sha: str,
    head_sha: str,
) -> ScenarioObservation:
    content = _load_artifact(manifest_root, artifact_file, artifact_sha256, function)
    safe_id = case.case_id.replace("-", "_")
    kind = "oracle" if artifact_kind is ArtifactKind.DEVELOPER_ORACLE else "weak"
    reader = DeterministicContextRetriever(source_repository=repository)
    try:
        contract, _, _ = resolve_contract_for_pair(
            prober=DependencyInstallProber(reader=reader),
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except ContractSynthesisError as error:
        raise BenchmarkInfrastructureError(
            f"case {case.case_id} has no safe symmetric dependency plan: {error}"
        ) from error
    relative_path = f"{contract.allowed_test_paths[0]}test_{safe_id}_{kind}.py"
    artifact = TestArtifact(
        relative_path=relative_path,
        node_id=f"{relative_path}::{function}",
        content=content,
    )
    runner = BaseHeadChallenge(
        workspaces=GitWorkspaceManager(
            source_repository=repository,
            workspace_root=workspace_root / case.case_id / kind,
            git_timeout_seconds=60,
        ),
        runner=PytestRunner(
            contract=contract,
            python_executable=executable,
            install_dependencies=True,
        ),
    )
    with runner.session(base_ref=base_sha, head_ref=head_sha) as session:
        readiness = session.prepare_environment()
        if not readiness.ready:
            raise BenchmarkInfrastructureError(
                f"case {case.case_id} environment is not ready: {readiness.reason}"
            )
        challenge = session.run(artifact=artifact)
    strategies = tuple(
        _strategy_observation(strategy, truth, challenge) for strategy in EvaluationStrategy
    )
    return ScenarioObservation(
        scenario_id=f"{case.case_id}-{kind}",
        case_id=case.case_id,
        repository=case.repository,
        pull_request_number=case.pull_request_number,
        truth=truth,
        artifact_kind=artifact_kind,
        artifact_sha256=artifact.sha256,
        base=_revision_observation(challenge.base),
        head=_revision_observation(challenge.head),
        mechanical_status=challenge.assessment.mechanical_status,
        differential_pattern=challenge.assessment.pattern,
        mechanical_reason=challenge.assessment.reason,
        strategies=strategies,
    )


def _strategy_observation(
    strategy: EvaluationStrategy,
    truth: BenchmarkTruth,
    challenge: ChallengeResult,
) -> StrategyObservation:
    if strategy is EvaluationStrategy.HEAD_ONLY:
        support = challenge.head.status is TestExecutionStatus.PASSED
    else:
        support = (
            challenge.assessment.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING
            and challenge.assessment.pattern
            is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
        )
    decision = EvaluationDecision.SUPPORT if support else EvaluationDecision.NO_SUPPORT
    return StrategyObservation(
        strategy=strategy,
        decision=decision,
        is_false_support=support and truth is BenchmarkTruth.FIX_ABSENT,
    )


def _revision_observation(result) -> RevisionObservation:
    return RevisionObservation(
        sha=result.revision.sha,
        status=result.status,
        collected_count=result.collected_count,
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
        artifact_sha256_before=result.artifact_sha256_before,
        artifact_sha256_after=result.artifact_sha256_after,
        stdout=result.stdout,
        stderr=result.stderr,
        detail=result.detail,
    )


def summarize(report: RawBenchmarkReport) -> BenchmarkSummary:
    """Derive transparent rates from raw rows; zero denominators remain unmeasured (`null`)."""
    metrics: list[StrategyMetrics] = []
    for strategy in EvaluationStrategy:
        rows = [
            (scenario, observation)
            for scenario in report.scenarios
            for observation in scenario.strategies
            if observation.strategy is strategy
        ]
        supports = [row for row in rows if row[1].decision is EvaluationDecision.SUPPORT]
        positives = [row for row in rows if row[0].truth is BenchmarkTruth.FIX_PRESENT]
        negatives = [row for row in rows if row[0].truth is BenchmarkTruth.FIX_ABSENT]
        true_supports = sum(row[0].truth is BenchmarkTruth.FIX_PRESENT for row in supports)
        false_supports = sum(row[1].is_false_support for row in supports)
        metrics.append(
            StrategyMetrics(
                strategy=strategy,
                evaluated_scenarios=len(rows),
                strong_supports=len(supports),
                true_supports=true_supports,
                false_supports=false_supports,
                no_supports=len(rows) - len(supports),
                false_support_rate=_rate(false_supports, len(supports)),
                negative_false_support_rate=_rate(false_supports, len(negatives)),
                positive_support_rate=_rate(true_supports, len(positives)),
            )
        )
    positive = [
        scenario for scenario in report.scenarios if scenario.truth is BenchmarkTruth.FIX_PRESENT
    ]
    negative = [
        scenario for scenario in report.scenarios if scenario.truth is BenchmarkTruth.FIX_ABSENT
    ]
    return BenchmarkSummary(
        methodology=report.methodology,
        manifest_sha256=report.manifest_sha256,
        historical_cases=len(positive),
        repositories=len({scenario.repository for scenario in report.scenarios}),
        scenarios=len(report.scenarios),
        historical_oracles_reproduced=sum(
            scenario.differential_pattern is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
            for scenario in positive
        ),
        weak_candidates_rejected=sum(
            scenario.mechanical_status is MechanicalEvidenceStatus.NON_DISCRIMINATING
            for scenario in negative
        ),
        controlled_selections=len(report.controlled_suite.node_ids),
        controlled_checks=(
            report.controlled_suite.passed
            + report.controlled_suite.failed
            + report.controlled_suite.errors
            + report.controlled_suite.skipped
        ),
        controlled_checks_passed=report.controlled_suite.passed,
        controlled_recovery_rate=_rate(
            report.controlled_suite.passed,
            report.controlled_suite.passed
            + report.controlled_suite.failed
            + report.controlled_suite.errors
            + report.controlled_suite.skipped,
        ),
        mean_challenge_latency_seconds=fmean(
            scenario.base.duration_seconds + scenario.head.duration_seconds
            for scenario in report.scenarios
        ),
        agent_candidate_generation=report.agent_candidate_generation,
        unmeasured_comparisons=(
            "existing upstream test-suite baseline",
            "live Gemini candidate generation",
            "end-to-end semantic claim accuracy",
            "model calls and token usage",
        ),
        strategy_metrics=tuple(metrics),
    )


def render_summary_markdown(summary: BenchmarkSummary) -> str:
    """Render a compact human-readable summary without overstating benchmark scope."""
    rows = []
    for item in summary.strategy_metrics:
        rows.append(
            "| "
            f"{item.strategy} | {item.evaluated_scenarios} | {item.strong_supports} | "
            f"{item.true_supports} | {item.false_supports} | "
            f"{_percent(item.false_support_rate)} | "
            f"{_percent(item.negative_false_support_rate)} |"
        )
    unmeasured = "\n".join(f"- {item}" for item in summary.unmeasured_comparisons)
    return (
        "# PatchProof Bench Summary\n\n"
        f"Methodology: `{summary.methodology}`.\n\n"
        f"- Historical PR cases: {summary.historical_cases} across "
        f"{summary.repositories} repositories\n"
        f"- Executed scenarios: {summary.scenarios}\n"
        f"- Developer oracles reproduced as BASE assertion failure / HEAD pass: "
        f"{summary.historical_oracles_reproduced}/{summary.historical_cases}\n"
        f"- Controlled weak candidates rejected as non-discriminating: "
        f"{summary.weak_candidates_rejected}/{summary.historical_cases}\n"
        f"- Controlled failure/recovery checks passed: "
        f"{summary.controlled_checks_passed}/{summary.controlled_checks} "
        f"({_percent(summary.controlled_recovery_rate)}) across "
        f"{summary.controlled_selections} selected nodes\n"
        f"- Mean two-revision challenge latency: "
        f"{summary.mean_challenge_latency_seconds:.3f} seconds\n\n"
        "## Policy comparison\n\n"
        "| Strategy | Scenarios | Supports | True supports | False supports | "
        "False/support | False/negative |\n"
        "|---|---:|---:|---:|---:|---:|---:|\n" + "\n".join(rows) + "\n\n"
        "`HEAD_ONLY` treats a passing candidate on HEAD as support. "
        "`PATCHPROOF_BASE_HEAD` requires "
        "the identical artifact to fail by assertion on BASE and pass on HEAD. The controlled "
        "negative uses an intentionally weak candidate and an unchanged buggy revision.\n\n"
        "## Not measured\n\n"
        f"{unmeasured}\n\n"
        "These are reference-oracle and policy-mechanics results, not a live Gemini generation "
        "quality claim or a representative production false-support estimate.\n"
    )


def write_reports(
    *,
    raw: RawBenchmarkReport,
    raw_path: Path,
    summary_path: Path,
    markdown_path: Path,
) -> BenchmarkSummary:
    """Write a raw report and both summary formats after the complete run succeeds."""
    summary = summarize(raw)
    _write_text(raw_path, raw.model_dump_json(indent=2) + "\n")
    _write_text(summary_path, summary.model_dump_json(indent=2) + "\n")
    _write_text(markdown_path, render_summary_markdown(summary))
    return summary


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percent(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.1%}"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("benchmark clock must return an aware timestamp")
    return value.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the reproducible PatchProof benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="validate manifest and artifact hashes")
    verify.add_argument("--manifest", type=Path, default=Path("benchmarks/manifest.json"))
    run = subparsers.add_parser("run", help="fetch revisions and execute every benchmark scenario")
    run.add_argument("--manifest", type=Path, default=Path("benchmarks/manifest.json"))
    run.add_argument("--cache", type=Path, default=Path(".patchproof-bench/repositories"))
    run.add_argument("--workspaces", type=Path, default=Path(".patchproof-bench/workspaces"))
    run.add_argument("--raw", type=Path, default=Path("benchmarks/results/raw.json"))
    run.add_argument("--summary", type=Path, default=Path("benchmarks/results/summary.json"))
    run.add_argument("--markdown", type=Path, default=Path("benchmarks/results/summary.md"))
    summarize_parser = subparsers.add_parser("summarize", help="regenerate summaries from raw JSON")
    summarize_parser.add_argument("--raw", type=Path, default=Path("benchmarks/results/raw.json"))
    summarize_parser.add_argument(
        "--summary", type=Path, default=Path("benchmarks/results/summary.json")
    )
    summarize_parser.add_argument(
        "--markdown", type=Path, default=Path("benchmarks/results/summary.md")
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """CLI entry point used by local evaluation and CI verification."""
    parsed = _parser().parse_args(arguments)
    if parsed.command == "verify":
        manifest, digest = load_manifest(parsed.manifest)
        print(f"verified {len(manifest.cases)} cases; manifest sha256={digest}")
        return 0
    if parsed.command == "run":
        raw = run_benchmark(
            manifest_path=parsed.manifest,
            cache_root=parsed.cache,
            workspace_root=parsed.workspaces,
        )
        summary = write_reports(
            raw=raw,
            raw_path=parsed.raw,
            summary_path=parsed.summary,
            markdown_path=parsed.markdown,
        )
        print(render_summary_markdown(summary))
        return 0
    try:
        raw = RawBenchmarkReport.model_validate_json(parsed.raw.read_bytes())
    except (OSError, ValueError) as error:
        raise BenchmarkConfigurationError("raw benchmark report could not be loaded") from error
    summary = summarize(raw)
    _write_text(parsed.summary, summary.model_dump_json(indent=2) + "\n")
    _write_text(parsed.markdown, render_summary_markdown(summary))
    print(render_summary_markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
