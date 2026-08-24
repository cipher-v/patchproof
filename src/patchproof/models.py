"""Typed domain models shared by the Phase 1 execution pipeline."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import ClassVar

_GIT_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class RevisionRole(StrEnum):
    """The counterfactual role played by an immutable Git revision."""

    BASE = "BASE"
    HEAD = "HEAD"


@dataclass(frozen=True, slots=True)
class Revision:
    """A resolved immutable Git commit used in a verification challenge."""

    role: RevisionRole
    sha: str

    def __post_init__(self) -> None:
        normalized_sha = self.sha.lower()
        if _GIT_SHA_PATTERN.fullmatch(normalized_sha) is None:
            raise ValueError("revision SHA must be a full 40- or 64-character hexadecimal hash")
        object.__setattr__(self, "sha", normalized_sha)


@dataclass(frozen=True, slots=True)
class TestArtifact:
    """Immutable candidate-test bytes and the single pytest node they select."""

    __test__: ClassVar[bool] = False

    relative_path: str
    node_id: str
    content: bytes

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise TypeError("test artifact content must be immutable bytes")
        if not self.content:
            raise ValueError("test artifact content must not be empty")
        if "\\" in self.relative_path or "\x00" in self.relative_path:
            raise ValueError("test artifact path must be a safe POSIX-style relative path")

        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or path.is_absolute()
            or path.as_posix() != self.relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix != ".py"
        ):
            raise ValueError("test artifact path must be a normalized relative Python path")

        expected_prefix = f"{self.relative_path}::"
        if not self.node_id.startswith(expected_prefix) or self.node_id == expected_prefix:
            raise ValueError("pytest node ID must select a test inside the artifact path")
        if "\x00" in self.node_id or "\n" in self.node_id or "\r" in self.node_id:
            raise ValueError("pytest node ID contains an invalid control character")

    @classmethod
    def from_text(cls, *, relative_path: str, node_id: str, content: str) -> TestArtifact:
        """Encode generated source exactly once without newline normalization."""
        return cls(relative_path=relative_path, node_id=node_id, content=content.encode("utf-8"))

    @property
    def sha256(self) -> str:
        """Return the content-addressed identity of the candidate bytes."""
        return hashlib.sha256(self.content).hexdigest()


class TestExecutionStatus(StrEnum):
    """Mechanically observed result for one selected pytest node."""

    __test__: ClassVar[bool] = False

    PASSED = "PASSED"
    ASSERTION_FAILED = "ASSERTION_FAILED"
    TEST_ERROR = "TEST_ERROR"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    NOT_COLLECTED = "NOT_COLLECTED"
    MULTIPLE_TESTS_COLLECTED = "MULTIPLE_TESTS_COLLECTED"
    SKIPPED = "SKIPPED"
    XFAILED = "XFAILED"
    XPASSED = "XPASSED"
    INVALID_ARTIFACT = "INVALID_ARTIFACT"
    TIMED_OUT = "TIMED_OUT"
    PROCESS_ERROR = "PROCESS_ERROR"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Bounded facts captured from one revision's candidate-test execution."""

    revision: Revision
    test_node_id: str
    expected_artifact_sha256: str
    artifact_sha256_before: str | None
    artifact_sha256_after: str | None
    status: TestExecutionStatus
    collected_count: int
    exit_code: int | None
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    detail: str | None = None

    def __post_init__(self) -> None:
        if _SHA256_PATTERN.fullmatch(self.expected_artifact_sha256) is None:
            raise ValueError("expected artifact hash must be a lowercase SHA-256 value")
        for artifact_hash in (self.artifact_sha256_before, self.artifact_sha256_after):
            if artifact_hash is not None and _SHA256_PATTERN.fullmatch(artifact_hash) is None:
                raise ValueError("observed artifact hash must be a lowercase SHA-256 value")
        if self.collected_count < 0:
            raise ValueError("collected test count cannot be negative")
        if self.duration_seconds < 0:
            raise ValueError("execution duration cannot be negative")

    @property
    def artifact_was_unchanged(self) -> bool:
        """Whether the expected bytes existed before and after pytest execution."""
        return (
            self.artifact_sha256_before == self.expected_artifact_sha256
            and self.artifact_sha256_after == self.expected_artifact_sha256
        )


class MechanicalEvidenceStatus(StrEnum):
    """Evidence status derived only from execution facts."""

    DISCRIMINATING = "DISCRIMINATING"
    NON_DISCRIMINATING = "NON_DISCRIMINATING"
    INVALID_TEST = "INVALID_TEST"
    ENVIRONMENTAL = "ENVIRONMENTAL"
    COUNTERFACTUAL_NOT_APPLICABLE = "COUNTERFACTUAL_NOT_APPLICABLE"


class DifferentialPattern(StrEnum):
    """The comparable BASE/HEAD outcome pattern, when one exists."""

    BASE_ASSERTION_FAILED_HEAD_PASSED = "BASE_ASSERTION_FAILED_HEAD_PASSED"
    BASE_PASSED_HEAD_ASSERTION_FAILED = "BASE_PASSED_HEAD_ASSERTION_FAILED"
    BOTH_PASSED = "BOTH_PASSED"
    BOTH_ASSERTION_FAILED = "BOTH_ASSERTION_FAILED"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class ClaimOutcome(StrEnum):
    """Semantic conclusions reserved for a later claim-assessment phase."""

    CLAIM_SUPPORTED_FOR_SCENARIO = "CLAIM_SUPPORTED_FOR_SCENARIO"
    CLAIM_NOT_SUPPORTED_FOR_SCENARIO = "CLAIM_NOT_SUPPORTED_FOR_SCENARIO"
    POTENTIAL_REGRESSION = "POTENTIAL_REGRESSION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """Mechanical classification kept separate from an optional semantic outcome."""

    mechanical_status: MechanicalEvidenceStatus
    pattern: DifferentialPattern
    reason: str
    claim_outcome: ClaimOutcome | None = None


@dataclass(frozen=True, slots=True)
class ChallengeResult:
    """The durable in-memory result of challenging one artifact on BASE and HEAD."""

    artifact: TestArtifact
    base: ExecutionResult
    head: ExecutionResult
    assessment: EvidenceAssessment

    def __post_init__(self) -> None:
        if self.base.revision.role is not RevisionRole.BASE:
            raise ValueError("base execution must use the BASE revision role")
        if self.head.revision.role is not RevisionRole.HEAD:
            raise ValueError("head execution must use the HEAD revision role")
