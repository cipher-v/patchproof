"""Deterministic starting context for grounded behavioral-claim discovery.

Why this exists
---------------

Phase 1 built `RepositoryIndex` and `RepositoryInvestigator` but nothing consumed
them: claim selection still received `DeterministicContextRetriever`'s diff-shaped
snippets. That context answers "what changed", which is not the question claim
selection has to answer. The question is "which observable behavior, reachable through
an interface that exists on *both* revisions, can distinguish BASE from HEAD".

The v1 abstentions were dominated by claim discovery, not by candidate generation:
every abstention in the frozen v1 result occurred *before* a candidate was ever
produced. This module is the deterministic half of the fix. It converts two immutable
indexes into a small, ranked, high-signal starting context so that the common case
needs no model tool calls at all.

Design commitments
------------------

**Identity is `path` + `qualified_name`, never a leaf name.** The pre-Phase-2 claim
path compared bare leaf names, so an unrelated ``Config.render`` and ``Report.render``
were indistinguishable. `ObservableIdentity` is the only identity this module accepts.

**A changed implementation is not automatically the right interface.** The ranking
prefers exported, then public, then internal, then private observables. A private
helper is a last resort and is marked as such.

**A HEAD-only symbol can never be the differential interface** -- BASE cannot resolve
it, so the test can only fail to import. But a HEAD-only helper is still a strong
*signal*: this module follows its HEAD callers back to shared observables and promotes
those, which is how "the PR added a helper" becomes a testable claim about the
behavior the helper was introduced to change.

**Uncertainty is preserved, never flattened.** Partial indexes propagate as explicit
flags. A symbol absent from a truncated index is reported as unknown, never as absent.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchproof.investigation_tools import (
    InvestigationQueryError,
    ObservablePresence,
    RepositoryInvestigator,
    SymbolComparison,
)
from patchproof.models import RevisionRole
from patchproof.repository_index import (
    IndexedSymbol,
    ObservableSymbolKind,
    RepositoryIndexStats,
    validate_repository_source_path,
)

#: Separator for the textual form of an observable identity. Chosen because it cannot
#: occur in a POSIX path or a Python qualified name, so the encoding is unambiguous and
#: round-trips exactly.
IDENTITY_SEPARATOR = "::"

#: Kinds a generated pytest candidate can plausibly exercise. MODULE_VALUE is included
#: deliberately: a module-level singleton legitimately anchors behaviors such as copy,
#: pickle, and identity semantics, which is exactly the Jinja-class `missing` sentinel
#: shape. Excluding non-callables would silently make that class of claim impossible.
OBSERVABLE_KINDS: frozenset[ObservableSymbolKind] = frozenset(ObservableSymbolKind)


class ObservableRank(StrEnum):
    """Preference ordering for which shared observable should anchor a claim.

    Lower `priority` sorts first. The ordering encodes the rule that a claim should be
    expressed through the most externally visible interface that is grounded, because
    only such a claim describes behavior a user could observe.
    """

    EXPORTED_SHARED = "EXPORTED_SHARED"
    PUBLIC_SHARED = "PUBLIC_SHARED"
    INTERNAL_SHARED = "INTERNAL_SHARED"
    PRIVATE_SHARED = "PRIVATE_SHARED"

    @property
    def priority(self) -> int:
        return _RANK_PRIORITY[self]


_RANK_PRIORITY: dict[ObservableRank, int] = {
    ObservableRank.EXPORTED_SHARED: 0,
    ObservableRank.PUBLIC_SHARED: 1,
    ObservableRank.INTERNAL_SHARED: 2,
    ObservableRank.PRIVATE_SHARED: 3,
}


class ObservableSelectionReason(StrEnum):
    """Why a shared observable entered the starting context."""

    #: The observable's own implementation differs between BASE and HEAD.
    IMPLEMENTATION_CHANGED = "IMPLEMENTATION_CHANGED"
    #: The observable calls or references a symbol introduced by this pull request.
    REACHES_NEW_HEAD_SYMBOL = "REACHES_NEW_HEAD_SYMBOL"
    #: The observable references a symbol this pull request removed.
    REACHES_REMOVED_BASE_SYMBOL = "REACHES_REMOVED_BASE_SYMBOL"
    #: The observable was not in the deterministic pre-fetch but was surfaced by a
    #: tool call and then mechanically re-verified as present on both revisions.
    DISCOVERED_DURING_INVESTIGATION = "DISCOVERED_DURING_INVESTIGATION"
    #: The observable's own definition references another symbol whose implementation
    #: changed. This is what lets a public caller be preferred over the changed private
    #: implementation it delegates to, and it is also how a module-level singleton is
    #: recognized as affected when its *class* changed but its assignment did not.
    REACHES_CHANGED_SYMBOL = "REACHES_CHANGED_SYMBOL"


class ObservableIdentity(BaseModel):
    """The only symbol identity this module accepts: a path and a qualified name.

    Leaf-name identity is deliberately unavailable here. Two unrelated symbols sharing
    a leaf are different observables, and treating them as one produced ungrounded
    claims in the pre-Phase-2 path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    qualified_name: str = Field(min_length=1, max_length=512)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repository_source_path(value)

    @classmethod
    def from_symbol(cls, symbol: IndexedSymbol) -> ObservableIdentity:
        return cls(path=symbol.path, qualified_name=symbol.qualified_name)

    @classmethod
    def parse(cls, value: str) -> ObservableIdentity:
        """Parse the textual `path::qualified_name` form, rejecting anything else."""
        if not isinstance(value, str) or value.count(IDENTITY_SEPARATOR) != 1:
            raise ValueError(
                f"observable identity must be exactly 'path{IDENTITY_SEPARATOR}qualified_name'"
            )
        path, qualified_name = value.split(IDENTITY_SEPARATOR)
        return cls(path=path, qualified_name=qualified_name)

    @property
    def text(self) -> str:
        return f"{self.path}{IDENTITY_SEPARATOR}{self.qualified_name}"


class RankedObservable(BaseModel):
    """One shared observable offered to claim selection, with its evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: ObservableIdentity
    kind: ObservableSymbolKind
    rank: ObservableRank
    public: bool
    exported: bool | None
    implementation_changed: bool
    reasons: tuple[ObservableSelectionReason, ...] = Field(min_length=1, max_length=4)
    #: HEAD-only symbols this observable statically reaches. Present so the model can
    #: see *why* a shared observable is interesting without being tempted to test the
    #: new symbol directly.
    reaches_new_head_symbols: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    base_start_line: int = Field(gt=0)
    base_end_line: int = Field(gt=0)
    head_start_line: int = Field(gt=0)
    head_end_line: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> RankedObservable:
        if self.base_end_line < self.base_start_line or self.head_end_line < self.head_start_line:
            raise ValueError("observable line ranges must be ordered")
        return self

    @property
    def sort_key(self) -> tuple[int, int, str, str]:
        """Deterministic ordering: rank, then changed-first, then stable identity."""
        return (
            self.rank.priority,
            0 if self.implementation_changed else 1,
            self.identity.path,
            self.identity.qualified_name,
        )


class NewHeadSymbol(BaseModel):
    """A symbol introduced by the pull request, offered as signal and never as interface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: ObservableIdentity
    kind: ObservableSymbolKind
    public: bool
    #: Shared observables whose HEAD bodies statically reference this symbol.
    reached_by: tuple[str, ...] = Field(default_factory=tuple, max_length=8)


class IndexCoverage(BaseModel):
    """Explicit partial-index semantics carried into every downstream decision.

    A truncated index cannot prove absence. Any consumer that treats "not in the index"
    as "not in the repository" is wrong whenever `complete` is False, and the claim
    grounding validator refuses to reject on absence alone in that case.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    head_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_truncated: bool
    head_truncated: bool
    base_syntax_errors: int = Field(ge=0)
    head_syntax_errors: int = Field(ge=0)
    observables_truncated: bool

    @property
    def complete(self) -> bool:
        """Whether absence from the index may be treated as absence from the revision."""
        return not (self.base_truncated or self.head_truncated or self.observables_truncated)

    @classmethod
    def from_stats(
        cls,
        *,
        base_index_sha256: str,
        head_index_sha256: str,
        base: RepositoryIndexStats,
        head: RepositoryIndexStats,
        observables_truncated: bool,
    ) -> IndexCoverage:
        return cls(
            base_index_sha256=base_index_sha256,
            head_index_sha256=head_index_sha256,
            base_truncated=base.truncated,
            head_truncated=head.truncated,
            base_syntax_errors=base.syntax_errors,
            head_syntax_errors=head.syntax_errors,
            observables_truncated=observables_truncated,
        )


class InvestigationStartingContext(BaseModel):
    """Deterministic, high-signal context computed before any model call.

    This is intended to be sufficient on its own for the common case. Tool calls exist
    for the cases it cannot anticipate, not as the primary path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_sha: str
    head_sha: str
    coverage: IndexCoverage
    shared_observables: tuple[RankedObservable, ...] = Field(max_length=40)
    new_head_symbols: tuple[NewHeadSymbol, ...] = Field(default_factory=tuple, max_length=20)
    removed_base_symbols: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    #: `path::qualified_name` of tests that statically reference a candidate observable.
    related_tests: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    #: Leaf names of every symbol this pull request changed or introduced. Carried so the
    #: relevance rule can be re-applied to an interface discovered later during
    #: investigation, without recomputing the whole cross-revision comparison.
    changed_symbol_names: tuple[str, ...] = Field(default_factory=tuple, max_length=80)
    notes: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @property
    def identities(self) -> frozenset[str]:
        """Every shared observable identity, in textual form, for grounding checks."""
        return frozenset(item.identity.text for item in self.shared_observables)

    def observable(self, identity: ObservableIdentity) -> RankedObservable | None:
        for item in self.shared_observables:
            if item.identity == identity:
                return item
        return None


@dataclass(frozen=True, slots=True)
class StartingContextBudget:
    """Hard limits on the deterministic pre-fetch, independent of index budgets."""

    max_shared_observables: int = 12
    max_new_head_symbols: int = 8
    max_removed_symbols: int = 8
    max_related_tests: int = 8
    #: How many HEAD-only symbols to trace back to their callers. Each trace is one
    #: bounded investigator query, so this directly bounds pre-fetch cost.
    max_caller_traces: int = 6
    max_callers_per_symbol: int = 10

    def __post_init__(self) -> None:
        for item in fields(self):
            if getattr(self, item.name) <= 0:
                raise ValueError(f"starting-context budget {item.name} must be positive")
        if self.max_shared_observables > 40 or self.max_new_head_symbols > 20:
            raise ValueError("starting-context budget exceeds the prompt-safe schema limits")


def rank_observable(
    symbol: IndexedSymbol, *, parent: IndexedSymbol | None = None
) -> ObservableRank:
    """Classify one shared symbol by how externally visible it is.

    `exported` is None when the defining module declares no `__all__`, which is not
    evidence against the symbol -- most modules have no `__all__`. It is therefore
    treated as ordinary public visibility, while an explicit exclusion from a declared
    `__all__` ranks lower.

    Membership of `__all__` is a *module-level* notion, so a method or property can never
    appear in it. Ranking every public method of an exported class as INTERNAL would
    therefore mislabel the most useful interfaces in the repository. When a public member
    belongs to an exported or public parent, it inherits ordinary public visibility: it
    is reachable by any caller who can reach the parent.
    """
    if not symbol.public:
        return ObservableRank.PRIVATE_SHARED
    if symbol.exported is True:
        return ObservableRank.EXPORTED_SHARED
    if symbol.exported is False:
        if parent is not None and parent.public and parent.exported is not False:
            return ObservableRank.PUBLIC_SHARED
        return ObservableRank.INTERNAL_SHARED
    return ObservableRank.PUBLIC_SHARED


class DeterministicInvestigationPlanner:
    """Build the starting context from two immutable indexes without a model."""

    def __init__(
        self,
        *,
        investigator: RepositoryInvestigator,
        budget: StartingContextBudget | None = None,
    ) -> None:
        self.investigator = investigator
        self.budget = budget or StartingContextBudget()

    def build(self) -> InvestigationStartingContext:
        """Compute the ranked shared-observable context for one revision pair."""
        observables = self.investigator.compare_observables()
        coverage = IndexCoverage.from_stats(
            base_index_sha256=observables.base_index_sha256,
            head_index_sha256=observables.head_index_sha256,
            base=observables.base_index_stats,
            head=observables.head_index_stats,
            observables_truncated=observables.truncated,
        )

        new_head = tuple(
            symbol for symbol in observables.new_on_head if symbol.kind in OBSERVABLE_KINDS
        )[: self.budget.max_new_head_symbols]

        # Trace HEAD-only symbols back to the shared observables that reach them. This
        # is what lets "the pull request added a helper" become a claim about the
        # behavior the helper changed, rather than a claim that the helper exists.
        reached_by: dict[str, set[str]] = {}
        reaches: dict[str, set[str]] = {}
        for symbol in new_head[: self.budget.max_caller_traces]:
            for caller in self._callers_of(symbol):
                reached_by.setdefault(symbol.qualified_name, set()).add(caller.text)
                reaches.setdefault(caller.text, set()).add(symbol.qualified_name)

        removed = tuple(
            ObservableIdentity.from_symbol(symbol).text
            for symbol in observables.removed_from_head
            if symbol.kind in OBSERVABLE_KINDS
        )[: self.budget.max_removed_symbols]

        changed_names = {
            comparison.qualified_name.rsplit(".", maxsplit=1)[-1]
            for comparison in observables.present_on_both
            if comparison.presence is ObservablePresence.PRESENT_ON_BOTH
            and comparison.implementation_changed
        }
        new_names = {symbol.qualified_name.rsplit(".", maxsplit=1)[-1] for symbol in new_head}
        ranked = self._rank_shared(observables.present_on_both, reaches, changed_names | new_names)
        selected = ranked[: self.budget.max_shared_observables]

        notes: list[str] = []
        if not coverage.complete:
            notes.append(
                "One or both repository indexes are partial. Absence of a symbol from this "
                "context is NOT evidence that the symbol is absent from the revision."
            )
        if not selected:
            notes.append(
                "No shared observable was found whose implementation changed or which "
                "reaches a symbol introduced by this pull request."
            )
        if new_head and not any(item.reaches_new_head_symbols for item in selected):
            notes.append(
                "This pull request introduces HEAD-only symbols with no statically "
                "identified shared caller. A HEAD-only symbol cannot be the differential "
                "interface; investigate for a shared observable that changed behavior."
            )

        return InvestigationStartingContext(
            base_sha=self.investigator.base.revision.sha,
            head_sha=self.investigator.head.revision.sha,
            coverage=coverage,
            shared_observables=selected,
            new_head_symbols=tuple(
                NewHeadSymbol(
                    identity=ObservableIdentity.from_symbol(symbol),
                    kind=symbol.kind,
                    public=symbol.public,
                    reached_by=tuple(sorted(reached_by.get(symbol.qualified_name, set()))[:8]),
                )
                for symbol in new_head
            ),
            removed_base_symbols=removed,
            related_tests=self._related_tests(selected),
            changed_symbol_names=tuple(sorted(changed_names | new_names))[:80],
            notes=tuple(notes),
        )

    def _rank_shared(
        self,
        comparisons: tuple[SymbolComparison, ...],
        reaches: dict[str, set[str]],
        interesting_names: set[str],
    ) -> tuple[RankedObservable, ...]:
        """Select and order shared observables that this pull request plausibly affects.

        An observable qualifies when its own implementation changed, when it reaches a
        HEAD-only symbol, or when its definition references some other symbol the pull
        request changed. The third rule is what makes a *public caller* eligible even
        though the edit landed in a private callee -- which is the whole point of
        preferring an externally visible interface -- and it is also how a module-level
        singleton is recognized as affected when the change landed on its class.
        """
        ranked: list[RankedObservable] = []
        head_symbols = {
            (symbol.path, symbol.qualified_name): symbol
            for symbol in self.investigator.head.symbols
        }
        for comparison in comparisons:
            if comparison.presence is not ObservablePresence.PRESENT_ON_BOTH:
                continue
            base_symbol, head_symbol = comparison.base, comparison.head
            if base_symbol is None or head_symbol is None:
                continue
            if head_symbol.kind not in OBSERVABLE_KINDS:
                continue
            identity = ObservableIdentity.from_symbol(head_symbol)
            reached = tuple(sorted(reaches.get(identity.text, set()))[:8])
            reasons: list[ObservableSelectionReason] = []
            if comparison.implementation_changed:
                reasons.append(ObservableSelectionReason.IMPLEMENTATION_CHANGED)
            if reached:
                reasons.append(ObservableSelectionReason.REACHES_NEW_HEAD_SYMBOL)
            own_leaf = head_symbol.qualified_name.rsplit(".", maxsplit=1)[-1]
            if self.references_any(head_symbol, interesting_names - {own_leaf}):
                reasons.append(ObservableSelectionReason.REACHES_CHANGED_SYMBOL)
            if not reasons:
                # Unchanged and unrelated to the diff: not a candidate anchor.
                continue
            ranked.append(
                RankedObservable(
                    identity=identity,
                    kind=head_symbol.kind,
                    rank=rank_observable(
                        head_symbol,
                        parent=(
                            head_symbols.get((head_symbol.path, head_symbol.parent_symbol))
                            if head_symbol.parent_symbol
                            else None
                        ),
                    ),
                    public=head_symbol.public,
                    exported=head_symbol.exported,
                    implementation_changed=bool(comparison.implementation_changed),
                    reasons=tuple(reasons),
                    reaches_new_head_symbols=reached,
                    base_start_line=base_symbol.start_line,
                    base_end_line=base_symbol.end_line,
                    head_start_line=head_symbol.start_line,
                    head_end_line=head_symbol.end_line,
                )
            )
        ranked.sort(key=lambda item: item.sort_key)
        return tuple(ranked)

    def references_any(self, symbol: IndexedSymbol, names: set[str]) -> bool:
        """Whether the symbol's own HEAD definition syntactically references any name.

        Line-range containment is used rather than scope attribution, because a
        module-level assignment such as ``missing = _Missing()`` has module scope while
        still being the definition of the observable.
        """
        if not names:
            return False
        for reference in self.investigator.head.references:
            if reference.path != symbol.path:
                continue
            if not symbol.start_line <= reference.start_line <= symbol.end_line:
                continue
            if reference.name in names:
                return True
        return False

    def _callers_of(self, symbol: IndexedSymbol) -> tuple[ObservableIdentity, ...]:
        """Map HEAD callers of one symbol back to enclosing shared observable identities."""
        try:
            result = self.investigator.find_callers(
                revision=RevisionRole.HEAD,
                symbol=symbol.qualified_name,
                maximum=self.budget.max_callers_per_symbol,
            )
        except InvestigationQueryError:
            return ()
        identities: list[ObservableIdentity] = []
        for match in result.matches:
            reference = match.reference
            if reference is None or reference.scope == "<module>":
                continue
            if reference.path == symbol.path and reference.scope == symbol.qualified_name:
                continue
            identity = ObservableIdentity(path=reference.path, qualified_name=reference.scope)
            if identity not in identities:
                identities.append(identity)
        return tuple(identities)

    def _related_tests(self, observables: tuple[RankedObservable, ...]) -> tuple[str, ...]:
        """Collect tests that statically reference the highest-ranked observables."""
        found: list[str] = []
        for observable in observables[: self.budget.max_caller_traces]:
            if len(found) >= self.budget.max_related_tests:
                break
            try:
                result = self.investigator.find_related_tests(
                    revision=RevisionRole.HEAD,
                    symbol=observable.identity.qualified_name,
                    maximum=self.budget.max_related_tests,
                )
            except InvestigationQueryError:
                continue
            for match in result.matches:
                symbol = match.symbol
                if symbol is None:
                    continue
                text = ObservableIdentity.from_symbol(symbol).text
                if text not in found:
                    found.append(text)
                if len(found) >= self.budget.max_related_tests:
                    break
        return tuple(found)
