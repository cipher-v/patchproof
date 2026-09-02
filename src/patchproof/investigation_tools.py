"""Bounded read-only investigation operations over immutable repository indexes."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchproof.models import RevisionRole
from patchproof.repository_index import (
    IndexedImport,
    IndexedReference,
    IndexedSymbol,
    ObservableSymbolKind,
    ReferenceKind,
    RepositoryIndex,
    RepositoryIndexStats,
    validate_repository_source_path,
)

_QUALIFIED_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


class InvestigationQueryError(ValueError):
    """Raised when an untrusted query exceeds or violates the investigation contract."""


class InvestigationQueryKind(StrEnum):
    """Auditable operation names for a future investigator transcript."""

    INSPECT_SYMBOL = "INSPECT_SYMBOL"
    COMPARE_SYMBOL = "COMPARE_SYMBOL"
    COMPARE_OBSERVABLES = "COMPARE_OBSERVABLES"
    FIND_REFERENCES = "FIND_REFERENCES"
    FIND_CALLERS = "FIND_CALLERS"
    FIND_RELATED_TESTS = "FIND_RELATED_TESTS"
    INSPECT_SOURCE_WINDOW = "INSPECT_SOURCE_WINDOW"


class InvestigationMatchType(StrEnum):
    """Why one deterministic query result matched."""

    EXACT_PATH_AND_SYMBOL = "EXACT_PATH_AND_SYMBOL"
    EXACT_SYMBOL = "EXACT_SYMBOL"
    AGGREGATED_SYMBOL_DEFINITIONS = "AGGREGATED_SYMBOL_DEFINITIONS"
    QUALIFIED_SYNTAX_REFERENCE = "QUALIFIED_SYNTAX_REFERENCE"
    STATIC_IMPORT_ALIAS_REFERENCE = "STATIC_IMPORT_ALIAS_REFERENCE"
    NAME_SYNTAX_REFERENCE = "NAME_SYNTAX_REFERENCE"
    ATTRIBUTE_SYNTAX_REFERENCE = "ATTRIBUTE_SYNTAX_REFERENCE"
    STATIC_IMPORT_ALIAS_CALL = "STATIC_IMPORT_ALIAS_CALL"
    POSSIBLE_NAME_CALL = "POSSIBLE_NAME_CALL"
    POSSIBLE_ATTRIBUTE_CALL = "POSSIBLE_ATTRIBUTE_CALL"
    SYNTACTIC_IMPORT_BINDING = "SYNTACTIC_IMPORT_BINDING"
    MODULE_SUFFIX_REFERENCE = "MODULE_SUFFIX_REFERENCE"
    TEXTUAL_FALLBACK = "TEXTUAL_FALLBACK"
    EXACT_SOURCE_WINDOW = "EXACT_SOURCE_WINDOW"


class ObservablePresence(StrEnum):
    """Cross-revision availability of one observable identity."""

    PRESENT_ON_BOTH = "PRESENT_ON_BOTH"
    NEW_ON_HEAD = "NEW_ON_HEAD"
    REMOVED_FROM_HEAD = "REMOVED_FROM_HEAD"


@dataclass(frozen=True, slots=True)
class InvestigationBudget:
    """Hard query/result limits independent from the index build limits."""

    default_max_results: int = 20
    hard_max_results: int = 50
    max_symbol_query_chars: int = 256
    max_symbol_source_lines: int = 120
    max_source_window_lines: int = 120
    max_source_chars: int = 12_000
    max_total_source_chars: int = 24_000
    max_relevance_hops: int = 3

    def __post_init__(self) -> None:
        for item in fields(self):
            if getattr(self, item.name) <= 0:
                raise ValueError(f"investigation budget {item.name} must be positive")
        if self.default_max_results > self.hard_max_results or self.hard_max_results > 100:
            raise ValueError("investigation result budgets exceed their safety ceiling")
        if self.max_symbol_query_chars > 512:
            raise ValueError("symbol query budget exceeds its safety ceiling")
        if self.max_symbol_source_lines > 300 or self.max_source_window_lines > 300:
            raise ValueError("source line budget exceeds its safety ceiling")
        if self.max_source_chars > 32_000:
            raise ValueError("source character budget exceeds its safety ceiling")
        if self.max_relevance_hops > 5:
            raise ValueError("relevance traversal exceeds its safety ceiling")
        if (
            self.max_total_source_chars < self.max_source_chars
            or self.max_total_source_chars > 64_000
        ):
            raise ValueError("aggregate source character budget exceeds its safety contract")


class InvestigationProvenance(BaseModel):
    """Location and deterministic reason attached to every returned match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_kind: InvestigationQueryKind
    revision: RevisionRole
    path: str
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    symbol_name: str | None = Field(default=None, max_length=512)
    match_type: InvestigationMatchType
    reason: str = Field(min_length=1, max_length=500)
    source_truncated: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> InvestigationProvenance:
        validate_repository_source_path(self.path)
        if self.end_line < self.start_line:
            raise ValueError("provenance end line cannot precede its start line")
        return self


class InvestigationMatch(BaseModel):
    """Typed match payload plus auditable provenance and optional bounded source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provenance: InvestigationProvenance
    symbol: IndexedSymbol | None = None
    reference: IndexedReference | None = None
    import_record: IndexedImport | None = None
    content: str | None = Field(default=None, max_length=32_000)

    @model_validator(mode="after")
    def validate_payload(self) -> InvestigationMatch:
        if not any((self.symbol, self.reference, self.import_record, self.content is not None)):
            raise ValueError("investigation match requires a typed payload or source content")
        return self


class InvestigationResult(BaseModel):
    """Bounded ordered matches for one revision-scoped query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_kind: InvestigationQueryKind
    revision: RevisionRole
    revision_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_stats: RepositoryIndexStats
    matches: tuple[InvestigationMatch, ...] = Field(max_length=100)
    truncated: bool


class SymbolComparison(BaseModel):
    """BASE/HEAD views for identity defined strictly as path plus qualified name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    qualified_name: str = Field(min_length=1, max_length=512)
    presence: ObservablePresence
    base: IndexedSymbol | None
    head: IndexedSymbol | None
    implementation_changed: bool | None

    @model_validator(mode="after")
    def validate_presence(self) -> SymbolComparison:
        validate_repository_source_path(self.path)
        expected = {
            ObservablePresence.PRESENT_ON_BOTH: (True, True),
            ObservablePresence.NEW_ON_HEAD: (False, True),
            ObservablePresence.REMOVED_FROM_HEAD: (True, False),
        }[self.presence]
        if (self.base is not None, self.head is not None) != expected:
            raise ValueError("symbol comparison presence contradicts BASE/HEAD views")
        if self.presence is ObservablePresence.PRESENT_ON_BOTH:
            if self.implementation_changed is None:
                raise ValueError("shared symbol comparison requires an implementation decision")
        elif self.implementation_changed is not None:
            raise ValueError("absent symbol comparison cannot claim implementation change")
        return self


class SymbolComparisonResult(BaseModel):
    """Bounded comparison results for an explicit path/name query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_kind: InvestigationQueryKind = InvestigationQueryKind.COMPARE_SYMBOL
    base_revision_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_revision_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    head_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_index_stats: RepositoryIndexStats
    head_index_stats: RepositoryIndexStats
    comparisons: tuple[SymbolComparison, ...] = Field(max_length=100)
    truncated: bool


class CrossRevisionObservables(BaseModel):
    """Bounded observable partition for the complete indexed BASE/HEAD pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_kind: InvestigationQueryKind = InvestigationQueryKind.COMPARE_OBSERVABLES
    base_revision_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_revision_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    head_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_index_stats: RepositoryIndexStats
    head_index_stats: RepositoryIndexStats
    present_on_both: tuple[SymbolComparison, ...] = Field(max_length=100)
    new_on_head: tuple[IndexedSymbol, ...] = Field(max_length=100)
    removed_from_head: tuple[IndexedSymbol, ...] = Field(max_length=100)
    truncated: bool


class RepositoryInvestigator:
    """Query two prebuilt immutable indexes without filesystem, network, or process access."""

    def __init__(
        self,
        *,
        base: RepositoryIndex,
        head: RepositoryIndex,
        budget: InvestigationBudget | None = None,
    ) -> None:
        if base.revision.role is not RevisionRole.BASE:
            raise ValueError("base repository index must use the BASE role")
        if head.revision.role is not RevisionRole.HEAD:
            raise ValueError("head repository index must use the HEAD role")
        if base.revision.sha == head.revision.sha:
            raise ValueError("BASE and HEAD repository indexes must use distinct revisions")
        self.base = base
        self.head = head
        self.budget = budget or InvestigationBudget()

    def inspect_symbol(
        self,
        *,
        revision: RevisionRole | str,
        path: str | None = None,
        qualified_name: str | None = None,
        maximum: int | None = None,
    ) -> InvestigationResult:
        """Return exact bounded source spans for matching indexed symbols."""
        index, role = self._index(revision)
        normalized_path = self._optional_path(index, path)
        normalized_name = self._optional_symbol(qualified_name)
        if normalized_path is None and normalized_name is None:
            raise InvestigationQueryError("inspect_symbol requires path and/or qualified_name")
        limit = self._maximum(maximum)
        matches: list[InvestigationMatch] = []
        for symbol in index.symbols:
            if normalized_path is not None and symbol.path != normalized_path:
                continue
            if normalized_name is not None and not self._symbol_matches(symbol, normalized_name):
                continue
            content, source_end, source_truncated = self._symbol_source(index, symbol)
            match_type = InvestigationMatchType.AGGREGATED_SYMBOL_DEFINITIONS
            reason = f"enclosing source for {symbol.definition_count} same-identity definitions"
            if symbol.definition_count == 1:
                match_type = (
                    InvestigationMatchType.EXACT_PATH_AND_SYMBOL
                    if normalized_path is not None and normalized_name is not None
                    else InvestigationMatchType.EXACT_SYMBOL
                )
                reason = "exact indexed symbol definition"
            matches.append(
                InvestigationMatch(
                    provenance=InvestigationProvenance(
                        query_kind=InvestigationQueryKind.INSPECT_SYMBOL,
                        revision=role,
                        path=symbol.path,
                        start_line=symbol.start_line,
                        end_line=source_end,
                        symbol_name=symbol.qualified_name,
                        match_type=match_type,
                        reason=reason,
                        source_truncated=source_truncated,
                    ),
                    symbol=symbol,
                    content=content,
                )
            )
            if len(matches) > limit:
                break
        return self._result(InvestigationQueryKind.INSPECT_SYMBOL, role, matches, limit=limit)

    def compare_symbol(
        self,
        *,
        path: str | None = None,
        qualified_name: str | None = None,
        maximum: int | None = None,
    ) -> SymbolComparisonResult:
        """Compare symbol identity and source hashes with explicit absence indicators."""
        normalized_path = self._optional_pair_path(path)
        normalized_name = self._optional_symbol(qualified_name)
        if normalized_path is None and normalized_name is None:
            raise InvestigationQueryError("compare_symbol requires path and/or qualified_name")
        comparisons = self._comparisons(
            path=normalized_path,
            qualified_name=normalized_name,
        )
        limit = self._maximum(maximum)
        return SymbolComparisonResult(
            base_revision_sha=self.base.revision.sha,
            head_revision_sha=self.head.revision.sha,
            base_index_sha256=self.base.sha256,
            head_index_sha256=self.head.sha256,
            base_index_stats=self.base.stats,
            head_index_stats=self.head.stats,
            comparisons=tuple(comparisons[:limit]),
            truncated=len(comparisons) > limit,
        )

    def compare_observables(self, *, maximum: int | None = None) -> CrossRevisionObservables:
        """Return a relevance-first bounded partition of cross-revision observables.

        Direct changes, additions, removals, and shared definitions that transitively
        reference a changed or added symbol are selected before unrelated shared symbols. The
        high-signal groups are interleaved deterministically so one large change kind
        cannot crowd every other kind out of the bounded result.
        """
        limit = self._maximum(maximum)
        comparisons = self._comparisons(path=None, qualified_name=None)
        selected = self._prioritized_observable_comparisons(comparisons)[:limit]
        shared = [item for item in selected if item.presence is ObservablePresence.PRESENT_ON_BOTH]
        added = [item.head for item in selected if item.presence is ObservablePresence.NEW_ON_HEAD]
        removed = [
            item.base for item in selected if item.presence is ObservablePresence.REMOVED_FROM_HEAD
        ]
        return CrossRevisionObservables(
            base_revision_sha=self.base.revision.sha,
            head_revision_sha=self.head.revision.sha,
            base_index_sha256=self.base.sha256,
            head_index_sha256=self.head.sha256,
            base_index_stats=self.base.stats,
            head_index_stats=self.head.stats,
            present_on_both=tuple(shared),
            new_on_head=tuple(item for item in added if item is not None),
            removed_from_head=tuple(item for item in removed if item is not None),
            truncated=len(comparisons) > limit,
        )

    def changed_symbol_names(self) -> frozenset[str]:
        """Return changed/added leaf names from the complete bounded repository indexes.

        This internal planning signal is computed before observable-result truncation.
        Only the separately bounded starting-context representation is exposed to the
        model.
        """
        return self._changed_symbol_names(self._comparisons(path=None, qualified_name=None))

    def relevance_identities(self) -> frozenset[str]:
        """Return exact `path::qualified_name` identities in the bounded caller closure."""
        comparisons = self._comparisons(path=None, qualified_name=None)
        relevant = self._relevance_identities(comparisons)
        return frozenset(f"{path}::{qualified_name}" for path, qualified_name in relevant)

    def find_references(
        self,
        *,
        revision: RevisionRole | str,
        symbol: str,
        maximum: int | None = None,
    ) -> InvestigationResult:
        """Find bounded syntactic/import references ranked by static exactness."""
        index, role = self._index(revision)
        query = self._symbol(symbol)
        limit = self._maximum(maximum)
        ranked = self._ranked_references(index, role, query, callers_only=False)
        return self._result(
            InvestigationQueryKind.FIND_REFERENCES,
            role,
            [item[1] for item in ranked],
            limit=limit,
        )

    def find_callers(
        self,
        *,
        revision: RevisionRole | str,
        symbol: str,
        maximum: int | None = None,
    ) -> InvestigationResult:
        """Find possible syntactic call sites without claiming runtime callee identity."""
        index, role = self._index(revision)
        query = self._symbol(symbol)
        limit = self._maximum(maximum)
        ranked = self._ranked_references(index, role, query, callers_only=True)
        return self._result(
            InvestigationQueryKind.FIND_CALLERS,
            role,
            [item[1] for item in ranked],
            limit=limit,
        )

    def find_related_tests(
        self,
        *,
        revision: RevisionRole | str,
        symbol: str | None = None,
        path: str | None = None,
        maximum: int | None = None,
    ) -> InvestigationResult:
        """Rank tests by exact reference, import, module, then labelled textual fallback."""
        index, role = self._index(revision)
        query = self._optional_symbol(symbol)
        normalized_path = self._optional_path(index, path)
        if query is None and normalized_path is None:
            raise InvestigationQueryError("find_related_tests requires symbol and/or path")
        limit = self._maximum(maximum)
        leaf = query.rsplit(".", maxsplit=1)[-1] if query else None
        module = index.file(normalized_path).module if normalized_path else None  # type: ignore[union-attr]
        module_leaf = module.rsplit(".", maxsplit=1)[-1] if module else None
        test_keys = {(item.path, item.qualified_name) for item in index.test_functions}
        references_by_test: dict[tuple[str, str], list[IndexedReference]] = {}
        for reference in index.references:
            candidate_scope = reference.scope
            while candidate_scope and candidate_scope != "<module>":
                key = (reference.path, candidate_scope)
                if key in test_keys:
                    references_by_test.setdefault(key, []).append(reference)
                    break
                candidate_scope = (
                    candidate_scope.rsplit(".", maxsplit=1)[0] if "." in candidate_scope else ""
                )
        module_imports_by_path: dict[str, list[IndexedImport]] = {}
        imports_by_test: dict[tuple[str, str], list[IndexedImport]] = {}
        for imported in index.imports:
            if imported.scope == "<module>":
                module_imports_by_path.setdefault(imported.path, []).append(imported)
                continue
            candidate_scope = imported.scope
            while candidate_scope:
                key = (imported.path, candidate_scope)
                if key in test_keys:
                    imports_by_test.setdefault(key, []).append(imported)
                    break
                candidate_scope = (
                    candidate_scope.rsplit(".", maxsplit=1)[0] if "." in candidate_scope else ""
                )
        source_lines_by_path: dict[str, list[str]] = {}
        ranked: list[tuple[int, InvestigationMatch]] = []
        for test in index.test_functions:
            references = references_by_test.get((test.path, test.qualified_name), [])
            local_imports = imports_by_test.get((test.path, test.qualified_name), [])
            imports = [*local_imports, *module_imports_by_path.get(test.path, [])]
            rank: int | None = None
            match_type: InvestigationMatchType | None = None
            reason = ""
            alias_reference = next(
                (
                    item
                    for item in references
                    if item.module_import_alias_expansion
                    and (
                        item.module_import_alias_expansion == query
                        or item.module_import_alias_expansion.endswith(f".{query}")
                    )
                ),
                None,
            )
            syntax_reference = next((item for item in references if item.expression == query), None)
            name_reference = next((item for item in references if item.name == leaf), None)
            if query and alias_reference is not None:
                rank, match_type, reason = (
                    0,
                    InvestigationMatchType.STATIC_IMPORT_ALIAS_REFERENCE,
                    (
                        f"test has module-import alias syntax expanding to {query}; "
                        "runtime rebinding is not proven"
                    ),
                )
            elif query and syntax_reference is not None:
                rank, match_type, reason = (
                    1,
                    (
                        InvestigationMatchType.QUALIFIED_SYNTAX_REFERENCE
                        if "." in syntax_reference.expression
                        else InvestigationMatchType.NAME_SYNTAX_REFERENCE
                    ),
                    f"test contains exact syntax {query}; runtime identity is unresolved",
                )
            elif leaf and name_reference is not None:
                rank, match_type, reason = (
                    2,
                    (
                        InvestigationMatchType.ATTRIBUTE_SYNTAX_REFERENCE
                        if "." in name_reference.expression
                        else InvestigationMatchType.NAME_SYNTAX_REFERENCE
                    ),
                    f"test contains terminal identifier {leaf}; binding identity is unresolved",
                )
            elif (
                query
                and (
                    matching_import := next(
                        (
                            item
                            for item in imports
                            if query == item.imported_target
                            or item.imported_target.endswith(f".{query}")
                            or ("." not in query and item.alias == leaf)
                        ),
                        None,
                    )
                )
                is not None
            ):
                rank, match_type, reason = (
                    3,
                    InvestigationMatchType.SYNTACTIC_IMPORT_BINDING,
                    (
                        f"test scope contains a syntactic import binding for {query}"
                        if matching_import.scope != "<module>"
                        else f"test file contains a module import binding for {query}"
                    ),
                )
            elif module_leaf and any(
                item.module.lstrip(".").endswith(module or "")
                or item.module.rsplit(".", maxsplit=1)[-1] == module_leaf
                for item in imports
            ):
                rank, match_type, reason = (
                    4,
                    InvestigationMatchType.MODULE_SUFFIX_REFERENCE,
                    (
                        f"test import suffix matches module for {normalized_path}; "
                        "package identity is unresolved"
                    ),
                )
            else:
                if test.path not in source_lines_by_path:
                    source_lines_by_path[test.path] = (index._source(test.path) or "").splitlines()
                source_lines = source_lines_by_path[test.path]
                bounded_test_source = "\n".join(source_lines[test.start_line - 1 : test.end_line])
                term = leaf or module_leaf
                if term and re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                    bounded_test_source,
                ):
                    rank, match_type, reason = (
                        5,
                        InvestigationMatchType.TEXTUAL_FALLBACK,
                        f"bounded textual fallback matched literal identifier {term}",
                    )
            if rank is None or match_type is None:
                continue
            ranked.append(
                (
                    rank,
                    InvestigationMatch(
                        provenance=InvestigationProvenance(
                            query_kind=InvestigationQueryKind.FIND_RELATED_TESTS,
                            revision=role,
                            path=test.path,
                            start_line=test.start_line,
                            end_line=test.end_line,
                            symbol_name=test.qualified_name,
                            match_type=match_type,
                            reason=reason,
                        ),
                        symbol=test,
                    ),
                )
            )
        ranked.sort(
            key=lambda item: (
                item[0],
                item[1].provenance.path,
                item[1].provenance.start_line,
                item[1].provenance.symbol_name or "",
            )
        )
        return self._result(
            InvestigationQueryKind.FIND_RELATED_TESTS,
            role,
            [item[1] for item in ranked],
            limit=limit,
        )

    def inspect_source_window(
        self,
        *,
        revision: RevisionRole | str,
        path: str,
        start_line: int,
        line_count: int,
    ) -> InvestigationResult:
        """Read one bounded window only from source bytes already retained in the index."""
        index, role = self._index(revision)
        normalized_path = self._required_path(index, path)
        if type(start_line) is not int or start_line <= 0:
            raise InvestigationQueryError("source window start_line must be a positive integer")
        if (
            type(line_count) is not int
            or line_count <= 0
            or line_count > self.budget.max_source_window_lines
        ):
            raise InvestigationQueryError("source window line_count exceeds its hard limit")
        metadata = index.file(normalized_path)
        source = index._source(normalized_path)
        if metadata is None or source is None or metadata.line_count == 0:
            raise InvestigationQueryError("source window path has no indexed text lines")
        if start_line > metadata.line_count:
            raise InvestigationQueryError("source window starts beyond the indexed file")
        requested_end = start_line + line_count - 1
        end_line = min(metadata.line_count, requested_end)
        content = "\n".join(source.splitlines()[start_line - 1 : end_line])
        content, chars_truncated = self._truncate_content(content)
        truncated = requested_end > metadata.line_count or chars_truncated
        match = InvestigationMatch(
            provenance=InvestigationProvenance(
                query_kind=InvestigationQueryKind.INSPECT_SOURCE_WINDOW,
                revision=role,
                path=normalized_path,
                start_line=start_line,
                end_line=end_line,
                match_type=InvestigationMatchType.EXACT_SOURCE_WINDOW,
                reason="exact bounded window from pre-indexed immutable source",
                source_truncated=truncated,
            ),
            content=content,
        )
        return InvestigationResult(
            query_kind=InvestigationQueryKind.INSPECT_SOURCE_WINDOW,
            revision=role,
            revision_sha=index.revision.sha,
            index_sha256=index.sha256,
            index_stats=index.stats,
            matches=(match,),
            truncated=truncated,
        )

    def _comparisons(
        self, *, path: str | None, qualified_name: str | None
    ) -> list[SymbolComparison]:
        def selected(index: RepositoryIndex) -> dict[tuple[str, str], IndexedSymbol]:
            return {
                (symbol.path, symbol.qualified_name): symbol
                for symbol in index.symbols
                if (path is None or symbol.path == path)
                and (qualified_name is None or self._symbol_matches(symbol, qualified_name))
            }

        base = selected(self.base)
        head = selected(self.head)
        comparisons: list[SymbolComparison] = []
        for key in sorted(set(base) | set(head)):
            base_symbol = base.get(key)
            head_symbol = head.get(key)
            if base_symbol is not None and head_symbol is not None:
                presence = ObservablePresence.PRESENT_ON_BOTH
                changed = (
                    base_symbol.source_sha256 != head_symbol.source_sha256
                    or base_symbol.kind is not head_symbol.kind
                    or base_symbol.definition_kinds != head_symbol.definition_kinds
                    or base_symbol.definition_count != head_symbol.definition_count
                )
            elif head_symbol is not None:
                presence = ObservablePresence.NEW_ON_HEAD
                changed = None
            else:
                presence = ObservablePresence.REMOVED_FROM_HEAD
                changed = None
            comparisons.append(
                SymbolComparison(
                    path=key[0],
                    qualified_name=key[1],
                    presence=presence,
                    base=base_symbol,
                    head=head_symbol,
                    implementation_changed=changed,
                )
            )
        return comparisons

    def _prioritized_observable_comparisons(
        self, comparisons: list[SymbolComparison]
    ) -> list[SymbolComparison]:
        relevant_identities = self._relevance_identities(comparisons)
        changed_shared: list[SymbolComparison] = []
        added: list[SymbolComparison] = []
        removed: list[SymbolComparison] = []
        relevant_shared: list[SymbolComparison] = []
        unrelated_shared: list[SymbolComparison] = []

        for comparison in comparisons:
            if comparison.presence is ObservablePresence.NEW_ON_HEAD:
                added.append(comparison)
            elif comparison.presence is ObservablePresence.REMOVED_FROM_HEAD:
                removed.append(comparison)
            elif comparison.implementation_changed:
                changed_shared.append(comparison)
            elif (comparison.path, comparison.qualified_name) in relevant_identities:
                relevant_shared.append(comparison)
            else:
                unrelated_shared.append(comparison)

        groups = (changed_shared, added, removed, relevant_shared)
        for group in (*groups, unrelated_shared):
            group.sort(key=self._observable_priority_key)
        prioritized: list[SymbolComparison] = []
        offset = 0
        while any(offset < len(group) for group in groups):
            for group in groups:
                if offset < len(group):
                    prioritized.append(group[offset])
            offset += 1
        prioritized.extend(unrelated_shared)
        return prioritized

    @staticmethod
    def _changed_symbol_names(comparisons: list[SymbolComparison]) -> frozenset[str]:
        return frozenset(
            comparison.qualified_name.rsplit(".", maxsplit=1)[-1]
            for comparison in comparisons
            if comparison.presence is ObservablePresence.NEW_ON_HEAD
            or (
                comparison.presence is ObservablePresence.PRESENT_ON_BOTH
                and comparison.implementation_changed
            )
        )

    def _relevance_identities(
        self, comparisons: list[SymbolComparison]
    ) -> frozenset[tuple[str, str]]:
        """Expand direct changes through a bounded exact-identity caller closure."""
        relevant = {
            (comparison.path, comparison.qualified_name)
            for comparison in comparisons
            if comparison.presence is ObservablePresence.NEW_ON_HEAD
            or (
                comparison.presence is ObservablePresence.PRESENT_ON_BOTH
                and comparison.implementation_changed
            )
        }
        shared = [
            comparison
            for comparison in comparisons
            if comparison.presence is ObservablePresence.PRESENT_ON_BOTH
            and comparison.head is not None
        ]
        shared.sort(key=self._observable_priority_key)
        targets_by_expansion: dict[str, set[tuple[str, str]]] = {}
        targets_by_path_and_name: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for comparison in comparisons:
            target = comparison.head
            if target is None:
                continue
            identity = (comparison.path, comparison.qualified_name)
            expansions = {f"{target.module}.{target.qualified_name}"}
            if target.path.startswith("src/") and target.module.startswith("src."):
                expansions.add(f"{target.module[4:]}.{target.qualified_name}")
            for expansion in expansions:
                targets_by_expansion.setdefault(expansion, set()).add(identity)
            targets_by_path_and_name.setdefault((target.path, target.qualified_name), set()).add(
                identity
            )

        references_by_path: dict[str, list[IndexedReference]] = {}
        for reference in self.head.references:
            references_by_path.setdefault(reference.path, []).append(reference)
        dependency_graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for comparison in shared:
            symbol = comparison.head
            assert symbol is not None
            identity = (comparison.path, comparison.qualified_name)
            dependencies = dependency_graph.setdefault(identity, set())
            for reference in references_by_path.get(symbol.path, ()):
                if not symbol.start_line <= reference.start_line <= symbol.end_line:
                    continue
                expansion = reference.module_import_alias_expansion
                if expansion is not None:
                    dependencies.update(targets_by_expansion.get(expansion, ()))
                    continue
                local_name = self._same_file_reference_name(symbol, reference)
                if local_name is not None:
                    dependencies.update(targets_by_path_and_name.get((symbol.path, local_name), ()))

        for _ in range(self.budget.max_relevance_hops):
            reached = {
                identity
                for identity, dependencies in dependency_graph.items()
                if dependencies & relevant
            }
            additions = reached - relevant
            if not additions:
                break
            relevant.update(additions)
        return frozenset(relevant)

    @staticmethod
    def _same_file_reference_name(symbol: IndexedSymbol, reference: IndexedReference) -> str | None:
        """Resolve only lexically defensible same-file identities.

        Bare names can refer to module-level definitions. ``self``/``cls`` attributes
        can refer to a method on the enclosing indexed class. Other unresolved
        attributes are deliberately ignored instead of being joined by leaf name.
        """
        if "." not in reference.expression:
            return reference.name
        receiver, _, _ = reference.expression.rpartition(".")
        if receiver not in {"self", "cls"}:
            return None
        parent = (
            symbol.qualified_name
            if symbol.kind is ObservableSymbolKind.CLASS
            else symbol.qualified_name.rsplit(".", maxsplit=1)[0]
        )
        return f"{parent}.{reference.name}"

    @staticmethod
    def _observable_priority_key(comparison: SymbolComparison) -> tuple[int, int, str, str]:
        symbol = comparison.head or comparison.base
        if symbol is None:  # Forbidden by SymbolComparison validation; fail closed in sorting.
            return (2, 1, comparison.path, comparison.qualified_name)
        return (
            0 if symbol.public else 1,
            0 if symbol.exported is True else 1,
            comparison.path,
            comparison.qualified_name,
        )

    def _ranked_references(
        self,
        index: RepositoryIndex,
        role: RevisionRole,
        query: str,
        *,
        callers_only: bool,
    ) -> list[tuple[int, InvestigationMatch]]:
        leaf = query.rsplit(".", maxsplit=1)[-1]
        kind = (
            InvestigationQueryKind.FIND_CALLERS
            if callers_only
            else InvestigationQueryKind.FIND_REFERENCES
        )
        ranked: list[tuple[int, InvestigationMatch]] = []
        seen_locations: set[tuple[str, int, int, int, str, str | None]] = set()
        for reference in index.references:
            if callers_only and reference.kind is not ReferenceKind.CALL:
                continue
            if reference.module_import_alias_expansion == query or (
                reference.module_import_alias_expansion
                and reference.module_import_alias_expansion.endswith(f".{query}")
            ):
                rank = 0
                match_type = (
                    InvestigationMatchType.STATIC_IMPORT_ALIAS_CALL
                    if callers_only
                    else InvestigationMatchType.STATIC_IMPORT_ALIAS_REFERENCE
                )
                reason = (
                    f"module-import alias syntax expands to {query}; "
                    "runtime rebinding is not proven"
                )
            elif reference.expression == query:
                rank = 1
                if callers_only:
                    match_type = (
                        InvestigationMatchType.POSSIBLE_ATTRIBUTE_CALL
                        if "." in reference.expression
                        else InvestigationMatchType.POSSIBLE_NAME_CALL
                    )
                else:
                    match_type = (
                        InvestigationMatchType.QUALIFIED_SYNTAX_REFERENCE
                        if "." in reference.expression
                        else InvestigationMatchType.NAME_SYNTAX_REFERENCE
                    )
                reason = (
                    f"exact syntax matches {query}; lexical and runtime identity are unresolved"
                )
            elif reference.name == leaf:
                rank = 2
                if callers_only:
                    match_type = (
                        InvestigationMatchType.POSSIBLE_ATTRIBUTE_CALL
                        if "." in reference.expression
                        else InvestigationMatchType.POSSIBLE_NAME_CALL
                    )
                    reason = (
                        f"syntactic call shares terminal name {leaf}; "
                        "receiver/binding is unresolved"
                    )
                else:
                    match_type = (
                        InvestigationMatchType.ATTRIBUTE_SYNTAX_REFERENCE
                        if "." in reference.expression
                        else InvestigationMatchType.NAME_SYNTAX_REFERENCE
                    )
                    reason = (
                        f"syntactic reference shares terminal name {leaf}; binding is unresolved"
                    )
            else:
                continue
            location_key = (
                reference.path,
                reference.start_line,
                reference.end_line,
                reference.column,
                reference.expression,
                reference.module_import_alias_expansion,
            )
            if location_key in seen_locations:
                continue
            seen_locations.add(location_key)
            ranked.append(
                (
                    rank,
                    InvestigationMatch(
                        provenance=InvestigationProvenance(
                            query_kind=kind,
                            revision=role,
                            path=reference.path,
                            start_line=reference.start_line,
                            end_line=reference.end_line,
                            symbol_name=reference.scope,
                            match_type=match_type,
                            reason=reason,
                        ),
                        reference=reference,
                    ),
                )
            )
        if not callers_only:
            for imported in index.imports:
                if not (
                    imported.imported_target == query
                    or imported.imported_target.endswith(f".{query}")
                    or ("." not in query and imported.alias == leaf)
                ):
                    continue
                ranked.append(
                    (
                        3,
                        InvestigationMatch(
                            provenance=InvestigationProvenance(
                                query_kind=kind,
                                revision=role,
                                path=imported.path,
                                start_line=imported.start_line,
                                end_line=imported.end_line,
                                symbol_name=imported.scope,
                                match_type=InvestigationMatchType.SYNTACTIC_IMPORT_BINDING,
                                reason=f"syntactic import binding targets {query}",
                            ),
                            import_record=imported,
                        ),
                    )
                )
        ranked.sort(
            key=lambda item: (
                item[0],
                item[1].provenance.path,
                item[1].provenance.start_line,
                item[1].provenance.match_type,
            )
        )
        return ranked

    def _symbol_source(
        self, index: RepositoryIndex, symbol: IndexedSymbol
    ) -> tuple[str, int, bool]:
        source = index._source(symbol.path) or ""
        end_line = min(
            symbol.end_line,
            symbol.start_line + self.budget.max_symbol_source_lines - 1,
        )
        content = "\n".join(source.splitlines()[symbol.start_line - 1 : end_line])
        content, chars_truncated = self._truncate_content(content)
        return content, end_line, end_line < symbol.end_line or chars_truncated

    def _truncate_content(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.budget.max_source_chars:
            return value, False
        return value[: self.budget.max_source_chars], True

    def _result(
        self,
        query_kind: InvestigationQueryKind,
        revision: RevisionRole,
        matches: list[InvestigationMatch],
        *,
        limit: int,
    ) -> InvestigationResult:
        index = self.base if revision is RevisionRole.BASE else self.head
        truncated = len(matches) > limit
        selected: list[InvestigationMatch] = []
        source_chars = 0
        for item in matches[:limit]:
            if item.content is not None:
                remaining = self.budget.max_total_source_chars - source_chars
                if remaining <= 0:
                    truncated = True
                    break
                if len(item.content) > remaining:
                    item = item.model_copy(
                        update={
                            "content": item.content[:remaining],
                            "provenance": item.provenance.model_copy(
                                update={"source_truncated": True}
                            ),
                        }
                    )
                    truncated = True
                source_chars += len(item.content or "")
            selected.append(item)
            if source_chars >= self.budget.max_total_source_chars:
                if len(selected) < len(matches):
                    truncated = True
                break
        return InvestigationResult(
            query_kind=query_kind,
            revision=revision,
            revision_sha=index.revision.sha,
            index_sha256=index.sha256,
            index_stats=index.stats,
            matches=tuple(selected),
            truncated=truncated or any(item.provenance.source_truncated for item in selected),
        )

    def _index(self, revision: RevisionRole | str) -> tuple[RepositoryIndex, RevisionRole]:
        try:
            role = RevisionRole(revision)
        except ValueError as error:
            raise InvestigationQueryError("unknown immutable revision role") from error
        return (self.base if role is RevisionRole.BASE else self.head), role

    def _maximum(self, value: int | None) -> int:
        if value is None:
            return self.budget.default_max_results
        if type(value) is not int or value <= 0 or value > self.budget.hard_max_results:
            raise InvestigationQueryError("requested result count exceeds its hard limit")
        return value

    def _symbol(self, value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > self.budget.max_symbol_query_chars
            or _QUALIFIED_IDENTIFIER.fullmatch(value) is None
        ):
            raise InvestigationQueryError("symbol query must be a bounded dotted identifier")
        return value

    def _optional_symbol(self, value: str | None) -> str | None:
        return self._symbol(value) if value is not None else None

    @staticmethod
    def _symbol_matches(symbol: IndexedSymbol, query: str) -> bool:
        return query in {symbol.qualified_name, f"{symbol.module}.{symbol.qualified_name}"}

    def _required_path(self, index: RepositoryIndex, value: str) -> str:
        try:
            path = validate_repository_source_path(value)
        except ValueError as error:
            raise InvestigationQueryError(str(error)) from error
        if not index.has_path(path):
            raise InvestigationQueryError("path is absent from the bounded immutable index")
        return path

    def _optional_path(self, index: RepositoryIndex, value: str | None) -> str | None:
        return self._required_path(index, value) if value is not None else None

    def _optional_pair_path(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            path = validate_repository_source_path(value)
        except ValueError as error:
            raise InvestigationQueryError(str(error)) from error
        if not self.base.has_path(path) and not self.head.has_path(path):
            raise InvestigationQueryError("path is absent from both immutable indexes")
        return path
