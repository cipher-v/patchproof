"""Tests for the ADK adapter used by the structured candidate-test task."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai import types

import patchproof.adk_test_agent as adk_module
from patchproof.adk_test_agent import (
    CANDIDATE_AGENT_INSTRUCTION,
    DEFAULT_CANDIDATE_MAX_OUTPUT_TOKENS,
    AdkGeminiCandidateModel,
    CandidateAgentInvocationError,
)
from patchproof.claim_agent import (
    AffectedSymbolRef,
    BehavioralClaim,
    ClaimTestability,
    SupportingContextRef,
)
from patchproof.context_retrieval import PullRequestContext, RetrievalStats
from patchproof.test_generation import CandidateModelRequest, CandidateTestDraft


def _request() -> CandidateModelRequest:
    claim = BehavioralClaim(
        claim_id="claim-example",
        summary="Example behavior changes.",
        action="Call the example function.",
        expected_behavior="The new result is returned.",
        affected_symbols=(AffectedSymbolRef(path="example.py", qualified_name="example"),),
        supporting_context=(
            SupportingContextRef(
                path="example.py",
                start_line=1,
                end_line=2,
                relevance="Changed behavior.",
            ),
        ),
        confidence=0.9,
        testability=ClaimTestability.TESTABLE,
        reasoning_summary="The function has observable deterministic behavior.",
    )
    return CandidateModelRequest(
        claim=claim,
        context=PullRequestContext(
            base_sha="a" * 40,
            head_sha="b" * 40,
            diff="@@ -1 +1 @@\n-return 1\n+return 2",
            changed_files=(),
            changed_symbols=(),
            snippets=(),
            stats=RetrievalStats(
                changed_file_count=1,
                changed_python_file_count=1,
                python_path_count=1,
                test_files_scanned=0,
                reference_files_scanned=0,
                omitted_changed_files=0,
                truncated=False,
            ),
        ),
        allowed_test_paths=("tests/patchproof_generated/",),
    )


def _draft() -> CandidateTestDraft:
    return CandidateTestDraft(
        source="def test_patchproof_generated_behavior() -> None:\n    assert 1 + 1 == 2\n",
        rationale="A narrow deterministic example.",
    )


def test_candidate_adapter_is_the_same_logical_stateless_tool_free_agent() -> None:
    model = AdkGeminiCandidateModel()

    assert model.model_name == "gemini-3.6-flash"
    assert model.agent.name == "patchproof_agent"
    assert model.agent.model == "gemini-3.6-flash"
    assert model.agent.input_schema is CandidateModelRequest
    assert model.agent.output_schema is CandidateTestDraft
    assert model.agent.include_contents == "none"
    assert model.agent.tools == []
    assert model.agent.timeout == 60.0
    assert model.agent.generate_content_config.temperature == 0.1
    assert (
        model.agent.generate_content_config.max_output_tokens
        == DEFAULT_CANDIDATE_MAX_OUTPUT_TOKENS
        == 12_000
    )
    assert (
        model.agent.generate_content_config.thinking_config.thinking_level
        is types.ThinkingLevel.LOW
    )
    assert "UNTRUSTED DATA" in CANDIDATE_AGENT_INSTRUCTION
    assert "must not propose or execute commands" in CANDIDATE_AGENT_INSTRUCTION
    assert "exactly two string fields named `source` and `rationale`" in (
        CANDIDATE_AGENT_INSTRUCTION
    )
    assert "meaningful deterministic assertion" in CANDIDATE_AGENT_INSTRUCTION
    assert "Compare the previous candidate's intended behavior" in CANDIDATE_AGENT_INSTRUCTION
    assert "which assumption in the generated test caused" in CANDIDATE_AGENT_INSTRUCTION
    assert "merely renaming variables or rewriting equivalent code" in (CANDIDATE_AGENT_INSTRUCTION)
    assert "expected domain exception" in CANDIDATE_AGENT_INSTRUCTION
    assert "Do not catch broad or unrelated exceptions" in CANDIDATE_AGENT_INSTRUCTION
    assert "return only one repaired candidate" in CANDIDATE_AGENT_INSTRUCTION
    assert "do not return diagnosis steps or a private reasoning field" in (
        CANDIDATE_AGENT_INSTRUCTION
    )
    assert "assert True" not in CANDIDATE_AGENT_INSTRUCTION


def test_candidate_output_schema_omits_provider_unsupported_keywords() -> None:
    schema = CandidateTestDraft.model_json_schema()
    schema_json = json.dumps(schema)

    assert "exclusiveMinimum" not in schema_json
    assert "additionalProperties" not in schema_json
    assert set(schema["properties"]) == {"source", "rationale"}
    assert set(schema["required"]) == {"source", "rationale"}


@pytest.mark.parametrize("model_name", ["gemini-3.1-pro", "gemini-flash-latest", "other-3.6"])
def test_candidate_adapter_requires_explicit_gemini_3_5_or_newer(model_name: str) -> None:
    with pytest.raises(ValueError, match=r"Gemini 3\.5-or-newer"):
        AdkGeminiCandidateModel(model_name=model_name)


def test_candidate_adapter_collects_final_text_and_usage_without_network(monkeypatch) -> None:
    draft = _draft()

    class FakeSessionService:
        async def create_session(self, **_kwargs):
            return object()

    class FakeEvent:
        error_code = None
        error_message = None
        model_version = "gemini-3.6-flash-001"
        usage_metadata = SimpleNamespace(
            prompt_token_count=140,
            candidates_token_count=80,
            total_token_count=220,
            cached_content_token_count=4,
        )
        content = types.Content(parts=[types.Part(text=draft.model_dump_json())])

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
    response = asyncio.run(AdkGeminiCandidateModel().invoke(_request()))

    assert response.text == draft.model_dump_json()
    assert response.usage.model_version == "gemini-3.6-flash-001"
    assert response.usage.prompt_tokens == 140
    assert response.usage.output_tokens == 80
    assert response.usage.total_tokens == 220
    assert response.usage.cached_tokens == 4


def test_candidate_adapter_normalizes_provider_failure(monkeypatch) -> None:
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

    with pytest.raises(CandidateAgentInvocationError, match="did not complete") as captured:
        asyncio.run(AdkGeminiCandidateModel().invoke(_request()))
    assert "sensitive provider detail" not in str(captured.value)
