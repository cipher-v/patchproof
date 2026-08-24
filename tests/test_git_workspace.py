"""Integration tests for immutable detached Git workspaces."""

from __future__ import annotations

import subprocess

from conftest import RepositoryHistory

from patchproof.git_workspace import GitWorkspaceManager
from patchproof.models import RevisionRole


def _head_sha(worktree: str) -> str:
    return subprocess.run(
        ("git", "-C", worktree, "rev-parse", "HEAD"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_manager_resolves_full_shas_and_cleans_detached_worktrees(
    repository_history: RepositoryHistory,
) -> None:
    manager = GitWorkspaceManager(
        source_repository=repository_history.path,
        workspace_root=repository_history.workspace_root,
    )

    with manager.create_pair(base_ref="HEAD~1", head_ref="HEAD") as pair:
        assert pair.base_revision.role is RevisionRole.BASE
        assert pair.head_revision.role is RevisionRole.HEAD
        assert pair.base_revision.sha == repository_history.base_sha
        assert pair.head_revision.sha == repository_history.head_sha
        assert _head_sha(str(pair.base_path)) == repository_history.base_sha
        assert _head_sha(str(pair.head_path)) == repository_history.head_sha
        assert pair.base_path.is_dir()
        assert pair.head_path.is_dir()

    assert list(repository_history.workspace_root.iterdir()) == []
