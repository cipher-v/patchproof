from pathlib import Path

import pytest

from patchproof.readiness_preflight import CaseReadiness, load_preflight_cases, render_table


def test_holdout_preflight_loads_all_ten_cases_without_reading_oracles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "benchmarks/holdout/manifest.json"
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def guarded_read_bytes(path: Path) -> bytes:
        if "oracles" in path.parts:
            raise AssertionError("readiness preflight must not read oracle bytes")
        return original_read_bytes(path)

    def guarded_read_text(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if "oracles" in path.parts:
            raise AssertionError("readiness preflight must not read oracle text")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    cases = load_preflight_cases(manifest)

    assert len(cases) == 10
    assert len({case.repository for case in cases}) == 10


def test_readiness_table_reports_every_failure_without_hiding_partial_success() -> None:
    ready = CaseReadiness(
        case_id="ready-case",
        repository="owner/ready",
        install_strategy="UV_PIP_EDITABLE",
        plans_equivalent=True,
        environment_status="READY",
        investigation_status="READY",
        coverage_complete=False,
        observables_truncated=True,
        shared_observables=3,
        new_head_symbols=1,
        removed_base_symbols=0,
        duration_seconds=1.25,
        errors=(),
    )
    failed = CaseReadiness(
        case_id="failed-case",
        repository="owner/failed",
        install_strategy="NOT_RESOLVED",
        plans_equivalent=False,
        environment_status="ERROR",
        investigation_status="READY",
        coverage_complete=True,
        observables_truncated=False,
        shared_observables=1,
        new_head_symbols=0,
        removed_base_symbols=0,
        duration_seconds=2.0,
        errors=("ENVIRONMENT: deterministic failure",),
    )

    report = render_table((ready, failed))

    assert "ready-case" in report
    assert "failed-case" in report
    assert "ENVIRONMENT: deterministic failure" in report
    assert "ready-case | UV_PIP_EDITABLE | equivalent" in report
    assert "failed-case | NOT_RESOLVED | different" in report
    assert "READY: 1/2" in report
