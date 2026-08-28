"""Tests for explicit Gemini Developer API and Vertex AI infrastructure selection."""

from __future__ import annotations

import json

import pytest
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import DefaultCredentialsError
from google.genai.errors import ClientError
from pydantic import ValidationError

from patchproof.adk_claim_agent import (
    CLAIM_AGENT_INSTRUCTION,
    DEFAULT_CLAIM_MODEL,
    AdkGeminiClaimModel,
)
from patchproof.adk_evidence_assessor import (
    EVIDENCE_ASSESSOR_INSTRUCTION,
    AdkGeminiEvidenceAssessor,
)
from patchproof.adk_test_agent import CANDIDATE_AGENT_INSTRUCTION, AdkGeminiCandidateModel
from patchproof.gemini_provider import (
    GeminiProviderAuthenticationError,
    GeminiProviderConfig,
    GeminiProviderConfigurationError,
    GeminiProviderSurface,
    NormalizedGeminiUsage,
    normalize_provider_failure,
    preflight_vertex_authentication,
)


def _vertex() -> GeminiProviderConfig:
    return GeminiProviderConfig(
        provider_surface=GeminiProviderSurface.VERTEX_AI,
        project="example-project",
        location="global",
    )


def test_both_provider_surfaces_are_explicitly_configurable(monkeypatch) -> None:
    monkeypatch.setenv("PATCHPROOF_GEMINI_PROVIDER", "GEMINI_DEVELOPER_API")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ignored-for-developer-api")
    developer = GeminiProviderConfig.from_environment()

    monkeypatch.setenv("PATCHPROOF_GEMINI_PROVIDER", "VERTEX_AI")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    vertex = GeminiProviderConfig.from_environment()

    assert developer == GeminiProviderConfig.developer_api()
    assert vertex == _vertex()
    assert developer.adk_model(DEFAULT_CLAIM_MODEL).client_kwargs == {"enterprise": False}
    assert vertex.adk_model(DEFAULT_CLAIM_MODEL).client_kwargs == {
        "enterprise": True,
        "project": "example-project",
        "location": "global",
    }
    assert developer.adk_model(DEFAULT_CLAIM_MODEL).retry_options.attempts == 1
    assert vertex.adk_model(DEFAULT_CLAIM_MODEL).retry_options.attempts == 1


def test_invalid_provider_and_missing_vertex_coordinates_fail_clearly(monkeypatch) -> None:
    monkeypatch.setenv("PATCHPROOF_GEMINI_PROVIDER", "unknown")
    with pytest.raises(
        GeminiProviderConfigurationError, match="invalid PATCHPROOF_GEMINI_PROVIDER"
    ):
        GeminiProviderConfig.from_environment()

    with pytest.raises(ValidationError, match="Vertex AI project is required"):
        GeminiProviderConfig(provider_surface=GeminiProviderSurface.VERTEX_AI, location="global")
    with pytest.raises(ValidationError, match="Vertex AI location is required"):
        GeminiProviderConfig(
            provider_surface=GeminiProviderSurface.VERTEX_AI,
            project="example-project",
        )


def test_provider_config_contains_coordinates_but_no_credentials_or_api_keys() -> None:
    config = _vertex()
    serialized = json.dumps(config.model_dump(mode="json"))

    assert GeminiProviderConfig.model_fields["project"].default is None
    assert GeminiProviderConfig.model_fields["location"].default is None
    assert "example-project" in serialized
    assert "api_key" not in serialized.lower()
    assert "credential" not in serialized.lower()
    assert "token" not in serialized.lower()


def test_vertex_adc_preflight_reports_only_safe_configuration_facts() -> None:
    captured = {}

    def load_credentials(**kwargs):
        captured.update(kwargs)
        return AnonymousCredentials(), "ambient-project"

    result = preflight_vertex_authentication(_vertex(), credential_loader=load_credentials)

    assert result.model_dump(mode="json") == {
        "provider_surface": "VERTEX_AI",
        "project": "example-project",
        "location": "global",
        "credentials_available": True,
    }
    assert captured["quota_project_id"] == "example-project"
    assert captured["scopes"] == ("https://www.googleapis.com/auth/cloud-platform",)


def test_vertex_adc_preflight_normalizes_missing_credentials() -> None:
    def missing_credentials(**_kwargs):
        raise DefaultCredentialsError("sensitive lookup detail")

    with pytest.raises(
        GeminiProviderAuthenticationError,
        match="Application Default Credentials are unavailable",
    ) as captured:
        preflight_vertex_authentication(_vertex(), credential_loader=missing_credentials)
    assert "sensitive lookup detail" not in str(captured.value)


def test_vertex_and_developer_paths_preserve_the_same_semantic_agent_contracts() -> None:
    developer = GeminiProviderConfig.developer_api()
    vertex = _vertex()

    pairs = (
        (
            AdkGeminiClaimModel(provider_config=developer),
            AdkGeminiClaimModel(provider_config=vertex),
            CLAIM_AGENT_INSTRUCTION,
        ),
        (
            AdkGeminiCandidateModel(provider_config=developer),
            AdkGeminiCandidateModel(provider_config=vertex),
            CANDIDATE_AGENT_INSTRUCTION,
        ),
        (
            AdkGeminiEvidenceAssessor(provider_config=developer),
            AdkGeminiEvidenceAssessor(provider_config=vertex),
            EVIDENCE_ASSESSOR_INSTRUCTION,
        ),
    )
    for developer_adapter, vertex_adapter, instruction in pairs:
        assert developer_adapter.model_name == vertex_adapter.model_name == DEFAULT_CLAIM_MODEL
        assert developer_adapter.agent.input_schema is vertex_adapter.agent.input_schema
        assert developer_adapter.agent.output_schema is vertex_adapter.agent.output_schema
        assert (
            developer_adapter.agent.instruction == vertex_adapter.agent.instruction == instruction
        )
        assert developer_adapter.agent.tools == vertex_adapter.agent.tools == []
        assert developer_adapter.agent.generate_content_config == (
            vertex_adapter.agent.generate_content_config
        )


def test_vertex_usage_metadata_is_normalized_without_fabricating_missing_values() -> None:
    usage = NormalizedGeminiUsage().merge(
        {
            "promptTokenCount": 31,
            "candidatesTokenCount": 7,
            "totalTokenCount": 38,
        }
    )

    assert usage == NormalizedGeminiUsage(
        prompt_tokens=31,
        output_tokens=7,
        total_tokens=38,
        cached_tokens=None,
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (400, "rejected"),
        (401, "Application Default Credentials"),
        (403, "API is disabled or the runtime identity lacks"),
        (404, "configured Vertex project or location"),
    ],
)
def test_vertex_nontransient_failures_have_safe_actionable_categories(
    code: int,
    expected: str,
) -> None:
    failure = normalize_provider_failure(
        _vertex(),
        ClientError(code, {"message": "sensitive provider detail"}),
        task="claim selection",
    )

    assert expected in failure.message
    assert "sensitive provider detail" not in failure.message
    assert failure.retryable is False
