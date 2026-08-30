"""Human-facing PR analysis over PatchProof's frozen hardened evaluator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import patchproof.hard_mode as hard_mode
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.gemini_provider import (
    GeminiProviderConfig,
    GeminiProviderSurface,
    preflight_vertex_authentication,
)
from patchproof.hard_mode import HardModeCase, HardModeCaseKind, HardModeRepositoryCache

HARDENED_BEHAVIOR_BASELINE = "8ad46415e81192c3fb90768b6e6f85beb8d96a43"
MODEL_NAME = "gemini-3.6-flash"

_PR_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)/?$"
)

_PROTECTED_CORE_PATHS = (
    "src/patchproof/adk_claim_agent.py",
    "src/patchproof/adk_evidence_assessor.py",
    "src/patchproof/adk_test_agent.py",
    "src/patchproof/challenge.py",
    "src/patchproof/claim_agent.py",
    "src/patchproof/context_retrieval.py",
    "src/patchproof/evidence_workflow.py",
    "src/patchproof/hard_mode.py",
    "src/patchproof/models.py",
    "src/patchproof/pytest_runner.py",
    "src/patchproof/reasoning_budget.py",
    "src/patchproof/test_generation.py",
)

_KNOWN_MANIFESTS = (
    Path("benchmarks/holdout/manifest.json"),
    Path("benchmarks/hard_mode/manifest_v5.json"),
)


class PrAnalyzeError(RuntimeError):
    """Raised when a requested PR cannot be analyzed safely."""


@dataclass(frozen=True, slots=True)
class ParsedPullRequest:
    repository: str
    number: int
    url: str


@dataclass(frozen=True, slots=True)
class KnownPullRequest:
    case: HardModeCase
    manifest_path: Path


def parse_pr_url(value: str) -> ParsedPullRequest:
    """Parse one canonical public GitHub pull-request URL."""
    match = _PR_URL.fullmatch(value.strip())
    if match is None:
        raise PrAnalyzeError(
            "expected a GitHub PR URL like https://github.com/owner/repo/pull/123"
        )
    repository = f"{match.group('owner')}/{match.group('repo')}".lower()
    number = int(match.group("number"))
    return ParsedPullRequest(
        repository=repository,
        number=number,
        url=f"https://github.com/{repository}/pull/{number}",
    )


def find_project_root(start: Path | None = None) -> Path:
    """Find the checkout containing PatchProof's committed benchmark manifests."""
    candidates = [start.resolve()] if start is not None else []
    candidates.extend((Path.cwd().resolve(), *Path(__file__).resolve().parents))
    for candidate in dict.fromkeys(candidates):
        if (candidate / "benchmarks/holdout/manifest.json").is_file():
            return candidate
    raise PrAnalyzeError("run this command from the PatchProof repository checkout")


def _load_cases(path: Path) -> list[HardModeCase]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PrAnalyzeError(f"could not read committed case manifest: {path}") from error
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise PrAnalyzeError(f"committed case manifest has no cases: {path}")
    cases: list[HardModeCase] = []
    for raw_case in raw_cases:
        try:
            case = HardModeCase.model_validate(raw_case)
        except ValueError as error:
            raise PrAnalyzeError(f"invalid committed case in {path}") from error
        if case.kind is HardModeCaseKind.HISTORICAL_PR:
            cases.append(case)
    return cases


def find_known_pr(parsed: ParsedPullRequest, *, project_root: Path) -> KnownPullRequest:
    """Resolve a PR URL without reading any oracle source bytes."""
    matches: list[KnownPullRequest] = []
    for relative_path in _KNOWN_MANIFESTS:
        manifest_path = project_root / relative_path
        if not manifest_path.is_file():
            continue
        for case in _load_cases(manifest_path):
            if (
                case.repository.lower() == parsed.repository
                and case.pull_request_number == parsed.number
            ):
                matches.append(KnownPullRequest(case=case, manifest_path=manifest_path))
    if not matches:
        raise PrAnalyzeError(
            "this PR is not in the committed historical case set yet; "
            "generic onboarded-repository analysis is not wired into this CLI yet"
        )
    revisions = {(item.case.base_sha, item.case.head_sha) for item in matches}
    if len(revisions) != 1:
        raise PrAnalyzeError("the PR resolves to conflicting frozen BASE/HEAD revisions")
    return matches[0]


def verify_hardened_core(*, project_root: Path) -> None:
    """Fail closed if protected behavior differs from the hardened baseline."""
    try:
        completed = subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                HARDENED_BEHAVIOR_BASELINE,
                "--",
                *_PROTECTED_CORE_PATHS,
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PrAnalyzeError("could not verify the hardened PatchProof core") from error
    if completed.returncode == 1:
        raise PrAnalyzeError(
            "protected inference/execution files differ from hardened baseline "
            f"{HARDENED_BEHAVIOR_BASELINE}; refusing to run"
        )
    if completed.returncode != 0:
        raise PrAnalyzeError("Git failed while verifying the hardened PatchProof core")


def _write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _append_event(path: Path, event: str, **fields: Any) -> None:
    document = {"event": event, "at": datetime.now(UTC).isoformat(), **fields}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(document, sort_keys=True, ensure_ascii=False, default=str) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def _heartbeat(stop: threading.Event, case_id: str) -> None:
    started = time.monotonic()
    while not stop.wait(15):
        elapsed = int(time.monotonic() - started)
        print(f"[PatchProof] still running {case_id} ({elapsed}s elapsed)...", flush=True)


def _execution_text(execution: dict[str, Any]) -> str:
    return (
        f"status: {execution.get('status')}\n"
        f"revision: {execution.get('revision_sha')}\n"
        f"exit_code: {execution.get('exit_code')}\n"
        f"duration_seconds: {execution.get('duration_seconds')}\n\n"
        f"stdout:\n{execution.get('stdout') or ''}\n\n"
        f"stderr:\n{execution.get('stderr') or ''}\n"
    )


def _claim_text(result: dict[str, Any]) -> str:
    selection = (result.get("claim_result") or {}).get("selection") or {}
    claim = selection.get("claim") or {}
    if claim:
        return json.dumps(claim, indent=2, ensure_ascii=False)
    explanation = selection.get("explanation")
    return explanation if isinstance(explanation, str) else "No claim selected."


def persist_result(*, run_dir: Path, metadata: dict[str, Any], result: dict[str, Any]) -> None:
    """Save raw evidence plus human-readable candidate source and execution logs."""
    _write_json(run_dir / "raw.json", result)
    if result.get("claim_result") is not None:
        _write_json(run_dir / "claim.json", result["claim_result"])
    if result.get("semantic_assessment") is not None:
        _write_json(run_dir / "semantic_assessment.json", result["semantic_assessment"])

    evaluations = {
        item.get("attempt_sequence"): item
        for item in result.get("candidate_evaluations", [])
        if isinstance(item, dict)
    }
    report_lines = [
        "# PatchProof PR Analysis",
        "",
        f"- PR: {metadata['pr_url']}",
        f"- Case: `{metadata['case_id']}`",
        f"- BASE: `{metadata['base_sha']}`",
        f"- HEAD: `{metadata['head_sha']}`",
        "",
        "## Gemini claim",
        "",
        _claim_text(result),
        "",
    ]
    for attempt in result.get("candidate_attempts", []):
        sequence = attempt.get("sequence")
        if not isinstance(sequence, int):
            continue
        _write_json(run_dir / f"candidate_{sequence}.json", attempt)
        source = attempt.get("source")
        report_lines.extend([f"## Gemini candidate {sequence}", ""])
        if isinstance(source, str) and source:
            (run_dir / f"candidate_{sequence}.py").write_text(
                source.rstrip() + "\n", encoding="utf-8", newline="\n"
            )
            report_lines.extend(["```python", source.rstrip(), "```", ""])
        else:
            report_lines.extend([f"Validation status: `{attempt.get('status')}`", ""])

        evaluation = evaluations.get(sequence)
        if evaluation:
            base = evaluation.get("base_execution") or {}
            head = evaluation.get("head_execution") or {}
            if isinstance(base, dict):
                (run_dir / f"candidate_{sequence}_base.txt").write_text(
                    _execution_text(base), encoding="utf-8", newline="\n"
                )
            if isinstance(head, dict):
                (run_dir / f"candidate_{sequence}_head.txt").write_text(
                    _execution_text(head), encoding="utf-8", newline="\n"
                )
            report_lines.extend(
                [
                    f"- BASE: `{base.get('status')}`",
                    f"- HEAD: `{head.get('status')}`",
                    f"- Mechanical: `{evaluation.get('mechanical_status')}`",
                    "",
                ]
            )

    final = {
        "run_id": metadata["run_id"],
        "pr_url": metadata["pr_url"],
        "case_id": metadata["case_id"],
        "base_sha": metadata["base_sha"],
        "head_sha": metadata["head_sha"],
        "terminal_status": result.get("terminal_status"),
        "final_mechanical": result.get("final_mechanical"),
        "final_outcome": result.get("final_outcome"),
        "candidate_attempt_count": len(result.get("candidate_attempts", [])),
        "candidate_evaluation_count": len(result.get("candidate_evaluations", [])),
        "completed_at": result.get("completed_at"),
    }
    _write_json(run_dir / "result.json", final)
    report_lines.extend(
        [
            "## Final result",
            "",
            f"- Mechanical: `{result.get('final_mechanical')}`",
            f"- Outcome: `{result.get('final_outcome')}`",
            f"- Terminal: `{result.get('terminal_status')}`",
            "",
        ]
    )
    (run_dir / "report.md").write_text(
        "\n".join(report_lines), encoding="utf-8", newline="\n"
    )


def print_result(*, metadata: dict[str, Any], result: dict[str, Any], run_dir: Path) -> None:
    """Show the exact generated candidate source and BASE/HEAD facts."""
    print("\n" + "=" * 76)
    print("PatchProof")
    print("=" * 76)
    print(f"PR:    {metadata['pr_url']}")
    print(f"Title: {metadata['title']}")
    print(f"BASE:  {metadata['base_sha']}")
    print(f"HEAD:  {metadata['head_sha']}")
    print("\nGemini selected claim:")
    print(_claim_text(result))

    evaluations = {
        item.get("attempt_sequence"): item
        for item in result.get("candidate_evaluations", [])
        if isinstance(item, dict)
    }
    for attempt in result.get("candidate_attempts", []):
        sequence = attempt.get("sequence")
        print("\n" + "-" * 76)
        print(f"Gemini candidate #{sequence}")
        print("-" * 76)
        source = attempt.get("source")
        print(source.rstrip() if isinstance(source, str) and source else "<no valid source>")
        evaluation = evaluations.get(sequence)
        if evaluation:
            base = evaluation.get("base_execution") or {}
            head = evaluation.get("head_execution") or {}
            print(f"\nBASE:       {base.get('status')}")
            print(f"HEAD:       {head.get('status')}")
            print(f"Mechanical: {evaluation.get('mechanical_status')}")
        else:
            print(f"\nValidation: {attempt.get('status')}")

    print("\n" + "=" * 76)
    print(f"FINAL MECHANICAL: {result.get('final_mechanical')}")
    print(f"FINAL OUTCOME:    {result.get('final_outcome')}")
    print(f"TERMINAL STATUS:  {result.get('terminal_status')}")
    print(f"ARTIFACTS:        {run_dir}")
    print("=" * 76)


def analyze_known_pr(
    pr_url: str,
    *,
    output_root: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """Run one committed historical PR through the hardened Gemini evaluator."""
    root = project_root or find_project_root()
    parsed = parse_pr_url(pr_url)
    known = find_known_pr(parsed, project_root=root)
    verify_hardened_core(project_root=root)

    configured_model = os.environ.get("PATCHPROOF_GEMINI_MODEL", "").strip()
    if configured_model and configured_model != MODEL_NAME:
        raise PrAnalyzeError(
            f"PATCHPROOF_GEMINI_MODEL={configured_model!r}; this path is pinned to {MODEL_NAME!r}"
        )
    provider = GeminiProviderConfig.from_environment()
    if provider.provider_surface is not GeminiProviderSurface.VERTEX_AI:
        raise PrAnalyzeError(
            "set PATCHPROOF_GEMINI_PROVIDER=VERTEX_AI before running a known historical PR"
        )
    preflight_vertex_authentication(provider)

    run_id = str(uuid4())
    run_dir = (output_root or (root / ".patchproof/runs")).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    journal = run_dir / "journal.jsonl"
    case = known.case

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "pr_url": parsed.url,
        "repository": case.repository,
        "pr_number": case.pull_request_number,
        "case_id": case.case_id,
        "title": case.title,
        "body": case.body,
        "base_sha": case.base_sha,
        "head_sha": case.head_sha,
        "model_name": MODEL_NAME,
        "provider_surface": str(provider.provider_surface),
        "provider_project": provider.project,
        "provider_location": provider.location,
        "hardened_behavior_baseline": HARDENED_BEHAVIOR_BASELINE,
        "manifest_path": str(known.manifest_path.relative_to(root)),
        "oracle_bytes_loaded_by_product_path": False,
        "started_at": datetime.now(UTC).isoformat(),
    }
    _write_json(run_dir / "metadata.json", metadata)
    _append_event(journal, "RUN_DECLARED", **metadata)

    temp_root = (root.parent / ".pp").resolve()
    repositories = HardModeRepositoryCache(temp_root / "repositories", known.manifest_path.parent)
    workspace_root = temp_root / "w"

    print(f"[PatchProof] PR: {parsed.url}", flush=True)
    print(f"[PatchProof] case: {case.case_id}", flush=True)
    print("[PatchProof] preparing exact frozen BASE/HEAD...", flush=True)
    repository = repositories.prepare(case)

    changed_tests = repositories.changed_python_test_paths(case, repository)
    if changed_tests != tuple(sorted(case.excluded_paths)):
        raise PrAnalyzeError(
            f"changed-test exclusions differ from the committed case: {changed_tests!r}"
        )

    context = DeterministicContextRetriever(
        source_repository=repository,
        excluded_paths=frozenset(case.excluded_paths),
    ).retrieve(base_sha=case.base_sha, head_sha=case.head_sha)
    context_document = context.model_dump(mode="json")
    context_sha = hashlib.sha256(context.model_dump_json().encode("utf-8")).hexdigest()
    _write_json(run_dir / "pr_context.json", context_document)
    metadata["context_sha256"] = context_sha
    _write_json(run_dir / "metadata.json", metadata)
    _append_event(journal, "CONTEXT_FROZEN", context_sha256=context_sha)

    print("[PatchProof] starting Gemini generation and BASE/HEAD challenge...", flush=True)
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(stop, case.case_id), daemon=True)
    heartbeat.start()
    try:
        result = asyncio.run(
            hard_mode._run_live_case(
                case=case,
                repository=repository,
                workspace_root=workspace_root,
                model_name=MODEL_NAME,
                provider_config=provider,
                expected_context_sha256=context_sha,
            )
        )
    finally:
        stop.set()
        heartbeat.join(timeout=1)

    persist_result(run_dir=run_dir, metadata=metadata, result=result)
    _append_event(
        journal,
        "RUN_COMPLETED",
        terminal_status=result.get("terminal_status"),
        final_mechanical=result.get("final_mechanical"),
        final_outcome=result.get("final_outcome"),
    )
    print_result(metadata=metadata, result=result, run_dir=run_dir)
    return run_dir
