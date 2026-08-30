from pathlib import Path

import pytest

import patchproof.cli as cli_module
from patchproof.pr_analyze import (
    PrAnalyzeError,
    _runtime_paths,
    find_known_pr,
    parse_pr_url,
    persist_infrastructure_failure,
    persist_result,
    print_result,
)


def _metadata() -> dict:
    return {
        "run_id": "run-123",
        "pr_url": "https://github.com/owner/repo/pull/123",
        "repository": "owner/repo",
        "pr_number": 123,
        "case_id": "demo-case",
        "title": "Fix observable behavior",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "source_commit": "c" * 40,
    }


def _execution(status: str, sha: str) -> dict:
    return {
        "status": status,
        "revision_sha": sha,
        "exit_code": 1 if status == "ASSERTION_FAILED" else 0,
        "duration_seconds": 0.25,
        "stdout": "bounded output",
        "stderr": "",
    }


def _selected_result(*, attempts: int = 1) -> dict:
    candidate_attempts = []
    evaluations = []
    for sequence in range(1, attempts + 1):
        source = f"def test_generated_{sequence}():\n    assert {sequence} == {sequence}\n"
        candidate_attempts.append(
            {
                "sequence": sequence,
                "origin": "INITIAL" if sequence == 1 else "REPAIR",
                "status": "VALIDATED",
                "source": source,
                "issues": [],
            }
        )
        evaluations.append(
            {
                "attempt_sequence": sequence,
                "mechanical_status": (
                    "DISCRIMINATING" if sequence == attempts else "NON_DISCRIMINATING"
                ),
                "base_execution": _execution("ASSERTION_FAILED", "a" * 40),
                "head_execution": _execution(
                    "PASSED" if sequence == attempts else "ASSERTION_FAILED", "b" * 40
                ),
            }
        )
    return {
        "environment_readiness": {"status": "READY", "reason": "setup passed"},
        "claim_result": {
            "selection": {
                "disposition": "SELECTED",
                "claim": {
                    "summary": "The public operation returns the corrected value.",
                    "observable_operation": "public_operation",
                    "trigger_condition": "the edge case is supplied",
                },
            }
        },
        "candidate_attempts": candidate_attempts,
        "candidate_evaluations": evaluations,
        "semantic_assessment": {
            "decision": {
                "assertion_relation": "RELATED",
                "outcome": "CLAIM_SUPPORTED_FOR_SCENARIO",
                "explanation": "The assertion tests the selected claim.",
            }
        },
        "terminal_status": "CLAIM_SUPPORTED_FOR_SCENARIO",
        "final_mechanical": "DISCRIMINATING",
        "final_outcome": "CLAIM_SUPPORTED_FOR_SCENARIO",
        "completed_at": "2026-08-30T00:00:00+00:00",
    }


def _environment_failure_result() -> dict:
    return {
        "environment_readiness": {
            "status": "BASE_SETUP_FAILED",
            "reason": "BASE repository environment setup failed: dependency install failed",
        },
        "claim_result": None,
        "candidate_attempts": [],
        "candidate_evaluations": [],
        "semantic_assessment": None,
        "terminal_status": "ENVIRONMENT_NOT_READY",
        "final_mechanical": "ENVIRONMENTAL",
        "final_outcome": "INSUFFICIENT_EVIDENCE",
        "completed_at": "2026-08-30T00:00:00+00:00",
    }


def _abstention_result() -> dict:
    return {
        "environment_readiness": {"status": "READY", "reason": "setup passed"},
        "claim_result": {
            "selection": {
                "disposition": "COUNTERFACTUAL_NOT_APPLICABLE",
                "claim": None,
                "explanation": "The diff does not establish a testable changed behavior.",
            }
        },
        "candidate_attempts": [],
        "candidate_evaluations": [],
        "semantic_assessment": None,
        "terminal_status": "CLAIM_COUNTERFACTUAL_NOT_APPLICABLE",
        "final_mechanical": None,
        "final_outcome": "INSUFFICIENT_EVIDENCE",
        "completed_at": "2026-08-30T00:00:00+00:00",
    }


def test_parse_pr_url_normalizes_repository() -> None:
    parsed = parse_pr_url("https://github.com/PyPA/packaging/pull/1345")
    assert parsed.repository == "pypa/packaging"
    assert parsed.number == 1345
    assert parsed.url == "https://github.com/pypa/packaging/pull/1345"


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/pypa/packaging/issues/1345",
        "https://example.com/pypa/packaging/pull/1345",
        "github.com/pypa/packaging/pull/1345",
        "https://github.com/pypa/packaging/pull/0",
    ],
)
def test_parse_pr_url_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(PrAnalyzeError):
        parse_pr_url(value)


def test_known_holdout_pr_resolves_from_committed_manifest() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parsed = parse_pr_url("https://github.com/pypa/packaging/pull/1345")
    known = find_known_pr(parsed, project_root=project_root)
    assert known.case.repository == "pypa/packaging"
    assert known.case.pull_request_number == 1345
    assert known.case.base_sha != known.case.head_sha


def test_unknown_pr_fails_before_model_work() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parsed = parse_pr_url("https://github.com/python/cpython/pull/1")
    with pytest.raises(PrAnalyzeError, match="not in the committed historical case set"):
        find_known_pr(parsed, project_root=project_root)


def test_known_case_lookup_never_reads_oracle_source(monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    observed: list[Path] = []
    original = Path.read_text

    def tracked(path: Path, *args, **kwargs):
        observed.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    known = find_known_pr(
        parse_pr_url("https://github.com/python-jsonschema/jsonschema/pull/1208"),
        project_root=project_root,
    )

    assert known.case.pull_request_number == 1208
    assert observed
    assert all("oracles" not in path.parts for path in observed)


def test_successful_result_renders_source_and_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _selected_result()

    print_result(metadata=_metadata(), result=result, run_dir=tmp_path)

    output = capsys.readouterr().out
    assert "PatchProof" in output
    assert "Gemini claim selection: SELECTED" in output
    assert "def test_generated_1" in output
    assert "BASE:       ASSERTION_FAILED" in output
    assert "HEAD:       PASSED" in output
    assert "Semantic: RELATED" in output
    assert "CLAIM_SUPPORTED_FOR_SCENARIO" in output


def test_environment_failure_rendering_does_not_claim_gemini_ran(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    print_result(
        metadata=_metadata(),
        result=_environment_failure_result(),
        run_dir=tmp_path,
    )

    output = capsys.readouterr().out
    assert "Environment preparation: FAILED" in output
    assert "BASE repository environment setup failed" in output
    assert "Gemini claim selection: NOT RUN" in output
    assert "Candidate generation: NOT RUN" in output
    assert "Mechanical: ENVIRONMENTAL" in output
    assert "No claim selected" not in output


def test_genuine_claim_abstention_is_distinct_from_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    print_result(metadata=_metadata(), result=_abstention_result(), run_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Environment preparation: READY" in output
    assert "Gemini claim selection: ABSTAINED" in output
    assert "does not establish a testable changed behavior" in output
    assert "Candidate generation: NOT RUN" in output
    assert "INSUFFICIENT_EVIDENCE" in output


def test_multiple_candidates_render_repairs_and_each_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    print_result(metadata=_metadata(), result=_selected_result(attempts=3), run_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Gemini candidate #1 (initial)" in output
    assert "Gemini candidate #2 (repair)" in output
    assert "Gemini candidate #3 (repair)" in output
    assert "def test_generated_1" in output
    assert "def test_generated_2" in output
    assert "def test_generated_3" in output
    assert output.count("Mechanical: NON_DISCRIMINATING") == 2
    assert "Mechanical: DISCRIMINATING" in output


def test_artifact_persistence_tracks_only_completed_stages(tmp_path: Path) -> None:
    run_dir = tmp_path / "selected"
    run_dir.mkdir()
    persist_result(run_dir=run_dir, metadata=_metadata(), result=_selected_result(attempts=2))

    assert (run_dir / "raw.json").is_file()
    assert (run_dir / "result.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "claim.json").is_file()
    assert (run_dir / "semantic_assessment.json").is_file()
    assert (run_dir / "candidate_1.py").is_file()
    assert (run_dir / "candidate_1_base.txt").is_file()
    assert (run_dir / "candidate_2.py").is_file()
    assert not (run_dir / "candidate_3.py").exists()
    assert not (run_dir / "failure.json").exists()


def test_environment_failure_persists_no_fake_claim_or_candidate(tmp_path: Path) -> None:
    run_dir = tmp_path / "environment"
    run_dir.mkdir()
    persist_result(
        run_dir=run_dir,
        metadata=_metadata(),
        result=_environment_failure_result(),
    )

    assert (run_dir / "raw.json").is_file()
    assert (run_dir / "result.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert not (run_dir / "claim.json").exists()
    assert not (run_dir / "candidate_1.py").exists()
    assert not (run_dir / "semantic_assessment.json").exists()
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Status: `NOT RUN`" in report


def test_abstention_persists_real_claim_result_but_no_candidate(tmp_path: Path) -> None:
    run_dir = tmp_path / "abstention"
    run_dir.mkdir()
    persist_result(run_dir=run_dir, metadata=_metadata(), result=_abstention_result())

    assert (run_dir / "claim.json").is_file()
    assert not (run_dir / "candidate_1.json").exists()
    assert not (run_dir / "semantic_assessment.json").exists()
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Status: `ABSTAINED`" in report


def test_infrastructure_failure_persists_diagnostic_not_evidence(tmp_path: Path) -> None:
    run_dir = tmp_path / "failure"
    run_dir.mkdir()
    error = RuntimeError("repository fetch failed")

    persist_infrastructure_failure(
        run_dir=run_dir,
        metadata=_metadata(),
        stage="repository preparation",
        error=error,
    )

    assert (run_dir / "failure.json").is_file()
    assert (run_dir / "journal.jsonl").is_file()
    assert "No PatchProof evidence outcome was produced" in (run_dir / "report.md").read_text(
        encoding="utf-8"
    )
    assert not (run_dir / "raw.json").exists()
    assert not (run_dir / "result.json").exists()
    assert not (run_dir / "claim.json").exists()


def test_cli_handles_unexpected_infrastructure_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_module,
        "analyze_known_pr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )

    exit_code = cli_module.main(["analyze", "https://github.com/owner/repo/pull/1"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "unexpected product failure: RuntimeError: network unavailable" in captured.err
    assert "Traceback" not in captured.err


def test_cli_handles_expected_product_error_concisely(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_module,
        "analyze_known_pr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PrAnalyzeError("unsupported PR")),
    )

    exit_code = cli_module.main(["analyze", "https://github.com/owner/repo/pull/1"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err.strip() == "PatchProof: unsupported PR"


def test_windows_safe_runtime_paths_are_short_and_drive_independent(tmp_path: Path) -> None:
    project_root = tmp_path / "nested" / "patchproof"

    repositories, workspaces = _runtime_paths(project_root)

    assert repositories == (project_root.parent / ".pp" / "repositories").resolve()
    assert workspaces == (project_root.parent / ".pp" / "w").resolve()
    assert repositories.parts[-2:] == (".pp", "repositories")
    assert workspaces.parts[-2:] == (".pp", "w")
