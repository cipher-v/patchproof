"""Bounded query and cross-revision tests for the repository investigator."""

from __future__ import annotations

import json

import pytest

from patchproof.investigation_tools import (
    InvestigationBudget,
    InvestigationMatchType,
    InvestigationQueryError,
    ObservablePresence,
    RepositoryInvestigator,
)
from patchproof.models import Revision, RevisionRole
from patchproof.repository_index import ObservableSymbolKind, RepositoryIndex

_BASE_SHA = "a" * 40
_HEAD_SHA = "b" * 40

_BASE_API = """missing = object()
removed_value = 1


class Service:
    @property
    def status(self):
        return "old"

    def run(self, value):
        return helper(value)


def helper(value):
    return value + 1


def removed_function():
    return "old"
"""

_HEAD_API = """missing = object()
new_value = 2


class Service:
    @property
    def status(self):
        return "new"

    def run(self, value):
        return helper(value) * 2


def helper(value):
    return value + 1


def new_function():
    return "new"
"""

_CONSUMER = """from pkg.api import Service as PublicService, helper


def consume(value):
    service = PublicService()
    return service.run(helper(value))
"""

_TESTS = """from pkg.api import Service


def test_service_run():
    assert Service().run(2) > 0


def test_textual_note():
    note = "new_function behavior"
    assert note
"""


@pytest.fixture
def investigator() -> RepositoryInvestigator:
    common = {"pkg/consumer.py": _CONSUMER, "tests/test_api.py": _TESTS}
    base = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.BASE, _BASE_SHA),
        files={"pkg/api.py": _BASE_API, **common},
    )
    head = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.HEAD, _HEAD_SHA),
        files={"pkg/api.py": _HEAD_API, **common},
    )
    return RepositoryInvestigator(base=base, head=head)


def test_inspect_symbol_returns_exact_bounded_source_and_provenance(
    investigator: RepositoryInvestigator,
) -> None:
    result = investigator.inspect_symbol(
        revision=RevisionRole.HEAD,
        path="pkg/api.py",
        qualified_name="Service.run",
    )

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.symbol is not None
    assert match.symbol.kind is ObservableSymbolKind.METHOD
    assert "return helper(value) * 2" in (match.content or "")
    assert match.provenance.path == "pkg/api.py"
    assert match.provenance.symbol_name == "Service.run"
    assert match.provenance.match_type is InvestigationMatchType.EXACT_PATH_AND_SYMBOL
    assert result.revision_sha == _HEAD_SHA
    assert result.index_sha256 == investigator.head.sha256
    assert result.index_stats.truncated is False


def test_compare_symbol_marks_changed_body_with_stable_identity(
    investigator: RepositoryInvestigator,
) -> None:
    result = investigator.compare_symbol(path="pkg/api.py", qualified_name="Service.run")

    comparison = result.comparisons[0]
    assert comparison.presence is ObservablePresence.PRESENT_ON_BOTH
    assert comparison.base is not None and comparison.head is not None
    assert comparison.implementation_changed is True


def test_cross_revision_observables_include_module_values_and_absences(
    investigator: RepositoryInvestigator,
) -> None:
    result = investigator.compare_observables(maximum=50)
    shared = {(item.path, item.qualified_name): item for item in result.present_on_both}

    assert shared[("pkg/api.py", "missing")].implementation_changed is False
    assert shared[("pkg/api.py", "missing")].head is not None
    assert shared[("pkg/api.py", "missing")].head.kind is ObservableSymbolKind.MODULE_VALUE
    assert {item.qualified_name for item in result.new_on_head} >= {"new_value", "new_function"}
    assert {item.qualified_name for item in result.removed_from_head} >= {
        "removed_value",
        "removed_function",
    }


def test_find_references_and_callers_use_static_import_resolution(
    investigator: RepositoryInvestigator,
) -> None:
    references = investigator.find_references(
        revision="HEAD",
        symbol="pkg.api.Service",
        maximum=20,
    )
    callers = investigator.find_callers(
        revision=RevisionRole.HEAD,
        symbol="pkg.api.helper",
        maximum=20,
    )

    assert any(
        item.reference is not None
        and item.reference.expression == "PublicService"
        and item.reference.module_import_alias_expansion == "pkg.api.Service"
        for item in references.matches
    )
    assert any(
        item.import_record is not None and item.import_record.alias == "PublicService"
        for item in references.matches
    )
    assert any(
        item.reference is not None
        and item.reference.kind == "CALL"
        and item.provenance.symbol_name == "consume"
        for item in callers.matches
    )
    reference_locations = [
        (
            item.reference.path,
            item.reference.start_line,
            item.reference.column,
            item.reference.expression,
        )
        for item in references.matches
        if item.reference is not None
    ]
    assert len(reference_locations) == len(set(reference_locations))


def test_find_related_tests_ranks_exact_reference_before_textual_fallback(
    investigator: RepositoryInvestigator,
) -> None:
    exact = investigator.find_related_tests(
        revision=RevisionRole.HEAD,
        symbol="pkg.api.Service",
    )
    fallback = investigator.find_related_tests(
        revision=RevisionRole.HEAD,
        symbol="new_function",
    )

    assert exact.matches[0].provenance.symbol_name == "test_service_run"
    assert exact.matches[0].provenance.match_type in {
        InvestigationMatchType.STATIC_IMPORT_ALIAS_REFERENCE,
        InvestigationMatchType.NAME_SYNTAX_REFERENCE,
    }
    assert fallback.matches[0].provenance.symbol_name == "test_textual_note"
    assert fallback.matches[0].provenance.match_type is InvestigationMatchType.TEXTUAL_FALLBACK


def test_function_local_import_does_not_make_unrelated_tests_look_related() -> None:
    source = """def test_uses_target():
    from pkg.api import Target
    assert True

def test_unrelated():
    assert True
"""
    base = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.BASE, _BASE_SHA), files={"tests/test_local.py": source}
    )
    head = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.HEAD, _HEAD_SHA), files={"tests/test_local.py": source}
    )
    local = RepositoryInvestigator(base=base, head=head)

    result = local.find_related_tests(revision="HEAD", symbol="pkg.api.Target")

    assert [item.provenance.symbol_name for item in result.matches] == ["test_uses_target"]
    assert (
        result.matches[0].provenance.match_type is InvestigationMatchType.SYNTACTIC_IMPORT_BINDING
    )
    assert "test scope" in result.matches[0].provenance.reason


def test_source_window_is_bounded_and_uses_only_indexed_source(
    investigator: RepositoryInvestigator,
) -> None:
    result = investigator.inspect_source_window(
        revision=RevisionRole.HEAD,
        path="pkg/api.py",
        start_line=1,
        line_count=3,
    )

    assert result.matches[0].content == "missing = object()\nnew_value = 2\n"
    assert result.matches[0].provenance.start_line == 1
    assert result.matches[0].provenance.end_line == 3


@pytest.mark.parametrize(
    ("path", "start_line", "line_count"),
    [
        ("../secret.py", 1, 1),
        ("/absolute.py", 1, 1),
        ("pkg\\api.py", 1, 1),
        ("pkg/api.py", 0, 1),
        ("pkg/api.py", 1, 121),
        ("pkg/api.py", 10_000, 1),
    ],
)
def test_source_window_rejects_unsafe_paths_and_ranges(
    investigator: RepositoryInvestigator,
    path: str,
    start_line: int,
    line_count: int,
) -> None:
    with pytest.raises(InvestigationQueryError):
        investigator.inspect_source_window(
            revision=RevisionRole.HEAD,
            path=path,
            start_line=start_line,
            line_count=line_count,
        )


def test_queries_reject_unknown_revision_symbols_and_excessive_results(
    investigator: RepositoryInvestigator,
) -> None:
    with pytest.raises(InvestigationQueryError, match="unknown immutable revision"):
        investigator.find_references(revision="CURRENT", symbol="Service")
    with pytest.raises(InvestigationQueryError, match="bounded dotted identifier"):
        investigator.find_references(revision="HEAD", symbol="Service.*")
    for maximum in (0, -1, True, 51):
        with pytest.raises(InvestigationQueryError, match="hard limit"):
            investigator.find_references(revision="HEAD", symbol="Service", maximum=maximum)


def test_result_truncation_is_explicit_and_byte_deterministic(
    investigator: RepositoryInvestigator,
) -> None:
    bounded = RepositoryInvestigator(
        base=investigator.base,
        head=investigator.head,
        budget=InvestigationBudget(default_max_results=1, hard_max_results=2),
    )
    first = bounded.find_references(revision="HEAD", symbol="Service")
    second = bounded.find_references(revision="HEAD", symbol="Service")

    assert first.truncated is True
    assert first.matches[0].provenance.source_truncated is False
    assert first.model_dump_json() == second.model_dump_json()
    assert json.loads(first.model_dump_json())["truncated"] is True


def test_observable_partition_limit_is_explicit(investigator: RepositoryInvestigator) -> None:
    result = investigator.compare_observables(maximum=1)

    assert result.truncated is True
    assert len(result.present_on_both) <= 1
    assert len(result.new_on_head) <= 1
    assert len(result.removed_from_head) <= 1
    assert (
        len(result.present_on_both) + len(result.new_on_head) + len(result.removed_from_head) <= 1
    )


def test_caller_provenance_does_not_claim_runtime_identity_for_same_named_calls() -> None:
    source = """from pkg.api import helper as imported_helper

def helper():
    return None

class First:
    def run(self):
        return None

    @classmethod
    def create(cls):
        return cls()

class Second:
    def run(self):
        return None

def outer(obj):
    def helper():
        return None
    helper()
    obj.run()
    imported_helper()
"""
    base = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.BASE, _BASE_SHA), files={"calls.py": source}
    )
    head = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.HEAD, _HEAD_SHA), files={"calls.py": source}
    )
    local = RepositoryInvestigator(base=base, head=head)

    qualified_names = {item.qualified_name for item in head.symbols}
    assert {"First.run", "First.create", "Second.run", "outer.helper"} <= qualified_names

    helper_calls = local.find_callers(revision="HEAD", symbol="pkg.api.helper")
    run_calls = local.find_callers(revision="HEAD", symbol="pkg.api.Service.run")

    helper_types = {
        item.reference.expression: item.provenance.match_type
        for item in helper_calls.matches
        if item.reference is not None
    }
    assert helper_types["imported_helper"] is InvestigationMatchType.STATIC_IMPORT_ALIAS_CALL
    assert helper_types["helper"] is InvestigationMatchType.POSSIBLE_NAME_CALL
    assert all(
        "runtime" in item.provenance.reason or "unresolved" in item.provenance.reason
        for item in helper_calls.matches
    )
    assert any(
        item.reference is not None
        and item.reference.expression == "obj.run"
        and item.provenance.match_type is InvestigationMatchType.POSSIBLE_ATTRIBUTE_CALL
        for item in run_calls.matches
    )


def test_cross_revision_identity_handles_kind_changes_renames_and_same_names() -> None:
    base = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.BASE, _BASE_SHA),
        files={
            "api.py": (
                "def operation():\n    return 1\nvalue = 1\n"
                "class State:\n    @property\n    def current(self):\n        return 1\n"
            ),
            "old.py": "def moved():\n    return 1\n",
            "one.py": "def duplicate():\n    return 1\n",
        },
    )
    head = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.HEAD, _HEAD_SHA),
        files={
            "api.py": (
                "async def operation():\n    return 1\nvalue = 2\n"
                "class State:\n    def current(self):\n        return 1\n"
            ),
            "new.py": "def moved():\n    return 1\n",
            "one.py": "def duplicate():\n    return 1\n",
            "two.py": "def duplicate():\n    return 1\n",
        },
    )
    local = RepositoryInvestigator(base=base, head=head)

    operation = local.compare_symbol(path="api.py", qualified_name="operation").comparisons[0]
    current = local.compare_symbol(path="api.py", qualified_name="State.current").comparisons[0]
    value = local.compare_symbol(path="api.py", qualified_name="value").comparisons[0]
    moved = local.compare_symbol(qualified_name="moved", maximum=10).comparisons
    duplicate = local.compare_symbol(qualified_name="duplicate", maximum=10).comparisons

    assert operation.presence is ObservablePresence.PRESENT_ON_BOTH
    assert operation.implementation_changed is True
    assert current.presence is ObservablePresence.PRESENT_ON_BOTH
    assert current.base is not None and current.base.kind is ObservableSymbolKind.PROPERTY
    assert current.head is not None and current.head.kind is ObservableSymbolKind.METHOD
    assert current.implementation_changed is True
    assert value.implementation_changed is True
    assert {item.presence for item in moved} == {
        ObservablePresence.REMOVED_FROM_HEAD,
        ObservablePresence.NEW_ON_HEAD,
    }
    assert {(item.path, item.presence) for item in duplicate} == {
        ("one.py", ObservablePresence.PRESENT_ON_BOTH),
        ("two.py", ObservablePresence.NEW_ON_HEAD),
    }


def test_inspect_symbol_enforces_aggregate_source_character_budget() -> None:
    body = "def target():\n    return '" + ("x" * 80) + "'\n"
    base = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.BASE, _BASE_SHA),
        files={f"file_{number}.py": body for number in range(3)},
    )
    head = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.HEAD, _HEAD_SHA),
        files={f"file_{number}.py": body for number in range(3)},
    )
    local = RepositoryInvestigator(
        base=base,
        head=head,
        budget=InvestigationBudget(
            default_max_results=3,
            hard_max_results=3,
            max_source_chars=100,
            max_total_source_chars=150,
        ),
    )

    result = local.inspect_symbol(revision="HEAD", qualified_name="target", maximum=3)

    assert sum(len(item.content or "") for item in result.matches) <= 150
    assert result.truncated is True
    assert any(item.provenance.source_truncated for item in result.matches)


def test_query_envelope_exposes_partial_index_state() -> None:
    base = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.BASE, _BASE_SHA), files={"broken.py": "def bad(:\n"}
    )
    head = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.HEAD, _HEAD_SHA), files={"broken.py": "value = 1\n"}
    )
    local = RepositoryInvestigator(base=base, head=head)

    result = local.find_references(revision="BASE", symbol="bad")
    comparison = local.compare_observables()

    assert result.matches == ()
    assert result.index_stats.syntax_errors == 1
    assert result.index_stats.truncated is True
    assert comparison.base_index_stats.truncated is True
    assert comparison.head_index_stats.truncated is False
