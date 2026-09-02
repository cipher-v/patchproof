"""Zero-model readiness preflight for committed historical development cases.

This developer utility exercises every deterministic stage that precedes claim inference:
manifest validation, immutable repository preparation, symmetric environment synthesis,
the real injected-node readiness path, and Phase-2 index/planner construction. It never
loads oracle source and has no model invocation path.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from patchproof.claim_investigator import GitClaimInvestigatorFactory
from patchproof.hard_mode import (
    HardModeCase,
    HardModeRepositoryCache,
    _challenge,
    _execution_plan,
)
from patchproof.pr_analyze import _load_cases, _runtime_paths, find_project_root


class _ModelMustNotRun:
    async def invoke(self, _request: object) -> None:  # pragma: no cover - safety tripwire
        raise AssertionError("readiness preflight must never invoke a model")


@dataclass(frozen=True, slots=True)
class CaseReadiness:
    case_id: str
    repository: str
    install_strategy: str
    plans_equivalent: bool
    environment_status: str
    investigation_status: str
    coverage_complete: bool | None
    observables_truncated: bool | None
    shared_observables: int | None
    new_head_symbols: int | None
    removed_base_symbols: int | None
    duration_seconds: float
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.environment_status == "READY" and self.investigation_status == "READY"


def load_preflight_cases(manifest_path: Path) -> tuple[HardModeCase, ...]:
    """Load historical cases from manifest metadata without opening oracle files."""
    cases = tuple(_load_cases(manifest_path))
    if not cases:
        raise ValueError("readiness preflight manifest contains no historical cases")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("readiness preflight case IDs must be unique")
    return cases


def run_preflight(
    *, project_root: Path, manifest_path: Path, case_ids: frozenset[str] | None = None
) -> tuple[CaseReadiness, ...]:
    """Run every selected case, continuing after failures so one pass finds all blockers."""
    cases = load_preflight_cases(manifest_path)
    if case_ids is not None:
        unknown = case_ids - {case.case_id for case in cases}
        if unknown:
            raise ValueError(f"unknown readiness-preflight cases: {sorted(unknown)!r}")
        cases = tuple(case for case in cases if case.case_id in case_ids)
    repository_root, workspace_root = _runtime_paths(project_root)
    cache = HardModeRepositoryCache(repository_root, manifest_path.parent)
    return tuple(
        _preflight_case(case, cache=cache, workspace_root=workspace_root / "preflight")
        for case in cases
    )


def _preflight_case(
    case: HardModeCase, *, cache: HardModeRepositoryCache, workspace_root: Path
) -> CaseReadiness:
    started = time.monotonic()
    errors: list[str] = []
    strategy = "NOT_RESOLVED"
    equivalent = False
    environment_status = "NOT_RUN"
    investigation_status = "NOT_RUN"
    coverage_complete: bool | None = None
    observables_truncated: bool | None = None
    shared: int | None = None
    added: int | None = None
    removed: int | None = None

    try:
        repository = cache.prepare(case)
        changed_tests = cache.changed_python_test_paths(case, repository)
        if changed_tests != tuple(sorted(case.excluded_paths)):
            errors.append("EXCLUSIONS: changed test paths differ from the committed exclusion set")
    except Exception as error:
        errors.append(f"REPOSITORY: {type(error).__name__}: {error}")
        return CaseReadiness(
            case_id=case.case_id,
            repository=case.repository,
            install_strategy=strategy,
            plans_equivalent=equivalent,
            environment_status=environment_status,
            investigation_status=investigation_status,
            coverage_complete=coverage_complete,
            observables_truncated=observables_truncated,
            shared_observables=shared,
            new_head_symbols=added,
            removed_base_symbols=removed,
            duration_seconds=time.monotonic() - started,
            errors=tuple(errors),
        )

    try:
        plan = _execution_plan(repository, case)
        assert plan.base_install is not None and plan.head_install is not None
        strategy = plan.base_install.strategy.value
        equivalent = plan.base_install.commands == plan.head_install.commands
        if not equivalent:
            raise RuntimeError("BASE and HEAD install commands differ")
        challenge = _challenge(repository, workspace_root, case, plan=plan)
        with challenge.session(base_ref=case.base_sha, head_ref=case.head_sha) as session:
            readiness = session.prepare_environment()
        environment_status = readiness.status.value
        if not readiness.ready:
            detail = readiness.reason
            if readiness.setup_diagnostic is not None:
                diagnostic = readiness.setup_diagnostic
                terminal = (diagnostic.stderr or diagnostic.stdout).strip().splitlines()
                if terminal:
                    detail = f"{detail}; terminal diagnostic: {terminal[-1]}"
            errors.append(f"ENVIRONMENT: {detail}")
    except Exception as error:
        environment_status = "ERROR"
        errors.append(f"ENVIRONMENT: {type(error).__name__}: {error}")

    try:
        factory = GitClaimInvestigatorFactory(
            model=_ModelMustNotRun(),  # type: ignore[arg-type]
            source_repository=repository,
            excluded_paths=frozenset(case.excluded_paths),
            priority_paths=frozenset(case.production_files_changed),
        )
        investigator = factory.build(base_sha=case.base_sha, head_sha=case.head_sha)
        context = investigator.planner.build()
        if context.base_sha != case.base_sha or context.head_sha != case.head_sha:
            raise RuntimeError("starting context revisions differ from the manifest")
        coverage = context.coverage
        base_index = investigator.planner.investigator.base
        head_index = investigator.planner.investigator.head
        if (
            coverage.base_index_sha256 != base_index.sha256
            or coverage.head_index_sha256 != head_index.sha256
            or coverage.base_truncated != base_index.stats.truncated
            or coverage.head_truncated != head_index.stats.truncated
            or coverage.base_syntax_errors != base_index.stats.syntax_errors
            or coverage.head_syntax_errors != head_index.stats.syntax_errors
        ):
            raise RuntimeError("starting-context coverage metadata contradicts its indexes")
        budget = investigator.planner.budget
        if (
            len(context.shared_observables) > budget.max_shared_observables
            or len(context.new_head_symbols) > budget.max_new_head_symbols
            or len(context.removed_base_symbols) > budget.max_removed_symbols
            or len(context.identities) != len(context.shared_observables)
        ):
            raise RuntimeError("starting context violates a bound or has duplicate identities")
        if not coverage.complete and not any("NOT evidence" in note for note in context.notes):
            raise RuntimeError("partial starting context omits its absence-safety warning")
        coverage_complete = context.coverage.complete
        observables_truncated = context.coverage.observables_truncated
        shared = len(context.shared_observables)
        added = len(context.new_head_symbols)
        removed = len(context.removed_base_symbols)
        investigation_status = "READY"
    except Exception as error:
        investigation_status = "ERROR"
        errors.append(f"INVESTIGATION: {type(error).__name__}: {error}")

    return CaseReadiness(
        case_id=case.case_id,
        repository=case.repository,
        install_strategy=strategy,
        plans_equivalent=equivalent,
        environment_status=environment_status,
        investigation_status=investigation_status,
        coverage_complete=coverage_complete,
        observables_truncated=observables_truncated,
        shared_observables=shared,
        new_head_symbols=added,
        removed_base_symbols=removed,
        duration_seconds=time.monotonic() - started,
        errors=tuple(errors),
    )


def render_table(results: tuple[CaseReadiness, ...]) -> str:
    lines = [
        "case | strategy | plans | environment | investigation | coverage | seconds | ready",
        "--- | --- | --- | --- | --- | --- | ---: | ---",
    ]
    for result in results:
        coverage = "complete" if result.coverage_complete else "partial"
        if result.coverage_complete is None:
            coverage = "n/a"
        lines.append(
            " | ".join(
                (
                    result.case_id,
                    result.install_strategy,
                    "equivalent" if result.plans_equivalent else "different",
                    result.environment_status,
                    result.investigation_status,
                    coverage,
                    f"{result.duration_seconds:.1f}",
                    "YES" if result.ready else "NO",
                )
            )
        )
        for error in result.errors:
            lines.append(f"  error: {error}")
    ready = sum(result.ready for result in results)
    lines.append(f"READY: {ready}/{len(results)}")
    return "\n".join(lines)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/holdout/manifest.json"))
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--json", type=Path)
    parsed = parser.parse_args(arguments)
    root = find_project_root()
    manifest = (root / parsed.manifest).resolve()
    results = run_preflight(
        project_root=root,
        manifest_path=manifest,
        case_ids=frozenset(parsed.case) if parsed.case else None,
    )
    print(render_table(results))
    if parsed.json is not None:
        document = [asdict(result) | {"ready": result.ready} for result in results]
        parsed.json.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0 if all(result.ready for result in results) else 1


if __name__ == "__main__":  # pragma: no cover - exercised as an operator command
    raise SystemExit(main())
