"""Deterministic dependency-install strategy selection from committed repository files.

Why this exists
---------------

Before this module, `ExecutionContract` admitted exactly two install commands, both
of the form ``uv sync --frozen``. That requires a committed ``uv.lock`` at both BASE
and HEAD. Almost no public Python repository satisfies that, so PatchProof's
dependency installation had only ever been exercised against its own repository, and
every historical-PR evaluation fell back to a dependency-light path that could not
import the repository's own runtime requirements. See
``docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md`` section B.3.

What this module does *not* do
------------------------------

It does not let a model choose, propose, influence, or even observe an install
command. ADR-004's invariant is preserved exactly: every command PatchProof executes
is an argument vector assembled from a fixed template set, filled only with values
read from committed repository files, and validated before use. There is no shell,
no interpolation, and no free-form string anywhere in the resulting plan.

The probe reads only files that are already committed at the revision being probed:
``uv.lock``, ``pyproject.toml``, ``setup.py``, ``setup.cfg``, and ``requirements*.txt``.
It never reads working-tree state, never reads test files, and never reads anything a
holdout has excluded.

Determinism and BASE/HEAD symmetry
----------------------------------

The probe is a pure function of the committed file set at one revision. The caller is
responsible for probing BASE and HEAD independently and refusing to proceed when the
two plans differ, which preserves the existing invariant that BASE and HEAD must
execute under identical contracts.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from patchproof.execution_contract import ExecutionContract

_REQUIREMENTS_PATTERN = re.compile(r"requirements(?:[\w.-]*)\.txt")
_REQUIREMENTS_DIR_PATTERN = re.compile(r"requirements/[\w.-]+\.txt")
_EXTRA_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,30}")

#: Extras that conventionally carry a project's test requirements, in preference order.
#: Matching is exact against names declared in the repository's own
#: ``[project.optional-dependencies]``; PatchProof never invents an extra name.
_TEST_EXTRA_PREFERENCE = ("test", "tests", "testing", "dev", "develop", "development")

#: PEP 735 dependency groups that conventionally carry test requirements.
_TEST_GROUP_PREFERENCE = ("test", "tests", "dev", "testing")

#: Every install token PatchProof may ever emit. Any assembled command whose tokens are
#: not all drawn from this set, or whose shape does not match a declared template, is
#: rejected before execution. This is a defence-in-depth check on top of template
#: construction: it makes an accidental injection of a repository-derived value into a
#: command position mechanically impossible rather than merely unlikely.
_ALLOWED_FIXED_TOKENS = frozenset(
    {
        "uv",
        "sync",
        "venv",
        "pip",
        "install",
        "--frozen",
        "--all-groups",
        "--group",
        "-e",
        "-r",
        ".",
        "pytest",
    }
)


class InstallStrategy(StrEnum):
    """How dependencies are installed for one revision, chosen from committed files."""

    #: A committed uv lockfile exists: reproduce it exactly.
    UV_SYNC_LOCKED = "UV_SYNC_LOCKED"
    #: An installable project declaring a test extra or PEP 735 test group.
    UV_PIP_EDITABLE_WITH_TEST_EXTRA = "UV_PIP_EDITABLE_WITH_TEST_EXTRA"
    #: An installable project plus a committed requirements file.
    UV_PIP_EDITABLE_WITH_REQUIREMENTS = "UV_PIP_EDITABLE_WITH_REQUIREMENTS"
    #: An installable project with no separately declared test requirements.
    UV_PIP_EDITABLE = "UV_PIP_EDITABLE"
    #: No installable project metadata was found at this revision.
    UNSUPPORTED = "UNSUPPORTED"


class RevisionFileReader(Protocol):
    """Read committed bytes and paths for one immutable revision."""

    def committed_paths(self, revision_sha: str, *, max_paths: int = ...) -> frozenset[str]: ...

    def read_committed_file(
        self, *, revision_sha: str, path: str, max_bytes: int = ...
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """A validated, ordered install argument-vector sequence for one revision."""

    strategy: InstallStrategy
    commands: tuple[tuple[str, ...], ...]
    #: Human-readable, non-secret explanation of which committed files drove the choice.
    rationale: str

    def __post_init__(self) -> None:
        if self.strategy is InstallStrategy.UNSUPPORTED:
            if self.commands:
                raise ValueError("an unsupported install plan cannot carry commands")
            return
        if not self.commands or len(self.commands) > 4:
            raise ValueError("an install plan must contain between one and four commands")
        for command in self.commands:
            validate_probed_install_command(command)

    @property
    def supported(self) -> bool:
        """Whether this revision can have its dependencies installed at all."""
        return self.strategy is not InstallStrategy.UNSUPPORTED


def validate_probed_install_command(command: tuple[str, ...]) -> None:
    """Reject any command whose tokens are not fixed vocabulary or a validated path."""
    if not command or len(command) > 6 or command[0] != "uv":
        raise ValueError("install command must be a bounded uv argument vector")
    for index, token in enumerate(command):
        if token in _ALLOWED_FIXED_TOKENS:
            continue
        # The only variable positions are a requirements path after `-r`, and an
        # editable target carrying a declared extra after `-e`.
        previous = command[index - 1] if index else ""
        if previous == "-r" and _is_requirements_path(token):
            continue
        if previous == "-e" and _EDITABLE_WITH_EXTRA_PATTERN.fullmatch(token):
            continue
        if previous == "--group" and _EXTRA_NAME_PATTERN.fullmatch(token):
            continue
        raise ValueError(f"install command contains a non-allowlisted token: {token!r}")


_EDITABLE_WITH_EXTRA_PATTERN = re.compile(r"\.\[[a-z0-9][a-z0-9._-]{0,30}\]")


def _is_requirements_path(value: str) -> bool:
    """Accept only a normalized relative committed requirements file path."""
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        return False
    if any(part in {"", ".", ".."} for part in path.parts):
        return False
    return bool(
        _REQUIREMENTS_PATTERN.fullmatch(value) or _REQUIREMENTS_DIR_PATTERN.fullmatch(value)
    )


@dataclass(frozen=True, slots=True)
class _ProjectMetadata:
    """Facts read from committed packaging metadata at one revision."""

    installable: bool
    extras: tuple[str, ...]
    dependency_groups: tuple[str, ...]


class DependencyInstallProber:
    """Choose one install strategy for a revision from its committed files only."""

    def __init__(self, *, reader: RevisionFileReader, max_metadata_bytes: int = 262_144) -> None:
        if max_metadata_bytes <= 0:
            raise ValueError("install-probe metadata byte budget must be positive")
        self.reader = reader
        self.max_metadata_bytes = max_metadata_bytes

    def probe(self, revision_sha: str) -> InstallPlan:
        """Return the install plan implied by the files committed at one revision."""
        paths = self.reader.committed_paths(revision_sha)

        if "uv.lock" in paths and "pyproject.toml" in paths:
            return InstallPlan(
                strategy=InstallStrategy.UV_SYNC_LOCKED,
                commands=(("uv", "sync", "--frozen", "--all-groups"),),
                rationale="committed uv.lock and pyproject.toml reproduce a locked environment",
            )

        metadata = self._project_metadata(revision_sha, paths)
        if not metadata.installable:
            return InstallPlan(
                strategy=InstallStrategy.UNSUPPORTED,
                commands=(),
                rationale=(
                    "no committed pyproject.toml, setup.py, or setup.cfg declares an "
                    "installable project at this revision"
                ),
            )

        group = _first_match(_TEST_GROUP_PREFERENCE, metadata.dependency_groups)
        if group is not None:
            return InstallPlan(
                strategy=InstallStrategy.UV_PIP_EDITABLE_WITH_TEST_EXTRA,
                commands=(
                    ("uv", "venv"),
                    ("uv", "pip", "install", "-e", "."),
                    ("uv", "pip", "install", "--group", group),
                    ("uv", "pip", "install", "pytest"),
                ),
                rationale=f"project declares PEP 735 dependency group {group!r}",
            )

        extra = _first_match(_TEST_EXTRA_PREFERENCE, metadata.extras)
        if extra is not None:
            return InstallPlan(
                strategy=InstallStrategy.UV_PIP_EDITABLE_WITH_TEST_EXTRA,
                commands=(
                    ("uv", "venv"),
                    ("uv", "pip", "install", "-e", f".[{extra}]"),
                    ("uv", "pip", "install", "pytest"),
                ),
                rationale=f"project declares optional-dependency extra {extra!r}",
            )

        requirements = _select_requirements_path(paths)
        if requirements is not None:
            return InstallPlan(
                strategy=InstallStrategy.UV_PIP_EDITABLE_WITH_REQUIREMENTS,
                commands=(
                    ("uv", "venv"),
                    ("uv", "pip", "install", "-r", requirements),
                    ("uv", "pip", "install", "-e", "."),
                    ("uv", "pip", "install", "pytest"),
                ),
                rationale=f"committed requirements file {requirements!r} declares dependencies",
            )

        return InstallPlan(
            strategy=InstallStrategy.UV_PIP_EDITABLE,
            commands=(
                ("uv", "venv"),
                ("uv", "pip", "install", "-e", "."),
                ("uv", "pip", "install", "pytest"),
            ),
            rationale="installable project with no separately declared test requirements",
        )

    def _project_metadata(self, revision_sha: str, paths: frozenset[str]) -> _ProjectMetadata:
        installable = bool(paths & {"setup.py", "setup.cfg"})
        extras: tuple[str, ...] = ()
        groups: tuple[str, ...] = ()
        if "pyproject.toml" in paths:
            document = self._read_pyproject(revision_sha)
            if document is not None:
                project = document.get("project")
                if isinstance(project, dict) or "build-system" in document:
                    installable = True
                extras = _declared_names(
                    project.get("optional-dependencies") if isinstance(project, dict) else None
                )
                groups = _declared_names(document.get("dependency-groups"))
        return _ProjectMetadata(installable=installable, extras=extras, dependency_groups=groups)

    def _read_pyproject(self, revision_sha: str) -> dict[str, object] | None:
        try:
            raw = self.reader.read_committed_file(
                revision_sha=revision_sha,
                path="pyproject.toml",
                max_bytes=self.max_metadata_bytes,
            )
            return tomllib.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            # A malformed or oversized manifest is treated as absent rather than fatal;
            # the probe degrades to the next strategy instead of failing the run.
            return None


def _declared_names(section: object) -> tuple[str, ...]:
    """Return validated lowercase names declared in a mapping section, or empty."""
    if not isinstance(section, dict):
        return ()
    names = []
    for key in section:
        if isinstance(key, str):
            normalized = key.strip().lower()
            if _EXTRA_NAME_PATTERN.fullmatch(normalized):
                names.append(normalized)
    return tuple(sorted(set(names)))


def _first_match(preference: tuple[str, ...], declared: tuple[str, ...]) -> str | None:
    """Return the first conventional name the repository actually declares."""
    available = set(declared)
    for name in preference:
        if name in available:
            return name
    return None


def _select_requirements_path(paths: frozenset[str]) -> str | None:
    """Choose one committed requirements file deterministically."""
    candidates = sorted(path for path in paths if _is_requirements_path(path))
    if not candidates:
        return None
    # Prefer an explicitly test-scoped requirements file, then the shortest generic one,
    # so the choice is stable across revisions rather than dependent on iteration order.
    for marker in ("test", "dev"):
        scoped = [path for path in candidates if marker in PurePosixPath(path).name.lower()]
        if scoped:
            return min(scoped, key=lambda path: (len(path), path))
    return min(candidates, key=lambda path: (len(path), path))


#: Where a synthesized contract places generated tests. This directory is deliberately
#: not a conventional test directory name, so it cannot collide with a repository's own
#: layout, and `PytestRunner` refuses to overwrite any committed path regardless.
SYNTHESIZED_TEST_PATH = "patchproof_generated_tests/"
SYNTHESIZED_TIMEOUT_SECONDS = 300.0


class ContractSynthesisError(RuntimeError):
    """Raised when a revision pair cannot be given a safe, identical execution contract."""


def synthesize_contract(plan: InstallPlan) -> ExecutionContract:
    """Build an execution contract from a probed install plan.

    The result is validated through the same `ExecutionContract` model as a
    repository-declared `.patchproof.yaml`, with `synthesized=True` selecting the
    probed-template allowlist rather than the narrow literal one.
    """
    from patchproof.execution_contract import ExecutionContract

    if not plan.supported:
        raise ContractSynthesisError(
            f"no install strategy is available for this revision: {plan.rationale}"
        )
    return ExecutionContract(
        version=1,
        python="3.12",
        install=plan.commands,
        test={"command": ["python", "-m", "pytest"]},
        allowed_test_paths=(SYNTHESIZED_TEST_PATH,),
        timeout_seconds=SYNTHESIZED_TIMEOUT_SECONDS,
        synthesized=True,
    )


def resolve_contract_for_pair(
    *,
    prober: DependencyInstallProber,
    base_sha: str,
    head_sha: str,
) -> tuple[ExecutionContract, InstallPlan, InstallPlan]:
    """Probe both revisions and return one contract only when the two plans agree.

    Requiring the BASE and HEAD plans to be identical preserves the existing invariant
    that the two revisions must execute under the same contract. A pull request that
    changes how the project is installed is therefore not comparable, and PatchProof
    abstains rather than silently comparing two different environments.
    """
    base_plan = prober.probe(base_sha)
    head_plan = prober.probe(head_sha)
    if not base_plan.supported or not head_plan.supported:
        unsupported = base_plan if not base_plan.supported else head_plan
        raise ContractSynthesisError(
            f"dependency installation is unsupported for this repository: {unsupported.rationale}"
        )
    if base_plan.commands != head_plan.commands:
        raise ContractSynthesisError(
            "BASE and HEAD imply different install strategies "
            f"({base_plan.strategy} vs {head_plan.strategy}); comparison is unsafe"
        )
    return synthesize_contract(head_plan), base_plan, head_plan
