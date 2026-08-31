"""End-to-end claim investigation with a scripted model. Zero billable calls.

Every test here drives the real orchestrator, the real toolbox, and the real Phase 1
index. Only the model is replaced, by a script of pre-written JSON turns. That is the
same boundary the ADK adapter implements, so these tests exercise production code paths
rather than a parallel simulation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from patchproof.claim_agent import (
    ClaimSelectionDisposition,
    InvalidClaimAgentOutput,
    ModelUsage,
    PullRequestNarrative,
)
from patchproof.claim_investigation import (
    DeterministicInvestigationPlanner,
    ObservableIdentity,
    ObservableRank,
    StartingContextBudget,
)
from patchproof.claim_investigator import (
    ClaimInvestigator,
    InvestigatorTurnRequest,
    RawInvestigatorResponse,
)
from patchproof.investigation_toolbox import ToolBudget, ToolCallStatus
from patchproof.investigation_tools import RepositoryInvestigator
from patchproof.model_reliability import ModelInvocationFailure
from patchproof.models import Revision, RevisionRole
from patchproof.repository_index import RepositoryIndex

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

_BASE = """__all__ = ["Console"]


class Console:
    def render(self, text, style):
        return style + text
"""

_HEAD = """__all__ = ["Console"]


class Console:
    def render(self, text, style):
        return terminate_lines(style + text)


def terminate_lines(value):
    return value.replace("\\n", "|\\n")
"""


class ScriptedModel:
    """Return pre-written turns in order, recording every request it received."""

    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[InvestigatorTurnRequest] = []

    async def invoke(self, request: InvestigatorTurnRequest) -> RawInvestigatorResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("scripted model exhausted; orchestrator made an extra call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return RawInvestigatorResponse(
            text=item,
            usage=ModelUsage(model_name="scripted-model", duration_seconds=0.01),
        )


def build_investigator(
    *responses: str | Exception,
    base_files: dict[str, str] | None = None,
    head_files: dict[str, str] | None = None,
    tool_budget: ToolBudget | None = None,
    context_budget: StartingContextBudget | None = None,
) -> tuple[ClaimInvestigator, ScriptedModel]:
    base = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.BASE, sha=BASE_SHA),
        files=base_files or {"pkg/console.py": _BASE},
    )
    head = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.HEAD, sha=HEAD_SHA),
        files=head_files or {"pkg/console.py": _HEAD},
    )
    planner = DeterministicInvestigationPlanner(
        investigator=RepositoryInvestigator(base=base, head=head), budget=context_budget
    )
    model = ScriptedModel(*responses)
    return (
        ClaimInvestigator(model=model, planner=planner, tool_budget=tool_budget),
        model,
    )


def conclude(
    *,
    path: str = "pkg/console.py",
    qualified_name: str = "Console.render",
    disposition: str = "SELECTED",
    **overrides: Any,
) -> str:
    if disposition != "SELECTED":
        return json.dumps(
            {
                "action": "CONCLUDE",
                "reasoning": "no grounded shared observable distinguishes the revisions",
                "disposition": disposition,
            }
        )
    claim: dict[str, Any] = {
        "summary": "Rendered output marks line terminators.",
        "interface_path": path,
        "interface_qualified_name": qualified_name,
        "observable_operation": "Console.render(text, style)",
        "trigger_condition": "the rendered text contains a newline",
        "expected_head_observation": "the newline is preceded by a terminator marker",
        "expected_base_hypothesis": "the newline is emitted without a terminator marker",
        "action": "render text containing a newline",
        "expected_behavior": "output marks the line terminator",
        "preconditions": ["a console instance exists"],
        "confidence": 0.9,
    }
    claim.update(overrides)
    return json.dumps(
        {
            "action": "CONCLUDE",
            "reasoning": "Console.render is the exported shared observable that changed.",
            "disposition": "SELECTED",
            "claim": claim,
        }
    )


def investigate(*tool_calls: dict[str, Any]) -> str:
    return json.dumps(
        {
            "action": "INVESTIGATE",
            "reasoning": "need more repository facts before concluding",
            "tool_calls": list(tool_calls),
        }
    )


def run(investigator: ClaimInvestigator):
    return asyncio.run(
        investigator.investigate(
            narrative=PullRequestNarrative.from_untrusted(title="Terminate rendered lines")
        )
    )


# ---------------------------------------------------------------------------------
# Zero / one / multi tool paths
# ---------------------------------------------------------------------------------


def test_zero_tool_claim_selection_uses_only_the_deterministic_context() -> None:
    """The common case must need no tool calls at all."""
    investigator, model = build_investigator(conclude())

    result = run(investigator)

    assert result.transcript.tool_calls == ()
    assert result.transcript.turns == 1
    assert len(model.requests) == 1
    claim = result.agent_result.selection.claim
    assert claim is not None
    assert claim.shared_interface == "pkg/console.py::Console.render"
    # The deterministic context alone already surfaced the anchor, and the exported
    # class ranks at the top of the offered observables.
    names = [item.identity.qualified_name for item in result.starting_context.shared_observables]
    assert "Console.render" in names
    assert result.starting_context.shared_observables[0].rank is ObservableRank.EXPORTED_SHARED


def test_one_tool_call_then_conclusion_is_recorded() -> None:
    investigator, model = build_investigator(
        investigate(
            {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "terminate_lines"}
        ),
        conclude(),
    )

    result = run(investigator)

    assert len(result.transcript.tool_calls) == 1
    assert result.transcript.tool_calls[0].status is ToolCallStatus.OK
    assert result.transcript.turns == 2
    assert result.agent_result.selection.disposition is ClaimSelectionDisposition.SELECTED
    # The second turn saw the observation from the first.
    assert model.requests[1].observations


def test_multi_tool_investigation_across_turns() -> None:
    investigator, _ = build_investigator(
        investigate(
            {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "Console.render"},
            {"tool": "inspect_symbol", "revision": "BASE", "qualified_name": "Console.render"},
        ),
        investigate({"tool": "find_references", "revision": "HEAD", "symbol": "terminate_lines"}),
        conclude(),
    )

    result = run(investigator)

    assert len(result.transcript.tool_calls) == 3
    assert result.transcript.successful_tool_calls == 3
    assert result.transcript.turns == 3


# ---------------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------------


def test_invented_interface_is_rejected() -> None:
    investigator, _ = build_investigator(
        conclude(path="pkg/console.py", qualified_name="Console.no_such_method")
    )

    with pytest.raises(InvalidClaimAgentOutput, match="not a shared observable"):
        run(investigator)


def test_invented_path_is_rejected() -> None:
    investigator, _ = build_investigator(conclude(path="pkg/imaginary.py"))

    with pytest.raises(InvalidClaimAgentOutput, match="not a shared observable"):
        run(investigator)


def test_head_only_interface_is_rejected_with_a_specific_reason() -> None:
    """Naming the new helper must fail loudly, not silently produce a doomed candidate."""
    investigator, _ = build_investigator(conclude(qualified_name="terminate_lines"))

    with pytest.raises(InvalidClaimAgentOutput, match="only on HEAD"):
        run(investigator)


def test_same_leaf_in_another_file_is_not_accepted_as_the_interface() -> None:
    """`pkg/a.py::render` must not satisfy a claim grounded in `pkg/b.py::render`."""
    base = {
        "pkg/a.py": "def render(v):\n    return v\n",
        "pkg/b.py": "def render(v):\n    return v\n",
    }
    head = {
        "pkg/a.py": "def render(v):\n    return v + 1\n",
        "pkg/b.py": "def render(v):\n    return v\n",
    }
    investigator, _ = build_investigator(
        conclude(path="pkg/b.py", qualified_name="render"),
        base_files=base,
        head_files=head,
    )

    with pytest.raises(InvalidClaimAgentOutput, match="not a shared observable"):
        run(investigator)

    # The identically named symbol in the file that actually changed is accepted.
    accepted, _ = build_investigator(
        conclude(path="pkg/a.py", qualified_name="render"),
        base_files=base,
        head_files=head,
    )
    result = run(accepted)
    assert result.agent_result.selection.claim is not None
    assert result.agent_result.selection.claim.shared_interface == "pkg/a.py::render"


def test_malformed_interface_identity_is_rejected() -> None:
    investigator, _ = build_investigator(conclude(path="../../etc/passwd"))
    with pytest.raises(InvalidClaimAgentOutput, match="not a valid repository symbol"):
        run(investigator)


def test_head_only_implementation_still_yields_a_valid_shared_observable() -> None:
    """The required behavior: follow the new helper to the interface that can be tested."""
    investigator, _ = build_investigator(conclude())

    result = run(investigator)
    claim = result.agent_result.selection.claim

    assert claim is not None
    assert claim.shared_interface == "pkg/console.py::Console.render"
    render = result.starting_context.observable(
        ObservableIdentity(path="pkg/console.py", qualified_name="Console.render")
    )
    assert render is not None
    assert "terminate_lines" in render.reaches_new_head_symbols


# ---------------------------------------------------------------------------------
# Abstention
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("disposition", ["INSUFFICIENT_EVIDENCE", "COUNTERFACTUAL_NOT_APPLICABLE"])
def test_conservative_abstention_is_a_valid_terminal_outcome(disposition: str) -> None:
    investigator, _ = build_investigator(conclude(disposition=disposition))

    result = run(investigator)

    assert result.agent_result.selection.claim is None
    assert result.agent_result.selection.disposition.value == disposition


def test_selected_without_a_claim_is_rejected_as_malformed() -> None:
    payload = json.dumps(
        {"action": "CONCLUDE", "reasoning": "inconsistent", "disposition": "SELECTED"}
    )
    investigator, _ = build_investigator(payload)

    with pytest.raises(InvalidClaimAgentOutput, match="invalid structured output"):
        run(investigator)


def test_investigate_turn_without_tool_calls_is_rejected() -> None:
    payload = json.dumps({"action": "INVESTIGATE", "reasoning": "thinking", "tool_calls": []})
    investigator, _ = build_investigator(payload)

    with pytest.raises(InvalidClaimAgentOutput, match="invalid structured output"):
        run(investigator)


# ---------------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------------


def test_budget_exhaustion_forces_one_final_conclusion_rather_than_failing() -> None:
    """Running out of budget must still produce a conservative decision."""
    investigator, model = build_investigator(
        investigate(
            {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "Console.render"}
        ),
        investigate({"tool": "find_references", "revision": "HEAD", "symbol": "Console.render"}),
        conclude(disposition="INSUFFICIENT_EVIDENCE"),
        tool_budget=ToolBudget(max_turns=2, max_total_calls=8, max_calls_per_tool=3),
    )

    result = run(investigator)

    assert result.agent_result.selection.claim is None
    assert "budget is exhausted" in model.requests[-1].budget_note


def test_refusing_to_conclude_after_budget_exhaustion_is_an_error() -> None:
    investigator, _ = build_investigator(
        investigate(
            {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "Console.render"}
        ),
        investigate({"tool": "find_references", "revision": "HEAD", "symbol": "Console.render"}),
        investigate({"tool": "find_references", "revision": "BASE", "symbol": "Console.render"}),
        tool_budget=ToolBudget(max_turns=2),
    )

    with pytest.raises(InvalidClaimAgentOutput, match="did not conclude"):
        run(investigator)


def test_exceeding_the_total_call_budget_refuses_further_calls_but_keeps_going() -> None:
    calls = [
        {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "Console.render"},
        {"tool": "find_references", "revision": "HEAD", "symbol": "Console.render"},
        {"tool": "find_related_tests", "revision": "HEAD", "symbol": "Console.render"},
    ]
    investigator, _ = build_investigator(
        investigate(*calls),
        conclude(),
        tool_budget=ToolBudget(max_turns=4, max_total_calls=2, max_calls_per_tool=2),
    )

    result = run(investigator)

    statuses = [item.status for item in result.transcript.tool_calls]
    assert ToolCallStatus.BUDGET_EXHAUSTED in statuses
    assert result.agent_result.selection.claim is not None


# ---------------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------------


def test_model_failure_propagates_as_a_model_invocation_failure() -> None:
    investigator, _ = build_investigator(ModelInvocationFailure("provider unavailable"))

    with pytest.raises(ModelInvocationFailure):
        run(investigator)


def test_malformed_model_output_is_rejected_with_provenance() -> None:
    investigator, _ = build_investigator("this is not json")

    with pytest.raises(InvalidClaimAgentOutput) as rejected:
        run(investigator)
    assert rejected.value.raw_response_sha256
    assert rejected.value.usage is not None


def test_oversized_model_output_is_rejected() -> None:
    investigator, _ = build_investigator("x" * 20_000)

    with pytest.raises(InvalidClaimAgentOutput, match="exceeds the configured budget"):
        run(investigator)


def test_invalid_tool_call_does_not_abort_the_investigation() -> None:
    investigator, _ = build_investigator(
        investigate({"tool": "run_shell", "revision": "HEAD", "symbol": "rm -rf /"}),
        conclude(),
    )

    result = run(investigator)

    assert result.agent_result.selection.claim is not None
    # The bogus tool never entered the transcript, and the run still concluded.
    assert result.transcript.tool_calls == ()


# ---------------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------------


def test_transcript_is_content_addressed_and_replayable() -> None:
    investigator, _ = build_investigator(
        investigate(
            {"tool": "inspect_symbol", "revision": "HEAD", "qualified_name": "Console.render"}
        ),
        conclude(),
    )

    result = run(investigator)

    assert len(result.transcript.starting_context_sha256) == 64
    assert all(len(item) == 64 for item in result.transcript.response_sha256)
    assert len(result.transcript.response_sha256) == 2
    assert result.agent_result.raw_response_sha256 == result.transcript.response_sha256[-1]


def test_selected_claim_carries_the_full_differential_hypothesis() -> None:
    investigator, _ = build_investigator(conclude())

    claim = run(investigator).agent_result.selection.claim

    assert claim is not None
    assert claim.observable_operation
    assert claim.trigger_condition
    assert claim.expected_head_observation
    assert claim.expected_base_hypothesis
    assert claim.affected_symbols[0].path == "pkg/console.py"
    assert claim.affected_symbols[0].qualified_name == "Console.render"
    assert claim.supporting_context[0].path == "pkg/console.py"
