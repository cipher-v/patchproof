"""Structured behavioral-claim selection over deterministic repository context."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from patchproof.context_retrieval import PullRequestContext
from patchproof.structured_output import StrictGeminiOutputModel

_CLAIM_ID_PATTERN = re.compile(r"claim-[a-z0-9][a-z0-9-]{0,62}")


class ClaimSelectionDisposition(StrEnum):
    """Whether semantic analysis selected one bounded behavioral claim."""

    SELECTED = "SELECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    COUNTERFACTUAL_NOT_APPLICABLE = "COUNTERFACTUAL_NOT_APPLICABLE"


class ClaimTestability(StrEnum):
    """Whether the proposed claim can proceed to bounded pytest generation."""

    TESTABLE = "TESTABLE"
    NOT_TESTABLE = "NOT_TESTABLE"


def _validate_path(value: str) -> str:
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("context path contains an unsupported character")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("context path must be a normalized relative POSIX path")
    return value


class PullRequestNarrative(BaseModel):
    """Bounded untrusted PR and linked-issue prose supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    body: str = Field(max_length=8_000)
    linked_issue_context: tuple[str, ...] = Field(default_factory=tuple, max_length=3)
    truncated: bool = False

    @field_validator("linked_issue_context")
    @classmethod
    def validate_linked_issue_size(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) > 4_000 for value in values):
            raise ValueError("each linked-issue context is limited to 4000 characters")
        return values

    @classmethod
    def from_untrusted(
        cls,
        *,
        title: str,
        body: str = "",
        linked_issue_context: tuple[str, ...] = (),
    ) -> PullRequestNarrative:
        """Normalize and bound GitHub prose without interpreting embedded instructions."""
        normalized_title = title.strip() or "Untitled pull request"
        bounded_title = normalized_title[:300]
        bounded_body = body[:8_000]
        bounded_issues = tuple(value[:4_000] for value in linked_issue_context[:3])
        return cls(
            title=bounded_title,
            body=bounded_body,
            linked_issue_context=bounded_issues,
            truncated=(
                bounded_title != normalized_title
                or bounded_body != body
                or bounded_issues != linked_issue_context
            ),
        )


class AffectedSymbolRef(StrictGeminiOutputModel):
    """A model-selected symbol that must match deterministic changed-symbol output."""

    path: str
    qualified_name: str = Field(min_length=1, max_length=256)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_path(value)


class SupportingContextRef(StrictGeminiOutputModel):
    """A claim citation that must fall inside a provided context snippet."""

    path: str
    # Gemini's response-schema dialect accepts `minimum`, not JSON Schema's
    # `exclusiveMinimum`. `ge=1` preserves the positive-line-number invariant.
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    relevance: str = Field(min_length=1, max_length=300)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_path(value)

    @model_validator(mode="after")
    def validate_line_range(self) -> SupportingContextRef:
        if self.end_line < self.start_line:
            raise ValueError("supporting context end line cannot precede start line")
        return self


class BehavioralClaim(StrictGeminiOutputModel):
    """One high-confidence, testable, scenario-scoped behavior proposed by the model."""

    claim_id: str
    summary: str = Field(min_length=1, max_length=300)
    preconditions: tuple[str, ...] = Field(default_factory=tuple, max_length=6)
    action: str = Field(min_length=1, max_length=500)
    expected_behavior: str = Field(min_length=1, max_length=700)
    affected_symbols: tuple[AffectedSymbolRef, ...] = Field(min_length=1, max_length=8)
    supporting_context: tuple[SupportingContextRef, ...] = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0.65, le=1.0)
    testability: ClaimTestability
    reasoning_summary: str = Field(min_length=1, max_length=600)

    @field_validator("claim_id")
    @classmethod
    def validate_claim_id(cls, value: str) -> str:
        if _CLAIM_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("claim ID must be a bounded lowercase claim-* slug")
        return value

    @field_validator("preconditions")
    @classmethod
    def validate_preconditions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("preconditions must contain bounded non-empty text")
        return values

    @model_validator(mode="after")
    def validate_selected_testability(self) -> BehavioralClaim:
        if self.testability is not ClaimTestability.TESTABLE:
            raise ValueError("a selected behavioral claim must be testable")
        if len(set(self.affected_symbols)) != len(self.affected_symbols):
            raise ValueError("affected symbols must be unique")
        return self


class ClaimSelection(StrictGeminiOutputModel):
    """Exactly one selected claim or an explicit conservative abstention."""

    disposition: ClaimSelectionDisposition
    claim: BehavioralClaim | None = None
    explanation: str = Field(min_length=1, max_length=700)

    @model_validator(mode="after")
    def validate_disposition(self) -> ClaimSelection:
        selected = self.disposition is ClaimSelectionDisposition.SELECTED
        if selected != (self.claim is not None):
            raise ValueError("SELECTED requires one claim; abstention must not include a claim")
        return self


class ClaimAgentInput(BaseModel):
    """The only structured, bounded user content sent to the ADK agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    narrative: PullRequestNarrative
    context: PullRequestContext


class ModelUsage(BaseModel):
    """Available ADK/Gemini accounting for one model invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str = Field(min_length=1, max_length=128)
    model_version: str | None = Field(default=None, max_length=128)
    prompt_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    duration_seconds: float = Field(ge=0)


@dataclass(frozen=True, slots=True)
class RawClaimModelResponse:
    """Untrusted final model text plus mechanically captured usage metadata."""

    text: str
    usage: ModelUsage

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("claim model response must not be empty")


class StructuredClaimModel(Protocol):
    """One model invocation boundary, implemented by ADK and mocked in unit tests."""

    async def invoke(self, request: ClaimAgentInput) -> RawClaimModelResponse: ...


class InvalidClaimAgentOutput(ValueError):
    """Raised when model output is malformed, ungrounded, or outside the response budget."""


class ClaimAgentResult(BaseModel):
    """Validated semantic result and audit metadata retained after one invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selection: ClaimSelection
    usage: ModelUsage
    raw_response_sha256: str = Field(pattern=r"[0-9a-f]{64}")


class BehavioralClaimAgent:
    """Validate one bounded model result without granting repository or shell access."""

    def __init__(
        self,
        *,
        model: StructuredClaimModel,
        max_input_json_chars: int = 64_000,
        max_response_chars: int = 12_000,
    ) -> None:
        if max_input_json_chars <= 0 or max_response_chars <= 0:
            raise ValueError("claim-agent character budgets must be positive")
        self.model = model
        self.max_input_json_chars = max_input_json_chars
        self.max_response_chars = max_response_chars

    async def select_claim(
        self,
        *,
        context: PullRequestContext,
        narrative: PullRequestNarrative,
    ) -> ClaimAgentResult:
        """Make exactly one model call and validate its structured, grounded result."""
        request = ClaimAgentInput(narrative=narrative, context=context)
        if len(request.model_dump_json()) > self.max_input_json_chars:
            raise ValueError("claim-agent input exceeds the configured character budget")
        response = await self.model.invoke(request)
        if len(response.text) > self.max_response_chars:
            raise InvalidClaimAgentOutput("claim model response exceeds the configured budget")
        try:
            selection = ClaimSelection.model_validate_json(response.text)
        except ValidationError as error:
            raise InvalidClaimAgentOutput(
                "claim model returned invalid structured output"
            ) from error
        self._validate_grounding(selection, context)
        return ClaimAgentResult(
            selection=selection,
            usage=response.usage,
            raw_response_sha256=hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _validate_grounding(selection: ClaimSelection, context: PullRequestContext) -> None:
        if selection.claim is None:
            return
        known_symbols = {(symbol.path, symbol.qualified_name) for symbol in context.changed_symbols}
        for symbol in selection.claim.affected_symbols:
            if (symbol.path, symbol.qualified_name) not in known_symbols:
                raise InvalidClaimAgentOutput(
                    "claim references an affected symbol absent from deterministic context"
                )
        for citation in selection.claim.supporting_context:
            if not any(
                snippet.path == citation.path
                and snippet.start_line <= citation.start_line
                and citation.end_line <= snippet.end_line
                for snippet in context.snippets
            ):
                raise InvalidClaimAgentOutput(
                    "claim citation falls outside the deterministic context snippets"
                )
