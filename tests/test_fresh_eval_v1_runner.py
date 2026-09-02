from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from patchproof.gemini_provider import GeminiProviderConfig, GeminiProviderSurface
from patchproof.models import ClaimOutcome

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tools" / "run_fresh_eval_v1.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("fresh_eval_v1_runner", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = RUNNER
RUNNER_SPEC.loader.exec_module(RUNNER)

from benchmarks.fresh_eval_v1 import evaluation as sealed_evaluation  # noqa: E402


def _public_document(context_json: str = '{"context":"stable"}') -> dict[str, Any]:
    manifest = sealed_evaluation.load_manifest(RUNNER.MANIFEST_PATH, verify_files=False)
    cases = []
    context_sha = hashlib.sha256(context_json.encode()).hexdigest()
    for case in manifest.cases:
        cases.append(
            {
                **sealed_evaluation.public_case_document(case),
                "kind": str(sealed_evaluation.inference_case(case).kind),
                "context_sha256": context_sha,
            }
        )
    return {
        "schema_version": 1,
        "benchmark_id": "fresh-eval-v1",
        "frozen_implementation_sha": RUNNER.FROZEN_IMPLEMENTATION,
        "pinned_model_name": RUNNER.MODEL_NAME,
        "cases": cases,
    }


def _prepare_artifacts(
    artifact_root: Path,
    *,
    public_document: dict[str, Any] | None = None,
    working_source_hash: str = "a" * 64,
    repository_cache: Path = RUNNER.REPOSITORY_CACHE,
    workspace_root: Path = RUNNER.WORKSPACE_ROOT,
) -> dict[str, Any]:
    document = public_document or _public_document()
    public_bytes = RUNNER._canonical_bytes(document)
    manifest_sha = hashlib.sha256(RUNNER.MANIFEST_PATH.read_bytes()).hexdigest()
    metadata = {
        "schema_version": 1,
        "benchmark_id": "fresh-eval-v1",
        "prepared_at": "2026-09-02T00:00:00+00:00",
        "sealed_manifest_sha256": manifest_sha,
        "sealed_integrity_sha256": "b" * 64,
        "public_document_sha256": hashlib.sha256(public_bytes).hexdigest(),
        "frozen_source_identity": {"working_src_sha256": working_source_hash},
        "repository_cache_path": str(repository_cache.resolve()),
        "workspace_root_path": str(workspace_root.resolve()),
        "model_calls": 0,
    }
    RUNNER._atomic_write(artifact_root / "public_cases.json", public_bytes)
    RUNNER._write_json(artifact_root / "prepare_metadata.json", metadata)
    return metadata


def test_public_document_contains_no_labels_expectations_or_oracle_identity() -> None:
    manifest = sealed_evaluation.load_manifest(RUNNER.MANIFEST_PATH)
    document = _public_document()
    serialized = json.dumps(document, sort_keys=True)

    validated = RUNNER.PublicRunDocument.model_validate(document)
    RUNNER._assert_public_document_blind(document, manifest.cases)
    assert validated.cases[0].kind is RUNNER.HardModeCaseKind.HISTORICAL_PR
    assert "POSITIVE" not in serialized
    assert "NEGATIVE_CONTROL" not in serialized
    assert "expected_interpretation" not in serialized
    assert "construction_readiness" not in serialized
    for case in manifest.cases:
        if case.oracle is not None:
            assert case.oracle.path not in serialized
            assert case.oracle.sha256 not in serialized
            assert case.oracle.test_function not in serialized


def test_prepare_model_tripwire_and_phase_boundary() -> None:
    with pytest.raises(AssertionError, match="PREPARE must never invoke a model"):
        asyncio.run(RUNNER._ModelMustNotRun().invoke(object()))

    prepare_source = RUNNER.prepare.__code__.co_names
    assert "AdkGeminiClaimInvestigator" not in prepare_source


def test_windows_runtime_defaults_are_short_deterministic_temp_paths() -> None:
    environment = {"TEMP": "C:/Temp"}

    first = RUNNER._default_runtime_paths(platform_name="nt", environment=environment)
    second = RUNNER._default_runtime_paths(platform_name="nt", environment=environment)

    assert first == second
    assert first[0].name == "patchproof-fresh-eval-repositories"
    assert first[1].name == "fe"
    assert first[0].parent == first[1].parent == Path("C:/Temp").resolve()
    assert not first[0].is_relative_to(RUNNER.ARTIFACT_ROOT)


def test_detected_tests_must_be_excluded_while_sealed_manual_exclusions_remain_valid() -> None:
    manual = SimpleNamespace(
        case_id="manual-typing-test",
        excluded_paths=("typing-examples/baseline.py",),
    )
    RUNNER._verify_changed_test_exclusions(manual, ())

    with pytest.raises(RUNNER.SealedHarnessError, match="not excluded"):
        RUNNER._verify_changed_test_exclusions(manual, ("tests/test_behavior.py",))


def test_repository_index_priorities_include_only_supported_python_source_paths() -> None:
    case = SimpleNamespace(
        production_files_changed=("src/package/runtime.py", "src/package/api.pyi")
    )

    assert RUNNER._priority_paths(case) == frozenset({"src/package/runtime.py"})


@pytest.mark.parametrize(
    ("label", "terminal", "outcome", "expected"),
    [
        (
            "POSITIVE",
            "CLAIM_SUPPORTED_FOR_SCENARIO",
            "CLAIM_SUPPORTED_FOR_SCENARIO",
            "SUPPORTED_POSITIVE",
        ),
        (
            "POSITIVE",
            "INSUFFICIENT_EVIDENCE",
            "INSUFFICIENT_EVIDENCE",
            "MISSED_POSITIVE",
        ),
        (
            "NEGATIVE_CONTROL",
            "INSUFFICIENT_EVIDENCE",
            "INSUFFICIENT_EVIDENCE",
            "EXPECTED_ABSTENTION",
        ),
        (
            "NEGATIVE_CONTROL",
            "CLAIM_SUPPORTED_FOR_SCENARIO",
            "CLAIM_SUPPORTED_FOR_SCENARIO",
            "INCORRECT_SUPPORT",
        ),
        (
            "POSITIVE",
            "ENVIRONMENT_NOT_READY",
            "INSUFFICIENT_EVIDENCE",
            "INVALID_ENVIRONMENT",
        ),
        ("POSITIVE", "MODEL_INVOCATION_ERROR", None, "INVALID_PROVIDER_MODEL"),
    ],
)
def test_scoring_uses_exact_production_outcome_and_separates_invalid_cases(
    label: str, terminal: str, outcome: str | None, expected: str
) -> None:
    case = SimpleNamespace(label=label)

    assert (
        RUNNER.classify_result(
            case,
            {"terminal_status": terminal, "final_outcome": outcome},
        )
        == expected
    )


def test_external_scorer_transparently_corrects_sealed_suffix_mismatch() -> None:
    manifest = sealed_evaluation.load_manifest(RUNNER.MANIFEST_PATH, verify_files=False)
    positive = next(case for case in manifest.cases if case.label == "POSITIVE")
    result = {
        "terminal_status": ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO.value,
        "final_outcome": ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO.value,
    }

    assert sealed_evaluation.score_result(positive, result) == "MISSED_POSITIVE"
    assert RUNNER.classify_result(positive, result) == "SUPPORTED_POSITIVE"


def test_second_run_is_refused_before_provider_or_model_access(
    writable_test_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = writable_test_directory / "artifacts"
    journal = artifact_root / "live" / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"event":"RUN_STARTED"}\n', encoding="utf-8")
    monkeypatch.setattr(
        RUNNER,
        "_load_prepared",
        lambda _root: pytest.fail("prepared data must not be opened on a second RUN"),
    )

    with pytest.raises(RUNNER.SealedHarnessError, match="already started"):
        RUNNER.run(artifact_root=artifact_root)


def test_run_is_label_blind_and_uses_current_product_wiring(
    writable_test_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = writable_test_directory / "artifacts"
    repository = writable_test_directory / "repository"
    repository.mkdir()
    context_json = '{"context":"stable"}'
    document = _public_document(context_json)
    document["cases"] = document["cases"][:2]
    repository_cache = writable_test_directory / "cache"
    workspace_root = writable_test_directory / "workspaces"
    _prepare_artifacts(
        artifact_root,
        public_document=document,
        repository_cache=repository_cache,
        workspace_root=workspace_root,
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        RUNNER,
        "verify_frozen_source",
        lambda _root: {"working_src_sha256": "a" * 64},
    )
    provider = GeminiProviderConfig(
        provider_surface=GeminiProviderSurface.VERTEX_AI,
        project="test-project",
        location="global",
    )
    monkeypatch.setattr(
        RUNNER.GeminiProviderConfig,
        "from_environment",
        classmethod(lambda _cls: provider),
    )
    monkeypatch.setattr(
        RUNNER,
        "preflight_vertex_authentication",
        lambda value: captured.setdefault("preflight", value),
    )

    class RepositoryCache:
        def __init__(self, root: Path, manifest_root: Path) -> None:
            captured["cache"] = (root, manifest_root)

        def prepare(self, case: Any) -> Path:
            captured["prepared_case"] = case
            return repository

        def changed_python_test_paths(self, case: Any, source: Path) -> tuple[str, ...]:
            assert source == repository
            return tuple(sorted(case.excluded_paths))

    class Context:
        def model_dump_json(self) -> str:
            return context_json

    class Retriever:
        def __init__(self, *, source_repository: Path, excluded_paths: frozenset[str]) -> None:
            captured["retriever"] = (source_repository, excluded_paths)

        def retrieve(self, *, base_sha: str, head_sha: str) -> Context:
            captured["revisions"] = (base_sha, head_sha)
            return Context()

    class Model:
        def __init__(self, *, model_name: str, provider_config: GeminiProviderConfig) -> None:
            captured["model"] = (model_name, provider_config)

    class Factory:
        def __init__(self, **arguments: Any) -> None:
            captured["factory"] = arguments

    async def live_case(**arguments: Any) -> dict[str, Any]:
        captured["live_case"] = arguments
        if arguments["case"].case_id == document["cases"][0]["case_id"]:
            raise RuntimeError("isolated first-case failure")
        return {
            "case_id": arguments["case"].case_id,
            "terminal_status": "INSUFFICIENT_EVIDENCE",
            "final_outcome": "INSUFFICIENT_EVIDENCE",
            "candidate_attempts": [],
            "wall_duration_seconds": 1.0,
        }

    monkeypatch.setattr(RUNNER, "HardModeRepositoryCache", RepositoryCache)
    monkeypatch.setattr(RUNNER, "DeterministicContextRetriever", Retriever)
    monkeypatch.setattr(RUNNER, "GitClaimInvestigatorFactory", Factory)
    monkeypatch.setattr("patchproof.adk_claim_investigator.AdkGeminiClaimInvestigator", Model)
    monkeypatch.setattr(RUNNER.hard_mode, "_run_live_case", live_case)

    restricted_reads: list[Path] = []
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def guarded_read_bytes(path: Path) -> bytes:
        resolved = path.resolve()
        if resolved.is_relative_to(RUNNER.SEALED_ROOT.resolve()):
            restricted_reads.append(resolved)
            raise AssertionError(f"RUN opened sealed evaluation data: {resolved}")
        return original_read_bytes(path)

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        resolved = path.resolve()
        if resolved.is_relative_to(RUNNER.SEALED_ROOT.resolve()):
            restricted_reads.append(resolved)
            raise AssertionError(f"RUN opened sealed evaluation data: {resolved}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    raw = RUNNER.run(
        project_root=ROOT,
        artifact_root=artifact_root,
        repository_cache=repository_cache,
        workspace_root=workspace_root,
        inter_case_delay_seconds=0,
    )

    assert restricted_reads == []
    assert raw["run_completed"] is True
    assert len(raw["cases"]) == 2
    assert raw["cases"][0]["terminal_status"] == "HARNESS_OR_IMPLEMENTATION_ERROR"
    assert raw["cases"][1]["terminal_status"] == "INSUFFICIENT_EVIDENCE"
    assert "POSITIVE" not in json.dumps(raw)
    assert "NEGATIVE_CONTROL" not in json.dumps(raw)
    assert captured["model"] == (RUNNER.MODEL_NAME, provider)
    assert captured["preflight"] == provider
    assert captured["factory"]["excluded_paths"] == frozenset(
        document["cases"][-1]["excluded_paths"]
    )
    assert captured["factory"]["priority_paths"] == frozenset(
        document["cases"][-1]["production_files_changed"]
    )
    assert captured["live_case"]["claim_investigator"] is not None
    assert captured["live_case"]["model_name"] == RUNNER.MODEL_NAME
    assert raw["repository_cache_path"] == str(repository_cache.resolve())
    assert raw["workspace_root_path"] == str(workspace_root.resolve())
    events = [
        json.loads(line)["event"]
        for line in (artifact_root / "live" / "journal.jsonl").read_text().splitlines()
    ]
    assert events == [
        "RUN_STARTED",
        "CASE_STARTED",
        "CASE_COMPLETED",
        "INTER_CASE_PACING_STARTED",
        "INTER_CASE_PACING_COMPLETED",
        "CASE_STARTED",
        "CASE_COMPLETED",
        "RUN_COMPLETED",
    ]


def test_score_requires_complete_run_and_persists_all_required_metrics(
    writable_test_directory: Path,
) -> None:
    artifact_root = writable_test_directory / "artifacts"
    document = _public_document()
    metadata = _prepare_artifacts(artifact_root, public_document=document)
    manifest = sealed_evaluation.load_manifest(RUNNER.MANIFEST_PATH, verify_files=False)
    labels = {case.case_id: case.label for case in manifest.cases}
    cases = []
    for public in document["cases"]:
        supported = labels[public["case_id"]] == "POSITIVE"
        outcome = (
            ClaimOutcome.CLAIM_SUPPORTED_FOR_SCENARIO.value
            if supported
            else ClaimOutcome.INSUFFICIENT_EVIDENCE.value
        )
        cases.append(
            {
                "case_id": public["case_id"],
                "terminal_status": outcome,
                "final_outcome": outcome,
                "final_mechanical": "DISCRIMINATING" if supported else "NON_DISCRIMINATING",
                "claim_result": {"selection": {"claim": {"claim_id": "selected"}}},
                "candidate_attempts": [{"sequence": 1}],
                "semantic_assessment": (
                    {"decision": {"assertion_relation": "RELATED"}} if supported else None
                ),
                "wall_duration_seconds": 1.0,
            }
        )
    RUNNER._write_json(
        artifact_root / "live" / "raw_results.json",
        {
            "schema_version": 1,
            "benchmark_id": "fresh-eval-v1",
            "public_document_sha256": metadata["public_document_sha256"],
            "run_completed": True,
            "total_wall_duration_seconds": 16.0,
            "cases": cases,
        },
    )

    scored = RUNNER.score(artifact_root=artifact_root)

    assert scored["summary"]["positive_side"] == {
        "positives_total": 8,
        "evaluable_positives": 8,
        "supported_positives": 8,
        "missed_positives": 0,
        "positive_support_rate": 1.0,
    }
    assert scored["summary"]["negative_control_side"] == {
        "controls_total": 8,
        "evaluable_controls": 8,
        "expected_abstentions": 8,
        "incorrect_supports": 0,
        "negative_control_abstention_rate": 1.0,
    }
    assert scored["summary"]["generation_behavior"]["candidate_attempts_total"] == 16
    assert scored["summary"]["generation_behavior"]["semantic_related_supports"] == 8
    assert scored["summary"]["runtime_accounting"]["total_wall_time_seconds"] == 16.0
    assert scored["model_calls_during_scoring"] == 0
    assert (artifact_root / "live" / "scored_results.json").is_file()
    assert (artifact_root / "live" / "summary.md").is_file()
