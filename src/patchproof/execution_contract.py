"""Strict repository-owned execution contract; no shell command strings are accepted."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

_SAFE_TOKEN = re.compile(r"[^\x00-\x1f\x7f]{1,300}")

#: Install commands a repository may declare literally in its own `.patchproof.yaml`.
#: These stay narrow: a repository author writing this file by hand should not be able
#: to express anything beyond reproducing a committed lockfile.
_ALLOWED_INSTALL_COMMANDS = {
    ("uv", "sync", "--frozen"),
    ("uv", "sync", "--frozen", "--all-groups"),
}
_ALLOWED_TEST_COMMANDS = {
    ("python", "-m", "pytest"),
    ("uv", "run", "pytest"),
}


class ExecutionContractError(ValueError):
    """Raised when `.patchproof.yaml` is absent, malformed, or outside the allowlist."""


def _validate_argv(value: tuple[str, ...], *, allowed: set[tuple[str, ...]]) -> tuple[str, ...]:
    if not value or len(value) > 8 or any(_SAFE_TOKEN.fullmatch(token) is None for token in value):
        raise ValueError("command must contain 1-8 bounded argument tokens")
    if value not in allowed:
        raise ValueError("command is not an allowed PatchProof execution template")
    return value


def _validate_directory_prefix(value: str) -> str:
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("allowed test path contains an unsupported character")
    if not value.endswith("/"):
        raise ValueError("allowed test path must end with a slash")
    path = PurePosixPath(value.rstrip("/"))
    if (
        not value.rstrip("/")
        or path.is_absolute()
        or path.as_posix() != value.rstrip("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("allowed test path must be a normalized relative POSIX directory")
    return value


class TestCommandContract(BaseModel):
    """The allowlisted pytest command prefix; PatchProof appends its own fixed arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: tuple[str, ...]

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, allowed=_ALLOWED_TEST_COMMANDS)


class ExecutionContract(BaseModel):
    """Validated contents of one repository's `.patchproof.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    python: Literal["3.12"]
    install: tuple[tuple[str, ...], ...] = Field(min_length=1, max_length=4)
    test: TestCommandContract
    allowed_test_paths: tuple[str, ...] = Field(min_length=1, max_length=4)
    timeout_seconds: float = Field(ge=0.05, le=300)
    #: Separate budget for the repository-declared install commands. Installing a real
    #: project from a cold cache routinely takes longer than running one test, and
    #: sharing a single budget meant that on any substantial repository the install --
    #: not the test -- was what timed out.
    install_timeout_seconds: float = Field(default=900.0, ge=1.0, le=1_800.0)

    #: Whether this contract's install commands came from a deterministic probe of
    #: committed repository files rather than from a literal `.patchproof.yaml`. A
    #: synthesized contract validates its install commands through
    #: `patchproof.install_strategy`'s template allowlist instead of the narrow literal
    #: set; both paths forbid model involvement and forbid free-form strings.
    synthesized: bool = False

    @model_validator(mode="after")
    def validate_install_commands(self) -> ExecutionContract:
        if not self.synthesized and len(self.install) > 2:
            raise ValueError("a repository-declared contract may declare at most two installs")
        for command in self.install:
            if not command or len(command) > 8:
                raise ValueError("command must contain 1-8 bounded argument tokens")
            if any(_SAFE_TOKEN.fullmatch(token) is None for token in command):
                raise ValueError("command must contain 1-8 bounded argument tokens")
            if self.synthesized:
                # Imported lazily: install_strategy imports nothing from this module, but
                # keeping the dependency one-directional at import time avoids a cycle if
                # that ever changes.
                from patchproof.install_strategy import validate_probed_install_command

                validate_probed_install_command(command)
            elif command not in _ALLOWED_INSTALL_COMMANDS:
                raise ValueError("command is not an allowed PatchProof execution template")
        return self

    @field_validator("allowed_test_paths")
    @classmethod
    def validate_allowed_test_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_directory_prefix(path) for path in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed test paths must be unique")
        return normalized

    def resolved_test_command(self, *, python_executable: Path) -> tuple[str, ...]:
        """Resolve the special `python` token without invoking a command shell."""
        command = self.test.command
        if command[0] == "python":
            return (str(python_executable.resolve()), *command[1:])
        return command

    def permits_test_path(self, relative_path: str) -> bool:
        """Return whether a normalized candidate path is under an allowed directory."""
        return any(relative_path.startswith(prefix) for prefix in self.allowed_test_paths)


class ExecutionContractLoader:
    """Load a small YAML contract from a repository workspace with safe parsing."""

    filename = ".patchproof.yaml"

    def __init__(self, *, max_bytes: int = 8_192) -> None:
        if max_bytes <= 0:
            raise ValueError("execution-contract byte budget must be positive")
        self.max_bytes = max_bytes

    def load(self, workspace: Path) -> ExecutionContract:
        """Read and validate the contract without executing or interpolating its values."""
        workspace = workspace.resolve()
        path = workspace / self.filename
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ExecutionContractError(
                "repository execution contract could not be read"
            ) from error
        return self.load_bytes(raw)

    def load_bytes(self, raw: bytes) -> ExecutionContract:
        """Validate contract bytes read from an immutable Git object."""
        if not raw or len(raw) > self.max_bytes:
            raise ExecutionContractError("repository execution contract is empty or oversized")
        try:
            document = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise ExecutionContractError(
                "repository execution contract is not valid UTF-8 YAML"
            ) from error
        if not isinstance(document, dict):
            raise ExecutionContractError("repository execution contract must be a YAML mapping")
        try:
            return ExecutionContract.model_validate(document)
        except ValidationError as error:
            raise ExecutionContractError(
                "repository execution contract failed validation"
            ) from error
