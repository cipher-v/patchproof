"""Tests for case-neutral arbitrary-PR deterministic preparation."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from patchproof.install_strategy import InstallStrategy
from patchproof.pr_resolution import ResolvedPullRequest
from patchproof.product_preparation import (
    PreparedPullRequest,
    ProductExecutionPlanSource,
    UnsupportedExecutionPlanError,
    prepare_pull_request,
    resolve_product_execution_plan,
)


class FakeRevisionReader:
    def __init__(self, revisions: dict[str, dict[str, bytes]]) -> None:
        self.revisions = revisions

    def committed_paths(self, revision_sha: str, *, max_paths: int = 20_000) -> frozenset[str]:
        del max_paths
        return frozenset(self.revisions[revision_sha])

    def read_committed_file(self, *, revision_sha: str, path: str, max_bytes: int = 8_192) -> bytes:
        content = self.revisions[revision_sha][path]
        if len(content) > max_bytes:
            raise ValueError("oversized fixture")
        return content


def _resolved(*, base_sha: str, head_sha: str) -> ResolvedPullRequest:
    return ResolvedPullRequest(
        repository="owner/repo",
        number=37,
        url="https://github.com/owner/repo/pull/37",
        base_sha=base_sha,
        head_sha=head_sha,
        title="Fix behavior",
        body="Details",
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def test_changed_tests_are_excluded_and_runtime_python_is_prioritized(
    context_repository_history,
) -> None:
    repository = context_repository_history.path
    base_sha = context_repository_history.head_sha
    (repository / "workspace.py").write_text(
        (repository / "workspace.py").read_text(encoding="utf-8") + "\nRUNTIME_FLAG = True\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_workspace.py").write_text(
        "def test_new_behavior() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _git(repository, "add", "workspace.py", "tests/test_workspace.py")
    _git(repository, "commit", "-m", "change runtime and tests")
    head_sha = _git(repository, "rev-parse", "HEAD")

    prepared = prepare_pull_request(
        resolved=_resolved(base_sha=base_sha, head_sha=head_sha),
        source_repository=repository,
    )

    assert prepared.excluded_paths == ("tests/test_workspace.py",)
    assert prepared.priority_paths == ("workspace.py",)
    assert prepared.execution_plan.source is ProductExecutionPlanSource.REPOSITORY_CONTRACT


def test_equivalent_base_head_probe_plan_is_accepted() -> None:
    base_sha, head_sha = "a" * 40, "b" * 40
    manifest = b"[project]\nname='example'\n"
    reader = FakeRevisionReader(
        {
            base_sha: {"pyproject.toml": manifest},
            head_sha: {"pyproject.toml": manifest, "src/example.py": b"VALUE = 2\n"},
        }
    )

    plan = resolve_product_execution_plan(
        reader=reader,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert plan.source is ProductExecutionPlanSource.DETERMINISTIC_PROBE
    assert plan.base_install is not None
    assert plan.head_install is not None
    assert plan.base_install.strategy is InstallStrategy.UV_PIP_EDITABLE
    assert plan.base_install.commands == plan.head_install.commands
    assert plan.contract.synthesized


@pytest.mark.parametrize(
    "revisions",
    (
        {
            "a" * 40: {
                "pyproject.toml": (
                    b"[project]\nname='example'\n[project.optional-dependencies]\ntest=['pytest']\n"
                )
            },
            "b" * 40: {"pyproject.toml": b"[project]\nname='example'\n"},
        },
        {
            "a" * 40: {"pyproject.toml": b"[project]\nname='example'\n"},
            "b" * 40: {"README.md": b"no packaging metadata"},
        },
    ),
)
def test_asymmetric_or_unsupported_probe_plan_fails_closed(revisions) -> None:
    with pytest.raises(UnsupportedExecutionPlanError, match="supported equivalent"):
        resolve_product_execution_plan(
            reader=FakeRevisionReader(revisions),
            base_sha="a" * 40,
            head_sha="b" * 40,
        )


def test_repository_contract_must_exist_identically_on_both_revisions() -> None:
    base_sha, head_sha = "a" * 40, "b" * 40
    reader = FakeRevisionReader(
        {
            base_sha: {".patchproof.yaml": b"version: 1\n"},
            head_sha: {"pyproject.toml": b"[project]\nname='example'\n"},
        }
    )

    with pytest.raises(UnsupportedExecutionPlanError, match="do not both declare"):
        resolve_product_execution_plan(
            reader=reader,
            base_sha=base_sha,
            head_sha=head_sha,
        )


def test_product_preparation_model_contains_no_benchmark_or_oracle_fields() -> None:
    fields = set(PreparedPullRequest.__dataclass_fields__)

    assert fields == {"resolved", "excluded_paths", "priority_paths", "execution_plan"}
    assert not fields & {
        "oracle_source",
        "oracle_sha256",
        "expected_base_result",
        "expected_head_result",
        "category",
        "difficulty_rationale",
        "hidden_labels",
    }
