"""Deterministic, bounded static index over one immutable Python repository revision."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import threading
import time
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchproof.models import Revision

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class RepositoryIndexError(RuntimeError):
    """Raised when an immutable repository cannot be indexed within its safety contract."""


def validate_repository_source_path(value: str) -> str:
    """Validate one normalized repository-relative POSIX Python source path."""
    if not isinstance(value, str) or len(value) > 1_024:
        raise ValueError("repository path must be text within the path length limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("repository path contains a control character")
    if "\\" in value or re.match(r"[A-Za-z]:/", value):
        raise ValueError("repository path must use POSIX separators")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", "..", ".git"} for part in path.parts)
        or path.suffix != ".py"
    ):
        raise ValueError("repository path must be a normalized relative Python source path")
    return value


class ObservableSymbolKind(StrEnum):
    """Statically observable Python definition kinds supported by the index."""

    FUNCTION = "FUNCTION"
    ASYNC_FUNCTION = "ASYNC_FUNCTION"
    METHOD = "METHOD"
    ASYNC_METHOD = "ASYNC_METHOD"
    CLASS = "CLASS"
    PROPERTY = "PROPERTY"
    MODULE_VALUE = "MODULE_VALUE"


class ReferenceKind(StrEnum):
    """Syntactic reference forms; none claim dynamic resolution."""

    NAME = "NAME"
    ATTRIBUTE = "ATTRIBUTE"
    CALL = "CALL"


class PythonParseStatus(StrEnum):
    """Whether a decoded Python file produced an AST."""

    PARSED = "PARSED"
    SYNTAX_ERROR = "SYNTAX_ERROR"


class IndexedPythonFile(BaseModel):
    """Content-addressed metadata for one bounded immutable Python file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    module: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    line_count: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parse_status: PythonParseStatus
    is_test: bool

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repository_source_path(value)


class IndexedSymbol(BaseModel):
    """One observable identity aggregating every same-name AST definition in a file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    module: str = Field(min_length=1, max_length=512)
    qualified_name: str = Field(min_length=1, max_length=512)
    kind: ObservableSymbolKind
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    parent_symbol: str | None = Field(default=None, max_length=512)
    public: bool
    exported: bool | None
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    definition_count: int = Field(default=1, ge=1)
    definition_kinds: tuple[ObservableSymbolKind, ...] = Field(min_length=1, max_length=7)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repository_source_path(value)

    @model_validator(mode="after")
    def validate_range(self) -> IndexedSymbol:
        if self.end_line < self.start_line:
            raise ValueError("symbol end line cannot precede its start line")
        if self.kind not in self.definition_kinds or len(set(self.definition_kinds)) != len(
            self.definition_kinds
        ):
            raise ValueError("symbol definition kinds must be unique and include its primary kind")
        return self


class IndexedImport(BaseModel):
    """One static import binding, without pretending it resolves at runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    scope: str = Field(min_length=1, max_length=512)
    module: str = Field(min_length=1, max_length=512)
    imported_name: str | None = Field(default=None, max_length=256)
    alias: str = Field(min_length=1, max_length=256)
    imported_target: str = Field(min_length=1, max_length=768)
    alias_target: str | None = Field(default=None, max_length=768)
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    relative_level: int = Field(ge=0, le=100)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repository_source_path(value)


class IndexedReference(BaseModel):
    """One syntactic reference with optional lexical module-import alias expansion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    scope: str = Field(min_length=1, max_length=512)
    kind: ReferenceKind
    name: str = Field(min_length=1, max_length=256)
    expression: str = Field(min_length=1, max_length=768)
    module_import_alias_expansion: str | None = Field(default=None, max_length=768)
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    column: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repository_source_path(value)


class RepositoryIndexStats(BaseModel):
    """Auditable budget and omission facts for one index build."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    python_files_discovered: int = Field(ge=0)
    files_indexed: int = Field(ge=0)
    oversized_files_skipped: int = Field(ge=0)
    undecodable_files_skipped: int = Field(ge=0)
    symlinks_skipped: int = Field(ge=0)
    excluded_files_skipped: int = Field(ge=0)
    file_limit_omitted: int = Field(ge=0)
    total_byte_budget_omitted: int = Field(ge=0)
    symbols_omitted: int = Field(ge=0)
    imports_omitted: int = Field(ge=0)
    references_omitted: int = Field(ge=0)
    syntax_errors: int = Field(ge=0)
    symbol_count: int = Field(ge=0)
    import_count: int = Field(ge=0)
    reference_count: int = Field(ge=0)
    total_source_bytes: int = Field(ge=0)
    truncated: bool


@dataclass(frozen=True, slots=True)
class RepositoryIndexBudget:
    """Operator-controlled hard limits for deterministic static indexing."""

    max_files: int = 2_000
    max_file_bytes: int = 256 * 1_024
    max_total_source_bytes: int = 32 * 1_024 * 1_024
    max_symbols: int = 30_000
    max_imports: int = 30_000
    max_references: int = 150_000
    max_tree_listing_bytes: int = 16 * 1_024 * 1_024
    git_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for item in fields(self):
            if getattr(self, item.name) <= 0:
                raise ValueError(f"repository index budget {item.name} must be positive")
        ceilings = {
            "max_files": 10_000,
            "max_file_bytes": 1_024 * 1_024,
            "max_total_source_bytes": 128 * 1_024 * 1_024,
            "max_symbols": 100_000,
            "max_imports": 100_000,
            "max_references": 500_000,
            "max_tree_listing_bytes": 64 * 1_024 * 1_024,
            "git_timeout_seconds": 120.0,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise ValueError(f"repository index budget {name} exceeds its safety ceiling")
        if self.max_file_bytes > self.max_total_source_bytes:
            raise ValueError("per-file byte budget cannot exceed total source byte budget")


@dataclass(frozen=True, slots=True)
class _Scope:
    name: str
    is_class: bool
    local_bindings: frozenset[str] = frozenset()


class _ScopeBindingCollector(ast.NodeVisitor):
    """Collect conservative lexical bindings without entering nested scopes."""

    def __init__(self) -> None:
        self.bindings: set[str] = set()
        self.globals: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bindings.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.bindings.update(
            alias.asname or alias.name.split(".", maxsplit=1)[0] for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.bindings.update(
            alias.asname or alias.name for alias in node.names if alias.name != "*"
        )

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.bindings.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.bindings.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bindings.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _scope_bindings(body: list[ast.stmt], arguments: ast.arguments | None = None) -> frozenset[str]:
    collector = _ScopeBindingCollector()
    if arguments is not None:
        collector.bindings.update(
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        )
        if arguments.vararg is not None:
            collector.bindings.add(arguments.vararg.arg)
        if arguments.kwarg is not None:
            collector.bindings.add(arguments.kwarg.arg)
    for statement in body:
        collector.visit(statement)
    return frozenset(collector.bindings - collector.globals)


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return "tests" in pure.parts or pure.name.startswith("test_") or pure.name.endswith("_test.py")


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _literal_exports(tree: ast.Module) -> frozenset[str] | None:
    for node in tree.body:
        value: ast.AST | None = None
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            value = node.value
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            continue
        names: list[str] = []
        for element in value.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                return None
            names.append(element.value)
        return frozenset(names)
    return None


class _ImportCollector(ast.NodeVisitor):
    def __init__(self, path: str, maximum: int) -> None:
        self.path = path
        self.module = _module_name(path)
        self.maximum = maximum
        self.scope: list[_Scope] = []
        self.imports: list[IndexedImport] = []
        self.truncated = False
        self.omitted_count = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(_Scope(node.name, True))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(_Scope(node.name, False))
        self.generic_visit(node)
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
            self._append(
                node,
                module=alias.name,
                imported_name=None,
                alias=bound,
                imported_target=alias.name,
                alias_target=alias.name if alias.asname else bound,
                relative_level=0,
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            bound = alias.asname or alias.name
            absolute_target = f"{node.module}.{alias.name}" if node.module else alias.name
            imported_target = f"{'.' * node.level}{absolute_target}"
            alias_target = absolute_target
            if node.level:
                module_parts = self.module.split(".") if self.module else []
                if PurePosixPath(self.path).name != "__init__.py" and module_parts:
                    module_parts.pop()
                ascend = node.level - 1
                if ascend > len(module_parts):
                    alias_target = None
                else:
                    prefix = module_parts[: len(module_parts) - ascend]
                    suffix = [*(node.module or "").split("."), alias.name]
                    alias_target = ".".join(part for part in (*prefix, *suffix) if part)
            self._append(
                node,
                module=module or ".",
                imported_name=alias.name,
                alias=bound,
                imported_target=imported_target,
                alias_target=alias_target,
                relative_level=node.level,
            )

    def _append(
        self,
        node: ast.Import | ast.ImportFrom,
        *,
        module: str,
        imported_name: str | None,
        alias: str,
        imported_target: str,
        alias_target: str | None,
        relative_level: int,
    ) -> None:
        if len(self.imports) >= self.maximum:
            self.truncated = True
            self.omitted_count += 1
            return
        self.imports.append(
            IndexedImport(
                path=self.path,
                scope=".".join(item.name for item in self.scope) or "<module>",
                module=module,
                imported_name=imported_name,
                alias=alias,
                imported_target=imported_target,
                alias_target=alias_target,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                relative_level=relative_level,
            )
        )


class _DefinitionAndReferenceVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        path: str,
        module: str,
        source_lines: list[str],
        exports: frozenset[str] | None,
        aliases: Mapping[str, str],
        max_symbols: int,
        max_references: int,
    ) -> None:
        self.path = path
        self.module = module
        self.source_lines = source_lines
        self.exports = exports
        self.aliases = aliases
        self.max_symbols = max_symbols
        self.max_references = max_references
        self.scope: list[_Scope] = []
        self.symbols: list[IndexedSymbol] = []
        self.references: list[IndexedReference] = []
        self.symbols_truncated = False
        self.references_truncated = False
        self.symbols_omitted = 0
        self.references_omitted = 0
        self._symbol_indexes: dict[str, int] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record_symbol(node, ObservableSymbolKind.CLASS)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        self.scope.append(_Scope(node.name, True, _scope_bindings(node.body)))
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, asynchronous=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, asynchronous=True)

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, asynchronous: bool
    ) -> None:
        is_method = bool(self.scope and self.scope[-1].is_class)
        decorator_names = tuple(_dotted_name(item) or "" for item in node.decorator_list)
        if is_method and any(
            item == "property" or item.endswith((".getter", ".setter", ".deleter"))
            for item in decorator_names
        ):
            kind = ObservableSymbolKind.PROPERTY
        elif is_method:
            kind = (
                ObservableSymbolKind.ASYNC_METHOD if asynchronous else ObservableSymbolKind.METHOD
            )
        else:
            kind = (
                ObservableSymbolKind.ASYNC_FUNCTION
                if asynchronous
                else ObservableSymbolKind.FUNCTION
            )
        self._record_symbol(node, kind)
        for decorator in node.decorator_list:
            self.visit(decorator)
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        for argument in (arguments.vararg, arguments.kwarg):
            if argument is not None and argument.annotation is not None:
                self.visit(argument.annotation)
        for default in (*arguments.defaults, *arguments.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        self.scope.append(_Scope(node.name, False, _scope_bindings(node.body, node.args)))
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.scope:
            for target in node.targets:
                for name in self._assignment_names(target):
                    self._record_module_value(name, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self.scope and node.value is not None:
            for name in self._assignment_names(node.target):
                self._record_module_value(name, node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record_reference(node, ReferenceKind.NAME, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record_reference(node, ReferenceKind.ATTRIBUTE, _dotted_name(node) or node.attr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        expression = _dotted_name(node.func)
        if expression:
            self._record_reference(node.func, ReferenceKind.CALL, expression)
        self.generic_visit(node)

    @staticmethod
    def _assignment_names(node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Name):
            return (node.id,)
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(
                name
                for element in node.elts
                for name in _DefinitionAndReferenceVisitor._assignment_names(element)
            )
        return ()

    def _record_module_value(self, name: str, node: ast.Assign | ast.AnnAssign) -> None:
        if _IDENTIFIER.fullmatch(name) is None:
            return
        self._append_symbol(
            qualified_name=name,
            kind=ObservableSymbolKind.MODULE_VALUE,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        )

    def _record_symbol(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: ObservableSymbolKind,
    ) -> None:
        decorators = [decorator.lineno for decorator in node.decorator_list]
        self._append_symbol(
            qualified_name=".".join((*[item.name for item in self.scope], node.name)),
            kind=kind,
            start_line=min([node.lineno, *decorators]),
            end_line=node.end_lineno or node.lineno,
        )

    def _append_symbol(
        self,
        *,
        qualified_name: str,
        kind: ObservableSymbolKind,
        start_line: int,
        end_line: int,
    ) -> None:
        content = "\n".join(self.source_lines[start_line - 1 : end_line])
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing_index = self._symbol_indexes.get(qualified_name)
        if existing_index is not None:
            existing = self.symbols[existing_index]
            kinds = tuple(dict.fromkeys((*existing.definition_kinds, kind)))
            aggregate_hash = hashlib.sha256(
                f"{existing.source_sha256}\0{content_hash}".encode()
            ).hexdigest()
            self.symbols[existing_index] = existing.model_copy(
                update={
                    "start_line": min(existing.start_line, start_line),
                    "end_line": max(existing.end_line, end_line),
                    "source_sha256": aggregate_hash,
                    "definition_count": existing.definition_count + 1,
                    "definition_kinds": kinds,
                }
            )
            return
        if len(self.symbols) >= self.max_symbols:
            self.symbols_truncated = True
            self.symbols_omitted += 1
            return
        leaf = qualified_name.rsplit(".", maxsplit=1)[-1]
        top_level = "." not in qualified_name
        self._symbol_indexes[qualified_name] = len(self.symbols)
        self.symbols.append(
            IndexedSymbol(
                path=self.path,
                module=self.module,
                qualified_name=qualified_name,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                parent_symbol=(
                    qualified_name.rsplit(".", maxsplit=1)[0] if "." in qualified_name else None
                ),
                public=not leaf.startswith("_"),
                exported=(leaf in self.exports if self.exports is not None and top_level else None),
                source_sha256=content_hash,
                definition_kinds=(kind,),
            )
        )

    def _record_reference(self, node: ast.AST, kind: ReferenceKind, expression: str) -> None:
        if len(self.references) >= self.max_references:
            self.references_truncated = True
            self.references_omitted += 1
            return
        leaf = expression.rsplit(".", maxsplit=1)[-1]
        if _IDENTIFIER.fullmatch(leaf) is None:
            return
        prefix, separator, suffix = expression.partition(".")
        shadowed = any(prefix in scope.local_bindings for scope in self.scope)
        resolved = None if shadowed else self.aliases.get(prefix)
        if resolved and separator:
            resolved = f"{resolved}.{suffix}"
        elif not resolved:
            resolved = None
        self.references.append(
            IndexedReference(
                path=self.path,
                scope=".".join(item.name for item in self.scope) or "<module>",
                kind=kind,
                name=leaf,
                expression=expression,
                module_import_alias_expansion=resolved,
                start_line=getattr(node, "lineno", 1),
                end_line=getattr(node, "end_lineno", None) or getattr(node, "lineno", 1),
                column=getattr(node, "col_offset", 0),
            )
        )


class RepositoryIndex:
    """Immutable in-memory index constructed once for one validated Git revision."""

    __slots__ = (
        "_files_by_path",
        "_sources",
        "budget",
        "files",
        "imports",
        "references",
        "revision",
        "sha256",
        "stats",
        "symbols",
        "test_functions",
    )

    def __init__(
        self,
        *,
        revision: Revision,
        budget: RepositoryIndexBudget,
        files: tuple[IndexedPythonFile, ...],
        symbols: tuple[IndexedSymbol, ...],
        imports: tuple[IndexedImport, ...],
        references: tuple[IndexedReference, ...],
        sources: Mapping[str, str],
        stats: RepositoryIndexStats,
    ) -> None:
        self.revision = revision
        self.budget = budget
        self.files = files
        self.symbols = symbols
        self.imports = imports
        self.references = references
        self._sources = MappingProxyType(dict(sources))
        self._files_by_path = MappingProxyType({item.path: item for item in files})
        self.test_functions = tuple(
            symbol
            for symbol in symbols
            if symbol.qualified_name.rsplit(".", maxsplit=1)[-1].startswith("test_")
            and self._files_by_path[symbol.path].is_test
            and symbol.kind
            in {
                ObservableSymbolKind.FUNCTION,
                ObservableSymbolKind.ASYNC_FUNCTION,
                ObservableSymbolKind.METHOD,
                ObservableSymbolKind.ASYNC_METHOD,
            }
        )
        self.stats = stats
        canonical = self.canonical_json
        self.sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_files(
        cls,
        *,
        revision: Revision,
        files: Mapping[str, bytes | str],
        budget: RepositoryIndexBudget | None = None,
        excluded_paths: frozenset[str] = frozenset(),
        priority_paths: frozenset[str] = frozenset(),
    ) -> RepositoryIndex:
        """Build from already trusted immutable bytes, primarily for adapters and tests."""
        active_budget = budget or RepositoryIndexBudget()
        try:
            excluded = frozenset(validate_repository_source_path(path) for path in excluded_paths)
        except ValueError as error:
            raise RepositoryIndexError(f"invalid excluded repository path: {error}") from error
        try:
            priority = frozenset(validate_repository_source_path(path) for path in priority_paths)
        except ValueError as error:
            raise RepositoryIndexError(f"invalid priority repository path: {error}") from error
        normalized: dict[str, bytes] = {}
        excluded_count = 0
        for path, content in files.items():
            normalized_path = validate_repository_source_path(path)
            if normalized_path in excluded:
                excluded_count += 1
                continue
            if normalized_path in normalized:
                raise RepositoryIndexError(f"duplicate repository path: {normalized_path}")
            if isinstance(content, str):
                normalized[normalized_path] = content.encode("utf-8")
            elif isinstance(content, bytes):
                normalized[normalized_path] = content
            else:
                raise TypeError("repository source content must be bytes or text")
        return cls._build(
            revision=revision,
            raw_files=normalized,
            budget=active_budget,
            discovered=len(normalized) + excluded_count,
            symlinks_skipped=0,
            excluded_files_skipped=excluded_count,
            priority_paths=priority,
        )

    @classmethod
    def from_git(
        cls,
        *,
        source_repository: Path,
        revision: Revision,
        budget: RepositoryIndexBudget | None = None,
        excluded_paths: frozenset[str] = frozenset(),
        priority_paths: frozenset[str] = frozenset(),
    ) -> RepositoryIndex:
        """Build using only fixed read-only Git object commands over one full commit SHA."""
        active_budget = budget or RepositoryIndexBudget()
        repository = source_repository.resolve()
        if not repository.is_dir():
            raise RepositoryIndexError("source repository does not exist")
        try:
            excluded = frozenset(validate_repository_source_path(path) for path in excluded_paths)
        except ValueError as error:
            raise RepositoryIndexError(f"invalid excluded repository path: {error}") from error
        try:
            priority = frozenset(validate_repository_source_path(path) for path in priority_paths)
        except ValueError as error:
            raise RepositoryIndexError(f"invalid priority repository path: {error}") from error
        deadline = time.monotonic() + active_budget.git_timeout_seconds

        def run(
            arguments: tuple[str, ...], *, output_limit: int, input_bytes: bytes | None = None
        ) -> bytes:
            process: subprocess.Popen[bytes] | None = None
            try:
                process = subprocess.Popen(
                    ("git", "-C", str(repository), *arguments),
                    stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                output = bytearray()
                exceeded = threading.Event()

                def read_stdout() -> None:
                    assert process is not None and process.stdout is not None
                    while len(output) <= output_limit:
                        chunk = process.stdout.read(min(64 * 1_024, output_limit + 1 - len(output)))
                        if not chunk:
                            return
                        output.extend(chunk)
                    exceeded.set()

                reader = threading.Thread(target=read_stdout, daemon=True)
                reader.start()
                if input_bytes is not None:
                    assert process.stdin is not None
                    process.stdin.write(input_bytes)
                    process.stdin.close()
                reader.join(max(0.0, deadline - time.monotonic()))
                if reader.is_alive() or exceeded.is_set():
                    process.kill()
                    reader.join()
                    process.wait()
                    if exceeded.is_set():
                        raise RepositoryIndexError(
                            "fixed Git object read exceeded its output budget"
                        )
                    raise RepositoryIndexError("fixed Git object read exceeded its time budget")
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as error:
                if process is not None:
                    process.kill()
                    process.wait()
                raise RepositoryIndexError("fixed Git object read could not complete") from error
            if process.returncode != 0:
                raise RepositoryIndexError("immutable Git revision could not be read")
            return bytes(output)

        resolved = run(("rev-parse", "--verify", f"{revision.sha}^{{commit}}"), output_limit=128)
        if resolved.decode("ascii", errors="strict").strip().lower() != revision.sha:
            raise RepositoryIndexError("revision did not resolve to the exact requested commit")
        listing = run(
            ("ls-tree", "-r", "-z", "--long", revision.sha),
            output_limit=active_budget.max_tree_listing_bytes,
        )
        entries: list[tuple[str, str, int]] = []
        discovered = 0
        symlinks_skipped = 0
        excluded_files_skipped = 0
        for raw_entry in listing.rstrip(b"\0").split(b"\0") if listing else []:
            metadata, separator, raw_path = raw_entry.partition(b"\t")
            if not separator:
                raise RepositoryIndexError("Git tree returned malformed metadata")
            try:
                path = raw_path.decode("utf-8")
                mode, object_type, object_id, size_text = metadata.decode("ascii").split()
            except (UnicodeDecodeError, ValueError) as error:
                raise RepositoryIndexError("Git tree metadata is not safely decodable") from error
            if not path.endswith(".py"):
                continue
            discovered += 1
            validate_repository_source_path(path)
            if path in excluded:
                excluded_files_skipped += 1
                continue
            if mode == "120000":
                symlinks_skipped += 1
                continue
            if object_type != "blob" or _OBJECT_ID.fullmatch(object_id) is None:
                raise RepositoryIndexError("Python tree entry is not a regular Git blob")
            try:
                size = int(size_text)
            except ValueError as error:
                raise RepositoryIndexError("Git blob size is invalid") from error
            entries.append((path, object_id, size))

        selected_entries: list[tuple[str, str, int]] = []
        total = 0
        file_limit_omitted = 0
        total_byte_budget_omitted = 0
        oversized = 0
        ordered_entries = sorted(entries, key=lambda item: (item[0] not in priority, item[0]))
        for path, object_id, size in ordered_entries:
            if size > active_budget.max_file_bytes:
                oversized += 1
                continue
            if len(selected_entries) >= active_budget.max_files:
                file_limit_omitted += 1
                continue
            if total + size > active_budget.max_total_source_bytes:
                total_byte_budget_omitted += 1
                continue
            selected_entries.append((path, object_id, size))
            total += size
        raw_files: dict[str, bytes] = {}
        if selected_entries:
            batch_input = b"".join(
                f"{object_id}\n".encode("ascii") for _, object_id, _ in selected_entries
            )
            batch_output = run(
                ("cat-file", "--batch"),
                output_limit=total + len(selected_entries) * 128,
                input_bytes=batch_input,
            )
            cursor = 0
            for path, object_id, size in selected_entries:
                header_end = batch_output.find(b"\n", cursor)
                if header_end < 0:
                    raise RepositoryIndexError("Git batch object read returned malformed metadata")
                try:
                    returned_id, object_type, returned_size = (
                        batch_output[cursor:header_end].decode("ascii").split()
                    )
                except (UnicodeDecodeError, ValueError) as error:
                    raise RepositoryIndexError(
                        "Git batch object metadata is not safely decodable"
                    ) from error
                if returned_id != object_id or object_type != "blob" or returned_size != str(size):
                    raise RepositoryIndexError("Git blob identity changed during index build")
                content_start = header_end + 1
                content_end = content_start + size
                if content_end >= len(batch_output) or batch_output[content_end] != 10:
                    raise RepositoryIndexError("Git batch object content is malformed")
                raw_files[path] = batch_output[content_start:content_end]
                cursor = content_end + 1
            if cursor != len(batch_output):
                raise RepositoryIndexError(
                    "Git batch object read returned unexpected trailing data"
                )
        return cls._build(
            revision=revision,
            raw_files=raw_files,
            budget=active_budget,
            discovered=discovered,
            symlinks_skipped=symlinks_skipped,
            excluded_files_skipped=excluded_files_skipped,
            pre_file_limit_omitted=file_limit_omitted,
            pre_total_byte_budget_omitted=total_byte_budget_omitted,
            pre_oversized=oversized,
            priority_paths=priority,
        )

    @classmethod
    def _build(
        cls,
        *,
        revision: Revision,
        raw_files: Mapping[str, bytes],
        budget: RepositoryIndexBudget,
        discovered: int,
        symlinks_skipped: int,
        excluded_files_skipped: int,
        pre_file_limit_omitted: int = 0,
        pre_total_byte_budget_omitted: int = 0,
        pre_oversized: int = 0,
        priority_paths: frozenset[str] = frozenset(),
    ) -> RepositoryIndex:
        selected: list[tuple[str, bytes]] = []
        oversized = pre_oversized
        file_limit_omitted = pre_file_limit_omitted
        total_byte_budget_omitted = pre_total_byte_budget_omitted
        total = 0
        truncated = any(
            (
                file_limit_omitted,
                total_byte_budget_omitted,
                symlinks_skipped,
                excluded_files_skipped,
                pre_oversized,
            )
        )
        ordered_files = sorted(
            raw_files.items(), key=lambda item: (item[0] not in priority_paths, item[0])
        )
        for path, content in ordered_files:
            if len(selected) >= budget.max_files:
                file_limit_omitted += 1
                truncated = True
                continue
            if len(content) > budget.max_file_bytes:
                oversized += 1
                truncated = True
                continue
            if total + len(content) > budget.max_total_source_bytes:
                total_byte_budget_omitted += 1
                truncated = True
                continue
            selected.append((path, content))
            total += len(content)

        indexed_files: list[IndexedPythonFile] = []
        symbols: list[IndexedSymbol] = []
        imports: list[IndexedImport] = []
        references: list[IndexedReference] = []
        sources: dict[str, str] = {}
        undecodable = 0
        syntax_errors = 0
        symbols_omitted = 0
        imports_omitted = 0
        references_omitted = 0
        for path, content in selected:
            try:
                encoding, _ = tokenize.detect_encoding(BytesIO(content).readline)
                source = content.decode(encoding)
            except (LookupError, SyntaxError, UnicodeDecodeError):
                undecodable += 1
                truncated = True
                continue
            sources[path] = source
            try:
                tree = ast.parse(source, filename=path)
                parse_status = PythonParseStatus.PARSED
            except (SyntaxError, ValueError):
                tree = None
                parse_status = PythonParseStatus.SYNTAX_ERROR
                syntax_errors += 1
                truncated = True
            indexed_files.append(
                IndexedPythonFile(
                    path=path,
                    module=_module_name(path),
                    size_bytes=len(content),
                    line_count=len(source.splitlines()),
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    parse_status=parse_status,
                    is_test=_is_test_path(path),
                )
            )
            if tree is None:
                continue
            import_visitor = _ImportCollector(path, max(0, budget.max_imports - len(imports)))
            import_visitor.visit(tree)
            imports.extend(import_visitor.imports)
            imports_omitted += import_visitor.omitted_count
            aliases = {
                item.alias: item.alias_target
                for item in import_visitor.imports
                if item.scope == "<module>"
                and item.alias_target is not None
                and item.imported_name != "*"
            }
            visitor = _DefinitionAndReferenceVisitor(
                path=path,
                module=_module_name(path),
                source_lines=source.splitlines(),
                exports=_literal_exports(tree),
                aliases=aliases,
                max_symbols=max(0, budget.max_symbols - len(symbols)),
                max_references=max(0, budget.max_references - len(references)),
            )
            visitor.visit(tree)
            symbols.extend(visitor.symbols)
            references.extend(visitor.references)
            symbols_omitted += visitor.symbols_omitted
            references_omitted += visitor.references_omitted
            truncated = truncated or any(
                (
                    import_visitor.truncated,
                    visitor.symbols_truncated,
                    visitor.references_truncated,
                )
            )

        indexed_files.sort(key=lambda item: item.path)
        symbols.sort(key=lambda item: (item.path, item.start_line, item.qualified_name, item.kind))
        imports.sort(
            key=lambda item: (item.path, item.start_line, item.module, item.imported_name or "")
        )
        references.sort(
            key=lambda item: (
                item.path,
                item.start_line,
                item.column,
                item.kind,
                item.expression,
            )
        )
        stats = RepositoryIndexStats(
            python_files_discovered=discovered,
            files_indexed=len(indexed_files),
            oversized_files_skipped=oversized,
            undecodable_files_skipped=undecodable,
            symlinks_skipped=symlinks_skipped,
            excluded_files_skipped=excluded_files_skipped,
            file_limit_omitted=file_limit_omitted,
            total_byte_budget_omitted=total_byte_budget_omitted,
            symbols_omitted=symbols_omitted,
            imports_omitted=imports_omitted,
            references_omitted=references_omitted,
            syntax_errors=syntax_errors,
            symbol_count=len(symbols),
            import_count=len(imports),
            reference_count=len(references),
            total_source_bytes=sum(item.size_bytes for item in indexed_files),
            truncated=truncated,
        )
        return cls(
            revision=revision,
            budget=budget,
            files=tuple(indexed_files),
            symbols=tuple(symbols),
            imports=tuple(imports),
            references=tuple(references),
            sources=sources,
            stats=stats,
        )

    @property
    def canonical_json(self) -> str:
        """Return stable metadata JSON; file bytes are represented by their SHA-256 hashes."""
        return json.dumps(
            {
                "revision": {"role": self.revision.role, "sha": self.revision.sha},
                "files": [item.model_dump(mode="json") for item in self.files],
                "symbols": [item.model_dump(mode="json") for item in self.symbols],
                "imports": [item.model_dump(mode="json") for item in self.imports],
                "references": [item.model_dump(mode="json") for item in self.references],
                "stats": self.stats.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def has_path(self, path: str) -> bool:
        """Return whether a validated source path is present in this bounded index."""
        return validate_repository_source_path(path) in self._files_by_path

    def file(self, path: str) -> IndexedPythonFile | None:
        """Return metadata only; source remains accessible through bounded investigation APIs."""
        return self._files_by_path.get(validate_repository_source_path(path))

    def _source(self, path: str) -> str | None:
        return self._sources.get(validate_repository_source_path(path))
