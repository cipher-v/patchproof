"""A repository's own pytest configuration must not be able to abort a challenge.

These are regression tests for the harness defect identified in
`docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md` section B.2. In the sealed unseen
holdout, anyio and cattrs both terminated as `PROCESS_ERROR` rather than producing
comparable assertion outcomes. The cause was not a missing module: it was that the
executor suppressed pytest plugin autoload for every child process while the
repositories declared plugin options in their own ``addopts``. pytest rejected the
unknown options during argument parsing and exited before a JUnit report existed.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from patchproof.challenge import BaseHeadChallenge
from patchproof.execution_contract import ExecutionContract
from patchproof.execution_runtime import ChildProcessEnvironmentPolicy
from patchproof.git_workspace import GitWorkspaceManager
from patchproof.models import (
    DifferentialPattern,
    MechanicalEvidenceStatus,
    TestArtifact,
    TestExecutionStatus,
)
from patchproof.pytest_runner import (
    DISABLED_PYTEST_PLUGINS,
    PYTEST_ISOLATION_ARGUMENTS,
    PytestRunner,
)

_ARTIFACT_PATH = "tests/patchproof_generated/test_patchproof_generated.py"
_CONTRACT = {
    "version": 1,
    "python": "3.12",
    "install": [["uv", "sync", "--frozen"]],
    "test": {"command": ["python", "-m", "pytest"]},
    "allowed_test_paths": ["tests/patchproof_generated/"],
    "timeout_seconds": 60,
}
_EXECUTION_CONTRACT_YAML = """version: 1
python: "3.12"
install:
  - ["uv", "sync", "--frozen"]
test:
  command: ["python", "-m", "pytest"]
allowed_test_paths:
  - "tests/patchproof_generated/"
timeout_seconds: 60
"""

# A repository that demands a plugin option PatchProof's environment does not provide.
# Before the fix this aborted pytest during argument parsing on both revisions.
_HOSTILE_PYPROJECT = """[tool.pytest.ini_options]
addopts = "--benchmark-disable --strict-markers -p no:doctest"
"""


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


@pytest.fixture
def hostile_config_repository() -> Iterator[tuple[Path, Path, str, str]]:
    """Two commits of a repository whose own pytest config would break a naive runner."""
    root = Path.cwd() / ".test-runs" / f"hostile-{uuid.uuid4().hex}"
    repository = root / "repo"
    workspaces = root / "workspaces"
    repository.mkdir(parents=True)
    workspaces.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "config", "user.name", "PatchProof Tests")
    _git(repository, "config", "user.email", "patchproof-tests@example.invalid")
    _git(repository, "config", "commit.gpgsign", "false")

    (repository / ".patchproof.yaml").write_text(_EXECUTION_CONTRACT_YAML, encoding="utf-8")
    (repository / "pyproject.toml").write_text(_HOSTILE_PYPROJECT, encoding="utf-8")
    module = repository / "calculator.py"
    module.write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "buggy addition with hostile pytest configuration")
    base_sha = _git(repository, "rev-parse", "HEAD")

    module.write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fix addition")
    head_sha = _git(repository, "rev-parse", "HEAD")

    try:
        yield repository, workspaces, base_sha, head_sha
    finally:
        subprocess.run(
            ("git", "-C", str(repository), "worktree", "prune"),
            capture_output=True,
            check=False,
        )
        _remove_tree(root)


def _remove_tree(path: Path) -> None:
    import shutil
    import stat

    def _force(function, target: str, _error) -> None:
        try:
            Path(target).chmod(stat.S_IWRITE)
            function(target)
        except FileNotFoundError:
            return

    shutil.rmtree(path, onexc=_force)


def test_isolation_arguments_neutralize_repository_addopts() -> None:
    """`-o addopts=` must be present, or any repository option can abort the run."""
    assert "-o" in PYTEST_ISOLATION_ARGUMENTS
    index = PYTEST_ISOLATION_ARGUMENTS.index("-o")
    assert PYTEST_ISOLATION_ARGUMENTS[index + 1] == "addopts="


def test_isolation_disables_only_named_interfering_plugins() -> None:
    """Comparability-breaking plugins are named; autoload as a whole is not suppressed."""
    assert set(DISABLED_PYTEST_PLUGINS) == {"cacheprovider", "randomly", "xdist", "cov"}
    for plugin in DISABLED_PYTEST_PLUGINS:
        assert f"no:{plugin}" in PYTEST_ISOLATION_ARGUMENTS
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in ChildProcessEnvironmentPolicy.inherited_names


def test_repository_addopts_cannot_abort_a_base_head_challenge(
    hostile_config_repository: tuple[Path, Path, str, str],
) -> None:
    """The holdout's anyio/cattrs PROCESS_ERROR shape must now discriminate normally."""
    repository, workspaces, base_sha, head_sha = hostile_config_repository
    challenge = BaseHeadChallenge(
        workspaces=GitWorkspaceManager(
            source_repository=repository,
            workspace_root=workspaces,
        ),
        runner=PytestRunner(
            contract=ExecutionContract.model_validate(_CONTRACT),
            python_executable=Path(sys.executable),
            install_dependencies=False,
            repository_python_paths=(".",),
        ),
    )
    artifact = TestArtifact.from_text(
        relative_path=_ARTIFACT_PATH,
        node_id=f"{_ARTIFACT_PATH}::test_patchproof_generated_behavior",
        content="from calculator import add\n\n\ndef test_patchproof_generated_behavior():\n"
        "    assert add(2, 3) == 5\n",
    )

    result = challenge.run(base_ref=base_sha, head_ref=head_sha, artifact=artifact)

    assert result.base.status is TestExecutionStatus.ASSERTION_FAILED
    assert result.head.status is TestExecutionStatus.PASSED
    assert result.assessment.mechanical_status is MechanicalEvidenceStatus.DISCRIMINATING
    assert result.assessment.pattern is DifferentialPattern.BASE_ASSERTION_FAILED_HEAD_PASSED
