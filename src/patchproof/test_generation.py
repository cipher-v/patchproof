"""Structured candidate-test generation, deterministic validation, and bounded lineage."""

from __future__ import annotations

import ast
import hashlib
import re
import sys
import textwrap
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from patchproof.claim_agent import BehavioralClaim, ModelUsage
from patchproof.context_retrieval import PullRequestContext
from patchproof.execution_contract import ExecutionContract
from patchproof.models import TestArtifact
from patchproof.structured_output import StrictGeminiOutputModel

_CANDIDATE_ID_PATTERN = re.compile(r"candidate-[a-z0-9][a-z0-9-]{0,58}")
_TEST_FUNCTION_PATTERN = re.compile(r"test_[a-zA-Z0-9_]{1,120}")
_TEST_FILENAME_PATTERN = re.compile(r"test_[a-zA-Z0-9_]+\.py")
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


@dataclass(frozen=True, slots=True)
class ValidatedCandidate:
    """A validated proposal tied to one immutable replay artifact and its imports."""

    proposal: CandidateTestProposal
    artifact: TestArtifact
    imported_roots: tuple[str, ...]


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


@dataclass(frozen=True, slots=True)
class CandidateGenerationSnapshot:
    """Immutable view of all candidate lineage for one verification run."""

    attempts: tuple[CandidateAttempt, ...]
    model_calls: int
    repair_used: bool

    @property
    def latest_validated(self) -> ValidatedCandidate | None:
        for attempt in reversed(self.attempts):
            if attempt.validated is not None:
                return attempt.validated
        return None


class CandidateBudgetExceeded(RuntimeError):
    """Raised before a third candidate or second repair model call can occur."""


class CandidateGenerationStateError(RuntimeError):
    """Raised when initial and repair operations are invoked out of order."""


class CandidateTestValidator:
    """Turn model source into an artifact only after deterministic safety checks."""

    def __init__(self, *, max_source_bytes: int = 16_000) -> None:
        if max_source_bytes <= 0:
            raise ValueError("candidate source byte budget must be positive")
        self.max_source_bytes = max_source_bytes

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
        )

    def _validate_imports(self, *, tree: ast.Module, context: PullRequestContext) -> set[str]:
        allowed_roots = self._allowed_import_roots(context)
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
    """Consume at most two model calls: one initial candidate and one repair."""

    def __init__(
        self,
        *,
        model: StructuredCandidateModel,
        validator: CandidateTestValidator,
        claim: BehavioralClaim,
        context: PullRequestContext,
        contract: ExecutionContract,
        existing_paths: frozenset[str],
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
        self.max_input_json_chars = max_input_json_chars
        self.max_response_chars = max_response_chars
        self._attempts: list[CandidateAttempt] = []
        self._model_calls = 0
        self._initial_started = False
        self._repair_used = False
        self._invoking = False

    @property
    def snapshot(self) -> CandidateGenerationSnapshot:
        return CandidateGenerationSnapshot(
            attempts=tuple(self._attempts),
            model_calls=self._model_calls,
            repair_used=self._repair_used,
        )

    async def generate_initial(self) -> CandidateAttempt:
        """Generate and validate the first candidate exactly once."""
        if self._initial_started:
            raise CandidateGenerationStateError("initial candidate generation already started")
        self._initial_started = True
        return await self._invoke(origin=CandidateOrigin.INITIAL, feedback=None)

    async def repair(self, *, feedback: CandidateFeedback) -> CandidateAttempt:
        """Use the single repair slot with bounded feedback from the prior attempt."""
        if not self._attempts:
            raise CandidateGenerationStateError("repair requires a completed initial attempt")
        if self._repair_used or self._model_calls >= 2:
            raise CandidateBudgetExceeded("candidate repair budget is exhausted")
        self._repair_used = True
        return await self._invoke(origin=CandidateOrigin.REPAIR, feedback=feedback)

    async def _invoke(
        self, *, origin: CandidateOrigin, feedback: CandidateFeedback | None
    ) -> CandidateAttempt:
        if self._invoking:
            raise CandidateGenerationStateError("candidate model invocation already in progress")
        if self._model_calls >= 2:
            raise CandidateBudgetExceeded("candidate model-call budget is exhausted")
        previous = self._attempts[-1] if self._attempts else None
        request = CandidateModelRequest(
            claim=self.claim,
            context=self.context,
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
        status = CandidateAttemptStatus.INVALID_MODEL_OUTPUT
        if len(response.text) > self.max_response_chars:
            issue = CandidateValidationIssue(
                code=CandidateIssueCode.RESPONSE_TOO_LARGE,
                message="candidate model response exceeds the configured budget",
            )
        else:
            try:
                proposal = CandidateTestProposal.model_validate_json(response.text)
            except ValidationError:
                issue = CandidateValidationIssue(
                    code=CandidateIssueCode.MALFORMED_OUTPUT,
                    message="candidate model returned invalid structured output",
                )
            else:
                if any(
                    attempt.proposal is not None
                    and attempt.proposal.candidate_id == proposal.candidate_id
                    for attempt in self._attempts
                ):
                    issue = CandidateValidationIssue(
                        code=CandidateIssueCode.DUPLICATE_CANDIDATE_ID,
                        message="candidate IDs must be unique within one generation run",
                    )
                    status = CandidateAttemptStatus.REJECTED
                else:
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
        )
        self._attempts.append(attempt)
        return attempt
