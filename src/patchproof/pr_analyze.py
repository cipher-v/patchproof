"""Human-facing PR analysis over PatchProof's frozen hardened evaluator."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import patchproof.hard_mode as hard_mode
from patchproof.adk_claim_investigator import AdkGeminiClaimInvestigator
from patchproof.claim_investigator import GitClaimInvestigatorFactory
from patchproof.context_retrieval import DeterministicContextRetriever
from patchproof.gemini_provider import (
    GeminiProviderConfig,
    GeminiProviderSurface,
    preflight_vertex_authentication,
)
from patchproof.hard_mode import HardModeCase, HardModeCaseKind, HardModeRepositoryCache
from patchproof.pr_resolution import (
    ParsedPullRequest,
    PullRequestUrlError,
    parse_github_pull_request_url,
)

HARDENED_BEHAVIOR_BASELINE = "8ad46415e81192c3fb90768b6e6f85beb8d96a43"
MODEL_NAME = "gemini-3.6-flash"

_PROTECTED_CORE_PATHS = (
    "src/patchproof/adk_claim_agent.py",
    "src/patchproof/adk_evidence_assessor.py",
    "src/patchproof/adk_test_agent.py",
    "src/patchproof/challenge.py",
    "src/patchproof/claim_agent.py",
    "src/patchproof/context_retrieval.py",
    "src/patchproof/models.py",
    "src/patchproof/reasoning_budget.py",
)

# Phase 2 intentionally changed claim-routing code in these modules. Keep the actual
# candidate loop and semantic admissibility gate frozen at the definition level.
_PROTECTED_CORE_DEFINITIONS = {
    "src/patchproof/evidence_workflow.py": (
        ("EvidenceReport", "validate_evidence_pair"),
        ("EvidenceWorkflow", "_validate_semantic_decision"),
        ("EvidenceWorkflow", "_execution_evidence"),
        ("EvidenceWorkflow", "_evaluation_evidence"),
    ),
    "src/patchproof/hard_mode.py": (
        (None, "_challenge"),
        (None, "_execution_document"),
        (None, "_bounded_candidate_challenges"),
    ),
    "src/patchproof/git_workspace.py": (
        ("GitWorkspaceManager", "resolve_revision"),
        ("GitWorkspaceManager", "_run_git"),
    ),
    "src/patchproof/pytest_runner.py": (
        ("PytestJUnitParser", "parse"),
        ("PytestJUnitParser", "_without_report"),
        ("PytestJUnitParser", "_pytest_expectation_failure"),
        ("PytestRunner", "_test_command"),
        ("PytestRunner", "_safe_artifact_path"),
        ("PytestRunner", "_hash_file"),
    ),
    "src/patchproof/test_generation.py": (
        ("CandidateTestValidator", "validate"),
        ("CandidateTestValidator", "_validate_imports"),
        ("CandidateTestValidator", "_validate_calls"),
        ("CandidateTestValidator", "_validate_shared_interface"),
        ("BoundedCandidateTestGenerator", "generate_initial"),
        ("BoundedCandidateTestGenerator", "repair"),
        ("BoundedCandidateTestGenerator", "_invoke"),
    ),
}

_KNOWN_MANIFESTS = (
    Path("benchmarks/holdout/manifest.json"),
    Path("benchmarks/hard_mode/manifest_v5.json"),
)


class PrAnalyzeError(RuntimeError):
    """Raised when a requested PR cannot be analyzed safely."""


class PrAnalyzeRunError(PrAnalyzeError):
    """Raised after a declared run fails outside the evidence outcome domain."""

    def __init__(self, message: str, *, run_dir: Path) -> None:
        super().__init__(message)
        self.run_dir = run_dir


@dataclass(frozen=True, slots=True)
class KnownPullRequest:
    case: HardModeCase
    manifest_path: Path


_INFRASTRUCTURE_TERMINALS = frozenset(
    {
        "ENVIRONMENT_NOT_READY",
        "CLAIM_INVOCATION_ERROR",
        "MODEL_INVOCATION_ERROR",
        "HARNESS_OR_IMPLEMENTATION_ERROR",
    }
)


def parse_pr_url(value: str) -> ParsedPullRequest:
    """Parse one canonical public GitHub pull-request URL."""
    try:
        return parse_github_pull_request_url(value)
    except PullRequestUrlError as error:
        raise PrAnalyzeError(str(error)) from error


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
    for relative_path, definitions in _PROTECTED_CORE_DEFINITIONS.items():
        try:
            baseline = subprocess.run(
                ["git", "show", f"{HARDENED_BEHAVIOR_BASELINE}:{relative_path}"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout
            current = (project_root / relative_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError, subprocess.SubprocessError) as error:
            raise PrAnalyzeError("could not verify protected evidence-core definitions") from error
        for owner, name in definitions:
            if _definition_fingerprint(current, owner=owner, name=name) != (
                _definition_fingerprint(baseline, owner=owner, name=name)
            ):
                raise PrAnalyzeError(
                    f"protected evidence-core definition changed: {relative_path}:{name}"
                )


def _definition_fingerprint(source: str, *, owner: str | None, name: str) -> str:
    """Hash one function AST so intentional surrounding orchestration may evolve."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise PrAnalyzeError("protected evidence-core source is not valid Python") from error
    body: list[ast.stmt] = tree.body
    if owner is not None:
        classes = [node for node in body if isinstance(node, ast.ClassDef) and node.name == owner]
        if len(classes) != 1:
            raise PrAnalyzeError(f"protected evidence-core class is missing: {owner}")
        body = classes[0].body
    functions = [
        node
        for node in body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    if len(functions) != 1:
        raise PrAnalyzeError(f"protected evidence-core definition is missing: {name}")
    normalized = ast.dump(functions[0], annotate_fields=True, include_attributes=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _source_commit(project_root: Path) -> str:
    """Return the exact PatchProof checkout commit recorded for a product run."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PrAnalyzeError("could not resolve the PatchProof source commit") from error
    commit = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PrAnalyzeError("Git returned an invalid PatchProof source commit")
    return commit


def _runtime_paths(project_root: Path) -> tuple[Path, Path]:
    """Use deliberately short Windows-safe repository and worktree roots."""
    temp_root = (project_root.parent / ".pp").resolve()
    return temp_root / "repositories", temp_root / "w"


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
        handle.write(json.dumps(document, sort_keys=True, ensure_ascii=False, default=str) + "\n")
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


def _claim_selection(result: dict[str, Any]) -> dict[str, Any] | None:
    claim_result = result.get("claim_result")
    if not isinstance(claim_result, dict):
        return None
    selection = claim_result.get("selection")
    return selection if isinstance(selection, dict) else None


def _claim_document(result: dict[str, Any]) -> dict[str, Any] | None:
    selection = _claim_selection(result)
    if selection is None:
        return None
    claim = selection.get("claim")
    return claim if isinstance(claim, dict) and claim else None


def _readiness(result: dict[str, Any]) -> dict[str, Any] | None:
    readiness = result.get("environment_readiness")
    return readiness if isinstance(readiness, dict) else None


def _claim_explanation(result: dict[str, Any]) -> str:
    selection = _claim_selection(result) or {}
    explanation = selection.get("explanation")
    if isinstance(explanation, str) and explanation.strip():
        return explanation.strip()
    return "Gemini did not provide a testable behavioral claim."


def _report_lines(*, metadata: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Render a truthful report without inferring stages that did not run."""
    lines = [
        "# PatchProof PR Analysis",
        "",
        f"- PR: {metadata['pr_url']}",
        f"- Title: {metadata['title']}",
        f"- Case: `{metadata['case_id']}`",
        f"- BASE: `{metadata['base_sha']}`",
        f"- HEAD: `{metadata['head_sha']}`",
        f"- PatchProof source: `{metadata['source_commit']}`",
        "",
        "## Environment preparation",
        "",
    ]
    readiness = _readiness(result)
    if readiness is None:
        lines.append("- Status: `NOT RECORDED`")
    else:
        lines.extend(
            [
                f"- Status: `{readiness.get('status')}`",
                f"- Reason: {readiness.get('reason')}",
            ]
        )

    lines.extend(["", "## Gemini claim selection", ""])
    claim = _claim_document(result)
    if claim is not None:
        lines.extend(
            [
                "- Status: `SELECTED`",
                "",
                "```json",
                json.dumps(claim, indent=2, ensure_ascii=False),
                "```",
            ]
        )
    elif _claim_selection(result) is not None:
        lines.extend(["- Status: `ABSTAINED`", f"- Reason: {_claim_explanation(result)}"])
    else:
        lines.append("- Status: `NOT RUN`")

    attempts = result.get("candidate_attempts", [])
    evaluations = {
        item.get("attempt_sequence"): item
        for item in result.get("candidate_evaluations", [])
        if isinstance(item, dict)
    }
    lines.extend(["", "## Candidate generation", ""])
    if not attempts:
        lines.append("- Status: `NOT RUN`")
    for attempt in attempts:
        sequence = attempt.get("sequence")
        origin = attempt.get("origin")
        lines.extend([f"### Candidate {sequence} ({origin})", ""])
        source = attempt.get("source")
        if isinstance(source, str) and source:
            lines.extend(["```python", source.rstrip(), "```", ""])
        else:
            lines.append(f"- Validation: `{attempt.get('status')}`")
            issues = attempt.get("issues") or []
            if issues:
                lines.append(f"- Issues: `{json.dumps(issues, ensure_ascii=False)}`")
        evaluation = evaluations.get(sequence)
        if evaluation:
            base = evaluation.get("base_execution") or {}
            head = evaluation.get("head_execution") or {}
            lines.extend(
                [
                    f"- BASE: `{base.get('status')}`",
                    f"- HEAD: `{head.get('status')}`",
                    f"- Mechanical: `{evaluation.get('mechanical_status')}`",
                ]
            )

    semantic = result.get("semantic_assessment")
    if isinstance(semantic, dict):
        decision = semantic.get("decision") or {}
        lines.extend(
            [
                "",
                "## Semantic assessment",
                "",
                f"- Relation: `{decision.get('assertion_relation')}`",
                f"- Outcome: `{decision.get('outcome')}`",
                f"- Explanation: {decision.get('explanation')}",
            ]
        )

    lines.extend(
        [
            "",
            "## Final result",
            "",
            f"- Mechanical: `{result.get('final_mechanical')}`",
            f"- Outcome: `{result.get('final_outcome')}`",
            f"- Terminal: `{result.get('terminal_status')}`",
            "",
        ]
    )
    return lines


def persist_result(*, run_dir: Path, metadata: dict[str, Any], result: dict[str, Any]) -> None:
    """Save raw evidence plus human-readable candidate source and execution logs."""
    _write_json(run_dir / "raw.json", result)
    if result.get("claim_result") is not None:
        _write_json(run_dir / "claim.json", result["claim_result"])
    if result.get("investigation_transcript") is not None:
        _write_json(
            run_dir / "investigation_transcript.json",
            result["investigation_transcript"],
        )
    if result.get("semantic_assessment") is not None:
        _write_json(run_dir / "semantic_assessment.json", result["semantic_assessment"])

    evaluations = {
        item.get("attempt_sequence"): item
        for item in result.get("candidate_evaluations", [])
        if isinstance(item, dict)
    }
    for attempt in result.get("candidate_attempts", []):
        sequence = attempt.get("sequence")
        if not isinstance(sequence, int):
            continue
        _write_json(run_dir / f"candidate_{sequence}.json", attempt)
        source = attempt.get("source")
        if isinstance(source, str) and source:
            (run_dir / f"candidate_{sequence}.py").write_text(
                source.rstrip() + "\n", encoding="utf-8", newline="\n"
            )

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
    (run_dir / "report.md").write_text(
        "\n".join(_report_lines(metadata=metadata, result=result)),
        encoding="utf-8",
        newline="\n",
    )


def persist_infrastructure_failure(
    *,
    run_dir: Path,
    metadata: dict[str, Any],
    stage: str,
    error: Exception,
) -> None:
    """Preserve a bounded local diagnostic without inventing an evidence result."""
    diagnostic = {
        "schema_version": 1,
        "run_id": metadata["run_id"],
        "stage": stage,
        "exception_type": type(error).__name__,
        "message": str(error)[:2_000],
        "traceback": "".join(traceback.format_exception(error))[-12_000:],
        "failed_at": datetime.now(UTC).isoformat(),
    }
    _write_json(run_dir / "failure.json", diagnostic)
    _append_event(
        run_dir / "journal.jsonl",
        "RUN_FAILED",
        stage=stage,
        exception_type=type(error).__name__,
        message=str(error)[:2_000],
    )
    (run_dir / "report.md").write_text(
        "\n".join(
            [
                "# PatchProof PR Analysis",
                "",
                f"- PR: {metadata['pr_url']}",
                f"- BASE: `{metadata['base_sha']}`",
                f"- HEAD: `{metadata['head_sha']}`",
                "",
                "## Infrastructure failure",
                "",
                f"- Stage: `{stage}`",
                f"- Error: `{type(error).__name__}`",
                "- Detailed local diagnostic: `failure.json`",
                "",
                "No PatchProof evidence outcome was produced.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )


def print_result(*, metadata: dict[str, Any], result: dict[str, Any], run_dir: Path) -> None:
    """Show the exact generated candidate source and BASE/HEAD facts."""
    print("\n" + "=" * 72)
    print("PatchProof")
    print("=" * 72)
    print(f"PR:    {metadata['repository']} #{metadata['pr_number']}")
    print(f"URL:   {metadata['pr_url']}")
    print(f"Title: {metadata['title']}")
    print(f"BASE:  {metadata['base_sha']}")
    print(f"HEAD:  {metadata['head_sha']}")

    readiness = _readiness(result)
    if readiness is not None:
        status = readiness.get("status")
        print(f"\nEnvironment preparation: {'READY' if status == 'READY' else 'FAILED'}")
        print("Reason:")
        print(readiness.get("reason"))

    claim = _claim_document(result)
    if claim is not None:
        print("\nGemini claim selection: SELECTED")
        print("Gemini selected claim:")
        print(claim.get("summary"))
        print(f"Observable operation: {claim.get('observable_operation')}")
        print(f"Trigger: {claim.get('trigger_condition')}")
    elif _claim_selection(result) is not None:
        print("\nGemini claim selection: ABSTAINED")
        print("Reason:")
        print(_claim_explanation(result))
    else:
        print("\nGemini claim selection: NOT RUN")

    evaluations = {
        item.get("attempt_sequence"): item
        for item in result.get("candidate_evaluations", [])
        if isinstance(item, dict)
    }
    for attempt in result.get("candidate_attempts", []):
        sequence = attempt.get("sequence")
        origin = attempt.get("origin")
        label = "initial" if origin == "INITIAL" else "repair"
        print("\n" + "-" * 72)
        print(f"Gemini candidate #{sequence} ({label})")
        print("-" * 72)
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
            for issue in attempt.get("issues") or []:
                print(f"Issue: {issue}")

    if not result.get("candidate_attempts"):
        print("\nCandidate generation: NOT RUN")

    semantic = result.get("semantic_assessment")
    if isinstance(semantic, dict):
        decision = semantic.get("decision") or {}
        print(f"\nSemantic: {decision.get('assertion_relation')}")

    print("\n" + "=" * 72)
    print("FINAL:")
    print(result.get("final_outcome") or result.get("terminal_status"))
    print(f"Mechanical: {result.get('final_mechanical') or 'NOT AVAILABLE'}")
    print(f"Terminal:   {result.get('terminal_status')}")
    print("\nArtifacts:")
    print(run_dir)
    print("=" * 72)


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
    try:
        provider = GeminiProviderConfig.from_environment()
    except Exception as error:
        raise PrAnalyzeError(f"invalid Gemini provider configuration: {error}") from error
    if provider.provider_surface is not GeminiProviderSurface.VERTEX_AI:
        raise PrAnalyzeError(
            "set PATCHPROOF_GEMINI_PROVIDER=VERTEX_AI before running a known historical PR"
        )
    try:
        preflight_vertex_authentication(provider)
    except Exception as error:
        raise PrAnalyzeError(
            "Vertex AI authentication is unavailable; run "
            "'gcloud auth application-default login' and verify the configured project"
        ) from error

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
        "source_commit": _source_commit(root),
        "hardened_behavior_baseline": HARDENED_BEHAVIOR_BASELINE,
        "manifest_path": str(known.manifest_path.relative_to(root)),
        "oracle_bytes_loaded_by_product_path": False,
        "started_at": datetime.now(UTC).isoformat(),
    }
    _write_json(run_dir / "metadata.json", metadata)
    _append_event(journal, "RUN_DECLARED", **metadata)

    repository_root, workspace_root = _runtime_paths(root)
    repositories = HardModeRepositoryCache(repository_root, known.manifest_path.parent)
    stage = "repository preparation"
    try:
        print(f"[PatchProof] PR: {parsed.url}", flush=True)
        print(f"[PatchProof] case: {case.case_id}", flush=True)
        print("[PatchProof] preparing exact frozen BASE/HEAD...", flush=True)
        repository = repositories.prepare(case)

        changed_tests = repositories.changed_python_test_paths(case, repository)
        if changed_tests != tuple(sorted(case.excluded_paths)):
            raise PrAnalyzeError(
                f"changed-test exclusions differ from the committed case: {changed_tests!r}"
            )

        claim_investigator = GitClaimInvestigatorFactory(
            model=AdkGeminiClaimInvestigator(
                model_name=MODEL_NAME,
                provider_config=provider,
            ),
            source_repository=repository,
            excluded_paths=frozenset(case.excluded_paths),
            priority_paths=frozenset(case.production_files_changed),
        )

        stage = "context retrieval"
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

        stage = "Gemini and BASE/HEAD evaluation"
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
                    claim_investigator=claim_investigator,
                )
            )
        finally:
            stop.set()
            heartbeat.join(timeout=1)

        stage = "artifact persistence"
        persist_result(run_dir=run_dir, metadata=metadata, result=result)
        _append_event(
            journal,
            "RUN_COMPLETED",
            terminal_status=result.get("terminal_status"),
            final_mechanical=result.get("final_mechanical"),
            final_outcome=result.get("final_outcome"),
        )
        print_result(metadata=metadata, result=result, run_dir=run_dir)
    except Exception as error:
        with suppress(OSError):
            persist_infrastructure_failure(
                run_dir=run_dir,
                metadata=metadata,
                stage=stage,
                error=error,
            )
        raise PrAnalyzeRunError(
            f"analysis failed during {stage}: {type(error).__name__}: {str(error)[:500]}; "
            f"artifacts: {run_dir}",
            run_dir=run_dir,
        ) from error

    if result.get("terminal_status") in _INFRASTRUCTURE_TERMINALS:
        raise PrAnalyzeRunError(
            f"analysis ended with infrastructure status {result.get('terminal_status')}; "
            f"artifacts: {run_dir}",
            run_dir=run_dir,
        )
    return run_dir
