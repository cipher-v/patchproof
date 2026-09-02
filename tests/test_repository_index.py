"""Deterministic and safety tests for immutable Python repository indexing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchproof.models import Revision, RevisionRole
from patchproof.repository_index import (
    ObservableSymbolKind,
    ReferenceKind,
    RepositoryIndex,
    RepositoryIndexBudget,
    RepositoryIndexError,
)

_REVISION = Revision(RevisionRole.HEAD, "b" * 40)

_API_SOURCE = """__all__ = ["Service", "missing"]


class _MissingType:
    pass


missing = _MissingType()
threshold: int = 3


class Service:
    @property
    def status(self):
        return "ready"

    def run(self, value):
        return helper(value)

    async def async_run(self, value):
        return value


def helper(value):
    return value + threshold


async def async_helper(value):
    return value
"""

_CONSUMER_SOURCE = """from src.pkg.api import Service as PublicService, helper
import src.pkg.api as api


def consume(value):
    service = PublicService()
    return service.run(helper(value)) + api.threshold
"""

_TEST_SOURCE = """from src.pkg.api import Service


def test_service_run():
    service = Service()
    assert service.run(1) == 4


class TestService:
    def test_status(self):
        assert Service().status == "ready"
"""


def _files(order: tuple[str, ...] | None = None) -> dict[str, str]:
    available = {
        "src/pkg/api.py": _API_SOURCE,
        "src/pkg/consumer.py": _CONSUMER_SOURCE,
        "tests/test_api.py": _TEST_SOURCE,
    }
    return {path: available[path] for path in (order or tuple(available))}


def test_index_models_definitions_imports_references_calls_and_tests() -> None:
    index = RepositoryIndex.from_files(revision=_REVISION, files=_files())

    kinds = {(item.qualified_name, item.kind) for item in index.symbols}
    assert ("Service", ObservableSymbolKind.CLASS) in kinds
    assert ("Service.status", ObservableSymbolKind.PROPERTY) in kinds
    assert ("Service.run", ObservableSymbolKind.METHOD) in kinds
    assert ("Service.async_run", ObservableSymbolKind.ASYNC_METHOD) in kinds
    assert ("helper", ObservableSymbolKind.FUNCTION) in kinds
    assert ("async_helper", ObservableSymbolKind.ASYNC_FUNCTION) in kinds
    assert ("missing", ObservableSymbolKind.MODULE_VALUE) in kinds
    assert ("threshold", ObservableSymbolKind.MODULE_VALUE) in kinds

    exported = {
        item.qualified_name: item.exported
        for item in index.symbols
        if "." not in item.qualified_name
    }
    assert exported["Service"] is True
    assert exported["missing"] is True
    assert exported["threshold"] is False

    assert any(
        item.path == "src/pkg/consumer.py"
        and item.alias == "PublicService"
        and item.imported_target == "src.pkg.api.Service"
        and item.alias_target == "src.pkg.api.Service"
        for item in index.imports
    )
    assert any(
        item.kind is ReferenceKind.CALL
        and item.expression == "PublicService"
        and item.module_import_alias_expansion == "src.pkg.api.Service"
        for item in index.references
    )
    assert any(
        item.kind is ReferenceKind.CALL
        and item.expression == "helper"
        and item.module_import_alias_expansion == "src.pkg.api.helper"
        for item in index.references
    )
    assert [item.qualified_name for item in index.test_functions] == [
        "test_service_run",
        "TestService.test_status",
    ]


def test_relative_import_aliases_expand_to_their_static_absolute_target() -> None:
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={
            "pkg/parser.py": "def parse(value):\n    return value\n",
            "pkg/api.py": (
                "from .parser import parse as _parse\n\n"
                "def public(value):\n    return _parse(value)\n"
            ),
        },
    )

    imported = next(item for item in index.imports if item.alias == "_parse")
    call = next(
        item
        for item in index.references
        if item.path == "pkg/api.py" and item.kind is ReferenceKind.CALL
    )

    assert imported.imported_target == ".parser.parse"
    assert imported.alias_target == "pkg.parser.parse"
    assert call.module_import_alias_expansion == "pkg.parser.parse"


def test_index_order_and_hash_do_not_depend_on_mapping_insertion_order() -> None:
    first = RepositoryIndex.from_files(
        revision=_REVISION,
        files=_files(("tests/test_api.py", "src/pkg/api.py", "src/pkg/consumer.py")),
    )
    second = RepositoryIndex.from_files(
        revision=_REVISION,
        files=_files(("src/pkg/consumer.py", "tests/test_api.py", "src/pkg/api.py")),
    )

    assert first.canonical_json == second.canonical_json
    assert first.sha256 == second.sha256
    assert tuple(item.path for item in first.files) == tuple(
        sorted(item.path for item in first.files)
    )


def test_module_names_and_dynamic_exports_are_conservative() -> None:
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={
            "pkg/__init__.py": "__all__ = make_exports()\nvisible = 1\n",
            "pkg/module.py": "value = 2\n",
        },
    )

    assert {item.path: item.module for item in index.files} == {
        "pkg/__init__.py": "pkg",
        "pkg/module.py": "pkg.module",
    }
    visible = next(item for item in index.symbols if item.qualified_name == "visible")
    assert visible.exported is None


def test_path_prefixes_that_are_not_git_metadata_remain_valid() -> None:
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={".github/helpers.py": "value = 1\n", ".gitish/module.py": "value = 2\n"},
    )

    assert {item.path for item in index.files} == {
        ".github/helpers.py",
        ".gitish/module.py",
    }


@pytest.mark.parametrize(
    "path",
    [
        "../secret.py",
        "/absolute.py",
        "C:/absolute.py",
        "//server/share.py",
        "pkg\\module.py",
        ".git/config.py",
        "pkg/.git/config.py",
        "pkg/./module.py",
        "pkg/../module.py",
        "pkg/evil\x00.py",
        "pkg/evil\n.py",
        "pkg/not_source.txt",
    ],
)
def test_index_rejects_unsafe_or_non_python_paths(path: str) -> None:
    with pytest.raises(ValueError):
        RepositoryIndex.from_files(revision=_REVISION, files={path: "value = 1\n"})


def test_index_skips_oversized_files_and_marks_truncation() -> None:
    budget = RepositoryIndexBudget(max_file_bytes=16, max_total_source_bytes=64)
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={"large.py": "value = 'this source is too large'\n", "small.py": "x = 1\n"},
        budget=budget,
    )

    assert tuple(item.path for item in index.files) == ("small.py",)
    assert index.stats.oversized_files_skipped == 1
    assert index.stats.truncated is True


def test_explicitly_excluded_source_never_enters_the_index() -> None:
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={"visible.py": "value = 1\n", "tests/oracle.py": "secret = 2\n"},
        excluded_paths=frozenset({"tests/oracle.py"}),
    )

    assert tuple(item.path for item in index.files) == ("visible.py",)
    assert index.stats.excluded_files_skipped == 1
    assert index.stats.truncated is True


def test_file_symbol_and_reference_budgets_truncate_deterministically() -> None:
    budget = RepositoryIndexBudget(
        max_files=2,
        max_file_bytes=1_024,
        max_total_source_bytes=2_048,
        max_symbols=1,
        max_imports=1,
        max_references=1,
    )
    first = RepositoryIndex.from_files(revision=_REVISION, files=_files(), budget=budget)
    second = RepositoryIndex.from_files(
        revision=_REVISION,
        files=_files(("tests/test_api.py", "src/pkg/consumer.py", "src/pkg/api.py")),
        budget=budget,
    )

    assert first.canonical_json == second.canonical_json
    assert first.stats.truncated is True
    assert first.stats.files_indexed == 2
    assert first.stats.file_limit_omitted == 1
    assert first.stats.symbol_count == 1
    assert first.stats.symbols_omitted > 0
    assert first.stats.import_count <= 1
    assert first.stats.imports_omitted > 0
    assert first.stats.reference_count <= 1
    assert first.stats.references_omitted > 0


def test_priority_file_is_indexed_before_alphabetical_file_truncation() -> None:
    budget = RepositoryIndexBudget(max_files=2, max_file_bytes=512, max_total_source_bytes=1_024)
    files = {
        "a_unrelated.py": "def a():\n    return 1\n",
        "b_unrelated.py": "def b():\n    return 2\n",
        "z_changed.py": "def changed():\n    return 3\n",
    }

    first = RepositoryIndex.from_files(
        revision=_REVISION,
        files=files,
        budget=budget,
        priority_paths=frozenset({"z_changed.py"}),
    )
    second = RepositoryIndex.from_files(
        revision=_REVISION,
        files=dict(reversed(tuple(files.items()))),
        budget=budget,
        priority_paths=frozenset({"z_changed.py"}),
    )

    assert {item.path for item in first.files} == {"a_unrelated.py", "z_changed.py"}
    assert first.stats.files_indexed == 2
    assert first.stats.file_limit_omitted == 1
    assert first.stats.truncated is True
    assert first.canonical_json == second.canonical_json


def test_module_value_semantics_cover_bindings_but_omit_augmented_and_attribute_targets() -> None:
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={
            "values.py": """VALUE = 1
ANNOTATED: int = 2
DECLARED: int
FACTORY = object()
LEFT, RIGHT = (1, 2)
target.attribute = 3
items[0] = 4
AUGMENTED += 1
"""
        },
    )

    values = {
        item.qualified_name
        for item in index.symbols
        if item.kind is ObservableSymbolKind.MODULE_VALUE
    }
    assert values >= {"VALUE", "ANNOTATED", "FACTORY", "LEFT", "RIGHT"}
    assert values.isdisjoint({"DECLARED", "attribute", "items", "AUGMENTED"})


def test_duplicate_property_definitions_are_aggregated_without_hiding_setter_changes() -> None:
    base_source = """class Service:
    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        self._status = value
"""
    head_source = base_source.replace("self._status = value", "self._status = value.strip()")
    base = RepositoryIndex.from_files(
        revision=Revision(RevisionRole.BASE, "a" * 40), files={"api.py": base_source}
    )
    head = RepositoryIndex.from_files(revision=_REVISION, files={"api.py": head_source})

    base_status = next(item for item in base.symbols if item.qualified_name == "Service.status")
    head_status = next(item for item in head.symbols if item.qualified_name == "Service.status")
    assert base_status.kind is ObservableSymbolKind.PROPERTY
    assert base_status.definition_count == 2
    assert base_status.definition_kinds == (ObservableSymbolKind.PROPERTY,)
    assert base_status.source_sha256 != head_status.source_sha256


def test_reference_alias_expansion_is_suppressed_by_lexical_shadowing() -> None:
    source = """from package.api import helper as imported_helper
import package.submodule

imported_helper()
package.action()

def uses_parameter(imported_helper):
    imported_helper()

def uses_local():
    imported_helper = lambda: None
    imported_helper()

def uses_module_alias():
    imported_helper()
"""
    index = RepositoryIndex.from_files(revision=_REVISION, files={"consumer.py": source})
    calls = [item for item in index.references if item.kind is ReferenceKind.CALL]

    imported_call = next(item for item in calls if item.expression == "imported_helper")
    assert imported_call.module_import_alias_expansion == "package.api.helper"
    package_call = next(item for item in calls if item.expression == "package.action")
    assert package_call.module_import_alias_expansion == "package.action"
    by_scope = {item.scope: item for item in calls if item.scope != "<module>"}
    assert by_scope["uses_parameter"].module_import_alias_expansion is None
    assert by_scope["uses_local"].module_import_alias_expansion is None
    assert by_scope["uses_module_alias"].module_import_alias_expansion == "package.api.helper"


def test_total_byte_budget_omission_is_distinct_from_oversized_files() -> None:
    budget = RepositoryIndexBudget(max_file_bytes=16, max_total_source_bytes=16)
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={"a.py": "first = 1\n", "b.py": "second = 2\n"},
        budget=budget,
    )

    assert index.stats.oversized_files_skipped == 0
    assert index.stats.total_byte_budget_omitted == 1
    assert index.stats.truncated is True


def test_syntax_error_is_retained_as_source_metadata_without_ast_claims() -> None:
    index = RepositoryIndex.from_files(
        revision=_REVISION,
        files={"broken.py": "def broken(:\n"},
    )

    assert index.files[0].parse_status == "SYNTAX_ERROR"
    assert index.stats.syntax_errors == 1
    assert index.stats.truncated is True
    assert index.symbols == ()


def _git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def test_git_index_skips_python_symlink_tree_entries(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "patchproof@example.invalid")
    _git(repository, "config", "user.name", "PatchProof Tests")
    (repository / "safe.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", "safe.py")
    _git(repository, "commit", "-m", "safe source")
    object_id = (
        _git(repository, "hash-object", "-w", "--stdin", input_bytes=b"../outside.py")
        .decode()
        .strip()
    )
    _git(repository, "update-index", "--add", "--cacheinfo", f"120000,{object_id},escape.py")
    _git(repository, "commit", "-m", "add symlink entry")
    revision_sha = _git(repository, "rev-parse", "HEAD").decode().strip()

    index = RepositoryIndex.from_git(
        source_repository=repository,
        revision=Revision(RevisionRole.HEAD, revision_sha),
    )

    assert tuple(item.path for item in index.files) == ("safe.py",)
    assert index.stats.symlinks_skipped == 1
    assert index.stats.truncated is True


def test_git_tree_enumeration_has_a_hard_output_budget(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "patchproof@example.invalid")
    _git(repository, "config", "user.name", "PatchProof Tests")
    (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", "source.py")
    _git(repository, "commit", "-m", "source")
    revision_sha = _git(repository, "rev-parse", "HEAD").decode().strip()

    with pytest.raises(RepositoryIndexError, match="output budget"):
        RepositoryIndex.from_git(
            source_repository=repository,
            revision=Revision(RevisionRole.HEAD, revision_sha),
            budget=RepositoryIndexBudget(max_tree_listing_bytes=1),
        )
