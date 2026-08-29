"""Reasoning budgets must be explicit, bounded, and applied by every ADK adapter."""

from __future__ import annotations

import pytest
from google.genai import types

from patchproof.adk_claim_agent import DEFAULT_CLAIM_MAX_OUTPUT_TOKENS
from patchproof.adk_evidence_assessor import DEFAULT_ASSESSMENT_MAX_OUTPUT_TOKENS
from patchproof.adk_test_agent import DEFAULT_CANDIDATE_MAX_OUTPUT_TOKENS
from patchproof.reasoning_budget import (
    DEFAULT_REASONING_BUDGETS,
    AgentTask,
    ReasoningBudget,
    budget_for,
)


def test_every_semantic_task_declares_exactly_one_budget() -> None:
    assert set(DEFAULT_REASONING_BUDGETS) == set(AgentTask)
    for task, budget in DEFAULT_REASONING_BUDGETS.items():
        assert budget.task is task


def test_reasoning_intensive_tasks_are_not_left_at_the_lowest_thinking_level() -> None:
    """Claim selection and repair carry the differential reasoning; they must not run at LOW."""
    assert budget_for(AgentTask.CLAIM_SELECTION).thinking_level is types.ThinkingLevel.MEDIUM
    assert budget_for(AgentTask.CANDIDATE_GENERATION).thinking_level is types.ThinkingLevel.MEDIUM


def test_assessment_stays_low_because_extra_deliberation_cannot_widen_evidence() -> None:
    assert budget_for(AgentTask.EVIDENCE_ASSESSMENT).thinking_level is types.ThinkingLevel.LOW


def test_claim_budget_leaves_room_for_the_differential_hypothesis_fields() -> None:
    assert budget_for(AgentTask.CLAIM_SELECTION).max_output_tokens >= 4_096


def test_adk_adapters_derive_their_defaults_from_the_shared_budgets() -> None:
    assert (
        budget_for(AgentTask.CLAIM_SELECTION).max_output_tokens == DEFAULT_CLAIM_MAX_OUTPUT_TOKENS
    )
    assert (
        budget_for(AgentTask.CANDIDATE_GENERATION).max_output_tokens
        == DEFAULT_CANDIDATE_MAX_OUTPUT_TOKENS
    )
    assert (
        budget_for(AgentTask.EVIDENCE_ASSESSMENT).max_output_tokens
        == DEFAULT_ASSESSMENT_MAX_OUTPUT_TOKENS
    )


def test_generation_config_carries_the_declared_settings() -> None:
    budget = budget_for(AgentTask.CLAIM_SELECTION)
    config = budget.generate_content_config()
    assert config.temperature == budget.temperature
    assert config.max_output_tokens == budget.max_output_tokens
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level is budget.thinking_level


@pytest.mark.parametrize(
    ("output_tokens", "temperature"),
    [(0, 0.1), (-1, 0.1), (1_024, -0.1), (1_024, 1.5)],
)
def test_out_of_range_budgets_are_rejected(output_tokens: int, temperature: float) -> None:
    with pytest.raises(ValueError):
        ReasoningBudget(
            task=AgentTask.CLAIM_SELECTION,
            thinking_level=types.ThinkingLevel.LOW,
            max_output_tokens=output_tokens,
            temperature=temperature,
        )
