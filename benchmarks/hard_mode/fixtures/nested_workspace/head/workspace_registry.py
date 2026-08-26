from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Workspace:
    name: str
    root: str

    def contains(self, candidate_path: str) -> bool:
        candidate = PurePosixPath(candidate_path)
        root = PurePosixPath(self.root)
        return self.root == "." or candidate == root or root in candidate.parents


class ProjectRegistry:
    def __init__(self, workspaces: list[Workspace]) -> None:
        self._workspaces = tuple(workspaces)

    def candidates_for(self, candidate_path: str) -> tuple[Workspace, ...]:
        return tuple(
            workspace
            for workspace in self._workspaces
            if workspace.contains(candidate_path)
        )


def resolve_owner(registry: ProjectRegistry, candidate_path: str) -> Workspace | None:
    candidates = registry.candidates_for(candidate_path)
    if not candidates:
        return None
    return max(candidates, key=lambda workspace: len(PurePosixPath(workspace.root).parts))
