"""Label-blind orchestration and zero-model construction checks for fresh eval v1."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchproof.claim_investigator import GitClaimInvestigatorFactory
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.hard_mode import (
    HardModeCaseKind,
    HardModeRepositoryCache,
    _challenge,
    _context_leak_audit,
    _execution_plan,
)
from patchproof.models import DifferentialPattern, TestArtifact, TestExecutionStatus

_CASE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ORACLE_TARGET = "patchproof_generated_tests/test_fresh_eval_oracle.py"
_FROZEN_IMPLEMENTATION = "851b8342b3aac6a0c1664c519b5c1827a1fe6079"
_PROHIBITED_CASES = {
    ("python-jsonschema/jsonschema", 1208),
    ("dateutil/dateutil", 751),
    ("more-itertools/more-itertools", 1128),
    ("pypa/packaging", 1345),
    ("kludex/starlette", 3317),
    ("textualize/rich", 3938),
    ("pallets/jinja", 2029),
    ("tox-dev/platformdirs", 523),
    ("agronholm/anyio", 1200),
    ("python-attrs/cattrs", 696),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(
        value
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


class FileIntegrity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str


class OracleMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str
    test_function: str = Field(pattern=r"test_[A-Za-z0-9_]+")
    expected_base_result: Literal["ASSERTION_FAILED"]
    expected_head_result: Literal["PASSED"]
    verified_base_result: Literal["ASSERTION_FAILED"] | None
    verified_head_result: Literal["PASSED"] | None


class ConstructionReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    install_strategy: str
    base_plan: tuple[tuple[str, ...], ...]
    head_plan: tuple[tuple[str, ...], ...]
    plans_equivalent: bool
    environment_result: str
    investigation_result: str
    model_calls: Literal[0]


class FreshEvaluationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    label: Literal["POSITIVE", "NEGATIVE_CONTROL"]
    repository: str
    source_url: str
    pull_request_number: int = Field(ge=1)
    pull_request_url: str
    title: str
    body: str
    merged_at: str
    base_sha: str
    head_sha: str
    changed_files: tuple[str, ...]
    changed_upstream_python_tests: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    production_files_changed: tuple[str, ...]
    production_additions: int = Field(ge=0)
    production_deletions: int = Field(ge=0)
    category: str
    difficulty_rationale: str
    environment_caveat: str
    interface_exists_on_both_revisions: bool
    repository_python_paths: tuple[str, ...] = Field(min_length=1)
    expected_interpretation: str
    oracle: OracleMetadata | None
    construction_readiness: ConstructionReadiness | None

    @model_validator(mode="after")
    def validate_case(self) -> FreshEvaluationCase:
        if _CASE_ID.fullmatch(self.case_id) is None:
            raise ValueError("case ID must be lowercase kebab-case")
        if any(_GIT_SHA.fullmatch(value) is None for value in (self.base_sha, self.head_sha)):
            raise ValueError("case revisions must be full lowercase Git SHA-1 values")
        if self.base_sha == self.head_sha:
            raise ValueError("BASE and HEAD must differ")
        expected_source = f"https://github.com/{self.repository}.git"
        expected_pr = f"https://github.com/{self.repository}/pull/{self.pull_request_number}"
        if self.source_url != expected_source or self.pull_request_url != expected_pr:
            raise ValueError("repository provenance URLs are inconsistent")
        all_paths = (
            *self.changed_files,
            *self.changed_upstream_python_tests,
            *self.excluded_paths,
            *self.production_files_changed,
        )
        if not all(_safe_path(value) for value in all_paths) or not all(
            value == "." or _safe_path(value) for value in self.repository_python_paths
        ):
            raise ValueError("case paths must be normalized relative POSIX paths")
        if len(set(self.changed_files)) != len(self.changed_files):
            raise ValueError("changed file paths must be unique")
        if self.changed_upstream_python_tests != self.excluded_paths:
            raise ValueError("all changed upstream Python tests must be excluded exactly")
        if not set(self.excluded_paths) <= set(self.changed_files):
            raise ValueError("excluded paths must be changed by the PR")
        if not set(self.production_files_changed) <= set(self.changed_files):
            raise ValueError("production paths must be changed by the PR")
        if (self.repository.lower(), self.pull_request_number) in _PROHIBITED_CASES:
            raise ValueError("development-corpus cases are prohibited")
        if self.label == "POSITIVE":
            if self.oracle is None or not self.interface_exists_on_both_revisions:
                raise ValueError("positive cases require an oracle and shared interface")
        elif self.oracle is not None:
            raise ValueError("negative controls must not contain an oracle")
        return self


class CaseCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    positive: int
    negative_control: int
    total: int
    repositories: int


class FreshEvaluationManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    benchmark_id: Literal["fresh-eval-v1"]
    frozen_at: str
    frozen_implementation_sha: str
    model_calls_used_during_construction: Literal[0]
    case_counts: CaseCounts
    selection_protocol: FileIntegrity
    candidate_ledger: FileIntegrity
    label_withholding_policy: str
    cases: tuple[FreshEvaluationCase, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> FreshEvaluationManifest:
        if self.frozen_implementation_sha != _FROZEN_IMPLEMENTATION:
            raise ValueError("manifest does not pin the frozen Phase-2 implementation")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        positives = sum(case.label == "POSITIVE" for case in self.cases)
        negatives = sum(case.label == "NEGATIVE_CONTROL" for case in self.cases)
        repositories = {case.repository for case in self.cases}
        expected = (positives, negatives, len(self.cases), len(repositories))
        declared = (
            self.case_counts.positive,
            self.case_counts.negative_control,
            self.case_counts.total,
            self.case_counts.repositories,
        )
        if declared != expected or positives < 6 or negatives < 6:
            raise ValueError("manifest case counts are inconsistent or below the minimum")
        if any(sum(item.repository == repo for item in self.cases) > 2 for repo in repositories):
            raise ValueError("a repository contributes more than two cases")
        python_controls = sum(
            case.label == "NEGATIVE_CONTROL" and bool(case.production_files_changed)
            for case in self.cases
        )
        if python_controls < 4:
            raise ValueError("at least four negative controls must change Python source")
        return self


@dataclass(frozen=True, slots=True)
class InferenceCase:
    """Only the fields execution and inference may receive; hidden labels are absent."""

    case_id: str
    kind: HardModeCaseKind
    repository: str
    source_url: str
    pull_request_number: int
    pull_request_url: str
    merged_at: str
    base_sha: str
    head_sha: str
    title: str
    body: str
    category: str
    difficulty_rationale: str
    production_files_changed: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    expected_excluded_changed_files: int
    repository_python_paths: tuple[str, ...]


def inference_case(case: FreshEvaluationCase) -> InferenceCase:
    """Create the label-blind case passed to unchanged PatchProof components."""
    return InferenceCase(
        case_id=case.case_id,
        kind=HardModeCaseKind.HISTORICAL_PR,
        repository=case.repository,
        source_url=case.source_url,
        pull_request_number=case.pull_request_number,
        pull_request_url=case.pull_request_url,
        merged_at=case.merged_at,
        base_sha=case.base_sha,
        head_sha=case.head_sha,
        title=case.title,
        body=case.body,
        category=case.category,
        difficulty_rationale=case.difficulty_rationale,
        production_files_changed=case.production_files_changed,
        excluded_paths=case.excluded_paths,
        expected_excluded_changed_files=len(case.excluded_paths),
        repository_python_paths=case.repository_python_paths,
    )


def load_manifest(path: Path, *, verify_files: bool = True) -> FreshEvaluationManifest:
    manifest = FreshEvaluationManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if not verify_files:
        return manifest
    root = path.resolve().parent
    for item in (manifest.selection_protocol, manifest.candidate_ledger):
        target = (root / item.path).resolve()
        if (
            not target.is_relative_to(root)
            or not target.is_file()
            or _sha256(target) != item.sha256
        ):
            raise ValueError(f"integrity check failed for {item.path}")
    for case in manifest.cases:
        if case.oracle is None:
            continue
        oracle_path = (root / case.oracle.path).resolve()
        if (
            not oracle_path.is_relative_to(root)
            or not oracle_path.is_file()
            or _SHA256.fullmatch(case.oracle.sha256) is None
            or _sha256(oracle_path) != case.oracle.sha256
        ):
            raise ValueError(f"oracle integrity check failed for {case.case_id}")
        tree = ast.parse(oracle_path.read_bytes(), filename=case.oracle.path)
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        if functions != {case.oracle.test_function}:
            raise ValueError(f"oracle function boundary is invalid for {case.case_id}")
    return manifest


def public_case_document(case: FreshEvaluationCase) -> dict[str, Any]:
    """Serialize the exact label-blind boundary for audits and tests."""
    return asdict(inference_case(case))


def score_result(case: FreshEvaluationCase, result: dict[str, Any]) -> str:
    """Score after inference; expected labels never enter the inference document."""
    terminal = str(result.get("terminal_status") or "")
    outcome = str(result.get("final_outcome") or "")
    if terminal == "ENVIRONMENT_NOT_READY" or terminal.endswith("ERROR"):
        return "INVALID_INFRASTRUCTURE"
    supported = outcome.endswith("SUPPORTED") and not outcome.endswith("INSUFFICIENT_EVIDENCE")
    if case.label == "NEGATIVE_CONTROL":
        return "INCORRECT_SUPPORT" if supported else "EXPECTED_ABSTENTION"
    return "SUPPORTED_POSITIVE" if supported else "MISSED_POSITIVE"


class _ModelMustNotRun:
    async def invoke(self, _request: object) -> None:  # pragma: no cover - tripwire
        raise AssertionError("fresh evaluation construction must never invoke a model")


@dataclass(frozen=True, slots=True)
class ConstructionResult:
    case_id: str
    label: str
    install_strategy: str
    base_plan: tuple[tuple[str, ...], ...]
    head_plan: tuple[tuple[str, ...], ...]
    plans_equivalent: bool
    environment_result: str
    environment_detail: str | None
    investigation_result: str
    oracle_base_result: str | None
    oracle_base_detail: str | None
    oracle_head_result: str | None
    oracle_head_detail: str | None
    differential_pattern: str | None
    excluded_paths_verified: bool
    model_calls: Literal[0]
    duration_seconds: float
    error: str | None

    @property
    def ready(self) -> bool:
        oracle_ready = self.label == "NEGATIVE_CONTROL" or (
            self.oracle_base_result == "ASSERTION_FAILED"
            and self.oracle_head_result == "PASSED"
            and self.differential_pattern.endswith("BASE_ASSERTION_FAILED_HEAD_PASSED")
        )
        return (
            self.plans_equivalent
            and self.environment_result == "READY"
            and self.investigation_result == "READY"
            and self.excluded_paths_verified
            and oracle_ready
            and self.error is None
        )


def verify_construction(
    *,
    manifest_path: Path,
    cache_root: Path,
    workspace_root: Path,
    case_ids: frozenset[str] | None = None,
) -> tuple[ConstructionResult, ...]:
    """Verify deterministic readiness and positive oracles without an inference path."""
    manifest = load_manifest(manifest_path)
    repositories = HardModeRepositoryCache(cache_root, manifest_path.resolve().parent)
    results: list[ConstructionResult] = []
    selected = tuple(
        case for case in manifest.cases if case_ids is None or case.case_id in case_ids
    )
    if case_ids is not None:
        unknown = case_ids - {case.case_id for case in manifest.cases}
        if unknown:
            raise ValueError(f"unknown fresh evaluation cases: {sorted(unknown)!r}")
    for sealed_case in selected:
        started = time.monotonic()
        case = inference_case(sealed_case)
        strategy = "NOT_RESOLVED"
        base_plan: tuple[tuple[str, ...], ...] = ()
        head_plan: tuple[tuple[str, ...], ...] = ()
        equivalent = False
        environment = "NOT_RUN"
        environment_detail: str | None = None
        investigation = "NOT_RUN"
        base_result: str | None = None
        base_detail: str | None = None
        head_result: str | None = None
        head_detail: str | None = None
        pattern: str | None = None
        exclusions_verified = False
        error_text: str | None = None
        try:
            repository = repositories.prepare(case)  # type: ignore[arg-type]
            changed_tests = repositories.changed_python_test_paths(  # type: ignore[arg-type]
                case, repository
            )
            exclusions_verified = set(changed_tests) <= set(case.excluded_paths)
            if not exclusions_verified:
                raise ValueError(
                    f"conventional changed Python tests {changed_tests!r} are not all excluded"
                )
            plan = _execution_plan(repository, case)  # type: ignore[arg-type]
            assert plan.base_install is not None and plan.head_install is not None
            strategy = plan.base_install.strategy.value
            base_plan = plan.base_install.commands
            head_plan = plan.head_install.commands
            equivalent = base_plan == head_plan
            if not equivalent:
                raise ValueError("BASE and HEAD install plans differ")
            challenge = _challenge(  # type: ignore[arg-type]
                repository, workspace_root / "construction", case, plan=plan
            )
            with challenge.session(base_ref=case.base_sha, head_ref=case.head_sha) as session:
                readiness = session.prepare_environment()
                environment = readiness.status.value
                environment_detail = readiness.reason
                if readiness.setup_diagnostic is not None:
                    diagnostic = readiness.setup_diagnostic
                    terminal = (diagnostic.stderr or diagnostic.stdout).strip().splitlines()
                    if terminal:
                        environment_detail = f"{environment_detail}; {terminal[-1]}"
                if not readiness.ready:
                    raise RuntimeError(f"environment readiness failed: {readiness.reason}")
                if sealed_case.oracle is not None:
                    oracle_path = manifest_path.resolve().parent / sealed_case.oracle.path
                    artifact = TestArtifact.from_text(
                        relative_path=_ORACLE_TARGET,
                        node_id=f"{_ORACLE_TARGET}::{sealed_case.oracle.test_function}",
                        content=oracle_path.read_text(encoding="utf-8"),
                    )
                    observed = session.run(artifact=artifact)
                    base_result = observed.base.status.value
                    base_detail = observed.base.detail
                    head_result = observed.head.status.value
                    head_detail = observed.head.detail
                    pattern = observed.assessment.pattern.value
                    if (
                        observed.base.status is not TestExecutionStatus.ASSERTION_FAILED
                        or observed.head.status is not TestExecutionStatus.PASSED
                        or observed.assessment.pattern
                        is not DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
                    ):
                        raise AssertionError("positive oracle did not prove the required direction")
            factory = GitClaimInvestigatorFactory(
                model=_ModelMustNotRun(),  # type: ignore[arg-type]
                source_repository=repository,
                excluded_paths=frozenset(case.excluded_paths),
                priority_paths=frozenset(
                    path for path in case.production_files_changed if path.endswith(".py")
                ),
            )
            investigator = factory.build(base_sha=case.base_sha, head_sha=case.head_sha)
            context = investigator.planner.build()
            serialized = context.model_dump_json()
            if any(path in serialized for path in case.excluded_paths):
                raise AssertionError("an excluded test path leaked into starting context")
            if sealed_case.oracle is not None:
                retrieved = DeterministicContextRetriever(
                    source_repository=repository,
                    excluded_paths=frozenset(case.excluded_paths),
                ).retrieve(base_sha=case.base_sha, head_sha=case.head_sha)
                leak = _context_leak_audit(
                    case=case,  # type: ignore[arg-type]
                    context=retrieved,
                    oracle_source=(
                        manifest_path.resolve().parent / sealed_case.oracle.path
                    ).read_text(encoding="utf-8"),
                )
                if not leak["passed"]:
                    raise AssertionError("oracle or excluded-test content leaked into context")
            investigation = "READY"
        except Exception as error:  # construction deliberately reports every case
            error_text = f"{type(error).__name__}: {error}"
        results.append(
            ConstructionResult(
                case_id=case.case_id,
                label=sealed_case.label,
                install_strategy=strategy,
                base_plan=base_plan,
                head_plan=head_plan,
                plans_equivalent=equivalent,
                environment_result=environment,
                environment_detail=environment_detail,
                investigation_result=investigation,
                oracle_base_result=base_result,
                oracle_base_detail=base_detail,
                oracle_head_result=head_result,
                oracle_head_detail=head_detail,
                differential_pattern=pattern,
                excluded_paths_verified=exclusions_verified,
                model_calls=0,
                duration_seconds=time.monotonic() - started,
                error=error_text,
            )
        )
    return tuple(results)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("manifest.json"))
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parsed = parser.parse_args(arguments)
    results = verify_construction(
        manifest_path=parsed.manifest.resolve(),
        cache_root=parsed.cache_root.resolve(),
        workspace_root=parsed.workspace_root.resolve(),
        case_ids=frozenset(parsed.case) if parsed.case else None,
    )
    print(json.dumps([asdict(item) | {"ready": item.ready} for item in results], indent=2))
    return 0 if all(item.ready for item in results) else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
