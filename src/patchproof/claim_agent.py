"""Structured behavioral-claim selection over deterministic repository context."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from patchproof.context_retrieval import PullRequestContext
from patchproof.structured_output import StrictGeminiOutputModel

_CLAIM_ID_PATTERN = re.compile(r"claim-[a-z0-9][a-z0-9-]{0,62}")
_DIAGNOSTIC_FIELD_PATTERN = re.compile(r"[^A-Za-z0-9_.\-\[\]$]")
_SENSITIVE_DIAGNOSTIC_FIELD_PATTERN = re.compile(
    r"(?i)(?:authorization|api[_-]?key|token|secret|password|credential)"
)
_OPAQUE_DIAGNOSTIC_FIELD_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,}")
_CLAIM_DRAFT_FIELDS = frozenset({"disposition", "claim", "explanation"})
_DETERMINISTIC_CLAIM_ID = "claim-selected-behavior"


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
    observable_operation: str = Field(default="", max_length=300)
    trigger_condition: str = Field(default="", max_length=300)
    expected_head_observation: str = Field(default="", max_length=400)
    expected_base_hypothesis: str = Field(default="", max_length=400)
    shared_interface: str = Field(default="", max_length=256)
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


#: The differential hypothesis fields.
#:
#: Before these existed, every claim field described HEAD: summary, preconditions,
#: action, expected_behavior. There was nowhere to say what BASE was supposed to do
#: differently and nowhere to commit to an interface, so the schema could not express a
#: falsifiable differential hypothesis and the agent was never asked for one.
#:
#: `shared_interface` is the field that earns its place mechanically rather than as
#: prose: it is validated against the deterministic BASE/HEAD symbol partition, so a
#: claim aimed at something only HEAD defines is rejected at selection time instead of
#: wasting all three candidate attempts. `expected_base_hypothesis` gives the semantic
#: assessor an actual prediction to judge relatedness against.


class BehavioralClaimDraft(StrictGeminiOutputModel):
    """Provider-visible semantic claim fields without control-plane metadata."""

    summary: str = Field(min_length=1, max_length=220)
    observable_operation: str = Field(min_length=1, max_length=220)
    trigger_condition: str = Field(min_length=1, max_length=220)
    expected_head_observation: str = Field(min_length=1, max_length=280)
    expected_base_hypothesis: str = Field(min_length=1, max_length=280)
    shared_interface: str = Field(min_length=1, max_length=256)
    preconditions: tuple[str, ...] = Field(default_factory=tuple, max_length=4)
    action: str = Field(min_length=1, max_length=350)
    expected_behavior: str = Field(min_length=1, max_length=450)
    affected_symbols: tuple[AffectedSymbolRef, ...] = Field(min_length=1, max_length=4)
    supporting_context: tuple[SupportingContextRef, ...] = Field(min_length=1, max_length=4)
    confidence: float = Field(ge=0.65, le=1.0)

    @field_validator("preconditions")
    @classmethod
    def validate_preconditions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 220 for value in values):
            raise ValueError("preconditions must contain bounded non-empty text")
        return values

    @model_validator(mode="after")
    def validate_unique_symbols(self) -> BehavioralClaimDraft:
        if len(set(self.affected_symbols)) != len(self.affected_symbols):
            raise ValueError("affected symbols must be unique")
        return self


class ClaimSelectionDraft(StrictGeminiOutputModel):
    """Compact provider-visible selection normalized into a durable claim locally."""

    disposition: ClaimSelectionDisposition
    claim: BehavioralClaimDraft | None = None
    explanation: str = Field(min_length=1, max_length=350)

    @model_validator(mode="after")
    def validate_disposition(self) -> ClaimSelectionDraft:
        selected = self.disposition is ClaimSelectionDisposition.SELECTED
        if selected != (self.claim is not None):
            raise ValueError("SELECTED requires one claim; abstention must not include a claim")
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
    provider_attempts: int = Field(default=1, ge=1, le=2)


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
    """Malformed or ungrounded output with safe accounting facts retained for audit."""

    def __init__(
        self,
        message: str,
        *,
        usage: ModelUsage | None = None,
        raw_response_sha256: str | None = None,
        diagnostic: ClaimOutputDiagnostic | None = None,
    ) -> None:
        self.usage = usage
        self.raw_response_sha256 = raw_response_sha256
        self.diagnostic = diagnostic
        super().__init__(message)


class ClaimJsonParseStatus(StrEnum):
    """Whether claim-output JSON was parsed without retaining the raw body."""

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    INVALID = "INVALID"
    VALID = "VALID"


class ClaimOutputDiagnostic(BaseModel):
    """Bounded structure-only facts for invalid claim output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Literal["RESPONSE_BUDGET", "DRAFT_SCHEMA", "NORMALIZATION", "GROUNDING"]
    response_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    response_chars: int = Field(ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    raw_body_retained: Literal[False] = False
    response_budget_exceeded: bool
    json_parse_status: ClaimJsonParseStatus
    json_error_line: int | None = Field(default=None, ge=1)
    json_error_column: int | None = Field(default=None, ge=1)
    top_level_kind: Literal["object", "array", "string", "number", "boolean", "null", "unknown"]
    expected_fields_present: tuple[str, ...] = Field(max_length=3)
    expected_fields_missing: tuple[str, ...] = Field(max_length=3)
    unexpected_fields: tuple[str, ...] = Field(max_length=8)
    unexpected_field_count: int = Field(ge=0)
    validation_errors: tuple[str, ...] = Field(max_length=8)
    diagnostic_truncated: bool


def _json_kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def _safe_diagnostic_field(value: str) -> str:
    """Retain ordinary schema keys but hash secret-like or excessively long names."""
    sanitized = _DIAGNOSTIC_FIELD_PATTERN.sub("?", value)
    if (
        len(value) > 80
        or _SENSITIVE_DIAGNOSTIC_FIELD_PATTERN.search(sanitized)
        or _OPAQUE_DIAGNOSTIC_FIELD_PATTERN.fullmatch(value)
    ):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"sha256:{digest}"
    return sanitized


def _claim_output_diagnostic(
    *,
    response_text: str,
    response_sha256: str,
    output_tokens: int | None,
    stage: Literal["RESPONSE_BUDGET", "DRAFT_SCHEMA", "NORMALIZATION", "GROUNDING"],
    response_budget_exceeded: bool,
    validation_error: ValidationError | None = None,
) -> ClaimOutputDiagnostic:
    """Describe response shape and validation fields without retaining semantic text."""
    parsed: object = None
    parse_status = ClaimJsonParseStatus.NOT_ATTEMPTED
    error_line: int | None = None
    error_column: int | None = None
    if not response_budget_exceeded:
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as error:
            parse_status = ClaimJsonParseStatus.INVALID
            error_line = error.lineno
            error_column = error.colno
        else:
            parse_status = ClaimJsonParseStatus.VALID

    expected_present: tuple[str, ...] = ()
    expected_missing: tuple[str, ...] = tuple(sorted(_CLAIM_DRAFT_FIELDS))
    unexpected_fields: tuple[str, ...] = ()
    unexpected_count = 0
    diagnostic_truncated = False
    if isinstance(parsed, dict):
        keys = {key for key in parsed if isinstance(key, str)}
        expected_present = tuple(sorted(keys & _CLAIM_DRAFT_FIELDS))
        expected_missing = tuple(sorted(_CLAIM_DRAFT_FIELDS - keys))
        unexpected = sorted(keys - _CLAIM_DRAFT_FIELDS)
        unexpected_count = len(unexpected)
        unexpected_fields = tuple(_safe_diagnostic_field(key) for key in unexpected[:8])
        diagnostic_truncated = len(unexpected) > 8 or any(len(key) > 80 for key in unexpected[:8])

    errors: list[str] = []
    if validation_error is not None:
        for item in validation_error.errors(
            include_url=False, include_context=False, include_input=False
        ):
            location = ".".join(_safe_diagnostic_field(str(part)) for part in item["loc"]) or "$"
            error_type = _DIAGNOSTIC_FIELD_PATTERN.sub("?", item["type"])[:80]
            errors.append(f"{location}:{error_type}")
        if len(errors) > 8:
            diagnostic_truncated = True
            errors = errors[:8]

    return ClaimOutputDiagnostic(
        stage=stage,
        response_sha256=response_sha256,
        response_chars=len(response_text),
        output_tokens=output_tokens,
        response_budget_exceeded=response_budget_exceeded,
        json_parse_status=parse_status,
        json_error_line=error_line,
        json_error_column=error_column,
        top_level_kind=(
            _json_kind(parsed) if parse_status is ClaimJsonParseStatus.VALID else "unknown"
        ),
        expected_fields_present=expected_present,
        expected_fields_missing=expected_missing,
        unexpected_fields=unexpected_fields,
        unexpected_field_count=unexpected_count,
        validation_errors=tuple(errors),
        diagnostic_truncated=diagnostic_truncated,
    )


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
        max_response_chars: int = 6_000,
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
        response_hash = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
        if len(response.text) > self.max_response_chars:
            raise InvalidClaimAgentOutput(
                "claim model response exceeds the configured budget",
                usage=response.usage,
                raw_response_sha256=response_hash,
                diagnostic=_claim_output_diagnostic(
                    response_text=response.text,
                    response_sha256=response_hash,
                    output_tokens=response.usage.output_tokens,
                    stage="RESPONSE_BUDGET",
                    response_budget_exceeded=True,
                ),
            )
        try:
            draft = ClaimSelectionDraft.model_validate_json(response.text)
        except ValidationError as error:
            raise InvalidClaimAgentOutput(
                "claim model returned invalid structured output",
                usage=response.usage,
                raw_response_sha256=response_hash,
                diagnostic=_claim_output_diagnostic(
                    response_text=response.text,
                    response_sha256=response_hash,
                    output_tokens=response.usage.output_tokens,
                    stage="DRAFT_SCHEMA",
                    response_budget_exceeded=False,
                    validation_error=error,
                ),
            ) from None
        try:
            selection = self._normalize(draft)
        except ValidationError as error:
            raise InvalidClaimAgentOutput(
                "claim model output could not be normalized safely",
                usage=response.usage,
                raw_response_sha256=response_hash,
                diagnostic=_claim_output_diagnostic(
                    response_text=response.text,
                    response_sha256=response_hash,
                    output_tokens=response.usage.output_tokens,
                    stage="NORMALIZATION",
                    response_budget_exceeded=False,
                    validation_error=error,
                ),
            ) from None
        try:
            self._validate_grounding(selection, context)
        except InvalidClaimAgentOutput as error:
            raise InvalidClaimAgentOutput(
                str(error),
                usage=response.usage,
                raw_response_sha256=response_hash,
                diagnostic=_claim_output_diagnostic(
                    response_text=response.text,
                    response_sha256=response_hash,
                    output_tokens=response.usage.output_tokens,
                    stage="GROUNDING",
                    response_budget_exceeded=False,
                ),
            ) from error
        return ClaimAgentResult(
            selection=selection,
            usage=response.usage,
            raw_response_sha256=response_hash,
        )

    @staticmethod
    def _normalize(draft: ClaimSelectionDraft) -> ClaimSelection:
        if draft.claim is None:
            return ClaimSelection(
                disposition=draft.disposition,
                explanation=draft.explanation,
            )
        claim = draft.claim
        return ClaimSelection(
            disposition=draft.disposition,
            claim=BehavioralClaim(
                claim_id=_DETERMINISTIC_CLAIM_ID,
                summary=claim.summary,
                observable_operation=claim.observable_operation,
                trigger_condition=claim.trigger_condition,
                expected_head_observation=claim.expected_head_observation,
                expected_base_hypothesis=claim.expected_base_hypothesis,
                shared_interface=claim.shared_interface,
                preconditions=claim.preconditions,
                action=claim.action,
                expected_behavior=claim.expected_behavior,
                affected_symbols=claim.affected_symbols,
                supporting_context=claim.supporting_context,
                confidence=claim.confidence,
                testability=ClaimTestability.TESTABLE,
                reasoning_summary=draft.explanation,
            ),
            explanation=draft.explanation,
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
        interfaces = context.interfaces
        shared = selection.claim.shared_interface
        if shared:
            leaf = shared.rsplit(".", maxsplit=1)[-1]
            shared_leaves = {
                name.rsplit(".", maxsplit=1)[-1] for name in interfaces.present_on_both
            }
            if leaf in interfaces.head_only_leaf_names:
                raise InvalidClaimAgentOutput(
                    "claim targets an interface that exists only on HEAD; a differential "
                    "experiment requires an interface present on both revisions"
                )
            if leaf not in shared_leaves:
                raise InvalidClaimAgentOutput(
                    "claim's shared interface is absent from the deterministic "
                    "BASE/HEAD symbol partition"
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
