"""Exactly-once, label-blind inference harness for sealed fresh evaluation v1."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import patchproof.hard_mode as hard_mode  # noqa: E402
from patchproof.claim_investigator import GitClaimInvestigatorFactory  # noqa: E402
from patchproof.context_retrieval import DeterministicContextRetriever  # noqa: E402
from patchproof.gemini_provider import (  # noqa: E402
    GeminiProviderConfig,
    GeminiProviderSurface,
    preflight_vertex_authentication,
)
from patchproof.hard_mode import HardModeCaseKind, HardModeRepositoryCache  # noqa: E402
from patchproof.models import ClaimOutcome, MechanicalEvidenceStatus  # noqa: E402
from patchproof.pr_analyze import MODEL_NAME  # noqa: E402

FROZEN_IMPLEMENTATION = "851b8342b3aac6a0c1664c519b5c1827a1fe6079"
BENCHMARK_ID = "fresh-eval-v1"
SEALED_ROOT = PROJECT_ROOT / "benchmarks" / "fresh_eval_v1"
MANIFEST_PATH = SEALED_ROOT / "manifest.json"
INTEGRITY_PATH = SEALED_ROOT / "integrity.json"
ARTIFACT_ROOT = PROJECT_ROOT / ".patchproof" / "fresh-eval-v1-sealed"
PUBLIC_CASES_PATH = ARTIFACT_ROOT / "public_cases.json"
PREPARE_METADATA_PATH = ARTIFACT_ROOT / "prepare_metadata.json"
LIVE_ROOT = ARTIFACT_ROOT / "live"
JOURNAL_PATH = LIVE_ROOT / "journal.jsonl"
RAW_RESULTS_PATH = LIVE_ROOT / "raw_results.json"
RUN_METADATA_PATH = LIVE_ROOT / "run_metadata.json"
SCORED_RESULTS_PATH = LIVE_ROOT / "scored_results.json"
SUMMARY_PATH = LIVE_ROOT / "summary.md"
INTER_CASE_DELAY_SECONDS = 60.0

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "label",
        "expected_interpretation",
        "oracle",
        "oracle_path",
        "oracle_sha256",
        "oracle_function",
        "construction_readiness",
    }
)


class SealedHarnessError(RuntimeError):
    """Fail-closed error raised by the external sealed harness."""


def _default_runtime_paths(
    *,
    platform_name: str = os.name,
    environment: Mapping[str, str] = os.environ,
) -> tuple[Path, Path]:
    """Choose short deterministic disposable roots without moving durable artifacts."""
    if platform_name == "nt":
        temp_value = environment.get("TEMP") or environment.get("TMP")
        if not temp_value:
            raise SealedHarnessError("Windows TEMP is unavailable for sealed evaluation")
        temp_root = Path(temp_value).resolve()
        return temp_root / "patchproof-fresh-eval-repositories", temp_root / "fe"
    return ARTIFACT_ROOT / "repositories", ARTIFACT_ROOT / "workspaces"


REPOSITORY_CACHE, WORKSPACE_ROOT = _default_runtime_paths()


class PublicCase(BaseModel):
    """The complete label-blind case boundary accepted by RUN."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    kind: Literal[HardModeCaseKind.HISTORICAL_PR]
    repository: str
    source_url: str
    pull_request_number: int = Field(ge=1)
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
    expected_excluded_changed_files: int = Field(ge=0)
    repository_python_paths: tuple[str, ...]
    context_sha256: str = Field(pattern=r"[0-9a-f]{64}")

    @model_validator(mode="after")
    def validate_public_case(self) -> PublicCase:
        if self.expected_excluded_changed_files != len(self.excluded_paths):
            raise ValueError("declared excluded-path count is inconsistent")
        return self


class PublicRunDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    benchmark_id: Literal["fresh-eval-v1"]
    frozen_implementation_sha: Literal[FROZEN_IMPLEMENTATION]
    pinned_model_name: str
    cases: tuple[PublicCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> PublicRunDocument:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("public case IDs must be unique")
        return self


class PrepareMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    benchmark_id: Literal["fresh-eval-v1"]
    prepared_at: str
    sealed_manifest_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    sealed_integrity_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    public_document_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    frozen_source_identity: dict[str, Any]
    repository_cache_path: str
    workspace_root_path: str
    model_calls: Literal[0]


class _ModelMustNotRun:
    async def invoke(self, _request: object) -> None:  # pragma: no cover - hard tripwire
        raise AssertionError("PREPARE must never invoke a model")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_bytes(document: Any) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_json(path: Path, document: Any) -> None:
    _atomic_write(path, _canonical_bytes(document))


def _append_journal(path: Path, document: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "a"
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _git(project_root: Path, *arguments: str, allowed_returncodes: tuple[int, ...] = (0,)) -> str:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SealedHarnessError("could not verify frozen production source") from error
    if completed.returncode not in allowed_returncodes:
        detail = (completed.stderr or completed.stdout).strip()[-1_000:]
        raise SealedHarnessError(f"Git source verification failed: {detail}")
    return completed.stdout.strip()


def verify_frozen_source(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Verify committed, staged, unstaged, and untracked production source."""
    root = project_root.resolve()
    _git(root, "cat-file", "-e", f"{FROZEN_IMPLEMENTATION}^{{commit}}")
    checks = (
        ("diff", "--quiet", FROZEN_IMPLEMENTATION, "HEAD", "--", "src/patchproof"),
        ("diff", "--quiet", "--", "src/patchproof"),
        ("diff", "--cached", "--quiet", "--", "src/patchproof"),
    )
    for arguments in checks:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise SealedHarnessError(
                "src/patchproof differs from the frozen implementation; refusing to proceed"
            )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", "src/patchproof")
    if untracked:
        raise SealedHarnessError("untracked files exist under src/patchproof")
    tracked = _git(root, "ls-files", "--", "src/patchproof").splitlines()
    digest = hashlib.sha256()
    for relative in tracked:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    frozen_tree = _git(root, "rev-parse", f"{FROZEN_IMPLEMENTATION}:src/patchproof")
    head_tree = _git(root, "rev-parse", "HEAD:src/patchproof")
    if head_tree != frozen_tree:
        raise SealedHarnessError("committed production tree identity differs from frozen source")
    return {
        "frozen_implementation_sha": FROZEN_IMPLEMENTATION,
        "current_head": _git(root, "rev-parse", "HEAD"),
        "frozen_src_tree": frozen_tree,
        "head_src_tree": head_tree,
        "working_src_sha256": digest.hexdigest(),
        "tracked_file_count": len(tracked),
        "staged_clean": True,
        "unstaged_clean": True,
        "untracked_clean": True,
    }


def _sealed_evaluation() -> Any:
    """Import sealed helpers only in PREPARE or SCORE, never from RUN."""
    from benchmarks.fresh_eval_v1 import evaluation

    return evaluation


def _verify_integrity() -> tuple[Any, str, str]:
    evaluation = _sealed_evaluation()
    manifest = evaluation.load_manifest(MANIFEST_PATH)
    raw_integrity = INTEGRITY_PATH.read_bytes()
    try:
        integrity = json.loads(raw_integrity)
    except json.JSONDecodeError as error:
        raise SealedHarnessError("sealed integrity document is invalid") from error
    if (
        integrity.get("algorithm") != "sha256"
        or integrity.get("frozen_implementation_sha") != FROZEN_IMPLEMENTATION
        or not isinstance(integrity.get("files"), dict)
    ):
        raise SealedHarnessError("sealed integrity metadata is invalid")
    for relative, expected in integrity["files"].items():
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise SealedHarnessError("sealed integrity document contains an unsafe path")
        target = (SEALED_ROOT / Path(*path.parts)).resolve()
        if not target.is_relative_to(SEALED_ROOT.resolve()) or not target.is_file():
            raise SealedHarnessError(f"sealed file is unavailable: {relative}")
        if _sha256(target.read_bytes()) != expected:
            raise SealedHarnessError(f"sealed file integrity failed: {relative}")
    return manifest, _sha256(MANIFEST_PATH.read_bytes()), _sha256(raw_integrity)


def _assert_public_document_blind(
    document: Mapping[str, Any], hidden_cases: tuple[Any, ...]
) -> None:
    serialized = json.dumps(document, sort_keys=True, ensure_ascii=False)
    lowered_keys: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            lowered_keys.update(str(key).lower() for key in value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list | tuple):
            for nested in value:
                visit(nested)

    visit(document)
    forbidden_keys = _FORBIDDEN_PUBLIC_KEYS & lowered_keys
    if forbidden_keys:
        raise SealedHarnessError(
            f"hidden keys leaked into public document: {sorted(forbidden_keys)}"
        )
    if "POSITIVE" in serialized or "NEGATIVE_CONTROL" in serialized:
        raise SealedHarnessError("hidden classification leaked into public document")
    for case in hidden_cases:
        if case.oracle is None:
            continue
        forbidden_values = (case.oracle.path, case.oracle.sha256, case.oracle.test_function)
        if any(value in serialized for value in forbidden_values):
            raise SealedHarnessError("hidden oracle identity leaked into public document")


def _case_with_context(document: dict[str, Any], context_sha256: str) -> dict[str, Any]:
    return {**document, "kind": str(document["kind"]), "context_sha256": context_sha256}


def _verify_changed_test_exclusions(case: Any, detected: tuple[str, ...]) -> None:
    missing = tuple(sorted(set(detected) - set(case.excluded_paths)))
    if missing:
        raise SealedHarnessError(
            f"changed Python tests are not excluded for {case.case_id}: {missing!r}"
        )


def _priority_paths(case: Any) -> frozenset[str]:
    return frozenset(path for path in case.production_files_changed if path.endswith(".py"))


def prepare(
    *,
    project_root: Path = PROJECT_ROOT,
    artifact_root: Path = ARTIFACT_ROOT,
    repository_cache: Path = REPOSITORY_CACHE,
    workspace_root: Path = WORKSPACE_ROOT,
) -> dict[str, Any]:
    """Verify and materialize the deterministic label-blind inference gate."""
    live_root = artifact_root / "live"
    if (live_root / "journal.jsonl").exists() or (live_root / "raw_results.json").exists():
        raise SealedHarnessError("sealed RUN has started; PREPARE can no longer be replaced")
    source_identity = verify_frozen_source(project_root)
    manifest, manifest_sha, integrity_sha = _verify_integrity()
    evaluation = _sealed_evaluation()
    repositories = HardModeRepositoryCache(repository_cache, SEALED_ROOT)
    public_cases: list[dict[str, Any]] = []
    for hidden_case in manifest.cases:
        public = evaluation.inference_case(hidden_case)
        print(f"[PREPARE] {public.case_id}: preparing repository and context", flush=True)
        repository = repositories.prepare(public)
        changed_tests = repositories.changed_python_test_paths(public, repository)
        _verify_changed_test_exclusions(public, changed_tests)
        retriever = DeterministicContextRetriever(
            source_repository=repository,
            excluded_paths=frozenset(public.excluded_paths),
        )
        context = retriever.retrieve(base_sha=public.base_sha, head_sha=public.head_sha)
        context_sha = _sha256(context.model_dump_json().encode("utf-8"))
        factory = GitClaimInvestigatorFactory(
            model=_ModelMustNotRun(),  # type: ignore[arg-type]
            source_repository=repository,
            excluded_paths=frozenset(public.excluded_paths),
            priority_paths=_priority_paths(public),
        )
        starting_context = factory.build(
            base_sha=public.base_sha, head_sha=public.head_sha
        ).planner.build()
        starting_serialized = starting_context.model_dump_json()
        if any(path in starting_serialized for path in public.excluded_paths):
            raise SealedHarnessError(f"excluded test leaked into Phase-2 context: {public.case_id}")
        public_cases.append(
            _case_with_context(evaluation.public_case_document(hidden_case), context_sha)
        )
    public_document = {
        "schema_version": 1,
        "benchmark_id": BENCHMARK_ID,
        "frozen_implementation_sha": FROZEN_IMPLEMENTATION,
        "pinned_model_name": MODEL_NAME,
        "cases": public_cases,
    }
    PublicRunDocument.model_validate(public_document)
    _assert_public_document_blind(public_document, manifest.cases)
    public_bytes = _canonical_bytes(public_document)
    metadata = {
        "schema_version": 1,
        "benchmark_id": BENCHMARK_ID,
        "prepared_at": _utc_now(),
        "sealed_manifest_sha256": manifest_sha,
        "sealed_integrity_sha256": integrity_sha,
        "public_document_sha256": _sha256(public_bytes),
        "frozen_source_identity": source_identity,
        "repository_cache_path": str(repository_cache.resolve()),
        "workspace_root_path": str(workspace_root.resolve()),
        "model_calls": 0,
    }
    PrepareMetadata.model_validate(metadata)
    _atomic_write(artifact_root / "public_cases.json", public_bytes)
    _write_json(artifact_root / "prepare_metadata.json", metadata)
    return metadata


def _load_prepared(artifact_root: Path) -> tuple[PublicRunDocument, PrepareMetadata, bytes]:
    public_path = artifact_root / "public_cases.json"
    metadata_path = artifact_root / "prepare_metadata.json"
    try:
        public_raw = public_path.read_bytes()
        public = PublicRunDocument.model_validate_json(public_raw)
        metadata = PrepareMetadata.model_validate_json(metadata_path.read_bytes())
    except (OSError, ValueError) as error:
        raise SealedHarnessError(
            "prepared public inference gate is unavailable or invalid"
        ) from error
    if _sha256(public_raw) != metadata.public_document_sha256:
        raise SealedHarnessError("prepared public inference gate hash mismatch")
    if public.pinned_model_name != MODEL_NAME:
        raise SealedHarnessError("prepared model pin differs from current PatchProof model")
    return public, metadata, public_raw


def _run_case_error(case: PublicCase, error: Exception) -> dict[str, Any]:
    return {
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


def run(
    *,
    project_root: Path = PROJECT_ROOT,
    artifact_root: Path = ARTIFACT_ROOT,
    repository_cache: Path = REPOSITORY_CACHE,
    workspace_root: Path = WORKSPACE_ROOT,
    inter_case_delay_seconds: float = INTER_CASE_DELAY_SECONDS,
) -> dict[str, Any]:
    """Run the sealed public cases exactly once through the existing product path."""
    live_root = artifact_root / "live"
    journal_path = live_root / "journal.jsonl"
    raw_path = live_root / "raw_results.json"
    metadata_path = live_root / "run_metadata.json"
    if journal_path.exists() or raw_path.exists():
        raise SealedHarnessError("sealed RUN has already started; retry is permanently refused")
    public, prepared, _ = _load_prepared(artifact_root)
    source_identity = verify_frozen_source(project_root)
    if source_identity["working_src_sha256"] != prepared.frozen_source_identity.get(
        "working_src_sha256"
    ):
        raise SealedHarnessError("production source identity changed after PREPARE")
    if str(repository_cache.resolve()) != prepared.repository_cache_path:
        raise SealedHarnessError("RUN repository cache differs from PREPARE")
    if str(workspace_root.resolve()) != prepared.workspace_root_path:
        raise SealedHarnessError("RUN workspace root differs from PREPARE")
    provider = GeminiProviderConfig.from_environment()
    if provider.provider_surface is not GeminiProviderSurface.VERTEX_AI:
        raise SealedHarnessError("sealed RUN requires PATCHPROOF_GEMINI_PROVIDER=VERTEX_AI")
    configured_model = os.environ.get("PATCHPROOF_GEMINI_MODEL", "").strip()
    if configured_model and configured_model != MODEL_NAME:
        raise SealedHarnessError(
            f"configured model {configured_model!r} differs from pinned model {MODEL_NAME!r}"
        )
    preflight_vertex_authentication(provider)
    repositories = HardModeRepositoryCache(repository_cache, project_root)

    # Importing the adapter is not a model invocation; do it before consuming the
    # exactly-once journal so a broken local installation fails during preflight.
    from patchproof.adk_claim_investigator import AdkGeminiClaimInvestigator

    started_at = _utc_now()
    run_started_clock = time.perf_counter()
    run_started = {
        "event": "RUN_STARTED",
        "at": started_at,
        "benchmark_id": BENCHMARK_ID,
        "public_document_sha256": prepared.public_document_sha256,
        "model_name": MODEL_NAME,
        "provider_surface": str(provider.provider_surface),
        "case_ids": [case.case_id for case in public.cases],
        "inter_case_delay_seconds": inter_case_delay_seconds,
        "repository_cache_path": prepared.repository_cache_path,
        "workspace_root_path": prepared.workspace_root_path,
    }
    try:
        _append_journal(journal_path, run_started, exclusive=True)
    except FileExistsError as error:
        raise SealedHarnessError("sealed RUN has already started") from error
    raw: dict[str, Any] = {
        "schema_version": 1,
        "benchmark_id": BENCHMARK_ID,
        "public_document_sha256": prepared.public_document_sha256,
        "model_name": MODEL_NAME,
        "provider_surface": str(provider.provider_surface),
        "repository_cache_path": prepared.repository_cache_path,
        "workspace_root_path": prepared.workspace_root_path,
        "started_at": started_at,
        "completed_at": None,
        "run_completed": False,
        "cases": [],
    }
    _write_json(raw_path, raw)
    _write_json(
        metadata_path,
        {
            **run_started,
            "event": "RUN_METADATA",
            "provider_project": provider.project,
            "provider_location": provider.location,
            "frozen_source_identity": source_identity,
        },
    )
    for index, case in enumerate(public.cases):
        if index:
            previous = public.cases[index - 1]
            _append_journal(
                journal_path,
                {
                    "event": "INTER_CASE_PACING_STARTED",
                    "at": _utc_now(),
                    "completed_case_id": previous.case_id,
                    "next_case_id": case.case_id,
                    "declared_delay_seconds": inter_case_delay_seconds,
                },
            )
            time.sleep(inter_case_delay_seconds)
            _append_journal(
                journal_path,
                {
                    "event": "INTER_CASE_PACING_COMPLETED",
                    "at": _utc_now(),
                    "completed_case_id": previous.case_id,
                    "next_case_id": case.case_id,
                    "declared_delay_seconds": inter_case_delay_seconds,
                },
            )
        _append_journal(
            journal_path, {"event": "CASE_STARTED", "at": _utc_now(), "case_id": case.case_id}
        )
        case_started_clock = time.perf_counter()
        try:
            repository = repositories.prepare(case)  # type: ignore[arg-type]
            changed_tests = repositories.changed_python_test_paths(case, repository)  # type: ignore[arg-type]
            _verify_changed_test_exclusions(case, changed_tests)
            context = DeterministicContextRetriever(
                source_repository=repository,
                excluded_paths=frozenset(case.excluded_paths),
            ).retrieve(base_sha=case.base_sha, head_sha=case.head_sha)
            context_sha = _sha256(context.model_dump_json().encode("utf-8"))
            if context_sha != case.context_sha256:
                raise SealedHarnessError(
                    f"deterministic context differs from PREPARE for {case.case_id}"
                )
            investigator = GitClaimInvestigatorFactory(
                model=AdkGeminiClaimInvestigator(
                    model_name=MODEL_NAME,
                    provider_config=provider,
                ),
                source_repository=repository,
                excluded_paths=frozenset(case.excluded_paths),
                priority_paths=_priority_paths(case),
            )
            result = asyncio.run(
                hard_mode._run_live_case(
                    case=case,  # type: ignore[arg-type]
                    repository=repository,
                    workspace_root=workspace_root,
                    model_name=MODEL_NAME,
                    provider_config=provider,
                    expected_context_sha256=case.context_sha256,
                    claim_investigator=investigator,
                )
            )
        except Exception as error:  # Preserve the sealed run and continue all declared cases.
            result = _run_case_error(case, error)
        result["sealed_harness_wall_duration_seconds"] = time.perf_counter() - case_started_clock
        raw["cases"].append(result)
        _write_json(raw_path, raw)
        _append_journal(
            journal_path,
            {"event": "CASE_COMPLETED", "at": _utc_now(), "result": result},
        )
    raw["completed_at"] = _utc_now()
    raw["total_wall_duration_seconds"] = time.perf_counter() - run_started_clock
    raw["run_completed"] = True
    _write_json(raw_path, raw)
    _append_journal(
        journal_path,
        {"event": "RUN_COMPLETED", "at": raw["completed_at"], "case_count": len(raw["cases"])},
    )
    return raw


def _invalid_classification(terminal: str) -> str | None:
    if terminal == "ENVIRONMENT_NOT_READY":
        return "INVALID_ENVIRONMENT"
    if terminal in {"CLAIM_INVOCATION_ERROR", "MODEL_INVOCATION_ERROR"}:
        return "INVALID_PROVIDER_MODEL"
    if terminal == "CLAIM_INVALID_OUTPUT":
        return "INVALID_CLAIM_OUTPUT"
    if terminal == "HARNESS_OR_IMPLEMENTATION_ERROR" or terminal.endswith("ERROR"):
        return "INVALID_HARNESS"
    return None


def classify_result(case: Any, result: dict[str, Any]) -> str:
    """Narrow external correction for the sealed scorer's production-enum mismatch."""
    terminal = str(result.get("terminal_status") or "")
    invalid = _invalid_classification(terminal)
    if invalid is not None:
        return invalid
    supported = result.get("final_outcome") == ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO.value
    if case.label == "NEGATIVE_CONTROL":
        return "INCORRECT_SUPPORT" if supported else "EXPECTED_ABSTENTION"
    return "SUPPORTED_POSITIVE" if supported else "MISSED_POSITIVE"


def _usage_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    claim = result.get("claim_result")
    if isinstance(claim, dict) and isinstance(claim.get("usage"), dict):
        records.append({"task": "CLAIM", "usage": claim["usage"]})
    for attempt in result.get("candidate_attempts") or []:
        if isinstance(attempt, dict) and isinstance(attempt.get("usage"), dict):
            records.append(
                {
                    "task": "CANDIDATE",
                    "sequence": attempt.get("sequence"),
                    "usage": attempt["usage"],
                }
            )
    assessment = result.get("semantic_assessment")
    if isinstance(assessment, dict) and isinstance(assessment.get("usage"), dict):
        records.append({"task": "ASSESSMENT", "usage": assessment["usage"]})
    error = result.get("error")
    if isinstance(error, dict) and isinstance(error.get("usage"), dict):
        records.append({"task": "FAILED_INVOCATION", "usage": error["usage"]})
    return records


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _score_summary(scored_cases: list[dict[str, Any]], raw: dict[str, Any]) -> dict[str, Any]:
    all_positives = [case for case in scored_cases if case["hidden_label"] == "POSITIVE"]
    all_controls = [case for case in scored_cases if case["hidden_label"] == "NEGATIVE_CONTROL"]
    positives = [
        case for case in all_positives if not case["classification"].startswith("INVALID_")
    ]
    controls = [case for case in all_controls if not case["classification"].startswith("INVALID_")]
    results = [case["result"] for case in scored_cases]
    attempts = [len(result.get("candidate_attempts") or []) for result in results]
    return {
        "positive_side": {
            "positives_total": len(all_positives),
            "evaluable_positives": len(positives),
            "supported_positives": sum(
                case["classification"] == "SUPPORTED_POSITIVE" for case in positives
            ),
            "missed_positives": sum(
                case["classification"] == "MISSED_POSITIVE" for case in positives
            ),
            "positive_support_rate": _ratio(
                sum(case["classification"] == "SUPPORTED_POSITIVE" for case in positives),
                len(positives),
            ),
        },
        "negative_control_side": {
            "controls_total": len(all_controls),
            "evaluable_controls": len(controls),
            "expected_abstentions": sum(
                case["classification"] == "EXPECTED_ABSTENTION" for case in controls
            ),
            "incorrect_supports": sum(
                case["classification"] == "INCORRECT_SUPPORT" for case in controls
            ),
            "negative_control_abstention_rate": _ratio(
                sum(case["classification"] == "EXPECTED_ABSTENTION" for case in controls),
                len(controls),
            ),
        },
        "reliability": {
            "environment_failures": sum(
                case["classification"] == "INVALID_ENVIRONMENT" for case in scored_cases
            ),
            "provider_model_invocation_failures": sum(
                case["classification"] == "INVALID_PROVIDER_MODEL" for case in scored_cases
            ),
            "harness_implementation_failures": sum(
                case["classification"] == "INVALID_HARNESS" for case in scored_cases
            ),
            "claim_invalid_outputs": sum(
                case["classification"] == "INVALID_CLAIM_OUTPUT" for case in scored_cases
            ),
        },
        "generation_behavior": {
            "claim_selected_count": sum(
                bool(((result.get("claim_result") or {}).get("selection") or {}).get("claim"))
                for result in results
            ),
            "claim_abstained_count": sum(
                str(result.get("terminal_status") or "").startswith("CLAIM_")
                and result.get("final_outcome") == ClaimOutcome.INSUFFICIENT_EVIDENCE.value
                for result in results
            ),
            "candidate_attempts_total": sum(attempts),
            "cases_requiring_repair": sum(count > 1 for count in attempts),
            "max_candidate_attempts": max(attempts, default=0),
            "mechanically_discriminating_cases": sum(
                result.get("final_mechanical") == MechanicalEvidenceStatus.DISCRIMINATING.value
                for result in results
            ),
            "semantic_related_supports": sum(
                result.get("final_outcome") == ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO.value
                and (
                    ((result.get("semantic_assessment") or {}).get("decision") or {}).get(
                        "assertion_relation"
                    )
                    == "RELATED"
                )
                for result in results
            ),
            "potential_regressions": sum(
                result.get("final_outcome") == ClaimOutcome.POTENTIAL_REGRESSION.value
                for result in results
            ),
        },
        "runtime_accounting": {
            "per_case_wall_time_seconds": {
                case["case_id"]: case["result"].get(
                    "sealed_harness_wall_duration_seconds",
                    case["result"].get("wall_duration_seconds"),
                )
                for case in scored_cases
            },
            "total_wall_time_seconds": raw.get("total_wall_duration_seconds"),
            "model_usage_by_case": {
                case["case_id"]: _usage_records(case["result"]) for case in scored_cases
            },
        },
    }


def _markdown_summary(summary: dict[str, Any]) -> str:
    positive = summary["positive_side"]
    negative = summary["negative_control_side"]
    reliability = summary["reliability"]
    generation = summary["generation_behavior"]
    positive_ratio = f"{positive['supported_positives']}/{positive['positives_total']}"
    negative_ratio = f"{negative['expected_abstentions']}/{negative['controls_total']}"
    return "\n".join(
        (
            "# PatchProof fresh evaluation v1",
            "",
            f"- Supported positives: {positive_ratio}",
            f"- Expected negative abstentions: {negative_ratio}",
            f"- Incorrect supports: {negative['incorrect_supports']}",
            f"- Environment failures: {reliability['environment_failures']}",
            f"- Provider/model failures: {reliability['provider_model_invocation_failures']}",
            f"- Harness failures: {reliability['harness_implementation_failures']}",
            f"- Claim invalid outputs: {reliability['claim_invalid_outputs']}",
            f"- Candidate attempts: {generation['candidate_attempts_total']}",
            f"- Cases requiring repair: {generation['cases_requiring_repair']}",
            f"- Potential regressions: {generation['potential_regressions']}",
            "",
        )
    )


def score(
    *,
    artifact_root: Path = ARTIFACT_ROOT,
    manifest_path: Path = MANIFEST_PATH,
) -> dict[str, Any]:
    """Reveal labels and score a complete raw run without invoking a model."""
    raw_path = artifact_root / "live" / "raw_results.json"
    scored_path = artifact_root / "live" / "scored_results.json"
    summary_path = artifact_root / "live" / "summary.md"
    if scored_path.exists() or summary_path.exists():
        raise SealedHarnessError("sealed results have already been scored")
    public, prepared, _ = _load_prepared(artifact_root)
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SealedHarnessError("raw sealed results are unavailable or invalid") from error
    if not raw.get("run_completed") or len(raw.get("cases") or []) != len(public.cases):
        raise SealedHarnessError("RUN did not complete every declared case; refusing to score")
    if raw.get("public_document_sha256") != prepared.public_document_sha256:
        raise SealedHarnessError("raw results do not belong to the prepared public document")
    manifest_raw = manifest_path.read_bytes()
    if _sha256(manifest_raw) != prepared.sealed_manifest_sha256:
        raise SealedHarnessError("sealed manifest differs from PREPARE")
    evaluation = _sealed_evaluation()
    manifest = evaluation.load_manifest(manifest_path, verify_files=False)
    hidden = {case.case_id: case for case in manifest.cases}
    raw_by_id = {result.get("case_id"): result for result in raw["cases"]}
    if set(raw_by_id) != {case.case_id for case in public.cases} or set(raw_by_id) != set(hidden):
        raise SealedHarnessError("raw result case identities differ from the sealed declaration")
    scored_cases = [
        {
            "case_id": public_case.case_id,
            "hidden_label": hidden[public_case.case_id].label,
            "classification": classify_result(
                hidden[public_case.case_id], raw_by_id[public_case.case_id]
            ),
            "result": raw_by_id[public_case.case_id],
        }
        for public_case in public.cases
    ]
    summary = _score_summary(scored_cases, raw)
    scored = {
        "schema_version": 1,
        "benchmark_id": BENCHMARK_ID,
        "scored_at": _utc_now(),
        "public_document_sha256": prepared.public_document_sha256,
        "sealed_manifest_sha256": prepared.sealed_manifest_sha256,
        "scoring_note": (
            "Support requires exact final_outcome equality with "
            "CLAIM_SUPPORTED_FOR_SCENARIO; this external correction avoids the sealed "
            "score_result suffix mismatch without changing sealed bytes."
        ),
        "cases": scored_cases,
        "summary": summary,
        "model_calls_during_scoring": 0,
    }
    _write_json(scored_path, scored)
    _atomic_write(summary_path, _markdown_summary(summary).encode("utf-8"))
    return scored


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "run", "score"))
    phase = parser.parse_args(arguments).phase
    if phase == "prepare":
        result = prepare()
    elif phase == "run":
        result = run()
    else:
        result = score()
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
