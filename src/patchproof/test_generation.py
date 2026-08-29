"""Structured candidate-test generation, deterministic validation, and bounded lineage."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import sys
import textwrap
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from patchproof.claim_agent import BehavioralClaim, ModelUsage
from patchproof.context_retrieval import PullRequestContext, RepositorySignatureContext
from patchproof.execution_contract import ExecutionContract
from patchproof.models import TestArtifact
from patchproof.structured_output import StrictGeminiOutputModel

_CANDIDATE_ID_PATTERN = re.compile(r"candidate-[a-z0-9][a-z0-9-]{0,58}")
_TEST_FUNCTION_PATTERN = re.compile(r"test_[a-zA-Z0-9_]{1,120}")
_TEST_FILENAME_PATTERN = re.compile(r"test_[a-zA-Z0-9_]+\.py")
_DIAGNOSTIC_FIELD_PATTERN = re.compile(r"[^A-Za-z0-9_.\-\[\]$]")
_CANDIDATE_DRAFT_FIELDS = frozenset({"source", "rationale"})
_GENERATED_TEST_FUNCTION = "test_patchproof_generated_behavior"
_MAX_CANDIDATE_MODEL_CALLS = 3
_MAX_REPAIRS = 2
_BLOCKED_IMPORT_ROOTS = {
    "asyncio.subprocess",
    "ctypes",
    "ftplib",
    "http",
    "httpx",
    "multiprocessing",
    "requests",
    "socket",
    "smtplib",
    "subprocess",
    "telnetlib",
    "urllib",
}
_FORBIDDEN_CALL_NAMES = {"__import__", "compile", "eval", "exec"}
_FORBIDDEN_ATTRIBUTE_CALLS = {
    ("os", "popen"),
    ("os", "system"),
    ("pytest", "main"),
    ("shutil", "rmtree"),
}


def _validate_relative_python_path(value: str) -> str:
    if "\\" in value or any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError("candidate path contains an unsupported character")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix != ".py"
        or _TEST_FILENAME_PATTERN.fullmatch(path.name) is None
    ):
        raise ValueError("candidate path must be a normalized relative test_*.py path")
    return value


class CandidateOrigin(StrEnum):
    """Why a candidate model invocation occurred."""

    INITIAL = "INITIAL"
    REPAIR = "REPAIR"


class CandidateAttemptStatus(StrEnum):
    """Deterministic disposition of one model-produced candidate."""

    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"


class CandidateJsonParseStatus(StrEnum):
    """Whether a candidate response was parseable JSON without retaining its body."""

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    INVALID = "INVALID"
    VALID = "VALID"


class CandidateIssueCode(StrEnum):
    """Stable reasons a proposal cannot become an executable artifact."""

    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    PATH_ALREADY_EXISTS = "PATH_ALREADY_EXISTS"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    TEST_FUNCTION_MISMATCH = "TEST_FUNCTION_MISMATCH"
    MULTIPLE_TESTS = "MULTIPLE_TESTS"
    RELATIVE_IMPORT = "RELATIVE_IMPORT"
    WILDCARD_IMPORT = "WILDCARD_IMPORT"
    BLOCKED_IMPORT = "BLOCKED_IMPORT"
    UNGROUNDED_IMPORT = "UNGROUNDED_IMPORT"
    FORBIDDEN_CALL = "FORBIDDEN_CALL"
    DUPLICATE_CANDIDATE_ID = "DUPLICATE_CANDIDATE_ID"
    DUPLICATE_CANDIDATE_SOURCE = "DUPLICATE_CANDIDATE_SOURCE"
    NO_BEHAVIORAL_REPAIR_CHANGE = "NO_BEHAVIORAL_REPAIR_CHANGE"


class CandidateTestProposal(StrictGeminiOutputModel):
    """The only test source shape the semantic agent may propose."""

    candidate_id: str
    target_path: str
    test_function: str
    source: str = Field(min_length=1, max_length=16_000)
    rationale: str = Field(min_length=1, max_length=700)

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if _CANDIDATE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("candidate ID must be a bounded lowercase candidate-* slug")
        return value

    @field_validator("target_path")
    @classmethod
    def validate_target_path(cls, value: str) -> str:
        return _validate_relative_python_path(value)

    @field_validator("test_function")
    @classmethod
    def validate_test_function(cls, value: str) -> str:
        if _TEST_FUNCTION_PATTERN.fullmatch(value) is None:
            raise ValueError("test function must be a bounded pytest test_* identifier")
        return value


class CandidateTestDraft(StrictGeminiOutputModel):
    """Provider-visible semantic output; PatchProof assigns control-plane metadata."""

    source: str = Field(
        min_length=1,
        max_length=16_000,
        description=(
            "Complete UTF-8 Python source defining exactly one top-level pytest function "
            f"named {_GENERATED_TEST_FUNCTION}."
        ),
    )
    rationale: str = Field(
        min_length=1,
        max_length=700,
        description="Concise audit summary of the observable behavior tested.",
    )


class CandidateFeedback(BaseModel):
    """Bounded validation or execution feedback available to the one repair invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=1_500)
    observations: tuple[str, ...] = Field(default_factory=tuple, max_length=4)

    @field_validator("observations")
    @classmethod
    def validate_observations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("repair observations must contain bounded non-empty text")
        return value

    @classmethod
    def from_validation_issues(
        cls, issues: tuple[CandidateValidationIssue, ...]
    ) -> CandidateFeedback:
        """Create bounded deterministic repair feedback without raw provider or process logs."""
        observations = tuple(f"{issue.code}: {issue.message}" for issue in issues[:4])
        return cls(
            category="VALIDATION",
            summary="The previous candidate failed deterministic validation.",
            observations=observations,
        )


class CandidateModelRequest(BaseModel):
    """Bounded semantic input; execution commands are deliberately excluded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: BehavioralClaim
    context: PullRequestContext
    repository_signatures: RepositorySignatureContext = Field(
        default_factory=RepositorySignatureContext.empty
    )
    allowed_test_paths: tuple[str, ...] = Field(min_length=1, max_length=4)
    previous_candidate: CandidateTestProposal | None = None
    feedback: CandidateFeedback | None = None

    @model_validator(mode="after")
    def validate_repair_pair(self) -> CandidateModelRequest:
        if self.feedback is None and self.previous_candidate is not None:
            raise ValueError("a previous candidate requires repair feedback")
        return self


@dataclass(frozen=True, slots=True)
class RawCandidateModelResponse:
    """Untrusted structured text and usage facts from one candidate invocation."""

    text: str
    usage: ModelUsage

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("candidate model response must not be empty")


class StructuredCandidateModel(Protocol):
    """One model-call boundary implemented by ADK and faked in deterministic tests."""

    async def invoke(self, request: CandidateModelRequest) -> RawCandidateModelResponse: ...


class CandidateValidationIssue(BaseModel):
    """One bounded deterministic reason for rejecting candidate source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: CandidateIssueCode
    message: str = Field(min_length=1, max_length=500)


class CandidateValidationError(ValueError):
    """Raised internally with structured deterministic validation issues."""

    def __init__(self, *issues: CandidateValidationIssue) -> None:
        if not issues:
            raise ValueError("candidate validation error requires at least one issue")
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.code}: {issue.message}" for issue in issues))


class CandidateOutputDiagnostic(BaseModel):
    """Bounded structure-only diagnostics for malformed local benchmark output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Literal["RESPONSE_BUDGET", "DRAFT_SCHEMA", "NORMALIZED_PROPOSAL"]
    response_sha256: str = Field(pattern=r"[0-9a-f]{64}")
    response_chars: int = Field(ge=0)
    raw_body_retained: Literal[False] = False
    response_budget_exceeded: bool
    json_parse_status: CandidateJsonParseStatus
    json_error_line: int | None = Field(default=None, ge=1)
    json_error_column: int | None = Field(default=None, ge=1)
    top_level_kind: Literal["object", "array", "string", "number", "boolean", "null", "unknown"]
    expected_fields_present: tuple[str, ...] = Field(max_length=2)
    expected_fields_missing: tuple[str, ...] = Field(max_length=2)
    unexpected_fields: tuple[str, ...] = Field(max_length=8)
    unexpected_field_count: int = Field(ge=0)
    source_present: bool
    source_json_type: str | None = Field(default=None, max_length=20)
    source_chars: int | None = Field(default=None, ge=0)
    source_sha256: str | None = Field(default=None, pattern=r"[0-9a-f]{64}")
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


def _candidate_output_diagnostic(
    *,
    response_text: str,
    response_sha256: str,
    stage: Literal["RESPONSE_BUDGET", "DRAFT_SCHEMA", "NORMALIZED_PROPOSAL"],
    response_budget_exceeded: bool,
    validation_error: ValidationError | None = None,
) -> CandidateOutputDiagnostic:
    """Describe JSON shape and validation failures without retaining response content."""
    parsed: object = None
    parse_status = CandidateJsonParseStatus.NOT_ATTEMPTED
    error_line: int | None = None
    error_column: int | None = None
    if not response_budget_exceeded:
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as error:
            parse_status = CandidateJsonParseStatus.INVALID
            error_line = error.lineno
            error_column = error.colno
        else:
            parse_status = CandidateJsonParseStatus.VALID

    expected_present: tuple[str, ...] = ()
    expected_missing: tuple[str, ...] = tuple(sorted(_CANDIDATE_DRAFT_FIELDS))
    unexpected_fields: tuple[str, ...] = ()
    unexpected_count = 0
    source_present = False
    source_type: str | None = None
    source_chars: int | None = None
    source_sha256: str | None = None
    diagnostic_truncated = False
    if isinstance(parsed, dict):
        keys = set(parsed)
        expected_present = tuple(sorted(keys & _CANDIDATE_DRAFT_FIELDS))
        expected_missing = tuple(sorted(_CANDIDATE_DRAFT_FIELDS - keys))
        unexpected = sorted(keys - _CANDIDATE_DRAFT_FIELDS)
        unexpected_count = len(unexpected)
        sanitized = tuple(_DIAGNOSTIC_FIELD_PATTERN.sub("?", key)[:80] for key in unexpected[:8])
        unexpected_fields = sanitized
        diagnostic_truncated = len(unexpected) > len(sanitized) or any(
            len(key) > 80 for key in unexpected[:8]
        )
        source_present = "source" in parsed
        if source_present:
            source = parsed["source"]
            source_type = _json_kind(source)
            if isinstance(source, str):
                source_chars = len(source)
                source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()

    errors: list[str] = []
    if validation_error is not None:
        for item in validation_error.errors(
            include_url=False, include_context=False, include_input=False
        ):
            location = ".".join(str(part) for part in item["loc"]) or "$"
            location = _DIAGNOSTIC_FIELD_PATTERN.sub("?", location)[:80]
            error_type = _DIAGNOSTIC_FIELD_PATTERN.sub("?", item["type"])[:80]
            errors.append(f"{location}:{error_type}")
        if len(errors) > 8:
            diagnostic_truncated = True
            errors = errors[:8]

    return CandidateOutputDiagnostic(
        stage=stage,
        response_sha256=response_sha256,
        response_chars=len(response_text),
        response_budget_exceeded=response_budget_exceeded,
        json_parse_status=parse_status,
        json_error_line=error_line,
        json_error_column=error_column,
        top_level_kind=(
            _json_kind(parsed) if parse_status is CandidateJsonParseStatus.VALID else "unknown"
        ),
        expected_fields_present=expected_present,
        expected_fields_missing=expected_missing,
        unexpected_fields=unexpected_fields,
        unexpected_field_count=unexpected_count,
        source_present=source_present,
        source_json_type=source_type,
        source_chars=source_chars,
        source_sha256=source_sha256,
        validation_errors=tuple(errors),
        diagnostic_truncated=diagnostic_truncated,
    )


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
                bindings[bound] = f"import:{alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                bindings[bound] = f"from:{node.level}:{module}:{alias.name}"
    return bindings


def _bound_names(node: ast.AST) -> tuple[str, ...]:
    ordered: list[str] = []

    def add(name: str) -> None:
        if name not in ordered:
            ordered.append(name)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = node.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            add(argument.arg)
        if arguments.vararg is not None:
            add(arguments.vararg.arg)
        if arguments.kwarg is not None:
            add(arguments.kwarg.arg)
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            add(child.id)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            add(child.name)
    return tuple(ordered)


def _loaded_external_names(node: ast.AST) -> tuple[str, ...]:
    bound = set(_bound_names(node))
    names: list[str] = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Name)
            and isinstance(child.ctx, ast.Load)
            and child.id not in bound
            and child.id not in names
        ):
            names.append(child.id)
    return tuple(names)


class _BehaviorNormalizer(ast.NodeTransformer):
    """Normalize local names while retaining callable, argument, and control-flow structure."""

    def __init__(self, external_bindings: dict[str, str]) -> None:
        self.external_bindings = external_bindings
        self.local_scopes: list[dict[str, str]] = []

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> ast.FunctionDef | ast.AsyncFunctionDef:
        mapping = {name: f"local_{index}" for index, name in enumerate(_bound_names(node))}
        self.local_scopes.append(mapping)
        node = self.generic_visit(node)
        self.local_scopes.pop()
        if node.name in self.external_bindings:
            node.name = self.external_bindings[node.name]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        return self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        return self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node = self.generic_visit(node)
        if node.name in self.external_bindings:
            node.name = self.external_bindings[node.name]
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        if self.local_scopes and node.arg in self.local_scopes[-1]:
            node.arg = self.local_scopes[-1][node.arg]
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.Name:
        for scope in reversed(self.local_scopes):
            if node.id in scope:
                node.id = scope[node.id]
                return node
        if node.id in self.external_bindings:
            node.id = self.external_bindings[node.id]
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.ExceptHandler:
        if node.name and self.local_scopes and node.name in self.local_scopes[-1]:
            node.name = self.local_scopes[-1][node.name]
        return self.generic_visit(node)


def candidate_behavior_fingerprint(source: str, *, test_function: str) -> str:
    """Hash syntax-level executable behavior without comments, formatting, or unused imports.

    This is deliberately narrower than semantic equivalence. It canonicalizes local identifier
    names and import aliases, includes referenced top-level helper definitions transitively, and
    retains assertions, calls, arguments, literals, context managers, and control flow.
    """
    tree = ast.parse(source)
    tests = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_function
    ]
    if len(tests) != 1:
        raise ValueError("behavior fingerprint requires exactly one declared test function")

    imports = _import_bindings(tree)
    definitions: dict[str, ast.stmt] = {}
    for node in tree.body:
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node is not tests[0]
        ):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
    dependency_names: list[str] = []
    pending = list(_loaded_external_names(tests[0]))
    while pending:
        name = pending.pop(0)
        if name in dependency_names:
            continue
        if name in imports or name in definitions:
            dependency_names.append(name)
        definition = definitions.get(name)
        if definition is not None:
            pending.extend(_loaded_external_names(definition))

    helper_names: list[str] = []
    helper_indexes: dict[str, int] = {}
    definition_indexes: dict[int, int] = {}
    for name in dependency_names:
        definition = definitions.get(name)
        if definition is None:
            continue
        definition_id = id(definition)
        if definition_id not in definition_indexes:
            definition_indexes[definition_id] = len(helper_names)
            helper_names.append(name)
        helper_indexes[name] = definition_indexes[definition_id]
    external_bindings = {
        name: f"import_binding:{imports[name]}" for name in dependency_names if name in imports
    }
    external_bindings.update({name: f"helper_{index}" for name, index in helper_indexes.items()})
    normalizer = _BehaviorNormalizer(external_bindings)
    normalized_test = normalizer.visit(copy.deepcopy(tests[0]))
    normalized_test.name = "generated_test"
    normalized_helpers = [
        normalizer.visit(copy.deepcopy(definitions[name])) for name in helper_names
    ]
    ast.fix_missing_locations(normalized_test)
    for helper in normalized_helpers:
        ast.fix_missing_locations(helper)
    canonical = ast.dump(
        ast.Module(body=[*normalized_helpers, normalized_test], type_ignores=[]),
        annotate_fields=True,
        include_attributes=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidatedCandidate:
    """A validated proposal tied to one immutable replay artifact and its imports."""

    proposal: CandidateTestProposal
    artifact: TestArtifact
    imported_roots: tuple[str, ...]
    behavior_fingerprint: str


@dataclass(frozen=True, slots=True)
class CandidateAttempt:
    """Audit lineage for one consumed model-call budget slot."""

    sequence: int
    origin: CandidateOrigin
    parent_candidate_id: str | None
    parent_artifact_sha256: str | None
    status: CandidateAttemptStatus
    proposal: CandidateTestProposal | None
    validated: ValidatedCandidate | None
    issues: tuple[CandidateValidationIssue, ...]
    feedback: CandidateFeedback | None
    usage: ModelUsage
    raw_response_sha256: str
    behavior_fingerprint: str | None
    signature_context_count: int
    signature_context_truncated: bool
    signature_context_sha256: str
    malformed_output_diagnostic: CandidateOutputDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class CandidateGenerationSnapshot:
    """Immutable view of all candidate lineage for one verification run."""

    attempts: tuple[CandidateAttempt, ...]
    model_calls: int
    repair_count: int

    @property
    def repair_used(self) -> bool:
        """Backward-compatible indication that at least one repair was consumed."""
        return self.repair_count > 0

    @property
    def latest_validated(self) -> ValidatedCandidate | None:
        for attempt in reversed(self.attempts):
            if attempt.validated is not None:
                return attempt.validated
        return None


class CandidateBudgetExceeded(RuntimeError):
    """Raised before a fourth candidate or third repair model call can occur."""


class CandidateGenerationStateError(RuntimeError):
    """Raised when initial and repair operations are invoked out of order."""


class CandidateTestValidator:
    """Turn model source into an artifact only after deterministic safety checks."""

    def __init__(
        self,
        *,
        max_source_bytes: int = 16_000,
        installed_import_roots: frozenset[str] = frozenset(),
    ) -> None:
        if max_source_bytes <= 0:
            raise ValueError("candidate source byte budget must be positive")
        self.max_source_bytes = max_source_bytes
        #: Top-level names importable in the prepared revision workspace. Empty when the
        #: environment has not been introspected, which leaves context-derived grounding
        #: in force. See `patchproof.environment_introspection`.
        self.installed_import_roots = installed_import_roots

    def validate(
        self,
        *,
        proposal: CandidateTestProposal,
        context: PullRequestContext,
        contract: ExecutionContract,
        existing_paths: frozenset[str],
    ) -> ValidatedCandidate:
        """Validate path, syntax, collection shape, calls, and grounded import roots."""
        if not contract.permits_test_path(proposal.target_path):
            self._reject(
                CandidateIssueCode.PATH_NOT_ALLOWED,
                "candidate target is outside allowed_test_paths",
            )
        if proposal.target_path in existing_paths:
            self._reject(
                CandidateIssueCode.PATH_ALREADY_EXISTS,
                "candidate target would overwrite a committed repository path",
            )
        source_bytes = proposal.source.encode("utf-8")
        if len(source_bytes) > self.max_source_bytes:
            self._reject(
                CandidateIssueCode.SOURCE_TOO_LARGE,
                "candidate source exceeds the configured byte budget",
            )
        try:
            tree = ast.parse(proposal.source, filename=proposal.target_path)
        except SyntaxError as error:
            self._reject(
                CandidateIssueCode.SYNTAX_ERROR,
                f"candidate source is not valid Python: {error.msg}",
            )

        top_level_tests = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]
        if not any(node.name == proposal.test_function for node in top_level_tests):
            self._reject(
                CandidateIssueCode.TEST_FUNCTION_MISMATCH,
                "declared test function is not a top-level pytest function",
            )
        if len(top_level_tests) != 1:
            self._reject(
                CandidateIssueCode.MULTIPLE_TESTS,
                "candidate source must define exactly one top-level test function",
            )

        imported_roots = self._validate_imports(tree=tree, context=context)
        self._validate_calls(tree)
        artifact = TestArtifact.from_text(
            relative_path=proposal.target_path,
            node_id=f"{proposal.target_path}::{proposal.test_function}",
            content=proposal.source,
        )
        return ValidatedCandidate(
            proposal=proposal,
            artifact=artifact,
            imported_roots=tuple(sorted(imported_roots)),
            behavior_fingerprint=candidate_behavior_fingerprint(
                proposal.source,
                test_function=proposal.test_function,
            ),
        )

    def _validate_imports(self, *, tree: ast.Module, context: PullRequestContext) -> set[str]:
        allowed_roots = self._allowed_import_roots(context) | self.installed_import_roots
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self._reject(
                        CandidateIssueCode.RELATIVE_IMPORT,
                        "relative imports are not allowed in generated candidates",
                    )
                if any(alias.name == "*" for alias in node.names):
                    self._reject(
                        CandidateIssueCode.WILDCARD_IMPORT,
                        "wildcard imports are not allowed in generated candidates",
                    )
                if node.module:
                    modules = [node.module]
            for module in modules:
                root = module.split(".", maxsplit=1)[0]
                imported_roots.add(root)
                if module in _BLOCKED_IMPORT_ROOTS or root in _BLOCKED_IMPORT_ROOTS:
                    self._reject(
                        CandidateIssueCode.BLOCKED_IMPORT,
                        f"generated candidate imports blocked module {root!r}",
                    )
                if root not in allowed_roots:
                    self._reject(
                        CandidateIssueCode.UNGROUNDED_IMPORT,
                        f"import root {root!r} is absent from deterministic context",
                    )
        return imported_roots

    @staticmethod
    def _validate_calls(tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALL_NAMES:
                CandidateTestValidator._reject(
                    CandidateIssueCode.FORBIDDEN_CALL,
                    f"generated candidate calls forbidden builtin {node.func.id!r}",
                )
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (node.func.value.id, node.func.attr) in _FORBIDDEN_ATTRIBUTE_CALLS
            ):
                CandidateTestValidator._reject(
                    CandidateIssueCode.FORBIDDEN_CALL,
                    "generated candidate calls forbidden API "
                    f"{node.func.value.id}.{node.func.attr}",
                )

    @staticmethod
    def _allowed_import_roots(context: PullRequestContext) -> set[str]:
        """Roots inferable from the context bundle alone.

        This is a floor, not the whole grounding set. It cannot see the repository's
        third-party runtime dependencies, so on its own it rejects candidates that
        import a package the project genuinely requires -- the cattrs `attrs`
        rejection in the sealed unseen holdout. `CandidateTestValidator` unions this
        with the roots actually installed in the prepared workspace.
        """
        roots = set(sys.stdlib_module_names)
        roots.add("pytest")
        paths = {file.path for file in context.changed_files}
        paths.update(snippet.path for snippet in context.snippets)
        for raw_path in paths:
            parts = PurePosixPath(raw_path).parts
            if not parts:
                continue
            if parts[0] == "src" and len(parts) > 1:
                roots.add(parts[1])
            elif parts[0] not in {"test", "tests"}:
                roots.add(PurePosixPath(parts[0]).stem)
        for snippet in context.snippets:
            try:
                snippet_tree = ast.parse(textwrap.dedent(snippet.content))
            except SyntaxError:
                continue
            for node in ast.walk(snippet_tree):
                module: str | None = None
                if isinstance(node, ast.Import) and node.names:
                    module = node.names[0].name
                elif isinstance(node, ast.ImportFrom):
                    module = node.module
                if module:
                    roots.add(module.split(".", maxsplit=1)[0])
        return roots

    @staticmethod
    def _reject(code: CandidateIssueCode, message: str) -> None:
        raise CandidateValidationError(CandidateValidationIssue(code=code, message=message))


class BoundedCandidateTestGenerator:
    """Consume at most three model calls: one initial candidate and two repairs."""

    def __init__(
        self,
        *,
        model: StructuredCandidateModel,
        validator: CandidateTestValidator,
        claim: BehavioralClaim,
        context: PullRequestContext,
        contract: ExecutionContract,
        existing_paths: frozenset[str],
        repository_signatures: RepositorySignatureContext | None = None,
        max_input_json_chars: int = 64_000,
        max_response_chars: int = 20_000,
    ) -> None:
        if max_input_json_chars <= 0 or max_response_chars <= 0:
            raise ValueError("candidate-agent character budgets must be positive")
        self.model = model
        self.validator = validator
        self.claim = claim
        self.context = context
        self.contract = contract
        self.existing_paths = existing_paths
        self.repository_signatures = repository_signatures or RepositorySignatureContext.empty()
        self.max_input_json_chars = max_input_json_chars
        self.max_response_chars = max_response_chars
        self._attempts: list[CandidateAttempt] = []
        self._model_calls = 0
        self._initial_started = False
        self._repair_count = 0
        self._invoking = False

    @property
    def snapshot(self) -> CandidateGenerationSnapshot:
        return CandidateGenerationSnapshot(
            attempts=tuple(self._attempts),
            model_calls=self._model_calls,
            repair_count=self._repair_count,
        )

    async def generate_initial(self) -> CandidateAttempt:
        """Generate and validate the first candidate exactly once."""
        if self._initial_started:
            raise CandidateGenerationStateError("initial candidate generation already started")
        self._initial_started = True
        return await self._invoke(origin=CandidateOrigin.INITIAL, feedback=None)

    async def repair(self, *, feedback: CandidateFeedback) -> CandidateAttempt:
        """Use one of two repair slots with bounded feedback from the prior attempt."""
        if not self._attempts:
            raise CandidateGenerationStateError("repair requires a completed initial attempt")
        if self._repair_count >= _MAX_REPAIRS or self._model_calls >= _MAX_CANDIDATE_MODEL_CALLS:
            raise CandidateBudgetExceeded("candidate repair budget is exhausted")
        self._repair_count += 1
        return await self._invoke(origin=CandidateOrigin.REPAIR, feedback=feedback)

    async def _invoke(
        self, *, origin: CandidateOrigin, feedback: CandidateFeedback | None
    ) -> CandidateAttempt:
        if self._invoking:
            raise CandidateGenerationStateError("candidate model invocation already in progress")
        if self._model_calls >= _MAX_CANDIDATE_MODEL_CALLS:
            raise CandidateBudgetExceeded("candidate model-call budget is exhausted")
        previous = self._attempts[-1] if self._attempts else None
        request = CandidateModelRequest(
            claim=self.claim,
            context=self.context,
            repository_signatures=self.repository_signatures,
            allowed_test_paths=self.contract.allowed_test_paths,
            previous_candidate=previous.proposal if origin is CandidateOrigin.REPAIR else None,
            feedback=feedback,
        )
        if len(request.model_dump_json()) > self.max_input_json_chars:
            raise ValueError("candidate-agent input exceeds the configured character budget")

        self._invoking = True
        self._model_calls += 1
        try:
            response = await self.model.invoke(request)
        finally:
            self._invoking = False

        response_hash = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
        issue: CandidateValidationIssue | None = None
        proposal: CandidateTestProposal | None = None
        validated: ValidatedCandidate | None = None
        behavior_fingerprint: str | None = None
        diagnostic: CandidateOutputDiagnostic | None = None
        issues: tuple[CandidateValidationIssue, ...] = ()
        status = CandidateAttemptStatus.INVALID_MODEL_OUTPUT
        if len(response.text) > self.max_response_chars:
            issue = CandidateValidationIssue(
                code=CandidateIssueCode.RESPONSE_TOO_LARGE,
                message="candidate model response exceeds the configured budget",
            )
            diagnostic = _candidate_output_diagnostic(
                response_text=response.text,
                response_sha256=response_hash,
                stage="RESPONSE_BUDGET",
                response_budget_exceeded=True,
            )
        else:
            try:
                draft = CandidateTestDraft.model_validate_json(response.text)
            except ValidationError as error:
                issue = CandidateValidationIssue(
                    code=CandidateIssueCode.MALFORMED_OUTPUT,
                    message="candidate model returned invalid structured output",
                )
                diagnostic = _candidate_output_diagnostic(
                    response_text=response.text,
                    response_sha256=response_hash,
                    stage="DRAFT_SCHEMA",
                    response_budget_exceeded=False,
                    validation_error=error,
                )
            else:
                if origin is CandidateOrigin.INITIAL:
                    candidate_suffix = path_suffix = "initial"
                else:
                    candidate_suffix = f"repair-{self._repair_count}"
                    path_suffix = f"repair_{self._repair_count}"
                try:
                    proposal = CandidateTestProposal(
                        candidate_id=f"candidate-{candidate_suffix}",
                        target_path=(
                            f"{self.contract.allowed_test_paths[0]}"
                            f"test_patchproof_generated_{path_suffix}.py"
                        ),
                        test_function=_GENERATED_TEST_FUNCTION,
                        source=draft.source,
                        rationale=draft.rationale,
                    )
                except ValidationError as error:
                    issue = CandidateValidationIssue(
                        code=CandidateIssueCode.MALFORMED_OUTPUT,
                        message="candidate model output could not be normalized safely",
                    )
                    diagnostic = _candidate_output_diagnostic(
                        response_text=response.text,
                        response_sha256=response_hash,
                        stage="NORMALIZED_PROPOSAL",
                        response_budget_exceeded=False,
                        validation_error=error,
                    )
                if (
                    proposal is not None
                    and origin is CandidateOrigin.REPAIR
                    and previous is not None
                    and previous.proposal is not None
                    and proposal.source.encode("utf-8") == previous.proposal.source.encode("utf-8")
                ):
                    issue = CandidateValidationIssue(
                        code=CandidateIssueCode.DUPLICATE_CANDIDATE_SOURCE,
                        message="repair source must differ byte-for-byte from the previous source",
                    )
                    status = CandidateAttemptStatus.REJECTED
                elif proposal is not None and any(
                    attempt.proposal is not None
                    and attempt.proposal.candidate_id == proposal.candidate_id
                    for attempt in self._attempts
                ):
                    issue = CandidateValidationIssue(
                        code=CandidateIssueCode.DUPLICATE_CANDIDATE_ID,
                        message="candidate IDs must be unique within one generation run",
                    )
                    status = CandidateAttemptStatus.REJECTED
                elif proposal is not None:
                    try:
                        validated = self.validator.validate(
                            proposal=proposal,
                            context=self.context,
                            contract=self.contract,
                            existing_paths=self.existing_paths,
                        )
                    except CandidateValidationError as error:
                        issues = error.issues
                        status = CandidateAttemptStatus.REJECTED
                    else:
                        behavior_fingerprint = validated.behavior_fingerprint
                        if (
                            origin is CandidateOrigin.REPAIR
                            and previous is not None
                            and previous.behavior_fingerprint is not None
                            and validated.behavior_fingerprint == previous.behavior_fingerprint
                        ):
                            issue = CandidateValidationIssue(
                                code=CandidateIssueCode.NO_BEHAVIORAL_REPAIR_CHANGE,
                                message=(
                                    "repair must materially change executable test behavior, "
                                    "not only formatting, comments, or unused imports"
                                ),
                            )
                            validated = None
                            status = CandidateAttemptStatus.REJECTED
                        else:
                            issues = ()
                            status = CandidateAttemptStatus.VALIDATED

        if issue is not None:
            issues = (issue,)
        parent_validated = previous.validated if previous is not None else None
        attempt = CandidateAttempt(
            sequence=len(self._attempts) + 1,
            origin=origin,
            parent_candidate_id=(
                previous.proposal.candidate_id
                if origin is CandidateOrigin.REPAIR and previous and previous.proposal
                else None
            ),
            parent_artifact_sha256=(
                parent_validated.artifact.sha256 if parent_validated is not None else None
            ),
            status=status,
            proposal=proposal,
            validated=validated,
            issues=issues,
            feedback=feedback,
            usage=response.usage,
            raw_response_sha256=response_hash,
            behavior_fingerprint=behavior_fingerprint,
            signature_context_count=self.repository_signatures.count,
            signature_context_truncated=self.repository_signatures.truncated,
            signature_context_sha256=self.repository_signatures.sha256,
            malformed_output_diagnostic=diagnostic,
        )
        self._attempts.append(attempt)
        return attempt
