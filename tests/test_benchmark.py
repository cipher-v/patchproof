"""Tests for benchmark provenance validation, transparent scoring, and reporting."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from patchproof.benchmark import (
    ArtifactKind,
    BenchmarkConfigurationError,
    BenchmarkSummary,
    BenchmarkTruth,
    ControlledSuiteObservation,
    EvaluationDecision,
    EvaluationStrategy,
    RawBenchmarkReport,
    RevisionObservation,
    ScenarioObservation,
    StrategyObservation,
    load_manifest,
    render_summary_markdown,
    summarize,
    write_reports,
)
from patchproof.models import DifferentialPattern, MechanicalEvidenceStatus, TestExecutionStatus

_PROJECT_ROOT = Path(__file__).parents[1]
_MANIFEST = _PROJECT_ROOT / "benchmarks" / "manifest.json"


def test_checked_in_manifest_has_cross_repository_provenance_and_valid_hashes() -> None:
    manifest, digest = load_manifest(_MANIFEST)

    assert len(manifest.cases) == 4
    assert {case.repository for case in manifest.cases} == {
        "more-itertools/more-itertools",
        "python-humanize/humanize",
    }
    assert {case.pull_request_number for case in manifest.cases} == {320, 329, 1211, 1223}
    assert len(digest) == 64
    assert all(case.base_sha != case.head_sha for case in manifest.cases)


def test_manifest_verification_detects_oracle_tampering(writable_test_directory: Path) -> None:
    benchmark_copy = writable_test_directory / "benchmarks"
    shutil.copytree(_PROJECT_ROOT / "benchmarks", benchmark_copy)
    oracle = benchmark_copy / "oracles" / "humanize_320.py"
    oracle.write_text(oracle.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(BenchmarkConfigurationError, match="hash mismatch"):
        load_manifest(benchmark_copy / "manifest.json")


def _revision(sha: str, status: TestExecutionStatus) -> RevisionObservation:
    artifact_hash = "f" * 64
    return RevisionObservation(
        sha=sha,
        status=status,
        collected_count=1,
        exit_code=0 if status is TestExecutionStatus.PASSED else 1,
        duration_seconds=0.25,
        artifact_sha256_before=artifact_hash,
        artifact_sha256_after=artifact_hash,
        stdout="bounded stdout",
        stderr="",
        detail=None,
    )


def _strategies(*, head_support: bool, patchproof_support: bool, negative: bool):
    return (
        StrategyObservation(
            strategy=EvaluationStrategy.HEAD_ONLY,
            decision=(
                EvaluationDecision.SUPPORT if head_support else EvaluationDecision.NO_SUPPORT
            ),
            is_false_support=head_support and negative,
        ),
        StrategyObservation(
            strategy=EvaluationStrategy.PATCHPROOF_BASE_HEAD,
            decision=(
                EvaluationDecision.SUPPORT if patchproof_support else EvaluationDecision.NO_SUPPORT
            ),
            is_false_support=patchproof_support and negative,
        ),
    )


def _report() -> RawBenchmarkReport:
    return RawBenchmarkReport(
        manifest_sha256="a" * 64,
        started_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        completed_at=datetime(2026, 8, 24, 10, 0, 1, tzinfo=UTC),
        python_version="3.12.0",
        platform="test-platform",
        agent_candidate_generation="NOT_MEASURED_NO_CREDENTIALS",
        controlled_suite=ControlledSuiteObservation(
            node_ids=("tests/test_control.py::test_failure_path",),
            passed=1,
            failed=0,
            errors=0,
            skipped=0,
            duration_seconds=0.1,
            exit_code=0,
            timed_out=False,
            stdout="1 passed",
            stderr="",
        ),
        scenarios=(
            ScenarioObservation(
                scenario_id="historical-oracle",
                case_id="historical-case",
                repository="owner/one",
                pull_request_number=1,
                truth=BenchmarkTruth.FIX_PRESENT,
                artifact_kind=ArtifactKind.DEVELOPER_ORACLE,
                artifact_sha256="f" * 64,
                base=_revision("a" * 40, TestExecutionStatus.ASSERTION_FAILED),
                head=_revision("b" * 40, TestExecutionStatus.PASSED),
                mechanical_status=MechanicalEvidenceStatus.DISCRIMINATING,
                differential_pattern=DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED,
                mechanical_reason="The oracle distinguishes the historical fix.",
                strategies=_strategies(head_support=True, patchproof_support=True, negative=False),
            ),
            ScenarioObservation(
                scenario_id="controlled-no-op",
                case_id="controlled-case",
                repository="owner/two",
                pull_request_number=2,
                truth=BenchmarkTruth.FIX_ABSENT,
                artifact_kind=ArtifactKind.CONTROLLED_WEAK_CANDIDATE,
                artifact_sha256="f" * 64,
                base=_revision("c" * 40, TestExecutionStatus.PASSED),
                head=_revision("c" * 40, TestExecutionStatus.PASSED),
                mechanical_status=MechanicalEvidenceStatus.NON_DISCRIMINATING,
                differential_pattern=DifferentialPattern.BOTH_PASSED,
                mechanical_reason="The weak candidate passes on the unchanged revision.",
                strategies=_strategies(head_support=True, patchproof_support=False, negative=True),
            ),
        ),
    )


def test_summary_exposes_false_support_denominators_and_unmeasured_work() -> None:
    summary = summarize(_report())
    by_strategy = {item.strategy: item for item in summary.strategy_metrics}

    head_only = by_strategy[EvaluationStrategy.HEAD_ONLY]
    assert head_only.strong_supports == 2
    assert head_only.false_supports == 1
    assert head_only.false_support_rate == 0.5
    assert head_only.negative_false_support_rate == 1.0

    patchproof = by_strategy[EvaluationStrategy.PATCHPROOF_BASE_HEAD]
    assert patchproof.strong_supports == 1
    assert patchproof.false_supports == 0
    assert patchproof.false_support_rate == 0.0
    assert summary.historical_oracles_reproduced == 1
    assert summary.weak_candidates_rejected == 1
    assert summary.controlled_selections == 1
    assert summary.controlled_checks == 1
    assert summary.controlled_checks_passed == 1
    assert summary.controlled_recovery_rate == 1.0
    assert "live Gemini candidate generation" in summary.unmeasured_comparisons


def test_reports_round_trip_from_raw_data_and_keep_the_scope_disclaimer(
    writable_test_directory: Path,
) -> None:
    raw_path = writable_test_directory / "raw.json"
    summary_path = writable_test_directory / "summary.json"
    markdown_path = writable_test_directory / "summary.md"

    written = write_reports(
        raw=_report(),
        raw_path=raw_path,
        summary_path=summary_path,
        markdown_path=markdown_path,
    )

    assert RawBenchmarkReport.model_validate_json(raw_path.read_bytes()) == _report()
    assert BenchmarkSummary.model_validate_json(summary_path.read_bytes()) == written
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "False/support" in markdown
    assert "not a live Gemini generation quality claim" in markdown
    assert json.loads(raw_path.read_text(encoding="utf-8"))["scenarios"]


def test_rendered_summary_never_calls_reference_policy_results_a_model_benchmark() -> None:
    rendered = render_summary_markdown(summarize(_report()))

    assert "REFERENCE_ORACLE_POLICY_REPLAY" in rendered
    assert "NOT MEASURED" not in rendered  # The readable list, not a fake numeric placeholder.
    assert "live Gemini candidate generation" in rendered
    assert "representative production false-support estimate" in rendered
