"""Deterministic starting context: ranking, identity, reachability, partial indexes.

These tests use synthetic repositories built through `RepositoryIndex.from_files`, so
they exercise the real Phase 1 index and investigator without touching Git, the network,
or any provider. They deliberately encode *classes* of pull request rather than any
specific historical case.
"""

from __future__ import annotations

import pytest

from patchproof.claim_investigation import (
    DeterministicInvestigationPlanner,
    IndexCoverage,
    ObservableIdentity,
    ObservableRank,
    ObservableSelectionReason,
    StartingContextBudget,
    rank_observable,
)
from patchproof.investigation_tools import RepositoryInvestigator
from patchproof.models import Revision, RevisionRole
from patchproof.repository_index import (
    ObservableSymbolKind,
    RepositoryIndex,
    RepositoryIndexBudget,
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def build_planner(
    base_files: dict[str, str],
    head_files: dict[str, str],
    *,
    budget: StartingContextBudget | None = None,
    index_budget: RepositoryIndexBudget | None = None,
) -> DeterministicInvestigationPlanner:
    base = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.BASE, sha=BASE_SHA),
        files=base_files,
        budget=index_budget,
    )
    head = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.HEAD, sha=HEAD_SHA),
        files=head_files,
        budget=index_budget,
    )
    return DeterministicInvestigationPlanner(
        investigator=RepositoryInvestigator(base=base, head=head), budget=budget
    )


# ---------------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------------

_EXPORTED_BASE = """__all__ = ["render"]


def render(value):
    return str(value)


def helper(value):
    return str(value)


def _internal(value):
    return str(value)
"""

_EXPORTED_HEAD = """__all__ = ["render"]


def render(value):
    return str(value).strip()


def helper(value):
    return str(value).strip()


def _internal(value):
    return str(value).strip()
"""


def test_exported_public_internal_and_private_are_ranked_in_that_order() -> None:
    planner = build_planner({"pkg/mod.py": _EXPORTED_BASE}, {"pkg/mod.py": _EXPORTED_HEAD})

    context = planner.build()
    ranks = {item.identity.qualified_name: item.rank for item in context.shared_observables}

    assert ranks["render"] is ObservableRank.EXPORTED_SHARED
    assert ranks["helper"] is ObservableRank.INTERNAL_SHARED
    assert ranks["_internal"] is ObservableRank.PRIVATE_SHARED
    # Ordering, not just labelling: the exported observable is offered first.
    assert context.shared_observables[0].identity.qualified_name == "render"


def test_public_symbol_without_dunder_all_ranks_above_private() -> None:
    base = "def render(v):\n    return v\n\n\ndef _hidden(v):\n    return v\n"
    head = "def render(v):\n    return v + 1\n\n\ndef _hidden(v):\n    return v + 1\n"
    planner = build_planner({"pkg/mod.py": base}, {"pkg/mod.py": head})

    context = planner.build()
    ranks = {item.identity.qualified_name: item.rank for item in context.shared_observables}

    assert ranks["render"] is ObservableRank.PUBLIC_SHARED
    assert ranks["_hidden"] is ObservableRank.PRIVATE_SHARED
    assert context.shared_observables[0].identity.qualified_name == "render"


@pytest.mark.parametrize(
    ("public", "exported", "expected"),
    [
        (True, True, ObservableRank.EXPORTED_SHARED),
        (True, None, ObservableRank.PUBLIC_SHARED),
        (True, False, ObservableRank.INTERNAL_SHARED),
        (False, None, ObservableRank.PRIVATE_SHARED),
        (False, True, ObservableRank.PRIVATE_SHARED),
    ],
)
def test_rank_observable_is_a_total_function_of_visibility(
    public: bool, exported: bool | None, expected: ObservableRank
) -> None:
    class Symbol:
        pass

    symbol = Symbol()
    symbol.public = public  # type: ignore[attr-defined]
    symbol.exported = exported  # type: ignore[attr-defined]
    assert rank_observable(symbol) is expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------


def test_identity_round_trips_and_rejects_malformed_text() -> None:
    identity = ObservableIdentity.parse("pkg/mod.py::Renderer.render")
    assert identity.path == "pkg/mod.py"
    assert identity.qualified_name == "Renderer.render"
    assert ObservableIdentity.parse(identity.text) == identity

    for malformed in ("render", "pkg/mod.py", "a::b::c", "", "::render", "pkg/mod.py::"):
        with pytest.raises(ValueError):
            ObservableIdentity.parse(malformed)


def test_same_leaf_in_different_files_produces_distinct_identities() -> None:
    """`Config.render` and `Report.render` must never collapse into one interface."""
    base = {
        "pkg/a.py": "class Config:\n    def render(self):\n        return 1\n",
        "pkg/b.py": "class Report:\n    def render(self):\n        return 1\n",
    }
    head = {
        "pkg/a.py": "class Config:\n    def render(self):\n        return 2\n",
        "pkg/b.py": "class Report:\n    def render(self):\n        return 1\n",
    }
    context = build_planner(base, head).build()

    identities = {item.identity.text for item in context.shared_observables}
    assert "pkg/a.py::Config.render" in identities
    # Only the file that actually changed is offered as an anchor.
    assert "pkg/b.py::Report.render" not in identities


# ---------------------------------------------------------------------------------
# MODULE_VALUE
# ---------------------------------------------------------------------------------

_SENTINEL_BASE = """__all__ = ["missing"]


class _Missing:
    def __repr__(self):
        return "missing"


missing = _Missing()
"""

_SENTINEL_HEAD = """__all__ = ["missing"]


class _Missing:
    def __repr__(self):
        return "missing"

    def __reduce__(self):
        return (_get_missing, ())


def _get_missing():
    return missing


missing = _Missing()
"""


def test_module_level_singleton_is_offered_as_an_observable() -> None:
    """A non-callable module value must be able to anchor copy/pickle-identity claims."""
    context = build_planner(
        {"pkg/utils.py": _SENTINEL_BASE}, {"pkg/utils.py": _SENTINEL_HEAD}
    ).build()

    kinds = {item.identity.qualified_name: item.kind for item in context.shared_observables}
    assert kinds.get("missing") is ObservableSymbolKind.MODULE_VALUE
    exported = [
        item for item in context.shared_observables if item.rank is ObservableRank.EXPORTED_SHARED
    ]
    assert any(item.identity.qualified_name == "missing" for item in exported)


# ---------------------------------------------------------------------------------
# HEAD-only helper reachability
# ---------------------------------------------------------------------------------

_HELPER_BASE = """__all__ = ["Console"]


class Console:
    def render(self, text, style):
        return style + text
"""

_HELPER_HEAD = """__all__ = ["Console"]


class Console:
    def render(self, text, style):
        return terminate_lines(style + text)


def terminate_lines(value):
    return value.replace("\\n", "|\\n")
"""


def test_head_only_helper_is_signal_and_its_shared_caller_is_promoted() -> None:
    context = build_planner(
        {"pkg/console.py": _HELPER_BASE}, {"pkg/console.py": _HELPER_HEAD}
    ).build()

    new_names = {item.identity.qualified_name for item in context.new_head_symbols}
    assert "terminate_lines" in new_names

    shared = {item.identity.qualified_name for item in context.shared_observables}
    assert "Console.render" in shared
    # The new symbol is never itself offered as a shared observable.
    assert "terminate_lines" not in shared

    render = next(
        item
        for item in context.shared_observables
        if item.identity.qualified_name == "Console.render"
    )
    assert "terminate_lines" in render.reaches_new_head_symbols
    assert ObservableSelectionReason.REACHES_NEW_HEAD_SYMBOL in render.reasons


def test_new_head_symbol_records_which_shared_observable_reaches_it() -> None:
    context = build_planner(
        {"pkg/console.py": _HELPER_BASE}, {"pkg/console.py": _HELPER_HEAD}
    ).build()

    helper = next(
        item
        for item in context.new_head_symbols
        if item.identity.qualified_name == "terminate_lines"
    )
    assert "pkg/console.py::Console.render" in helper.reached_by


# ---------------------------------------------------------------------------------
# Selection scope
# ---------------------------------------------------------------------------------


def test_unchanged_unrelated_observables_are_not_offered() -> None:
    base = {
        "pkg/mod.py": "def touched(v):\n    return v\n\n\ndef untouched(v):\n    return v\n",
    }
    head = {
        "pkg/mod.py": "def touched(v):\n    return v + 1\n\n\ndef untouched(v):\n    return v\n",
    }
    context = build_planner(base, head).build()

    names = {item.identity.qualified_name for item in context.shared_observables}
    assert names == {"touched"}


def test_removed_symbols_are_reported_without_being_offered_as_interfaces() -> None:
    base = {"pkg/mod.py": "def kept(v):\n    return v\n\n\ndef dropped(v):\n    return v\n"}
    head = {"pkg/mod.py": "def kept(v):\n    return v + 1\n"}
    context = build_planner(base, head).build()

    assert "pkg/mod.py::dropped" in context.removed_base_symbols
    assert "dropped" not in {item.identity.qualified_name for item in context.shared_observables}


def test_context_with_no_candidate_observable_says_so_explicitly() -> None:
    base = {"pkg/mod.py": "def kept(v):\n    return v\n"}
    head = {"pkg/mod.py": "def kept(v):\n    return v\n", "pkg/other.py": "X = 1\n"}
    context = build_planner(base, head).build()

    assert context.shared_observables == ()
    assert any("No shared observable" in note for note in context.notes)


# ---------------------------------------------------------------------------------
# Related tests
# ---------------------------------------------------------------------------------


def test_related_tests_are_discovered_for_ranked_observables() -> None:
    test_source = "from pkg.mod import render\n\n\ndef test_render():\n    assert render(1) == 1\n"
    base = {"pkg/mod.py": "def render(v):\n    return v\n", "tests/test_mod.py": test_source}
    head = {
        "pkg/mod.py": "def render(v):\n    return v + 1\n",
        "tests/test_mod.py": test_source,
    }
    context = build_planner(base, head).build()

    assert any("test_render" in item for item in context.related_tests)


# ---------------------------------------------------------------------------------
# Partial index semantics
# ---------------------------------------------------------------------------------


def test_partial_index_propagates_and_forbids_treating_absence_as_proof() -> None:
    files_base = {f"pkg/mod{index}.py": f"def f{index}(v):\n    return v\n" for index in range(6)}
    files_head = {
        f"pkg/mod{index}.py": f"def f{index}(v):\n    return v + 1\n" for index in range(6)
    }
    planner = build_planner(files_base, files_head, index_budget=RepositoryIndexBudget(max_files=2))

    context = planner.build()

    assert not context.coverage.complete
    assert context.coverage.base_truncated or context.coverage.head_truncated
    assert any("NOT evidence" in note for note in context.notes)


def test_complete_index_reports_complete_coverage() -> None:
    context = build_planner({"pkg/mod.py": _EXPORTED_BASE}, {"pkg/mod.py": _EXPORTED_HEAD}).build()
    assert context.coverage.complete
    assert not any("NOT evidence" in note for note in context.notes)


def test_index_coverage_completeness_requires_every_signal() -> None:
    def coverage(**overrides: object) -> IndexCoverage:
        payload: dict[str, object] = {
            "base_index_sha256": "0" * 64,
            "head_index_sha256": "1" * 64,
            "base_truncated": False,
            "head_truncated": False,
            "base_syntax_errors": 0,
            "head_syntax_errors": 0,
            "observables_truncated": False,
        }
        payload.update(overrides)
        return IndexCoverage(**payload)  # type: ignore[arg-type]

    assert coverage().complete
    assert not coverage(base_truncated=True).complete
    assert not coverage(head_truncated=True).complete
    assert not coverage(observables_truncated=True).complete


# ---------------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------------


def test_starting_context_respects_its_observable_budget() -> None:
    base = {"pkg/mod.py": "".join(f"def f{i}(v):\n    return v\n\n\n" for i in range(10))}
    head = {"pkg/mod.py": "".join(f"def f{i}(v):\n    return v + 1\n\n\n" for i in range(10))}
    planner = build_planner(base, head, budget=StartingContextBudget(max_shared_observables=3))

    context = planner.build()

    assert len(context.shared_observables) == 3


def test_starting_context_retains_a_changed_observable_after_an_irrelevant_prefix() -> None:
    unrelated = "".join(f"def a_{index:02d}(value):\n    return value\n\n" for index in range(25))
    base = {"pkg/mod.py": unrelated + "def z_target():\n    return 'base'\n"}
    head = {"pkg/mod.py": unrelated + "def z_target():\n    return 'head'\n"}

    context = build_planner(base, head).build()

    assert context.coverage.observables_truncated is True
    assert [item.identity.qualified_name for item in context.shared_observables] == ["z_target"]
    assert context.shared_observables[0].reasons == (
        ObservableSelectionReason.IMPLEMENTATION_CHANGED,
    )


def test_starting_context_retains_a_shared_caller_when_its_changed_callee_is_truncated() -> None:
    base_helpers = "".join(
        f"def _changed_{index:02d}():\n    return {index}\n\n" for index in range(25)
    )
    head_helpers = "".join(
        f"def _changed_{index:02d}():\n    return {index + 100}\n\n" for index in range(25)
    )
    caller = "def public_api():\n    return _changed_24()\n"

    context = build_planner(
        {"pkg/mod.py": base_helpers + caller},
        {"pkg/mod.py": head_helpers + caller},
    ).build()
    selected = {item.identity.qualified_name: item for item in context.shared_observables}

    assert "_changed_24" not in selected
    assert ObservableSelectionReason.REACHES_CHANGED_SYMBOL in selected["public_api"].reasons
    assert context.coverage.observables_truncated is True


def test_invalid_starting_context_budgets_are_rejected() -> None:
    with pytest.raises(ValueError):
        StartingContextBudget(max_shared_observables=0)
    with pytest.raises(ValueError):
        StartingContextBudget(max_shared_observables=100)
