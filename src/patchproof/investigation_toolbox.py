"""The complete, bounded capability surface a model may use to investigate a repository.

Security posture
----------------

This is the *only* thing the claim investigator model can act through. It exposes four
read-only queries over two prebuilt in-memory indexes. It does not accept, and cannot be
made to accept, any of:

* shell commands, subprocesses, or arbitrary Python
* network access
* arbitrary filesystem paths (every path is validated and must already exist in the index)
* Git commands, revision selection, or BASE/HEAD SHA control
* repository-root selection
* environment or index-budget control

The indexes are built before the model is invoked and are immutable for the run. A tool
call is a lookup against already-materialized data structures; there is no execution
boundary for a prompt to cross.

Budget posture
--------------

Three independent limits, all enforced here rather than requested in a prompt:

* a total call budget across all tools
* a per-tool call budget, so one tool cannot consume the whole allowance
* a turn budget, enforced by the orchestrator that owns this toolbox

Exhaustion is not an error. It returns a structured refusal so the model can still
produce a claim or abstain from what it already has, which keeps a budget-limited run
conservative rather than failed.

Uncertainty posture
-------------------

Phase 1's match semantics are preserved verbatim. `POSSIBLE_*` match types stay labelled
as possibilities, every match keeps its provenance and reason, and a truncated index is
reported as truncated. Nothing here upgrades a syntactic match into proven runtime
identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from patchproof.investigation_tools import (
    InvestigationQueryError,
    InvestigationResult,
    RepositoryInvestigator,
)
from patchproof.models import RevisionRole


class InvestigationToolName(StrEnum):
    """The four capabilities exposed to the model. There are deliberately no others."""

    INSPECT_SYMBOL = "inspect_symbol"
    FIND_REFERENCES = "find_references"
    FIND_RELATED_TESTS = "find_related_tests"
    INSPECT_SOURCE_WINDOW = "inspect_source_window"


class ToolCallStatus(StrEnum):
    """Disposition of one tool call."""

    OK = "OK"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ToolBudget:
    """Hard limits on model-driven investigation for one claim-selection run."""

    max_turns: int = 4
    max_total_calls: int = 8
    max_calls_per_tool: int = 3
    max_results_per_call: int = 10
    max_source_window_lines: int = 80
    #: Total characters of repository source the model may accumulate across all calls.
    max_total_source_chars: int = 24_000

    def __post_init__(self) -> None:
        for item in fields(self):
            if getattr(self, item.name) <= 0:
                raise ValueError(f"tool budget {item.name} must be positive")
        if self.max_turns > 8 or self.max_total_calls > 16 or self.max_calls_per_tool > 8:
            raise ValueError("tool budget exceeds its safety ceiling")
        if self.max_calls_per_tool > self.max_total_calls:
            raise ValueError("per-tool budget cannot exceed the total call budget")
        if self.max_total_source_chars > 64_000:
            raise ValueError("aggregate source budget exceeds its safety ceiling")


class ToolCallRecord(BaseModel):
    """One auditable entry in the investigation transcript."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=1)
    turn: int = Field(ge=1)
    tool: InvestigationToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus
    match_count: int = Field(default=0, ge=0)
    truncated: bool = False
    source_chars: int = Field(default=0, ge=0)
    detail: str = Field(min_length=1, max_length=500)


class ToolBudgetExhausted(RuntimeError):
    """Raised only when an orchestrator asks for a call it was already told to stop making."""


class InvestigationToolbox:
    """Bounded, auditable, read-only tool surface over one immutable revision pair."""

    def __init__(
        self,
        *,
        investigator: RepositoryInvestigator,
        budget: ToolBudget | None = None,
    ) -> None:
        self.investigator = investigator
        self.budget = budget or ToolBudget()
        self._calls: list[ToolCallRecord] = []
        self._per_tool: dict[InvestigationToolName, int] = dict.fromkeys(InvestigationToolName, 0)
        self._source_chars = 0
        self._turn = 1

    # -- transcript and budget state -------------------------------------------------

    @property
    def transcript(self) -> tuple[ToolCallRecord, ...]:
        return tuple(self._calls)

    @property
    def total_calls(self) -> int:
        return len(self._calls)

    @property
    def turn(self) -> int:
        return self._turn

    def begin_turn(self) -> bool:
        """Advance to the next model turn; return whether investigation may continue."""
        if self._turn >= self.budget.max_turns:
            return False
        self._turn += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.total_calls >= self.budget.max_total_calls

    def remaining_budget_note(self) -> str:
        """Human-readable budget state, supplied to the model each turn."""
        return (
            f"turn {self._turn}/{self.budget.max_turns}; "
            f"tool calls used {self.total_calls}/{self.budget.max_total_calls}; "
            f"per-tool limit {self.budget.max_calls_per_tool}"
        )

    # -- the four capabilities -------------------------------------------------------

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch one validated tool call and record it. Never raises for bad input."""
        raw_arguments = dict(arguments or {})
        try:
            name = InvestigationToolName(tool)
        except ValueError:
            return self._refuse(
                tool=None,
                arguments=raw_arguments,
                status=ToolCallStatus.INVALID_ARGUMENTS,
                detail=(
                    f"unknown tool {tool!r}; available tools are "
                    f"{', '.join(item.value for item in InvestigationToolName)}"
                ),
            )
        if self.exhausted:
            return self._refuse(
                tool=name,
                arguments=raw_arguments,
                status=ToolCallStatus.BUDGET_EXHAUSTED,
                detail=(
                    "total investigation call budget is exhausted; select a claim from the "
                    "facts already gathered or abstain"
                ),
            )
        if self._per_tool[name] >= self.budget.max_calls_per_tool:
            return self._refuse(
                tool=name,
                arguments=raw_arguments,
                status=ToolCallStatus.BUDGET_EXHAUSTED,
                detail=(
                    f"per-tool budget for {name.value} is exhausted; use a different tool or "
                    "conclude"
                ),
            )
        try:
            handler = {
                InvestigationToolName.INSPECT_SYMBOL: self._inspect_symbol,
                InvestigationToolName.FIND_REFERENCES: self._find_references,
                InvestigationToolName.FIND_RELATED_TESTS: self._find_related_tests,
                InvestigationToolName.INSPECT_SOURCE_WINDOW: self._inspect_source_window,
            }[name]
            result = handler(raw_arguments)
        except InvestigationQueryError as error:
            return self._refuse(
                tool=name,
                arguments=raw_arguments,
                status=ToolCallStatus.INVALID_ARGUMENTS,
                detail=f"rejected by the investigation contract: {error}"[:500],
            )
        except (KeyError, TypeError, ValueError) as error:
            return self._refuse(
                tool=name,
                arguments=raw_arguments,
                status=ToolCallStatus.INVALID_ARGUMENTS,
                detail=f"invalid tool arguments: {error}"[:500],
            )
        return self._accept(name, raw_arguments, result)

    def _inspect_symbol(self, arguments: dict[str, Any]) -> InvestigationResult:
        return self.investigator.inspect_symbol(
            revision=self._revision(arguments),
            path=self._optional_text(arguments, "path"),
            qualified_name=self._optional_text(arguments, "qualified_name"),
            maximum=self._maximum(arguments),
        )

    def _find_references(self, arguments: dict[str, Any]) -> InvestigationResult:
        return self.investigator.find_references(
            revision=self._revision(arguments),
            symbol=self._required_text(arguments, "symbol"),
            maximum=self._maximum(arguments),
        )

    def _find_related_tests(self, arguments: dict[str, Any]) -> InvestigationResult:
        return self.investigator.find_related_tests(
            revision=self._revision(arguments),
            symbol=self._optional_text(arguments, "symbol"),
            path=self._optional_text(arguments, "path"),
            maximum=self._maximum(arguments),
        )

    def _inspect_source_window(self, arguments: dict[str, Any]) -> InvestigationResult:
        line_count = self._integer(arguments, "line_count", default=40)
        return self.investigator.inspect_source_window(
            revision=self._revision(arguments),
            path=self._required_text(arguments, "path"),
            start_line=self._integer(arguments, "start_line", default=1),
            line_count=min(line_count, self.budget.max_source_window_lines),
        )

    # -- argument validation ---------------------------------------------------------

    @staticmethod
    def _revision(arguments: dict[str, Any]) -> RevisionRole:
        """Accept only the two roles. The model cannot name a SHA or any other revision."""
        value = arguments.get("revision", "HEAD")
        if not isinstance(value, str):
            raise ValueError("revision must be the text 'BASE' or 'HEAD'")
        normalized = value.strip().upper()
        if normalized not in {RevisionRole.BASE.value, RevisionRole.HEAD.value}:
            raise ValueError("revision must be exactly 'BASE' or 'HEAD'")
        return RevisionRole(normalized)

    @staticmethod
    def _required_text(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required and must be non-empty text")
        return value.strip()

    @staticmethod
    def _optional_text(arguments: dict[str, Any], key: str) -> str | None:
        value = arguments.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text when supplied")
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _integer(arguments: dict[str, Any], key: str, *, default: int) -> int:
        value = arguments.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        if value < 1:
            raise ValueError(f"{key} must be positive")
        return value

    def _maximum(self, arguments: dict[str, Any]) -> int:
        value = arguments.get("maximum", self.budget.max_results_per_call)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("maximum must be a positive integer")
        return min(value, self.budget.max_results_per_call)

    # -- result shaping --------------------------------------------------------------

    def _accept(
        self,
        tool: InvestigationToolName,
        arguments: dict[str, Any],
        result: InvestigationResult,
    ) -> dict[str, Any]:
        payload, source_chars, source_truncated = self._serialize(result)
        self._per_tool[tool] += 1
        self._source_chars += source_chars
        self._calls.append(
            ToolCallRecord(
                sequence=len(self._calls) + 1,
                turn=self._turn,
                tool=tool,
                arguments=self._safe_arguments(arguments),
                status=ToolCallStatus.OK,
                match_count=len(result.matches),
                truncated=result.truncated or source_truncated,
                source_chars=source_chars,
                detail=f"{len(result.matches)} match(es) on {result.revision}",
            )
        )
        return payload

    def _serialize(self, result: InvestigationResult) -> tuple[dict[str, Any], int, bool]:
        """Convert a result to a bounded JSON-safe payload, preserving uncertainty."""
        remaining = max(0, self.budget.max_total_source_chars - self._source_chars)
        matches: list[dict[str, Any]] = []
        used = 0
        source_truncated = False
        for match in result.matches:
            provenance = match.provenance
            entry: dict[str, Any] = {
                "path": provenance.path,
                "start_line": provenance.start_line,
                "end_line": provenance.end_line,
                "symbol_name": provenance.symbol_name,
                # Preserved verbatim: POSSIBLE_* stays POSSIBLE_*.
                "match_type": provenance.match_type.value,
                "reason": provenance.reason,
                "source_truncated": provenance.source_truncated,
            }
            if match.symbol is not None:
                entry["symbol"] = {
                    "identity": f"{match.symbol.path}::{match.symbol.qualified_name}",
                    "qualified_name": match.symbol.qualified_name,
                    "kind": match.symbol.kind.value,
                    "public": match.symbol.public,
                    "exported": match.symbol.exported,
                }
            if match.reference is not None:
                entry["reference"] = {
                    "scope": match.reference.scope,
                    "expression": match.reference.expression,
                    "kind": match.reference.kind.value,
                }
            if match.content is not None:
                allowance = remaining - used
                if allowance <= 0:
                    entry["content_omitted"] = "aggregate source budget exhausted"
                    source_truncated = True
                else:
                    content = match.content[:allowance]
                    if len(content) < len(match.content):
                        source_truncated = True
                        entry["content_truncated"] = True
                    entry["content"] = content
                    used += len(content)
            matches.append(entry)
        payload = {
            "status": ToolCallStatus.OK.value,
            "revision": result.revision.value,
            "query_kind": result.query_kind.value,
            "match_count": len(result.matches),
            "results_truncated": result.truncated,
            # A partial index can never prove absence; say so in-band, every time.
            "index_partial": result.index_stats.truncated,
            "absence_is_not_proof": result.index_stats.truncated,
            "matches": matches,
            "budget": self.remaining_budget_note(),
        }
        return payload, used, source_truncated

    def _refuse(
        self,
        *,
        tool: InvestigationToolName | None,
        arguments: dict[str, Any],
        status: ToolCallStatus,
        detail: str,
    ) -> dict[str, Any]:
        """Record and return a structured refusal without aborting the investigation."""
        if tool is not None:
            self._calls.append(
                ToolCallRecord(
                    sequence=len(self._calls) + 1,
                    turn=self._turn,
                    tool=tool,
                    arguments=self._safe_arguments(arguments),
                    status=status,
                    detail=detail,
                )
            )
        return {
            "status": status.value,
            "error": detail,
            "budget": self.remaining_budget_note(),
        }

    @staticmethod
    def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        """Retain only small JSON-serializable argument values for the audit record."""
        safe: dict[str, Any] = {}
        for key, value in list(arguments.items())[:8]:
            if not isinstance(key, str) or len(key) > 64:
                continue
            if isinstance(value, bool | int):
                safe[key] = value
            elif isinstance(value, str):
                safe[key] = value[:256]
        try:
            json.dumps(safe)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return {}
        return safe
