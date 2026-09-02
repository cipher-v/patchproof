"""Case-neutral deterministic preparation for URL-submitted product runs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from patchproof.context_retrieval import ContextRetrievalError, DeterministicContextRetriever
from patchproof.execution_contract import (
    ExecutionContract,
    ExecutionContractError,
    ExecutionContractLoader,
)
from patchproof.install_strategy import (
    ContractSynthesisError,
    DependencyInstallProber,
    InstallPlan,
    resolve_contract_for_pair,
)
from patchproof.pr_resolution import ResolvedPullRequest

_MAX_CHANGED_PATHS = 2_000


class UnsupportedExecutionPlanError(RuntimeError):
    """Raised when immutable revisions cannot share one validated execution plan."""


class ProductExecutionPlanSource(StrEnum):
    """Deterministic provenance for the product execution contract."""

    REPOSITORY_CONTRACT = "REPOSITORY_CONTRACT"
    DETERMINISTIC_PROBE = "DETERMINISTIC_PROBE"


@dataclass(frozen=True, slots=True)
class ProductExecutionPlan:
    """One equivalent execution contract and its non-model provenance."""

    contract: ExecutionContract
    source: ProductExecutionPlanSource
    base_install: InstallPlan | None = None
    head_install: InstallPlan | None = None


@dataclass(frozen=True, slots=True)
class PreparedPullRequest:
    """Only the case-neutral facts needed to execute one product analysis."""

    resolved: ResolvedPullRequest
    excluded_paths: tuple[str, ...]
    priority_paths: tuple[str, ...]
    execution_plan: ProductExecutionPlan


class ProductExecutionContextRetriever(DeterministicContextRetriever):
    """Expose a server-validated contract without adding benchmark metadata to context."""

    def __init__(
        self,
        *,
        source_repository: Path,
        excluded_paths: frozenset[str],
        contract: ExecutionContract,
        revisions: frozenset[str],
    ) -> None:
        super().__init__(source_repository=source_repository, excluded_paths=excluded_paths)
        if len(revisions) != 2:
            raise ValueError("product execution context requires exactly two revisions")
        self._product_contract = contract
        self._product_revisions = revisions

    def read_committed_file(
        self,
        *,
        revision_sha: str,
        path: str,
        max_bytes: int = 8_192,
    ) -> bytes:
        if path != ExecutionContractLoader.filename:
            return super().read_committed_file(
                revision_sha=revision_sha,
                path=path,
                max_bytes=max_bytes,
            )
        if revision_sha not in self._product_revisions:
            raise ValueError("product contract requested for an unexpected revision")
        content = self._product_contract.model_dump_json().encode("utf-8")
        if len(content) > max_bytes:
            raise ValueError("product execution contract exceeds its byte budget")
        return content


def prepare_pull_request(
    *,
    resolved: ResolvedPullRequest,
    source_repository: Path,
) -> PreparedPullRequest:
    """Derive exclusions, priorities, and one symmetric plan from immutable Git data."""
    reader = DeterministicContextRetriever(source_repository=source_repository)
    changes = reader._changed_files(resolved.base_sha, resolved.head_sha)
    if len(changes) > _MAX_CHANGED_PATHS:
        raise ContextRetrievalError("pull request exceeds the changed-path preparation budget")

    excluded: set[str] = set()
    priority: set[str] = set()
    for change in changes:
        paths = (
            (change.path,) if change.previous_path is None else (change.previous_path, change.path)
        )
        for path in paths:
            if path.endswith(".py") and reader._is_test_path(path):
                excluded.add(path)
        if change.is_python and not change.is_test:
            priority.add(change.path)

    plan = resolve_product_execution_plan(
        reader=reader,
        base_sha=resolved.base_sha,
        head_sha=resolved.head_sha,
    )
    return PreparedPullRequest(
        resolved=resolved,
        excluded_paths=tuple(sorted(excluded)),
        priority_paths=tuple(sorted(priority)),
        execution_plan=plan,
    )


def resolve_product_execution_plan(
    *,
    reader: DeterministicContextRetriever,
    base_sha: str,
    head_sha: str,
) -> ProductExecutionPlan:
    """Prefer an identical explicit contract, otherwise require symmetric safe probing."""
    loader = ExecutionContractLoader()
    base_paths = reader.committed_paths(base_sha)
    head_paths = reader.committed_paths(head_sha)
    filename = loader.filename
    base_declares = filename in base_paths
    head_declares = filename in head_paths
    if base_declares or head_declares:
        if not (base_declares and head_declares):
            raise UnsupportedExecutionPlanError(
                "BASE and HEAD do not both declare the repository execution contract"
            )
        try:
            base_contract = loader.load_bytes(
                reader.read_committed_file(revision_sha=base_sha, path=filename)
            )
            head_contract = loader.load_bytes(
                reader.read_committed_file(revision_sha=head_sha, path=filename)
            )
        except (ContextRetrievalError, ExecutionContractError) as error:
            raise UnsupportedExecutionPlanError(
                "repository execution contract is invalid"
            ) from error
        if base_contract.synthesized or head_contract.synthesized:
            raise UnsupportedExecutionPlanError(
                "repository execution contract cannot claim synthesized provenance"
            )
        if base_contract != head_contract:
            raise UnsupportedExecutionPlanError(
                "BASE and HEAD repository execution contracts differ"
            )
        return ProductExecutionPlan(
            contract=head_contract,
            source=ProductExecutionPlanSource.REPOSITORY_CONTRACT,
        )

    try:
        contract, base_plan, head_plan = resolve_contract_for_pair(
            prober=DependencyInstallProber(reader=reader),
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except ContractSynthesisError as error:
        raise UnsupportedExecutionPlanError(
            "no supported equivalent BASE/HEAD execution plan is available"
        ) from error
    return ProductExecutionPlan(
        contract=contract,
        source=ProductExecutionPlanSource.DETERMINISTIC_PROBE,
        base_install=base_plan,
        head_install=head_plan,
    )
