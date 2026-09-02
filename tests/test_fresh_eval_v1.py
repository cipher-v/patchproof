from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "fresh_eval_v1" / "manifest.json"
SPEC = importlib.util.spec_from_file_location(
    "fresh_eval_v1_evaluation", MANIFEST.with_name("evaluation.py")
)
assert SPEC is not None and SPEC.loader is not None
EVALUATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EVALUATION
SPEC.loader.exec_module(EVALUATION)

_FROZEN_IMPLEMENTATION = EVALUATION._FROZEN_IMPLEMENTATION
_ModelMustNotRun = EVALUATION._ModelMustNotRun
inference_case = EVALUATION.inference_case
load_manifest = EVALUATION.load_manifest
public_case_document = EVALUATION.public_case_document
score_result = EVALUATION.score_result


def test_fresh_manifest_is_balanced_diverse_and_integrity_checked() -> None:
    manifest = load_manifest(MANIFEST)

    assert manifest.frozen_implementation_sha == _FROZEN_IMPLEMENTATION
    assert manifest.model_calls_used_during_construction == 0
    assert manifest.case_counts.positive == 8
    assert manifest.case_counts.negative_control == 8
    assert manifest.case_counts.total == 16
    assert manifest.case_counts.repositories == 12
    assert (
        max(
            sum(case.repository == repository for case in manifest.cases)
            for repository in {case.repository for case in manifest.cases}
        )
        <= 2
    )
    assert (
        sum(
            case.label == "NEGATIVE_CONTROL" and bool(case.production_files_changed)
            for case in manifest.cases
        )
        >= 4
    )


def test_integrity_file_seals_manifest_protocol_ledger_and_oracles() -> None:
    root = MANIFEST.parent
    integrity = json.loads((root / "integrity.json").read_text(encoding="utf-8"))

    assert integrity["algorithm"] == "sha256"
    assert integrity["frozen_implementation_sha"] == _FROZEN_IMPLEMENTATION
    for relative_path, expected in integrity["files"].items():
        content = (root / relative_path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected


def test_all_changed_upstream_python_tests_are_excluded_exactly() -> None:
    manifest = load_manifest(MANIFEST)

    for case in manifest.cases:
        assert case.excluded_paths == case.changed_upstream_python_tests
        assert set(case.excluded_paths) <= set(case.changed_files)


def test_positive_oracles_and_negative_controls_are_sealed_separately() -> None:
    manifest = load_manifest(MANIFEST)

    for case in manifest.cases:
        if case.label == "POSITIVE":
            assert case.oracle is not None
            assert case.oracle.expected_base_result == "ASSERTION_FAILED"
            assert case.oracle.expected_head_result == "PASSED"
            assert case.interface_exists_on_both_revisions
        else:
            assert case.oracle is None


def test_inference_boundary_withholds_labels_expectations_and_oracles() -> None:
    manifest = load_manifest(MANIFEST)

    for sealed in manifest.cases:
        execution = inference_case(sealed)
        document = public_case_document(sealed)
        serialized = json.dumps(document, default=str)

        assert execution.base_sha == sealed.base_sha
        assert execution.head_sha == sealed.head_sha
        assert "label" not in document
        assert "oracle" not in document
        assert "expected_interpretation" not in document
        assert "construction_readiness" not in document
        assert sealed.label not in serialized


@pytest.mark.parametrize(
    ("label", "outcome", "expected"),
    [
        ("NEGATIVE_CONTROL", "SUPPORTED", "INCORRECT_SUPPORT"),
        ("NEGATIVE_CONTROL", "INSUFFICIENT_EVIDENCE", "EXPECTED_ABSTENTION"),
        ("POSITIVE", "SUPPORTED", "SUPPORTED_POSITIVE"),
        ("POSITIVE", "INSUFFICIENT_EVIDENCE", "MISSED_POSITIVE"),
    ],
)
def test_scoring_uses_hidden_label_only_after_inference(
    label: str, outcome: str, expected: str
) -> None:
    manifest = load_manifest(MANIFEST)
    case = next(case for case in manifest.cases if case.label == label)

    assert score_result(case, {"terminal_status": outcome, "final_outcome": outcome}) == expected


def test_environment_failure_is_not_scored_as_a_negative_abstention() -> None:
    manifest = load_manifest(MANIFEST)
    case = next(case for case in manifest.cases if case.label == "NEGATIVE_CONTROL")

    assert (
        score_result(
            case,
            {
                "terminal_status": "ENVIRONMENT_NOT_READY",
                "final_outcome": "INSUFFICIENT_EVIDENCE",
            },
        )
        == "INVALID_INFRASTRUCTURE"
    )


def test_construction_model_tripwire_fails_closed() -> None:
    with pytest.raises(AssertionError, match="must never invoke a model"):
        asyncio.run(_ModelMustNotRun().invoke(object()))
