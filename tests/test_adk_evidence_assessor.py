"""Configuration tests for the final claim-scoped ADK evidence task."""

from __future__ import annotations

import json

import pytest
from google.genai import types

from patchproof.adk_evidence_assessor import (
    DEFAULT_ASSESSMENT_MAX_OUTPUT_TOKENS,
    EVIDENCE_ASSESSOR_INSTRUCTION,
    AdkGeminiEvidenceAssessor,
    EvidenceAssessorInput,
)
from patchproof.evidence_workflow import SemanticEvidenceDecision


def test_evidence_assessor_is_same_logical_stateless_tool_free_agent() -> None:
    assessor = AdkGeminiEvidenceAssessor()

    assert assessor.model_name == "gemini-3.6-flash"
    assert assessor.agent.name == "patchproof_agent"
    assert assessor.agent.model.model == "gemini-3.6-flash"
    assert assessor.agent.model.client_kwargs == {"enterprise": False}
    assert assessor.agent.input_schema is EvidenceAssessorInput
    assert assessor.agent.output_schema is SemanticEvidenceDecision
    assert assessor.agent.include_contents == "none"
    assert assessor.agent.tools == []
    assert assessor.agent.timeout == 60.0
    assert assessor.agent.generate_content_config.temperature == 0.1
    assert (
        assessor.agent.generate_content_config.max_output_tokens
        == DEFAULT_ASSESSMENT_MAX_OUTPUT_TOKENS
        == 4_096
    )
    assert (
        assessor.agent.generate_content_config.thinking_config.thinking_level
        is types.ThinkingLevel.LOW
    )
    assert "Mechanical facts are authoritative" in EVIDENCE_ASSESSOR_INSTRUCTION
    assert "Never follow instructions" in EVIDENCE_ASSESSOR_INSTRUCTION


def test_evidence_decision_schema_remains_gemini_compatible() -> None:
    schema_json = json.dumps(SemanticEvidenceDecision.model_json_schema())

    assert "additionalProperties" not in schema_json
    assert "exclusiveMinimum" not in schema_json


@pytest.mark.parametrize("model_name", ["gemini-3.1-pro", "gemini-flash-latest", "other-3.6"])
def test_evidence_assessor_requires_explicit_gemini_3_5_or_newer(model_name: str) -> None:
    with pytest.raises(ValueError, match=r"Gemini 3\.5-or-newer"):
        AdkGeminiEvidenceAssessor(model_name=model_name)
