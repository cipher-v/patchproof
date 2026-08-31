"""Git revision resolution and temporary detached-worktree management."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from patchproof.models import Revision, RevisionRole


class GitWorkspaceError(RuntimeError):
    """Raised when immutable Git workspaces cannot be prepared or cleaned up."""


@dataclass(frozen=True, slots=True)
class RevisionWorkspaces:
    """Detached worktrees paired with their resolved immutable revisions."""

    base_revision: Revision
    head_revision: Revision
    base_path: Path
    head_path: Path


class GitWorkspaceManager:
    """Resolve refs once and materialize short-lived detached Git worktrees."""

    def __init__(
        self,
        *,
        source_repository: Path,
        workspace_root: Path,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        if git_timeout_seconds <= 0:
            raise ValueError("Git command timeout must be positive")
        self.source_repository = source_repository.resolve()
        self.workspace_root = workspace_root.resolve()
        self.git_timeout_seconds = git_timeout_seconds

        if not self.source_repository.is_dir():
            raise GitWorkspaceError(f"source repository does not exist: {self.source_repository}")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._run_git(("rev-parse", "--git-dir"))

    def resolve_revision(self, ref: str, role: RevisionRole) -> Revision:
        """Resolve an input ref to the full commit hash used for all later operations."""
        if not ref or "\x00" in ref or "\n" in ref or "\r" in ref:
            raise ValueError("Git revision reference is empty or contains a control character")
        completed = self._run_git(("rev-parse", "--verify", f"{ref}^{{commit}}"))
        return Revision(role=role, sha=completed.stdout.strip())

    @contextmanager
    def create_pair(self, *, base_ref: str, head_ref: str) -> Iterator[RevisionWorkspaces]:
        """Yield verified detached BASE and HEAD worktrees, then remove them."""
        base_revision = self.resolve_revision(base_ref, RevisionRole.BASE)
        head_revision = self.resolve_revision(head_ref, RevisionRole.HEAD)
        run_root = Path(tempfile.mkdtemp(prefix="r-", dir=self.workspace_root))
        base_path = run_root / "base"
        head_path = run_root / "head"
        registered_paths: list[Path] = []
        active_error: BaseException | None = None

        try:
            for revision, path in (
                (base_revision, base_path),
                (head_revision, head_path),
            ):
                self._run_git(("worktree", "add", "--detach", str(path), revision.sha))
                registered_paths.append(path)
                actual_sha = self._run_git(("-C", str(path), "rev-parse", "HEAD")).stdout.strip()
                if actual_sha.lower() != revision.sha:
                    raise GitWorkspaceError(
                        f"worktree revision mismatch: expected {revision.sha}, got {actual_sha}"
                    )

            yield RevisionWorkspaces(
                base_revision=base_revision,
                head_revision=head_revision,
                base_path=base_path,
                head_path=head_path,
            )
        except BaseException as error:
            active_error = error
            raise
        finally:
            cleanup_failures: list[str] = []
            for path in reversed(registered_paths):
                completed = self._run_git(("worktree", "remove", "--force", str(path)), check=False)
                if completed.returncode != 0 and path.exists():
                    cleanup_failures.append(completed.stderr.strip() or str(path))
                    self._remove_tree(path)

            self._run_git(("worktree", "prune"), check=False)
            if run_root.exists():
                self._remove_tree(run_root)
            if cleanup_failures or run_root.exists():
                details = "; ".join(cleanup_failures) or str(run_root)
                message = f"failed to clean temporary worktrees: {details}"
                if active_error is not None:
                    active_error.add_note(message)
                else:
                    raise GitWorkspaceError(message)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        """Retry disposal briefly for Windows scanners releasing vanished cache entries."""
        for attempt in range(20):
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                return
            if attempt < 19:
                time.sleep(0.1)

    def _run_git(
        self, arguments: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = ("git", "-C", str(self.source_repository), *arguments)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.git_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitWorkspaceError(f"Git command could not complete: {command!r}") from error

        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GitWorkspaceError(
                f"Git command failed with exit code {completed.returncode}: {detail}"
            )
        return completed
