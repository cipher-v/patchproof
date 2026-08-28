"""Explicit Gemini provider configuration shared by every semantic adapter."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import google.auth
from google.adk.models import Gemini
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchproof.model_reliability import (
    is_transient_event_code,
    is_transient_provider_error,
)

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GeminiProviderSurface(StrEnum):
    """Stable Gemini API surfaces supported by PatchProof."""

    GEMINI_DEVELOPER_API = "GEMINI_DEVELOPER_API"
    VERTEX_AI = "VERTEX_AI"


class GeminiProviderConfigurationError(ValueError):
    """Raised before invocation when provider configuration is incomplete."""


class GeminiProviderAuthenticationError(RuntimeError):
    """Raised when the selected Vertex path has no Application Default Credentials."""


class GeminiProviderConfig(BaseModel):
    """Non-secret provider coordinates used identically by all semantic tasks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_surface: GeminiProviderSurface = GeminiProviderSurface.GEMINI_DEVELOPER_API
    project: str | None = Field(default=None, max_length=256)
    location: str | None = Field(default=None, max_length=128)

    @field_validator("project", "location")
    @classmethod
    def normalize_optional_coordinate(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_surface_coordinates(self) -> GeminiProviderConfig:
        if self.provider_surface is GeminiProviderSurface.VERTEX_AI:
            if self.project is None:
                raise GeminiProviderConfigurationError("Vertex AI project is required")
            if self.location is None:
                raise GeminiProviderConfigurationError("Vertex AI location is required")
        elif self.project is not None or self.location is not None:
            raise GeminiProviderConfigurationError(
                "Gemini Developer API configuration must not include Vertex project or location"
            )
        return self

    @classmethod
    def developer_api(cls) -> GeminiProviderConfig:
        return cls(provider_surface=GeminiProviderSurface.GEMINI_DEVELOPER_API)

    @classmethod
    def from_environment(cls) -> GeminiProviderConfig:
        raw_surface = os.environ.get(
            "PATCHPROOF_GEMINI_PROVIDER",
            GeminiProviderSurface.GEMINI_DEVELOPER_API,
        ).strip()
        try:
            surface = GeminiProviderSurface(raw_surface.upper())
        except ValueError as error:
            choices = ", ".join(item.value for item in GeminiProviderSurface)
            raise GeminiProviderConfigurationError(
                f"invalid PATCHPROOF_GEMINI_PROVIDER; expected one of: {choices}"
            ) from error
        if surface is GeminiProviderSurface.GEMINI_DEVELOPER_API:
            return cls.developer_api()
        return cls(
            provider_surface=surface,
            project=(
                os.environ.get("PATCHPROOF_VERTEX_PROJECT")
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
            ),
            location=(
                os.environ.get("PATCHPROOF_VERTEX_LOCATION")
                or os.environ.get("GOOGLE_CLOUD_LOCATION")
            ),
        )

    def adk_model(self, model_name: str) -> Gemini:
        """Create one ADK Gemini model with an explicit, non-global provider surface."""
        client_kwargs: dict[str, Any] = {
            "enterprise": self.provider_surface is GeminiProviderSurface.VERTEX_AI,
        }
        if self.provider_surface is GeminiProviderSurface.VERTEX_AI:
            client_kwargs.update(project=self.project, location=self.location)
        return Gemini(
            model=model_name,
            client_kwargs=client_kwargs,
            retry_options=types.HttpRetryOptions(attempts=1),
        )


class VertexAuthenticationPreflight(BaseModel):
    """Safe result of locating ADC without refreshing or printing credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_surface: GeminiProviderSurface
    project: str
    location: str
    credentials_available: bool


CredentialLoader = Callable[..., tuple[Credentials, str | None]]


def preflight_vertex_authentication(
    config: GeminiProviderConfig,
    *,
    credential_loader: CredentialLoader | None = None,
) -> VertexAuthenticationPreflight:
    """Locate ADC before a Vertex run without making a model or quota request."""
    if config.provider_surface is not GeminiProviderSurface.VERTEX_AI:
        raise GeminiProviderConfigurationError("Vertex authentication preflight requires VERTEX_AI")
    loader = credential_loader or google.auth.default
    try:
        credentials, _ = loader(
            scopes=(_CLOUD_PLATFORM_SCOPE,),
            quota_project_id=config.project,
        )
    except (DefaultCredentialsError, RefreshError) as error:
        raise GeminiProviderAuthenticationError(
            "Vertex AI Application Default Credentials are unavailable"
        ) from error
    if credentials is None:
        raise GeminiProviderAuthenticationError(
            "Vertex AI Application Default Credentials are unavailable"
        )
    return VertexAuthenticationPreflight(
        provider_surface=config.provider_surface,
        project=config.project,
        location=config.location,
        credentials_available=True,
    )


@dataclass(frozen=True, slots=True)
class NormalizedGeminiUsage:
    """Usage fields exposed by either Gemini surface, without invented values."""

    prompt_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None

    def merge(self, metadata: object) -> NormalizedGeminiUsage:
        return NormalizedGeminiUsage(
            prompt_tokens=_maximum(
                self.prompt_tokens,
                _usage_value(
                    metadata,
                    "prompt_token_count",
                    "promptTokenCount",
                    "input_token_count",
                    "inputTokenCount",
                ),
            ),
            output_tokens=_maximum(
                self.output_tokens,
                _usage_value(
                    metadata,
                    "candidates_token_count",
                    "candidatesTokenCount",
                    "output_token_count",
                    "outputTokenCount",
                ),
            ),
            total_tokens=_maximum(
                self.total_tokens,
                _usage_value(metadata, "total_token_count", "totalTokenCount"),
            ),
            cached_tokens=_maximum(
                self.cached_tokens,
                _usage_value(
                    metadata,
                    "cached_content_token_count",
                    "cachedContentTokenCount",
                ),
            ),
        )


def _usage_value(metadata: object, *names: str) -> int | None:
    for name in names:
        value = metadata.get(name) if isinstance(metadata, dict) else getattr(metadata, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _maximum(current: int | None, candidate: int | None) -> int | None:
    if candidate is None:
        return current
    return candidate if current is None else max(current, candidate)


@dataclass(frozen=True, slots=True)
class NormalizedProviderFailure:
    """Safe provider failure category plus the existing bounded retry decision."""

    message: str
    retryable: bool


def normalize_provider_failure(
    config: GeminiProviderConfig,
    error: BaseException,
    *,
    task: str,
) -> NormalizedProviderFailure:
    """Normalize Developer API or Vertex exceptions without retaining provider prose."""
    retryable = is_transient_provider_error(error)
    if config.provider_surface is GeminiProviderSurface.GEMINI_DEVELOPER_API:
        return NormalizedProviderFailure(
            message=f"Gemini Developer API {task} invocation did not complete",
            retryable=retryable,
        )
    code = _provider_status_code(error)
    if isinstance(error, (DefaultCredentialsError, RefreshError)) or code == 401:
        message = "Vertex AI authentication failed; Application Default Credentials are unavailable"
    elif code == 403:
        message = (
            "Vertex AI API is disabled or the runtime identity lacks model invocation permission"
        )
    elif code == 404:
        message = "Gemini model is unavailable in the configured Vertex project or location"
    elif code == 400:
        message = "Vertex AI rejected the Gemini model invocation configuration"
    elif retryable:
        message = "Vertex AI model invocation is temporarily unavailable"
    else:
        message = "Vertex AI model invocation did not complete"
    return NormalizedProviderFailure(message=f"{message} during {task}", retryable=retryable)


def normalize_provider_event_failure(
    config: GeminiProviderConfig,
    event_code: str | None,
    *,
    task: str,
) -> NormalizedProviderFailure:
    """Normalize ADK event status codes using the same bounded retry contract."""
    normalized = (event_code or "").strip().upper()
    retryable = is_transient_event_code(normalized)
    if config.provider_surface is GeminiProviderSurface.GEMINI_DEVELOPER_API:
        message = f"Gemini Developer API {task} invocation failed"
    elif normalized in {"401", "UNAUTHENTICATED"}:
        message = "Vertex AI authentication failed; Application Default Credentials are unavailable"
    elif normalized in {"403", "PERMISSION_DENIED"}:
        message = (
            "Vertex AI API is disabled or the runtime identity lacks model invocation permission"
        )
    elif normalized in {"404", "NOT_FOUND"}:
        message = "Gemini model is unavailable in the configured Vertex project or location"
    elif normalized in {"400", "INVALID_ARGUMENT"}:
        message = "Vertex AI rejected the Gemini model invocation configuration"
    elif retryable:
        message = "Vertex AI model invocation is temporarily unavailable"
    else:
        message = "Vertex AI model invocation failed"
    return NormalizedProviderFailure(message=f"{message} during {task}", retryable=retryable)


def _provider_status_code(error: BaseException) -> int | None:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, genai_errors.APIError):
            return current.code
        current = current.__cause__ or current.__context__
    return None


def main() -> int:
    """Run a credential-only Vertex preflight without invoking a model."""
    config = GeminiProviderConfig.from_environment()
    result = preflight_vertex_authentication(config)
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
