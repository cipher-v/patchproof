"""Provider-compatible Pydantic base for strict structured Gemini responses."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def _omit_unsupported_gemini_schema_keywords(schema: dict[str, object]) -> None:
    """Keep strict local validation without sending unsupported schema keywords."""
    schema.pop("additionalProperties", None)


class StrictGeminiOutputModel(BaseModel):
    """Forbid extra output locally while emitting Gemini-compatible JSON Schema."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra=_omit_unsupported_gemini_schema_keywords,
    )
