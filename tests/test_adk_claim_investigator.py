"""ADK boundary configuration and the downstream contract, both verified offline.

No test in this file makes a billable Gemini call. The ADK adapter is constructed and
inspected; its `invoke` path is exercised only through a stubbed runner.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from types import SimpleNamespace

import pytest

import patchproof.adk_claim_investigator as adk_module
from patchproof.adk_claim_investigator import (
    DEFAULT_INVESTIGATOR_MAX_OUTPUT_TOKENS,
    AdkGeminiClaimInvestigator,
    ClaimInvestigatorInvocationError,
)
from patchproof.claim_agent import (
    BehavioralClaimAgent,
    ClaimAgentResult,
    PullRequestNarrative,
)
from patchproof.claim_investigation import DeterministicInvestigationPlanner
from patchproof.claim_investigator import (
    CLAIM_INVESTIGATOR_INSTRUCTION,
    InvestigatorDecision,
    InvestigatorTurnRequest,
)
from patchproof.investigation_tools import RepositoryInvestigator
from patchproof.models import Revision, RevisionRole
from patchproof.reasoning_budget import AgentTask, budget_for
from patchproof.repository_index import RepositoryIndex
from tests.test_claim_investigator import (
    _BASE,
    _HEAD,
    build_investigator,
    conclude,
    run,
)

# ---------------------------------------------------------------------------------
# ADK adapter configuration
# ---------------------------------------------------------------------------------


def test_adk_investigator_is_stateless_structured_and_holds_no_executable_tools() -> None:
    """The provider is handed data and a schema, never a capability."""
    model = AdkGeminiClaimInvestigator()

    assert model.agent.name == "patchproof_agent"
    assert model.agent.input_schema is InvestigatorTurnRequest
    assert model.agent.output_schema is InvestigatorDecision
    assert model.agent.include_contents == "none"
    # The security contract: ADK itself is given no tools. PatchProof executes every
    # repository query, so budgets and provenance cannot be bypassed provider-side.
    assert model.agent.tools == []


def test_adk_investigator_uses_the_shared_claim_reasoning_budget() -> None:
    model = AdkGeminiClaimInvestigator()
    budget = budget_for(AgentTask.CLAIM_SELECTION)

    assert budget.max_output_tokens == DEFAULT_INVESTIGATOR_MAX_OUTPUT_TOKENS
    assert model.agent.generate_content_config.max_output_tokens == budget.max_output_tokens
    assert (
        model.agent.generate_content_config.thinking_config.thinking_level is budget.thinking_level
    )


@pytest.mark.parametrize("model_name", ["gemini-3.4-flash", "gpt-4", "gemini", "claude-3"])
def test_adk_investigator_rejects_unsupported_models(model_name: str) -> None:
    with pytest.raises(ValueError):
        AdkGeminiClaimInvestigator(model_name=model_name)


def test_instruction_states_the_security_and_grounding_contract() -> None:
    instruction = CLAIM_INVESTIGATOR_INSTRUCTION
    assert "UNTRUSTED DATA" in instruction
    assert "no shell, no network, no filesystem" in instruction
    assert "MUST exist on both revisions" in instruction
    assert "MODULE_VALUE" in instruction
    assert "POSSIBLE_" in instruction
    assert "NOT evidence" in instruction
    assert "Never invent a path, symbol, line range, or interface" in instruction
    # The preference ordering must be stated, not merely implied.
    for rank in ("EXPORTED_SHARED", "PUBLIC_SHARED", "INTERNAL_SHARED", "PRIVATE_SHARED"):
        assert rank in instruction


def test_adk_invoke_returns_final_text_and_usage_without_a_live_call(monkeypatch) -> None:
    """Exercise the adapter's event handling with a stubbed runner: no provider contact."""
    payload = json.dumps(
        {"action": "CONCLUDE", "reasoning": "abstaining", "disposition": "INSUFFICIENT_EVIDENCE"}
    )

    class StubRunner:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def run_async(self, **kwargs):
            del kwargs
            yield SimpleNamespace(
                error_code=None,
                error_message=None,
                model_version="gemini-3.6-flash",
                usage_metadata=None,
                content=SimpleNamespace(parts=[SimpleNamespace(text=payload)]),
                is_final_response=lambda: True,
            )

    monkeypatch.setattr(adk_module, "Runner", StubRunner)
    model = AdkGeminiClaimInvestigator()
    request = _turn_request()

    response = asyncio.run(model.invoke(request))

    assert response.text == payload
    assert response.usage.model_name == "gemini-3.6-flash"
    assert InvestigatorDecision.model_validate_json(response.text)


def test_adk_invoke_normalizes_a_provider_event_failure(monkeypatch) -> None:
    class FailingRunner:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def run_async(self, **kwargs):
            del kwargs
            yield SimpleNamespace(
                error_code="RESOURCE_EXHAUSTED",
                error_message="quota",
                model_version=None,
                usage_metadata=None,
                content=None,
                is_final_response=lambda: False,
            )

    monkeypatch.setattr(adk_module, "Runner", FailingRunner)
    model = AdkGeminiClaimInvestigator()

    with pytest.raises(ClaimInvestigatorInvocationError):
        asyncio.run(model.invoke(_turn_request()))


def test_adk_invoke_without_a_final_response_is_an_error(monkeypatch) -> None:
    class SilentRunner:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def run_async(self, **kwargs):
            del kwargs
            return
            yield  # pragma: no cover - generator shape only

    monkeypatch.setattr(adk_module, "Runner", SilentRunner)
    model = AdkGeminiClaimInvestigator()

    with pytest.raises(ClaimInvestigatorInvocationError, match="no final text"):
        asyncio.run(model.invoke(_turn_request()))


def _turn_request() -> InvestigatorTurnRequest:
    base = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.BASE, sha="a" * 40),
        files={"pkg/console.py": _BASE},
    )
    head = RepositoryIndex.from_files(
        revision=Revision(role=RevisionRole.HEAD, sha="b" * 40),
        files={"pkg/console.py": _HEAD},
    )
    planner = DeterministicInvestigationPlanner(
        investigator=RepositoryInvestigator(base=base, head=head)
    )
    return InvestigatorTurnRequest(
        narrative=PullRequestNarrative.from_untrusted(title="Terminate rendered lines"),
        starting_context=planner.build(),
    )


# ---------------------------------------------------------------------------------
# Downstream contract
# ---------------------------------------------------------------------------------


def test_investigator_emits_the_same_result_type_the_pipeline_already_consumes() -> None:
    """Drop-in: the downstream pipeline cannot tell which claim source produced this."""
    investigator, _ = build_investigator(conclude())

    result = run(investigator)

    assert isinstance(result.agent_result, ClaimAgentResult)
    # Structural compatibility with the existing agent's output contract.
    existing_fields = set(ClaimAgentResult.model_fields)
    assert existing_fields == {"selection", "usage", "raw_response_sha256"}
    assert result.agent_result.selection.claim is not None


def test_the_produced_claim_satisfies_the_existing_claim_contract() -> None:
    """Every field downstream reads must be populated and valid."""
    investigator, _ = build_investigator(conclude())
    claim = run(investigator).agent_result.selection.claim

    assert claim is not None
    assert claim.claim_id == "claim-selected-behavior"
    assert claim.testability.value == "TESTABLE"
    assert 0.65 <= claim.confidence <= 1.0
    assert claim.affected_symbols and claim.supporting_context
    # Round-trips through the durable evidence encoding used by the report.
    assert claim.model_validate_json(claim.model_dump_json()) == claim


def test_evidence_gate_modules_are_not_imported_or_altered_by_phase_two() -> None:
    """Phase 2 must be invisible to the proof gate.

    A structural check rather than a behavioral one: the claim-discovery modules must not
    reach into evidence classification, execution, or candidate validation at all.
    """
    import patchproof.claim_investigation as investigation
    import patchproof.claim_investigator as investigator_module
    import patchproof.investigation_toolbox as toolbox_module

    forbidden = {"patchproof.evidence", "patchproof.challenge", "patchproof.pytest_runner"}
    for module in (investigation, investigator_module, toolbox_module):
        source = module.__file__
        assert source is not None
        text = pathlib.Path(source).read_text(encoding="utf-8")
        for name in forbidden:
            assert f"import {name}" not in text
            assert f"from {name}" not in text


def test_existing_claim_agent_remains_available_and_unmodified_in_contract() -> None:
    """Phase 2 adds a path; it does not remove the v1 one."""
    assert hasattr(BehavioralClaimAgent, "select_claim")
    # The legacy agent still consumes the retrieval-based context, unchanged.
    import inspect

    signature = inspect.signature(BehavioralClaimAgent.select_claim)
    assert set(signature.parameters) == {"self", "context", "narrative"}
