from workspace_registry import ProjectRegistry, Workspace, resolve_owner


def test_nested_workspace_resolution_prefers_deepest_owner() -> None:
    registry = ProjectRegistry(
        [
            Workspace(name="monorepo", root="."),
            Workspace(name="backend", root="backend"),
            Workspace(name="api", root="backend/services/api"),
        ]
    )

    owner = resolve_owner(registry, "backend/services/api/src/handler.py")

    assert owner is not None and owner.name == "api"
