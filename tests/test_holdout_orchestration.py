"""Deterministic safety tests for the sealed historical holdout adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from patchproof.hard_mode import HardModeCase, HardModeCaseKind, HardModeManifest

_PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

import benchmarks.holdout.orchestration as orchestration  # noqa: E402

_HOLDOUT_ROOT = _PROJECT_ROOT / "benchmarks" / "holdout"
_MANIFEST_PATH = _HOLDOUT_ROOT / "manifest.json"


def _copy_holdout(target: Path) -> Path:
    copied = target / "holdout"
    shutil.copytree(_HOLDOUT_ROOT, copied)
    return copied / "manifest.json"


def _mutate_manifest(path: Path, mutation) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutation(document)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _temporary_git_repository(root: Path, relative_path: str) -> tuple[Path, str, Path]:
    repository = root / "repository"
    tracked = repository / Path(*relative_path.split("/"))
    tracked.parent.mkdir(parents=True)
    tracked.write_text("sealed\n", encoding="utf-8")
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "PatchProof Test")
    _git(repository, "config", "user.email", "test@patchproof.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "baseline")
    return repository, _git(repository, "rev-parse", "HEAD"), tracked


def test_exactly_ten_historical_cases_are_accepted_and_mapped_to_frozen_types() -> None:
    manifest, digest = orchestration.load_holdout_manifest(_MANIFEST_PATH)
    frozen = orchestration.to_hard_mode_manifest(manifest)

    assert len(manifest.cases) == 10
    assert len({case.repository for case in manifest.cases}) == 10
    assert all(case.kind is HardModeCaseKind.HISTORICAL_PR for case in manifest.cases)
    assert all(isinstance(case, HardModeCase) for case in frozen.cases)
    assert isinstance(frozen, HardModeManifest)
    assert frozen.cases == manifest.cases
    assert len(digest) == 64


def test_synthetic_case_in_holdout_is_rejected(writable_test_directory: Path) -> None:
    path = _copy_holdout(writable_test_directory)
    _mutate_manifest(path, lambda document: document["cases"][0].update(kind="LOCAL_SYNTHETIC"))

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


@pytest.mark.parametrize("case_total", [9, 11])
def test_any_case_count_other_than_ten_is_rejected(
    writable_test_directory: Path,
    case_total: int,
) -> None:
    path = _copy_holdout(writable_test_directory)

    def resize(document: dict) -> None:
        if case_total == 9:
            document["cases"] = document["cases"][:9]
        else:
            document["cases"].append({**document["cases"][-1], "case_id": "extra-case"})

    _mutate_manifest(path, resize)

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


def test_duplicate_repository_is_rejected(writable_test_directory: Path) -> None:
    path = _copy_holdout(writable_test_directory)

    def duplicate(document: dict) -> None:
        first, second = document["cases"][:2]
        second["repository"] = first["repository"]
        second["source_url"] = first["source_url"]
        second["pull_request_url"] = (
            f"https://github.com/{first['repository']}/pull/{second['pull_request_number']}"
        )

    _mutate_manifest(path, duplicate)

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


def test_wrong_frozen_implementation_sha_is_rejected(writable_test_directory: Path) -> None:
    path = _copy_holdout(writable_test_directory)
    _mutate_manifest(
        path,
        lambda document: document.update(frozen_patchproof_implementation_sha="0" * 40),
    )

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_name", "gemini-9.9-flash"),
        ("provider_surface", "GEMINI_DEVELOPER_API"),
    ],
)
def test_wrong_model_or_provider_is_rejected(
    writable_test_directory: Path,
    field: str,
    value: str,
) -> None:
    path = _copy_holdout(writable_test_directory)
    _mutate_manifest(path, lambda document: document["protocol"].update({field: value}))

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


def test_wrong_provider_location_is_rejected(writable_test_directory: Path) -> None:
    path = _copy_holdout(writable_test_directory)
    _mutate_manifest(path, lambda document: document.update(future_provider_location="us-east1"))

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maximum_possible_logical_model_calls", 39),
        ("maximum_possible_provider_calls", 79),
    ],
)
def test_wrong_call_budget_is_rejected(
    writable_test_directory: Path,
    field: str,
    value: int,
) -> None:
    path = _copy_holdout(writable_test_directory)
    _mutate_manifest(path, lambda document: document["protocol"].update({field: value}))

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


def test_malformed_base_or_head_sha_is_rejected(writable_test_directory: Path) -> None:
    path = _copy_holdout(writable_test_directory)
    _mutate_manifest(path, lambda document: document["cases"][0].update(base_sha="abc"))

    with pytest.raises(orchestration.HoldoutConfigurationError):
        orchestration.load_holdout_manifest(path)


def test_modified_oracle_hash_is_rejected(writable_test_directory: Path) -> None:
    path = _copy_holdout(writable_test_directory)
    oracle = path.parent / "oracles" / "jsonschema_1208_enum_equality.py"
    oracle.write_text(oracle.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")

    with pytest.raises(orchestration.HoldoutConfigurationError, match="oracle hash mismatch"):
        orchestration.load_holdout_manifest(path)


def test_modified_sealed_manifest_is_detected(writable_test_directory: Path) -> None:
    repository, baseline, manifest = _temporary_git_repository(
        writable_test_directory,
        "benchmarks/holdout/manifest.json",
    )
    manifest.write_text("modified\n", encoding="utf-8")

    with pytest.raises(orchestration.HoldoutConfigurationError, match="sealed holdout"):
        orchestration.verify_sealed_holdout_integrity(repository, baseline)


def test_modified_frozen_production_source_is_detected(writable_test_directory: Path) -> None:
    repository, baseline, source = _temporary_git_repository(
        writable_test_directory,
        "src/patchproof/example.py",
    )
    source.write_text("modified\n", encoding="utf-8")

    with pytest.raises(orchestration.HoldoutConfigurationError, match="src/patchproof"):
        orchestration.verify_frozen_source_integrity(repository, baseline)


def test_default_execution_performs_preflight_without_entering_live_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = orchestration.HoldoutPreflight(
        dataset_id=orchestration.DATASET_ID,
        manifest_sha256="a" * 64,
        case_count=10,
        repository_count=10,
        model_name=orchestration.MODEL_NAME,
        provider_surface="VERTEX_AI",
        provider_location="global",
        maximum_logical_calls=40,
        maximum_provider_calls=80,
        execution_contract_version=1,
        output_root=str(orchestration.DEFAULT_OUTPUT_ROOT),
        frozen_source_unchanged=True,
        sealed_artifacts_unchanged=True,
    )
    manifest, _ = orchestration.load_holdout_manifest(_MANIFEST_PATH)
    frozen = orchestration.to_hard_mode_manifest(manifest)
    monkeypatch.setattr(
        orchestration,
        "preflight_holdout",
        lambda **_kwargs: (report, manifest, frozen),
    )

    def forbidden(**_kwargs):
        raise AssertionError("live/provider path was reached without --live")

    monkeypatch.setattr(orchestration, "_run_live_authorized", forbidden)
    monkeypatch.setattr(orchestration.hard_mode, "run_live", forbidden)

    assert orchestration.execute_holdout() == report
    assert report.model_or_provider_calls == 0


def test_cli_live_execution_requires_explicit_live_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[bool] = []
    report = orchestration.HoldoutPreflight(
        dataset_id=orchestration.DATASET_ID,
        manifest_sha256="a" * 64,
        case_count=10,
        repository_count=10,
        model_name=orchestration.MODEL_NAME,
        provider_surface="VERTEX_AI",
        provider_location="global",
        maximum_logical_calls=40,
        maximum_provider_calls=80,
        execution_contract_version=1,
        output_root=str(orchestration.DEFAULT_OUTPUT_ROOT),
        frozen_source_unchanged=True,
        sealed_artifacts_unchanged=True,
    )

    def fake_execute(**kwargs):
        observed.append(kwargs["live"])
        return replace(report, model_or_provider_calls=0)

    monkeypatch.setattr(orchestration, "execute_holdout", fake_execute)

    assert orchestration.main([]) == 0
    assert observed == [False]
    observed.clear()
    assert orchestration.main(["--live"]) == 0
    assert observed == [True]


def test_canonical_preflight_is_read_only_and_resolves_the_frozen_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration, "verify_frozen_source_integrity", lambda _root: None)
    before = {
        path.relative_to(orchestration.DEFAULT_OUTPUT_ROOT).as_posix(): path.read_bytes()
        for path in orchestration.DEFAULT_OUTPUT_ROOT.rglob("*")
        if path.is_file()
    }
    assert before

    report, _, frozen = orchestration.preflight_holdout()

    assert report.case_count == 10
    assert report.repository_count == 10
    assert report.maximum_logical_calls == 40
    assert report.maximum_provider_calls == 80
    assert report.execution_contract_version == 1
    assert report.model_or_provider_calls == 0
    assert all(isinstance(case, HardModeCase) for case in frozen.cases)
    after = {
        path.relative_to(orchestration.DEFAULT_OUTPUT_ROOT).as_posix(): path.read_bytes()
        for path in orchestration.DEFAULT_OUTPUT_ROOT.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_output_destination_outside_sealed_results_path_is_rejected(
    writable_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration, "verify_frozen_source_integrity", lambda _root: None)
    with pytest.raises(orchestration.HoldoutConfigurationError, match="output root"):
        orchestration.preflight_holdout(output_root=writable_test_directory / "results")
