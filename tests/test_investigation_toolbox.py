"""The tool surface must be bounded, validated, auditable, and free of capabilities.

The toolbox is the only thing a model can act through, so these tests are as much a
security contract as a functional one.
"""

from __future__ import annotations

import pytest

from patchproof.investigation_toolbox import (
    InvestigationToolbox,
    InvestigationToolName,
    ToolBudget,
    ToolCallStatus,
)
from patchproof.investigation_tools import InvestigationBudget, RepositoryInvestigator
from patchproof.models import Revision, RevisionRole
from patchproof.repository_index import RepositoryIndex, RepositoryIndexBudget

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

_BASE = """__all__ = ["render"]


def render(value):
    return str(value)


def caller(value):
    return render(value)
"""

_HEAD = """__all__ = ["render"]


def render(value):
    return str(value).strip()


def caller(value):
    return render(value)
"""

_TEST_FILE = """from pkg.mod import render


def test_render():
    assert render(" a ") == " a "
"""


def build_toolbox(
    *, budget: ToolBudget | None = None, index_budget: RepositoryIndexBudget | None = None
) -> InvestigationToolbox:
    base = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.BASE, sha=BASE_SHA),
        files={"pkg/mod.py": _BASE, "tests/test_mod.py": _TEST_FILE},
        budget=index_budget,
    )
    head = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.HEAD, sha=HEAD_SHA),
        files={"pkg/mod.py": _HEAD, "tests/test_mod.py": _TEST_FILE},
        budget=index_budget,
    )
    return InvestigationToolbox(
        investigator=RepositoryInvestigator(base=base, head=head, budget=InvestigationBudget()),
        budget=budget,
    )


# ---------------------------------------------------------------------------------
# Capability surface
# ---------------------------------------------------------------------------------


def test_exactly_four_tools_are_exposed() -> None:
    assert {item.value for item in InvestigationToolName} == {
        "inspect_symbol",
        "find_references",
        "find_related_tests",
        "inspect_source_window",
    }


@pytest.mark.parametrize(
    "tool",
    [
        "run_shell",
        "exec",
        "read_file",
        "git",
        "http_get",
        "compare_observables",
        "set_budget",
        "",
    ],
)
def test_capabilities_outside_the_surface_are_refused(tool: str) -> None:
    toolbox = build_toolbox()
    result = toolbox.call(tool, {"revision": "HEAD"})
    assert result["status"] == ToolCallStatus.INVALID_ARGUMENTS.value
    assert "unknown tool" in result["error"]
    # A refused unknown tool consumes no budget and leaves no forged transcript entry.
    assert toolbox.total_calls == 0


@pytest.mark.parametrize(
    "revision",
    ["main", BASE_SHA, "HEAD~1", "refs/heads/main", "HEAD^", 7, None, "HEADS", "BASE HEAD"],
)
def test_revision_selection_is_limited_to_base_and_head(revision: object) -> None:
    """The model may never name a SHA, a ref, or any revision of its own choosing."""
    toolbox = build_toolbox()
    result = toolbox.call("inspect_symbol", {"revision": revision, "qualified_name": "render"})
    assert result["status"] == ToolCallStatus.INVALID_ARGUMENTS.value


@pytest.mark.parametrize("revision", ["BASE", "HEAD", "base", "head", " Head ", "base "])
def test_the_two_valid_revision_spellings_are_accepted(revision: str) -> None:
    toolbox = build_toolbox()
    result = toolbox.call("inspect_symbol", {"revision": revision, "qualified_name": "render"})
    assert result["status"] == ToolCallStatus.OK.value


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/system32/config",
        "pkg/../../secret.py",
        ".git/config",
        "pkg/missing_module.py",
    ],
)
def test_arbitrary_filesystem_paths_are_refused(path: str) -> None:
    toolbox = build_toolbox()
    result = toolbox.call(
        "inspect_source_window",
        {"revision": "HEAD", "path": path, "start_line": 1, "line_count": 5},
    )
    assert result["status"] == ToolCallStatus.INVALID_ARGUMENTS.value


# ---------------------------------------------------------------------------------
# Valid calls
# ---------------------------------------------------------------------------------


def test_valid_inspect_symbol_returns_bounded_provenance_carrying_matches() -> None:
    toolbox = build_toolbox()
    result = toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})

    assert result["status"] == ToolCallStatus.OK.value
    assert result["revision"] == "HEAD"
    assert result["match_count"] >= 1
    match = result["matches"][0]
    assert match["path"] == "pkg/mod.py"
    assert match["symbol"]["identity"] == "pkg/mod.py::render"
    assert match["symbol"]["exported"] is True
    assert "reason" in match and "match_type" in match


def test_find_references_and_related_tests_are_usable() -> None:
    toolbox = build_toolbox()
    references = toolbox.call("find_references", {"revision": "HEAD", "symbol": "render"})
    tests = toolbox.call("find_related_tests", {"revision": "HEAD", "symbol": "render"})

    assert references["status"] == ToolCallStatus.OK.value
    assert tests["status"] == ToolCallStatus.OK.value
    assert tests["match_count"] >= 1


def test_source_window_line_count_is_clamped_to_the_budget() -> None:
    toolbox = build_toolbox(budget=ToolBudget(max_source_window_lines=2))
    result = toolbox.call(
        "inspect_source_window",
        {"revision": "HEAD", "path": "pkg/mod.py", "start_line": 1, "line_count": 500},
    )
    assert result["status"] == ToolCallStatus.OK.value
    content = result["matches"][0].get("content", "")
    assert content.count("\n") <= 3


# ---------------------------------------------------------------------------------
# Invalid parameters
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("find_references", {"revision": "HEAD"}),
        ("find_references", {"revision": "HEAD", "symbol": "   "}),
        ("find_references", {"revision": "HEAD", "symbol": 12}),
        ("find_related_tests", {"revision": "HEAD"}),
        ("inspect_source_window", {"revision": "HEAD", "path": "pkg/mod.py", "start_line": 0}),
        (
            "inspect_source_window",
            {"revision": "HEAD", "path": "pkg/mod.py", "line_count": "many"},
        ),
        ("inspect_symbol", {"revision": "HEAD", "qualified_name": "a" * 600}),
        ("inspect_symbol", {"revision": "HEAD", "maximum": 0}),
        ("inspect_symbol", {"revision": "HEAD", "maximum": True}),
    ],
)
def test_invalid_parameters_are_refused_without_raising(
    tool: str, arguments: dict[str, object]
) -> None:
    toolbox = build_toolbox()
    result = toolbox.call(tool, arguments)
    assert result["status"] == ToolCallStatus.INVALID_ARGUMENTS.value
    assert result["error"]
    # The refusal is recorded so the transcript shows what the model attempted.
    assert toolbox.transcript[-1].status is ToolCallStatus.INVALID_ARGUMENTS


def test_a_refused_call_still_allows_the_investigation_to_continue() -> None:
    toolbox = build_toolbox()
    toolbox.call("find_references", {"revision": "HEAD"})
    recovered = toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})
    assert recovered["status"] == ToolCallStatus.OK.value


# ---------------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------------


def test_per_tool_budget_is_enforced_independently() -> None:
    toolbox = build_toolbox(budget=ToolBudget(max_total_calls=8, max_calls_per_tool=2))
    for _ in range(2):
        assert (
            toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})[
                "status"
            ]
            == ToolCallStatus.OK.value
        )

    exhausted = toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})
    assert exhausted["status"] == ToolCallStatus.BUDGET_EXHAUSTED.value

    # A different tool still has its own allowance.
    other = toolbox.call("find_references", {"revision": "HEAD", "symbol": "render"})
    assert other["status"] == ToolCallStatus.OK.value


def test_total_call_budget_is_enforced_across_tools() -> None:
    toolbox = build_toolbox(budget=ToolBudget(max_total_calls=3, max_calls_per_tool=3))
    toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})
    toolbox.call("find_references", {"revision": "HEAD", "symbol": "render"})
    toolbox.call("find_related_tests", {"revision": "HEAD", "symbol": "render"})
    assert toolbox.exhausted

    blocked = toolbox.call(
        "inspect_source_window",
        {"revision": "HEAD", "path": "pkg/mod.py", "start_line": 1, "line_count": 3},
    )
    assert blocked["status"] == ToolCallStatus.BUDGET_EXHAUSTED.value


def test_turn_budget_stops_advancing_at_the_limit() -> None:
    toolbox = build_toolbox(budget=ToolBudget(max_turns=2))
    assert toolbox.turn == 1
    assert toolbox.begin_turn() is True
    assert toolbox.turn == 2
    assert toolbox.begin_turn() is False
    assert toolbox.turn == 2


def test_aggregate_source_budget_stops_accumulating_content() -> None:
    toolbox = build_toolbox(
        budget=ToolBudget(max_total_source_chars=40, max_total_calls=8, max_calls_per_tool=8)
    )
    seen_omission = False
    for _ in range(4):
        payload = toolbox.call(
            "inspect_source_window",
            {"revision": "HEAD", "path": "pkg/mod.py", "start_line": 1, "line_count": 9},
        )
        for match in payload.get("matches", []):
            if "content_omitted" in match or match.get("content_truncated"):
                seen_omission = True
    assert seen_omission


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_turns": 0},
        {"max_total_calls": 0},
        {"max_turns": 99},
        {"max_total_calls": 99},
        {"max_calls_per_tool": 99},
        {"max_total_calls": 2, "max_calls_per_tool": 4},
        {"max_total_source_chars": 999_999},
    ],
)
def test_out_of_contract_budgets_are_rejected(overrides: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ToolBudget(**overrides)


# ---------------------------------------------------------------------------------
# Uncertainty semantics
# ---------------------------------------------------------------------------------


def test_partial_index_is_reported_in_band_on_every_result() -> None:
    """A truncated index must never let a consumer read absence as proof of absence."""
    toolbox = build_toolbox(index_budget=RepositoryIndexBudget(max_files=1))
    result = toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})

    assert result["index_partial"] is True
    assert result["absence_is_not_proof"] is True


def test_complete_index_does_not_claim_partiality() -> None:
    toolbox = build_toolbox()
    result = toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})
    assert result["index_partial"] is False
    assert result["absence_is_not_proof"] is False


def test_possible_match_types_are_preserved_verbatim() -> None:
    """POSSIBLE_* must survive serialization; it may never be presented as proven."""
    toolbox = build_toolbox()
    result = toolbox.call("find_callers", {"revision": "HEAD", "symbol": "render"})
    # find_callers is deliberately outside the model surface.
    assert result["status"] == ToolCallStatus.INVALID_ARGUMENTS.value

    references = toolbox.call("find_references", {"revision": "HEAD", "symbol": "render"})
    types = {match["match_type"] for match in references["matches"]}
    assert types, "expected at least one classified reference"
    for match_type in types:
        assert match_type == match_type.upper()


# ---------------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------------


def test_transcript_records_sequence_tool_status_and_safe_arguments() -> None:
    toolbox = build_toolbox()
    toolbox.call("inspect_symbol", {"revision": "HEAD", "qualified_name": "render"})
    toolbox.call("find_references", {"revision": "BASE", "symbol": "render"})

    transcript = toolbox.transcript
    assert [item.sequence for item in transcript] == [1, 2]
    assert [item.tool for item in transcript] == [
        InvestigationToolName.INSPECT_SYMBOL,
        InvestigationToolName.FIND_REFERENCES,
    ]
    assert all(item.status is ToolCallStatus.OK for item in transcript)
    assert transcript[0].arguments["qualified_name"] == "render"


def test_transcript_arguments_drop_unserializable_values() -> None:
    toolbox = build_toolbox()
    toolbox.call(
        "inspect_symbol", {"revision": "HEAD", "qualified_name": "render", "junk": object()}
    )
    assert "junk" not in toolbox.transcript[-1].arguments
