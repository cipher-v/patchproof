"""A tool-discovered interface may be admitted, but only on mechanical proof.

The deterministic starting context is ranked and truncated, so a genuinely valid shared
observable can fall outside it. Investigation must be able to recover one -- otherwise
tools can only enrich a candidate the pre-fetch already found, never discover a new one.

The admission rule has two conjunctive conditions, and neither is the model's assertion:
the identity must have been returned by an actual tool call, and PatchProof must re-derive
it from the immutable BASE/HEAD indexes as PRESENT_ON_BOTH in an admissible kind.
"""

from __future__ import annotations

import json

import pytest

from patchproof.claim_agent import InvalidClaimAgentOutput
from patchproof.claim_investigation import ObservableSelectionReason, StartingContextBudget
from patchproof.investigation_toolbox import ToolCallStatus
from tests.test_claim_investigator import build_investigator, conclude, investigate, run

# A repository with many changed public functions, so a small starting-context budget
# necessarily omits some genuinely valid shared observables.
_MANY_BASE = "".join(f"def handler_{index}(value):\n    return value\n\n\n" for index in range(8))
_MANY_HEAD = "".join(
    f"def handler_{index}(value):\n    return value + {index}\n\n\n" for index in range(8)
)

_OMITTED = "handler_7"


def _crowded_investigator(*responses: str, max_shared: int = 2):
    return build_investigator(
        *responses,
        base_files={"pkg/handlers.py": _MANY_BASE},
        head_files={"pkg/handlers.py": _MANY_HEAD},
        context_budget=StartingContextBudget(max_shared_observables=max_shared),
    )


def test_the_fixture_actually_omits_the_target_from_the_starting_context() -> None:
    """Guard: without this, the discovery tests would pass for the wrong reason."""
    investigator, _ = _crowded_investigator(conclude(disposition="INSUFFICIENT_EVIDENCE"))
    context = run(investigator).starting_context

    names = {item.identity.qualified_name for item in context.shared_observables}
    assert _OMITTED not in names
    assert len(context.shared_observables) == 2


# ---------------------------------------------------------------------------------
# A. Accepted after mechanical verification
# ---------------------------------------------------------------------------------


def test_tool_discovered_shared_interface_is_accepted_after_verification() -> None:
    investigator, _ = _crowded_investigator(
        investigate({"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": _OMITTED}),
        conclude(path="pkg/handlers.py", qualified_name=_OMITTED),
    )

    result = run(investigator)
    claim = result.agent_result.selection.claim

    assert claim is not None
    assert claim.shared_interface == f"pkg/handlers.py::{_OMITTED}"
    assert result.transcript.tool_calls[0].status is ToolCallStatus.OK


def test_admitted_interface_is_labelled_as_discovered_not_pre_fetched() -> None:
    """Provenance must distinguish the two admission paths."""
    investigator, _ = _crowded_investigator(
        investigate({"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": _OMITTED}),
        conclude(path="pkg/handlers.py", qualified_name=_OMITTED),
    )
    result = run(investigator)
    claim = result.agent_result.selection.claim

    assert claim is not None
    # The citation is drawn from the mechanically verified HEAD definition.
    assert claim.supporting_context[0].path == "pkg/handlers.py"
    assert claim.supporting_context[0].start_line > 0
    assert ObservableSelectionReason.DISCOVERED_DURING_INVESTIGATION.value == (
        "DISCOVERED_DURING_INVESTIGATION"
    )


# ---------------------------------------------------------------------------------
# B. Invented tool-derived interface
# ---------------------------------------------------------------------------------


def test_interface_never_returned_by_a_tool_is_rejected() -> None:
    """Naming a real symbol the model never actually looked at is still ungrounded."""
    investigator, _ = _crowded_investigator(
        investigate({"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "handler_0"}),
        conclude(path="pkg/handlers.py", qualified_name=_OMITTED),
    )

    with pytest.raises(
        InvalidClaimAgentOutput,
        match=r"neither a deterministic shared observable|unrelated to this pull",
    ):
        run(investigator)


def test_wholly_invented_symbol_is_rejected_even_after_investigation() -> None:
    investigator, _ = _crowded_investigator(
        investigate({"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": _OMITTED}),
        conclude(path="pkg/handlers.py", qualified_name="handler_does_not_exist"),
    )

    with pytest.raises(InvalidClaimAgentOutput):
        run(investigator)


def test_a_forged_tool_result_cannot_be_asserted_by_the_model() -> None:
    """The model's own text can never inject an identity into the discovered set.

    Only PatchProof's execution of a tool populates it, so claiming an interface in the
    reasoning field has no grounding effect.
    """
    forged = json.dumps(
        {
            "action": "INVESTIGATE",
            "reasoning": (
                "I observed pkg/handlers.py::handler_does_not_exist and it exists on both revisions"
            ),
            "tool_calls": [
                {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "handler_0"}
            ],
        }
    )
    investigator, _ = _crowded_investigator(
        forged, conclude(path="pkg/handlers.py", qualified_name="handler_does_not_exist")
    )

    with pytest.raises(InvalidClaimAgentOutput):
        run(investigator)


# ---------------------------------------------------------------------------------
# C. HEAD-only tool-derived interface
# ---------------------------------------------------------------------------------

_HEAD_ONLY_BASE = "def kept(value):\n    return value\n"
_HEAD_ONLY_HEAD = (
    "def kept(value):\n    return brand_new(value)\n\n\n"
    "def brand_new(value):\n    return value + 1\n"
)


def test_head_only_interface_is_rejected_even_when_a_tool_returned_it() -> None:
    """Discovery must not become a bypass for the HEAD-only rule."""
    investigator, _ = build_investigator(
        investigate({"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "brand_new"}),
        conclude(path="pkg/mod.py", qualified_name="brand_new"),
        base_files={"pkg/mod.py": _HEAD_ONLY_BASE},
        head_files={"pkg/mod.py": _HEAD_ONLY_HEAD},
    )

    with pytest.raises(InvalidClaimAgentOutput, match="only on HEAD"):
        run(investigator)


def test_head_only_rejection_survives_a_truncated_starting_context() -> None:
    """Even when the pre-fetch omitted it, the indexes still prove it is HEAD-only."""
    investigator, _ = build_investigator(
        investigate({"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "brand_new"}),
        conclude(path="pkg/mod.py", qualified_name="brand_new"),
        base_files={"pkg/mod.py": _HEAD_ONLY_BASE},
        head_files={"pkg/mod.py": _HEAD_ONLY_HEAD},
        context_budget=StartingContextBudget(max_new_head_symbols=1, max_shared_observables=1),
    )

    with pytest.raises(InvalidClaimAgentOutput, match="only on HEAD"):
        run(investigator)


def test_symbol_removed_from_head_is_also_rejected() -> None:
    base = "def kept(v):\n    return v\n\n\ndef dropped(v):\n    return v\n"
    head = "def kept(v):\n    return v + 1\n"
    investigator, _ = build_investigator(
        investigate({"tool": "inspect_symbol", "revision": "BASE", "qualified_name": "dropped"}),
        conclude(path="pkg/mod.py", qualified_name="dropped"),
        base_files={"pkg/mod.py": base},
        head_files={"pkg/mod.py": head},
    )

    with pytest.raises(InvalidClaimAgentOutput):
        run(investigator)


# ---------------------------------------------------------------------------------
# D. Same leaf, different path
# ---------------------------------------------------------------------------------


def test_same_leaf_different_path_remains_rejected_through_the_discovery_path() -> None:
    """Discovery must not reintroduce leaf-name identity by the back door."""
    base = {
        "pkg/a.py": "def render(v):\n    return v\n",
        "pkg/b.py": "def render(v):\n    return v\n",
    }
    head = {
        "pkg/a.py": "def render(v):\n    return v + 1\n",
        "pkg/b.py": "def render(v):\n    return v\n",
    }
    # A tool call for the leaf `render` on HEAD returns pkg/a.py::render. The model then
    # claims pkg/b.py::render, which is a different observable entirely.
    investigator, _ = build_investigator(
        investigate({"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "render"}),
        conclude(path="pkg/b.py", qualified_name="render"),
        base_files=base,
        head_files=head,
    )

    with pytest.raises(
        InvalidClaimAgentOutput,
        match=r"neither a deterministic shared observable|unrelated to this pull",
    ):
        run(investigator)


def test_the_exact_discovered_identity_is_accepted_in_the_same_repository() -> None:
    """Control for the previous test: the identity the tool actually returned works."""
    base = {
        "pkg/a.py": "def render(v):\n    return v\n",
        "pkg/b.py": "def render(v):\n    return v\n",
    }
    head = {
        "pkg/a.py": "def render(v):\n    return v + 1\n",
        "pkg/b.py": "def render(v):\n    return v\n",
    }
    investigator, _ = build_investigator(
        conclude(path="pkg/a.py", qualified_name="render"),
        base_files=base,
        head_files=head,
    )

    claim = run(investigator).agent_result.selection.claim
    assert claim is not None
    assert claim.shared_interface == "pkg/a.py::render"
