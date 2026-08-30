"""Sealed, historical-only holdout orchestration for the frozen PatchProof evaluator.

The default command is a deterministic preflight.  Live inference is unreachable unless the
operator supplies ``--live``; even then, this adapter delegates to the frozen hard-mode runner.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import patchproof.hard_mode as hard_mode
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.gemini_provider import GeminiProviderSurface
from patchproof.hard_mode import (
    HardModeCase,
    HardModeCaseKind,
    HardModeConfigurationError,
    HardModeManifest,
    HardModePacingPolicy,
    HardModeProtocol,
    HardModeRepositoryCache,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

DATASET_ID = "unseen-historical-python-pr-holdout-v1"
FROZEN_IMPLEMENTATION_SHA = "baf333afc160cd75a90cea1e0568120a9889fb7e"
SEALED_CONSTRUCTION_SHA = "113fc1af287b42447a7cde6b0a91241b7363c52c"
MODEL_NAME = "gemini-3.6-flash"
PROVIDER_LOCATION = "global"
CASE_COUNT = 10
MAXIMUM_LOGICAL_CALLS = 40
MAXIMUM_PROVIDER_CALLS = 80

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parents[1]
DEFAULT_MANIFEST_PATH = _HERE / "manifest.json"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / ".patchproof-holdout"
DEFAULT_OUTPUT_ROOT = _HERE / "results"
_MAX_MANIFEST_BYTES = 256_000
_SHA256_PATTERN = r"[0-9a-f]{64}"
_PROTECTED_HOLDOUT_PATHS = (
    "benchmarks/holdout/manifest.json",
    "benchmarks/holdout/oracle_gate.json",
    "benchmarks/holdout/oracles",
)


class HoldoutConfigurationError(HardModeConfigurationError):
    """Raised when sealed holdout orchestration fails closed."""


class HoldoutManifest(BaseModel):
    """Historical-only manifest wrapper; it deliberately does not relax HardModeManifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    dataset_id: Literal[DATASET_ID]
    created_at_utc: datetime
    frozen_patchproof_implementation_sha: Literal[FROZEN_IMPLEMENTATION_SHA]
    final_development_evaluation_commit: str = Field(pattern=r"[0-9a-f]{40}")
    holdout_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_ledger_sha256: str = Field(pattern=_SHA256_PATTERN)
    case_count: Literal[CASE_COUNT]
    repository_count: Literal[CASE_COUNT]
    future_provider_location: Literal[PROVIDER_LOCATION]
    construction_integrity: dict[str, Any]
    future_runner_compatibility: dict[str, Any]
    protocol: HardModeProtocol
    cases: tuple[HardModeCase, ...] = Field(min_length=CASE_COUNT, max_length=CASE_COUNT)

    @model_validator(mode="after")
    def validate_holdout_contract(self) -> HoldoutManifest:
        if any(case.kind is not HardModeCaseKind.HISTORICAL_PR for case in self.cases):
            raise ValueError("sealed holdout cases must all be HISTORICAL_PR")
        if len({case.repository for case in self.cases}) != CASE_COUNT:
            raise ValueError("sealed holdout must contain ten distinct repositories")
        if len({case.case_id for case in self.cases}) != CASE_COUNT:
            raise ValueError("sealed holdout case IDs must be unique")

        protocol = self.protocol
        expected_protocol_values = {
            "model_name": MODEL_NAME,
            "provider_surface": GeminiProviderSurface.VERTEX_AI,
            "temperature": 0.1,
            "thinking_level": "LOW",
            "claim_calls_per_case": 1,
            "candidate_calls_per_case": 2,
            "assessment_calls_per_discriminating_case": 1,
            "transient_provider_retries_per_logical_call": 1,
            "maximum_possible_logical_model_calls": MAXIMUM_LOGICAL_CALLS,
            "maximum_possible_provider_calls": MAXIMUM_PROVIDER_CALLS,
            "declared_available_provider_calls": MAXIMUM_PROVIDER_CALLS,
            "model_call_budget_preflight_passed": True,
            "pacing_policy": HardModePacingPolicy.BETWEEN_CASES,
            "inter_case_delay_seconds": 60.0,
        }
        for field_name, expected in expected_protocol_values.items():
            if getattr(protocol, field_name) != expected:
                raise ValueError(f"sealed holdout protocol has invalid {field_name}")
        if protocol.derive_maximum_possible_logical_model_calls(case_count=CASE_COUNT) != 40:
            raise ValueError("sealed holdout logical-call arithmetic is invalid")
        if protocol.derive_maximum_possible_provider_calls(case_count=CASE_COUNT) != 80:
            raise ValueError("sealed holdout provider-call arithmetic is invalid")
        return self


@dataclass(frozen=True)
class HoldoutPreflight:
    """Serializable proof that no-request holdout validation completed."""

    dataset_id: str
    manifest_sha256: str
    case_count: int
    repository_count: int
    model_name: str
    provider_surface: str
    provider_location: str
    maximum_logical_calls: int
    maximum_provider_calls: int
    execution_contract_version: int
    output_root: str
    frozen_source_unchanged: bool
    sealed_artifacts_unchanged: bool
    model_or_provider_calls: int = 0


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _relative_posix_path(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise HoldoutConfigurationError(f"{label} must be a normalized relative POSIX path")
    return path


def _verify_declared_hash(root: Path, relative_path: str, expected: str) -> None:
    path = root / _relative_posix_path(relative_path, label="declared artifact path")
    if not path.is_file() or _sha256(path.read_bytes()) != expected:
        raise HoldoutConfigurationError(f"declared artifact hash mismatch: {relative_path}")


def load_holdout_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> tuple[HoldoutManifest, str]:
    """Parse the sealed manifest without invoking HardModeManifest's synthetic-case rule."""
    resolved = path.resolve()
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        raise HoldoutConfigurationError("holdout manifest is unavailable") from error
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise HoldoutConfigurationError("holdout manifest is empty or oversized")
    try:
        manifest = HoldoutManifest.model_validate_json(raw)
    except ValueError as error:
        raise HoldoutConfigurationError("holdout manifest failed validation") from error

    root = resolved.parent
    _verify_declared_hash(root, "HOLDOUT_PROTOCOL.md", manifest.holdout_protocol_sha256)
    _verify_declared_hash(root, "selection_protocol.json", manifest.selection_protocol_sha256)
    _verify_declared_hash(root, "candidate_ledger.json", manifest.candidate_ledger_sha256)
    for case in manifest.cases:
        for repository_path in case.repository_python_paths:
            _relative_posix_path(repository_path, label="repository Python path")
        oracle_relative = _relative_posix_path(case.oracle_file, label="oracle path")
        oracle_path = (root / Path(*oracle_relative.parts)).resolve()
        if not oracle_path.is_relative_to(root) or not oracle_path.is_file():
            raise HoldoutConfigurationError(f"oracle is unavailable for {case.case_id}")
        oracle_bytes = oracle_path.read_bytes()
        if _sha256(oracle_bytes) != case.oracle_sha256:
            raise HoldoutConfigurationError(f"oracle hash mismatch for {case.case_id}")
        try:
            tree = ast.parse(oracle_bytes, filename=case.oracle_file)
        except SyntaxError as error:
            raise HoldoutConfigurationError(f"oracle syntax error for {case.case_id}") from error
        functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if functions != {case.oracle_test_function}:
            raise HoldoutConfigurationError(
                f"oracle must contain exactly its declared test for {case.case_id}"
            )
    _validate_construction_gate(root / "oracle_gate.json", manifest)
    return manifest, _sha256(raw)


def _validate_construction_gate(path: Path, manifest: HoldoutManifest) -> None:
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HoldoutConfigurationError("sealed oracle gate is unavailable or invalid") from error
    if (
        gate.get("dataset_id") != DATASET_ID
        or gate.get("all_base_results_are_assertion_failed") is not True
        or gate.get("all_head_results_are_passed") is not True
        or gate.get("all_pairs_used_identical_oracle_bytes") is not True
        or gate.get("model_requests_made") != 0
        or gate.get("provider_attempts_made") != 0
    ):
        raise HoldoutConfigurationError("sealed oracle gate aggregate is invalid")
    gated_cases = {case.get("case_id"): case for case in gate.get("cases", [])}
    if len(gated_cases) != CASE_COUNT:
        raise HoldoutConfigurationError("sealed oracle gate must contain exactly ten cases")
    for case in manifest.cases:
        gated = gated_cases.get(case.case_id)
        if (
            gated is None
            or gated.get("base_sha") != case.base_sha
            or gated.get("head_sha") != case.head_sha
            or gated.get("oracle_sha256") != case.oracle_sha256
            or gated.get("base_execution_result") != "ASSERTION_FAILED"
            or gated.get("head_execution_result") != "PASSED"
            or gated.get("identical_oracle_bytes_used") is not True
        ):
            raise HoldoutConfigurationError(f"sealed oracle gate mismatch for {case.case_id}")


def to_hard_mode_manifest(manifest: HoldoutManifest) -> HardModeManifest:
    """Map to frozen evaluator types after the stricter historical-only validation passes.

    ``model_construct`` bypasses only HardModeManifest's development-only synthetic coverage rule.
    The protocol and every case are already validated immutable frozen evaluator objects.
    """
    return HardModeManifest.model_construct(
        version=manifest.version,
        protocol=manifest.protocol,
        cases=manifest.cases,
    )


def _git_diff_is_clean(repo_root: Path, baseline: str, paths: tuple[str, ...]) -> bool:
    command = ["git", "diff", "--quiet", baseline, "--", *paths]
    try:
        completed = subprocess.run(command, cwd=repo_root, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HoldoutConfigurationError("Git integrity check could not run") from error
    if completed.returncode not in {0, 1}:
        raise HoldoutConfigurationError("Git integrity check failed")
    return completed.returncode == 0


def verify_frozen_source_integrity(
    repo_root: Path = PROJECT_ROOT,
    baseline: str = FROZEN_IMPLEMENTATION_SHA,
) -> None:
    """Fail if production behavior differs from the frozen implementation tree."""
    if not _git_diff_is_clean(repo_root.resolve(), baseline, ("src/patchproof",)):
        raise HoldoutConfigurationError(
            f"src/patchproof changed since frozen implementation {baseline}"
        )


def verify_sealed_holdout_integrity(
    repo_root: Path = PROJECT_ROOT,
    baseline: str = SEALED_CONSTRUCTION_SHA,
) -> None:
    """Fail if any manifest, gate, or oracle byte differs from the sealed commit."""
    if not _git_diff_is_clean(repo_root.resolve(), baseline, _PROTECTED_HOLDOUT_PATHS):
        raise HoldoutConfigurationError(
            f"sealed holdout artifacts changed since construction commit {baseline}"
        )


def _validate_output_root(manifest_path: Path, output_root: Path) -> Path:
    manifest_root = manifest_path.resolve().parent
    declared_output = manifest_root / "results"
    expected = declared_output.resolve()
    resolved = output_root.resolve()
    if resolved != expected or not resolved.is_relative_to(manifest_root):
        raise HoldoutConfigurationError(f"holdout output root must resolve exactly to {expected}")
    if declared_output.is_symlink():
        raise HoldoutConfigurationError("holdout output root must not be a symbolic link")
    if resolved.exists() and not resolved.is_dir():
        raise HoldoutConfigurationError("holdout output root exists but is not a directory")
    return resolved


def preflight_holdout(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = PROJECT_ROOT,
) -> tuple[HoldoutPreflight, HoldoutManifest, HardModeManifest]:
    """Perform deterministic validation without constructing any provider or model adapter."""
    manifest, manifest_sha256 = load_holdout_manifest(manifest_path)
    verify_frozen_source_integrity(repo_root)
    verify_sealed_holdout_integrity(repo_root)
    frozen_manifest = to_hard_mode_manifest(manifest)
    contract = hard_mode._contract()
    if contract.version != 1 or contract.python != "3.12":
        raise HoldoutConfigurationError("frozen execution contract could not be resolved")
    resolved_output = _validate_output_root(manifest_path, output_root)
    report = HoldoutPreflight(
        dataset_id=manifest.dataset_id,
        manifest_sha256=manifest_sha256,
        case_count=len(manifest.cases),
        repository_count=len({case.repository for case in manifest.cases}),
        model_name=manifest.protocol.model_name,
        provider_surface=str(manifest.protocol.provider_surface),
        provider_location=manifest.future_provider_location,
        maximum_logical_calls=MAXIMUM_LOGICAL_CALLS,
        maximum_provider_calls=MAXIMUM_PROVIDER_CALLS,
        execution_contract_version=contract.version,
        output_root=str(resolved_output),
        frozen_source_unchanged=True,
        sealed_artifacts_unchanged=True,
    )
    return report, manifest, frozen_manifest


def _write_live_context_gate(
    *,
    manifest: HoldoutManifest,
    manifest_sha256: str,
    manifest_path: Path,
    cache_root: Path,
    gate_path: Path,
) -> None:
    """Create the no-oracle context gate consumed by the frozen live runner."""
    if gate_path.exists():
        return
    repositories = HardModeRepositoryCache(cache_root, manifest_path.resolve().parent)
    cases: list[dict[str, Any]] = []
    for case in manifest.cases:
        repository = repositories.prepare(case)
        changed_tests = repositories.changed_python_test_paths(case, repository)
        if changed_tests != tuple(sorted(case.excluded_paths)):
            raise HoldoutConfigurationError(
                f"changed-test exclusions differ for {case.case_id}: {changed_tests!r}"
            )
        context = DeterministicContextRetriever(
            source_repository=repository,
            excluded_paths=frozenset(case.excluded_paths),
        ).retrieve(base_sha=case.base_sha, head_sha=case.head_sha)
        cases.append(
            {
                "case_id": case.case_id,
                "accepted": True,
                "anti_leakage": {
                    "context_sha256": _sha256(context.model_dump_json().encode("utf-8"))
                },
            }
        )
    gate = {
        "schema_version": 1,
        "manifest_sha256": manifest_sha256,
        "accepted": True,
        "case_count": len(cases),
        "cases": cases,
    }
    hard_mode._write_json(gate_path, gate)


def _run_live_authorized(
    *,
    manifest_path: Path,
    runtime_root: Path,
    output_root: Path,
    manifest: HoldoutManifest,
    frozen_manifest: HardModeManifest,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Delegate the authorized run to V5's exact generic live runner."""
    cache_root = runtime_root / "repositories"
    gate_path = runtime_root / "context_gate.json"
    _write_live_context_gate(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        manifest_path=manifest_path,
        cache_root=cache_root,
        gate_path=gate_path,
    )
    original_loader = hard_mode.load_hard_mode_manifest

    def load_validated_holdout(requested_path: Path) -> tuple[HardModeManifest, str]:
        if requested_path.resolve() != manifest_path.resolve():
            raise HoldoutConfigurationError("frozen runner requested an unexpected manifest")
        return frozen_manifest, manifest_sha256

    hard_mode.load_hard_mode_manifest = load_validated_holdout
    try:
        return hard_mode.run_live(
            manifest_path=manifest_path,
            cache_root=cache_root,
            workspace_root=runtime_root / "workspaces",
            gate_path=gate_path,
            journal_path=output_root / "live_journal.jsonl",
            raw_path=output_root / "raw.json",
        )
    finally:
        hard_mode.load_hard_mode_manifest = original_loader


def execute_holdout(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = PROJECT_ROOT,
    live: bool = False,
) -> HoldoutPreflight | dict[str, Any]:
    """Preflight by default; enter the frozen live runner only with explicit authorization."""
    report, manifest, frozen_manifest = preflight_holdout(
        manifest_path=manifest_path,
        output_root=output_root,
        repo_root=repo_root,
    )
    if not live:
        return report
    runtime = runtime_root.resolve()
    if not runtime.is_relative_to(repo_root.resolve()) or runtime == repo_root.resolve():
        raise HoldoutConfigurationError("holdout runtime root must remain inside the repository")
    return _run_live_authorized(
        manifest_path=manifest_path,
        runtime_root=runtime,
        output_root=output_root.resolve(),
        manifest=manifest,
        frozen_manifest=frozen_manifest,
        manifest_sha256=report.manifest_sha256,
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly authorize the sealed live evaluation (omitting this is preflight only)",
    )
    args = parser.parse_args(arguments)
    result = execute_holdout(
        manifest_path=args.manifest,
        runtime_root=args.runtime_root,
        output_root=args.output_root,
        live=args.live,
    )
    document = asdict(result) if isinstance(result, HoldoutPreflight) else result
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
