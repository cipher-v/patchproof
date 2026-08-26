"""Tests for hard-mode provenance, deterministic fixtures, and honest metrics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from patchproof.hard_mode import (
    HardModeCaseKind,
    HardModeConfigurationError,
    HardModePacingPolicy,
    HardModeProtocol,
    HardModeRepositoryCache,
    _pace_between_cases,
    load_hard_mode_manifest,
    run_live,
    summarize_live,
)

_PROJECT_ROOT = Path(__file__).parents[1]
_MANIFEST_PATH = _PROJECT_ROOT / "benchmarks" / "hard_mode" / "manifest.json"
_RESULTS_ROOT = _MANIFEST_PATH.parent / "results"


def test_hard_mode_manifest_freezes_four_historical_repositories_and_one_synthetic() -> None:
    manifest, digest = load_hard_mode_manifest(_MANIFEST_PATH)

    historical = [case for case in manifest.cases if case.kind is HardModeCaseKind.HISTORICAL_PR]
    synthetic = [case for case in manifest.cases if case.kind is HardModeCaseKind.LOCAL_SYNTHETIC]
    assert len(historical) == 4
    assert len({case.repository for case in historical}) == 4
    assert len(synthetic) == 1
    assert len(digest) == 64
    assert all(case.interface_exists_on_both_revisions for case in manifest.cases)
    assert all(case.expected_base_result == "ASSERTION_FAILED" for case in manifest.cases)
    assert all(case.expected_head_result == "PASSED" for case in manifest.cases)


def test_synthetic_fixture_bootstraps_to_the_frozen_commits(
    writable_test_directory: Path,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    case = next(item for item in manifest.cases if item.kind is HardModeCaseKind.LOCAL_SYNTHETIC)
    repository = HardModeRepositoryCache(
        writable_test_directory / "cache",
        _MANIFEST_PATH.parent,
    ).prepare(case)

    assert repository.is_dir()
    assert (repository / ".git").is_dir()


def test_summary_uses_raw_denominators_and_handles_failed_model_calls() -> None:
    raw = {
        "declared_run_id": "hard-test",
        "model_name": "gemini-3.6-flash",
        "cases": [
            {
                "kind": "HISTORICAL_PR",
                "claim_result": {
                    "selection": {"claim": {"claim_id": "claim-one"}},
                    "usage": {
                        "total_tokens": 100,
                        "duration_seconds": 1.0,
                        "provider_attempts": 1,
                    },
                },
                "candidate_attempts": [
                    {
                        "sequence": 1,
                        "origin": "INITIAL",
                        "status": "VALIDATED",
                        "usage": {
                            "total_tokens": 200,
                            "duration_seconds": 2.0,
                            "provider_attempts": 1,
                        },
                    }
                ],
                "candidate_evaluations": [
                    {
                        "attempt_sequence": 1,
                        "mechanical_status": "DISCRIMINATING",
                        "matches_hidden_oracle_direction": True,
                    }
                ],
                "semantic_assessment": {
                    "usage": {
                        "total_tokens": 50,
                        "duration_seconds": 0.5,
                        "provider_attempts": 1,
                    }
                },
                "terminal_status": "CLAIM_SUPPORTED_FOR_SCENARIO",
                "final_outcome": "CLAIM_SUPPORTED_FOR_SCENARIO",
            },
            {
                "kind": "LOCAL_SYNTHETIC",
                "claim_result": None,
                "candidate_attempts": [],
                "candidate_evaluations": [],
                "semantic_assessment": None,
                "terminal_status": "CLAIM_INVOCATION_ERROR",
                "final_outcome": None,
            },
        ],
    }

    summary = summarize_live(raw)

    assert summary["case_count"] == 2
    assert summary["claim_selected_count"] == 1
    assert summary["discriminating_initial_candidate_count"] == 1
    assert summary["incorrect_support_count"] == 0
    assert summary["total_tokens"] == 350
    assert summary["logical_model_result_count"] == 3


def test_predeclared_inter_case_pacing_is_uniform_and_journaled(
    writable_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    protocol = HardModeProtocol.model_validate(
        {
            **manifest.protocol.model_dump(mode="json"),
            "pacing_policy": HardModePacingPolicy.BETWEEN_CASES,
            "inter_case_delay_seconds": 60.0,
        }
    )
    sleep_calls: list[float] = []
    clock = iter((10.0, 70.25))
    monkeypatch.setattr("patchproof.hard_mode.time.sleep", sleep_calls.append)
    monkeypatch.setattr("patchproof.hard_mode.time.perf_counter", lambda: next(clock))
    journal = writable_test_directory / "pacing.jsonl"

    _pace_between_cases(
        protocol=protocol,
        journal_path=journal,
        completed_case_id="case-one",
        next_case_id="case-two",
    )

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert sleep_calls == [60.0]
    assert [event["event"] for event in events] == [
        "INTER_CASE_PACING_STARTED",
        "INTER_CASE_PACING_COMPLETED",
    ]
    assert events[1]["declared_delay_seconds"] == 60.0
    assert events[1]["actual_delay_seconds"] == 60.25


def test_existing_journal_blocks_any_live_rerun(writable_test_directory: Path) -> None:
    journal = writable_test_directory / "live_journal.jsonl"
    journal.write_text('{"event":"RUN_STARTED"}\n', encoding="utf-8")

    with pytest.raises(HardModeConfigurationError, match="already started"):
        run_live(
            manifest_path=_MANIFEST_PATH,
            cache_root=writable_test_directory / "cache",
            workspace_root=writable_test_directory / "workspaces",
            gate_path=writable_test_directory / "gate.json",
            journal_path=journal,
            raw_path=writable_test_directory / "raw.json",
        )


def test_checked_in_results_match_the_frozen_manifest_gate_and_summary() -> None:
    manifest_bytes = _MANIFEST_PATH.read_bytes()
    gate_bytes = (_RESULTS_ROOT / "oracle_gate.json").read_bytes()
    raw = json.loads((_RESULTS_ROOT / "raw.json").read_text(encoding="utf-8"))
    summary = json.loads((_RESULTS_ROOT / "summary.json").read_text(encoding="utf-8"))
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)

    assert raw["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert raw["oracle_gate_sha256"] == hashlib.sha256(gate_bytes).hexdigest()
    assert [case["case_id"] for case in raw["cases"]] == [case.case_id for case in manifest.cases]
    assert summary == summarize_live(raw)
