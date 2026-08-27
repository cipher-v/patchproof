"""Tests for the local Google ADK adapter configuration and event normalization."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai import types

import patchproof.adk_claim_agent as adk_module
from patchproof.adk_claim_agent import (
    CLAIM_AGENT_INSTRUCTION,
    DEFAULT_CLAIM_MAX_OUTPUT_TOKENS,
    DEFAULT_CLAIM_MODEL,
    AdkGeminiClaimModel,
    ClaimAgentInvocationError,
)
from patchproof.claim_agent import (
    ClaimAgentInput,
    ClaimSelectionDisposition,
    ClaimSelectionDraft,
    PullRequestNarrative,
)
from patchproof.context_retrieval import PullRequestContext, RetrievalStats


def _request() -> ClaimAgentInput:
    return ClaimAgentInput(
        narrative=PullRequestNarrative.from_untrusted(title="Refactor without behavior change"),
        context=PullRequestContext(
            base_sha="a" * 40,
            head_sha="b" * 40,
            diff="",
            changed_files=(),
            changed_symbols=(),
            snippets=(),
            stats=RetrievalStats(
                changed_file_count=0,
                changed_python_file_count=0,
                python_path_count=0,
                test_files_scanned=0,
                reference_files_scanned=0,
                omitted_changed_files=0,
                truncated=False,
            ),
        ),
    )


def test_adk_agent_is_one_stateless_tool_free_structured_agent() -> None:
    model = AdkGeminiClaimModel()

    assert model.model_name == DEFAULT_CLAIM_MODEL == "gemini-3.6-flash"
    assert model.agent.name == "patchproof_agent"
    assert model.agent.model == DEFAULT_CLAIM_MODEL
    assert model.agent.input_schema is ClaimAgentInput
    assert model.agent.output_schema is ClaimSelectionDraft
    assert model.agent.include_contents == "none"
    assert model.agent.tools == []
    assert model.agent.timeout == 60.0
    assert model.agent.generate_content_config.temperature == 0.1
    assert (
        model.agent.generate_content_config.max_output_tokens
        == DEFAULT_CLAIM_MAX_OUTPUT_TOKENS
        == 2_048
    )
    assert (
        model.agent.generate_content_config.thinking_config.thinking_level
        is types.ThinkingLevel.LOW
    )
    assert "UNTRUSTED DATA" in CLAIM_AGENT_INSTRUCTION
    assert "no tools" in CLAIM_AGENT_INSTRUCTION
    assert "never hidden chain-of-thought" in CLAIM_AGENT_INSTRUCTION


def test_claim_output_schema_uses_gemini_compatible_numeric_bounds() -> None:
    schema_json = json.dumps(ClaimSelectionDraft.model_json_schema())

    assert '"minimum": 1' in schema_json
    assert "exclusiveMinimum" not in schema_json
    assert "additionalProperties" not in schema_json


@pytest.mark.parametrize("model_name", ["gemini-3.1-pro", "gemini-flash-latest", "other-3.6"])
def test_adapter_requires_an_explicit_gemini_3_5_or_newer_model(model_name: str) -> None:
    with pytest.raises(ValueError, match=r"Gemini 3\.5-or-newer"):
        AdkGeminiClaimModel(model_name=model_name)


def test_adapter_collects_final_text_and_usage_without_a_network_call(monkeypatch) -> None:
    selection = ClaimSelectionDraft(
        disposition=ClaimSelectionDisposition.INSUFFICIENT_EVIDENCE,
        explanation="No changed behavior is present.",
    )

    class FakeSessionService:
        async def create_session(self, **_kwargs):
            return object()

    class FakeEvent:
        error_code = None
        error_message = None
        model_version = "gemini-3.6-flash-001"
        usage_metadata = SimpleNamespace(
            prompt_token_count=88,
            candidates_token_count=21,
            total_token_count=109,
            cached_content_token_count=3,
        )
        content = types.Content(parts=[types.Part(text=selection.model_dump_json())])

        @staticmethod
        def is_final_response() -> bool:
            return True

    class FakeRunner:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run_async(self, **_kwargs):
            yield FakeEvent()

    monkeypatch.setattr(adk_module, "InMemorySessionService", FakeSessionService)
    monkeypatch.setattr(adk_module, "Runner", FakeRunner)
    response = asyncio.run(AdkGeminiClaimModel().invoke(_request()))

    assert response.text == selection.model_dump_json()
    assert response.usage.model_version == "gemini-3.6-flash-001"
    assert response.usage.prompt_tokens == 88
    assert response.usage.output_tokens == 21
    assert response.usage.total_tokens == 109
    assert response.usage.cached_tokens == 3


def test_adapter_normalizes_runner_failure_without_leaking_event_content(monkeypatch) -> None:
    class FakeSessionService:
        async def create_session(self, **_kwargs):
            return object()

    class FailingRunner:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run_async(self, **_kwargs):
            raise RuntimeError("sensitive provider detail")
            yield

    monkeypatch.setattr(adk_module, "InMemorySessionService", FakeSessionService)
    monkeypatch.setattr(adk_module, "Runner", FailingRunner)

    with pytest.raises(ClaimAgentInvocationError, match="did not complete") as captured:
        asyncio.run(AdkGeminiClaimModel().invoke(_request()))
    assert "sensitive provider detail" not in str(captured.value)


def test_adapter_normalizes_session_failure_without_leaking_provider_detail(monkeypatch) -> None:
    class FailingSessionService:
        async def create_session(self, **_kwargs):
            raise RuntimeError("sensitive session detail")

    monkeypatch.setattr(adk_module, "InMemorySessionService", FailingSessionService)

    with pytest.raises(ClaimAgentInvocationError, match="did not complete") as captured:
        asyncio.run(AdkGeminiClaimModel().invoke(_request()))
    assert "sensitive session detail" not in str(captured.value)
