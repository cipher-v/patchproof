"""Deterministic, bounded Git context retrieval for one immutable PR revision pair."""

from __future__ import annotations

import ast
import re
import subprocess
import tokenize
from dataclasses import dataclass, fields
from enum import StrEnum
from io import BytesIO
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchproof.models import Revision, RevisionRole

_HUNK_PATTERN = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
    flags=re.MULTILINE,
)
_WORD_BOUNDARY_TEMPLATE = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])"
_TRUNCATION_MARKER = "\n... [deterministically truncated by PatchProof]"


class ContextRetrievalError(RuntimeError):
    """Raised when immutable Git context cannot be retrieved safely."""


class ChangeStatus(StrEnum):
    """Normalized Git name-status value."""

    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"
    COPIED = "COPIED"
    TYPE_CHANGED = "TYPE_CHANGED"
    UNMERGED = "UNMERGED"
    UNKNOWN = "UNKNOWN"


class SymbolKind(StrEnum):
    """Python syntax construct intersecting a changed line range."""

    FUNCTION = "FUNCTION"
    ASYNC_FUNCTION = "ASYNC_FUNCTION"
    METHOD = "METHOD"
    ASYNC_METHOD = "ASYNC_METHOD"
    CLASS = "CLASS"
    MODULE = "MODULE"


class SnippetKind(StrEnum):
    """Why a bounded source excerpt was selected."""

    CHANGED_SYMBOL = "CHANGED_SYMBOL"
    LIKELY_TEST = "LIKELY_TEST"
    SYMBOL_REFERENCE = "SYMBOL_REFERENCE"
    IMPORT = "IMPORT"


def _validate_repository_path(value: str) -> str:
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("repository path contains an unsupported character")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("repository path must be a normalized relative POSIX path")
    return value


class ChangedFile(BaseModel):
    """One deterministic name-status entry from the immutable Git diff."""

    model_config = ConfigDict(frozen=True)

    path: str
    status: ChangeStatus
    previous_path: str | None = None
    is_python: bool
    is_test: bool

    @field_validator("path", "previous_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return _validate_repository_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_rename_source(self) -> ChangedFile:
        expects_previous = self.status in {ChangeStatus.RENAMED, ChangeStatus.COPIED}
        if expects_previous != (self.previous_path is not None):
            raise ValueError("renamed or copied files must identify exactly one previous path")
        return self


class ChangedSymbol(BaseModel):
    """A Python symbol whose source span intersects the Git hunk."""

    model_config = ConfigDict(frozen=True)

    path: str
    qualified_name: str = Field(min_length=1, max_length=256)
    kind: SymbolKind
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_repository_path(value)

    @model_validator(mode="after")
    def validate_line_range(self) -> ChangedSymbol:
        if self.end_line < self.start_line:
            raise ValueError("symbol end line cannot precede its start line")
        return self


class ContextSnippet(BaseModel):
    """A bounded source excerpt selected by deterministic relevance rules."""

    model_config = ConfigDict(frozen=True)

    kind: SnippetKind
    path: str
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    content: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_repository_path(value)

    @model_validator(mode="after")
    def validate_line_range(self) -> ContextSnippet:
        if self.end_line < self.start_line:
            raise ValueError("snippet end line cannot precede its start line")
        return self


class RetrievalStats(BaseModel):
    """Audit facts about bounded local scanning and truncation."""

    model_config = ConfigDict(frozen=True)

    changed_file_count: int = Field(ge=0)
    changed_python_file_count: int = Field(ge=0)
    python_path_count: int = Field(ge=0)
    test_files_scanned: int = Field(ge=0)
    reference_files_scanned: int = Field(ge=0)
    omitted_changed_files: int = Field(ge=0)
    truncated: bool


class PullRequestContext(BaseModel):
    """Prompt-safe bounded facts derived from immutable BASE and HEAD commits."""

    model_config = ConfigDict(frozen=True)

    base_sha: str
    head_sha: str
    diff: str
    changed_files: tuple[ChangedFile, ...]
    changed_symbols: tuple[ChangedSymbol, ...]
    snippets: tuple[ContextSnippet, ...]
    stats: RetrievalStats

    @field_validator("base_sha", "head_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        return Revision(role=RevisionRole.BASE, sha=value).sha


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Hard deterministic limits for local scanning and prompt-bound context."""

    max_changed_files: int = 80
    max_diff_chars: int = 14_000
    max_diff_chars_per_file: int = 3_000
    max_symbols: int = 24
    max_snippets: int = 16
    max_snippet_chars: int = 1_800
    max_source_file_bytes: int = 256 * 1_024
    max_python_paths: int = 5_000
    max_test_files_scanned: int = 200
    max_reference_files_scanned: int = 200
    max_context_json_chars: int = 48_000
    git_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for budget_field in fields(self):
            if getattr(self, budget_field.name) <= 0:
                raise ValueError(f"context budget {budget_field.name} must be positive")
        if self.max_diff_chars_per_file > self.max_diff_chars:
            raise ValueError("per-file diff budget cannot exceed total diff budget")


@dataclass(frozen=True, slots=True)
class _SymbolSpan:
    qualified_name: str
    kind: SymbolKind
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class _RankedSnippet:
    score: int
    snippet: ContextSnippet


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.spans: list[_SymbolSpan] = []
        self.scope: list[tuple[str, bool]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record(node, SymbolKind.CLASS)
        self.scope.append((node.name, True))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = SymbolKind.METHOD if self.scope and self.scope[-1][1] else SymbolKind.FUNCTION
        self._record(node, kind)
        self.scope.append((node.name, False))
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = (
            SymbolKind.ASYNC_METHOD
            if self.scope and self.scope[-1][1]
            else SymbolKind.ASYNC_FUNCTION
        )
        self._record(node, kind)
        self.scope.append((node.name, False))
        self.generic_visit(node)
        self.scope.pop()

    def _record(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: SymbolKind,
    ) -> None:
        decorator_lines = [decorator.lineno for decorator in node.decorator_list]
        start_line = min([node.lineno, *decorator_lines])
        self.spans.append(
            _SymbolSpan(
                qualified_name=".".join((*[name for name, _ in self.scope], node.name)),
                kind=kind,
                start_line=start_line,
                end_line=node.end_lineno or node.lineno,
            )
        )


class DeterministicContextRetriever:
    """Select diff, symbols, tests, imports, and references without model involvement."""

    def __init__(
        self,
        *,
        source_repository: Path,
        budget: ContextBudget | None = None,
    ) -> None:
        self.source_repository = source_repository.resolve()
        self.budget = budget or ContextBudget()
        self._source_cache: dict[tuple[str, str], str | None] = {}
        if not self.source_repository.is_dir():
            raise ContextRetrievalError(
                f"source repository does not exist: {self.source_repository}"
            )
        self._run_git(("rev-parse", "--git-dir"))

    def retrieve(self, *, base_sha: str, head_sha: str) -> PullRequestContext:
        """Build one JSON-bounded context bundle for an immutable revision pair."""
        base = self._resolve_sha(base_sha, RevisionRole.BASE)
        head = self._resolve_sha(head_sha, RevisionRole.HEAD)
        all_changed_files = self._changed_files(base.sha, head.sha)
        omitted_changed_files = max(0, len(all_changed_files) - self.budget.max_changed_files)
        changed_files = all_changed_files[: self.budget.max_changed_files]
        truncated = omitted_changed_files > 0

        diff, diff_truncated = self._bounded_diff(base.sha, head.sha, changed_files)
        truncated = truncated or diff_truncated
        changed_symbols, changed_snippets, imports = self._changed_python_context(
            base.sha, head.sha, changed_files
        )

        python_paths, paths_truncated = self._python_paths(head.sha)
        truncated = truncated or paths_truncated
        likely_tests, test_files_scanned = self._likely_test_snippets(
            head.sha, python_paths, changed_files, changed_symbols
        )
        references, reference_files_scanned = self._reference_snippets(
            head.sha, python_paths, changed_files, changed_symbols
        )

        ranked = [*changed_snippets, *imports, *likely_tests, *references]
        ranked.sort(key=lambda item: (-item.score, item.snippet.path, item.snippet.start_line))
        snippets = tuple(item.snippet for item in ranked[: self.budget.max_snippets])
        if len(ranked) > len(snippets):
            truncated = True

        context = PullRequestContext(
            base_sha=base.sha,
            head_sha=head.sha,
            diff=diff,
            changed_files=tuple(changed_files),
            changed_symbols=tuple(changed_symbols[: self.budget.max_symbols]),
            snippets=snippets,
            stats=RetrievalStats(
                changed_file_count=len(all_changed_files),
                changed_python_file_count=sum(file.is_python for file in all_changed_files),
                python_path_count=len(python_paths),
                test_files_scanned=test_files_scanned,
                reference_files_scanned=reference_files_scanned,
                omitted_changed_files=omitted_changed_files,
                truncated=truncated or len(changed_symbols) > self.budget.max_symbols,
            ),
        )
        return self._fit_json_budget(context)

    def committed_paths(self, revision_sha: str, *, max_paths: int = 20_000) -> frozenset[str]:
        """Return the complete bounded path set from one immutable tree for overwrite checks."""
        if max_paths <= 0:
            raise ValueError("committed path budget must be positive")
        revision = self._resolve_sha(revision_sha, RevisionRole.HEAD)
        output = self._run_git(("ls-tree", "-r", "-z", "--name-only", revision.sha)).stdout
        raw_paths = output.rstrip(b"\0").split(b"\0") if output else []
        if len(raw_paths) > max_paths:
            raise ContextRetrievalError(
                "immutable repository path count exceeds the safe overwrite-check budget"
            )
        return frozenset(self._decode_path(path) for path in raw_paths)

    def read_committed_file(self, *, revision_sha: str, path: str, max_bytes: int = 8_192) -> bytes:
        """Read one bounded file directly from an immutable Git tree."""
        if max_bytes <= 0:
            raise ValueError("committed file byte budget must be positive")
        revision = self._resolve_sha(revision_sha, RevisionRole.HEAD)
        normalized_path = self._decode_path(path.encode("utf-8"))
        completed = self._run_git(("show", f"{revision.sha}:{normalized_path}"), check=False)
        if completed.returncode != 0:
            raise ContextRetrievalError(
                f"required committed file is unavailable: {normalized_path}"
            )
        if len(completed.stdout) > max_bytes:
            raise ContextRetrievalError(
                f"required committed file exceeds its byte budget: {normalized_path}"
            )
        return completed.stdout

    def _resolve_sha(self, value: str, role: RevisionRole) -> Revision:
        requested = Revision(role=role, sha=value)
        completed = self._run_git(("rev-parse", "--verify", f"{requested.sha}^{{commit}}"))
        actual = Revision(role=role, sha=completed.stdout.decode("ascii").strip())
        if actual.sha != requested.sha:
            raise ContextRetrievalError("revision did not resolve to the requested immutable SHA")
        return actual

    def _changed_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        output = self._run_git(
            ("diff", "--name-status", "-z", "--find-renames=50%", base_sha, head_sha, "--")
        ).stdout
        tokens = output.rstrip(b"\0").split(b"\0") if output else []
        files: list[ChangedFile] = []
        index = 0
        while index < len(tokens):
            status_token = tokens[index].decode("ascii", errors="strict")
            index += 1
            status = self._normalize_status(status_token)
            previous_path: str | None = None
            if status in {ChangeStatus.RENAMED, ChangeStatus.COPIED}:
                if index + 1 >= len(tokens):
                    raise ContextRetrievalError("Git returned an incomplete rename/copy record")
                previous_path = self._decode_path(tokens[index])
                path = self._decode_path(tokens[index + 1])
                index += 2
            else:
                if index >= len(tokens):
                    raise ContextRetrievalError("Git returned an incomplete name-status record")
                path = self._decode_path(tokens[index])
                index += 1
            files.append(
                ChangedFile(
                    path=path,
                    previous_path=previous_path,
                    status=status,
                    is_python=path.endswith(".py"),
                    is_test=self._is_test_path(path),
                )
            )
        return files

    def _bounded_diff(
        self, base_sha: str, head_sha: str, changed_files: list[ChangedFile]
    ) -> tuple[str, bool]:
        prioritized = sorted(
            changed_files,
            key=lambda item: (not item.is_python, item.is_test, item.path),
        )
        sections: list[str] = []
        remaining = self.budget.max_diff_chars
        truncated = False
        for changed_file in prioritized:
            if remaining <= len(_TRUNCATION_MARKER):
                truncated = True
                break
            paths = [changed_file.path]
            if changed_file.previous_path is not None:
                paths.insert(0, changed_file.previous_path)
            arguments = (
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--unified=3",
                base_sha,
                head_sha,
                "--",
                *(f":(literal){path}" for path in paths),
            )
            section = self._run_git(arguments).stdout.decode("utf-8", errors="replace")
            if not section:
                continue
            per_file_limit = min(self.budget.max_diff_chars_per_file, remaining)
            bounded, was_truncated = self._truncate(section, per_file_limit)
            sections.append(bounded)
            remaining -= len(bounded)
            truncated = truncated or was_truncated
        return "\n".join(sections), truncated

    def _changed_python_context(
        self,
        base_sha: str,
        head_sha: str,
        changed_files: list[ChangedFile],
    ) -> tuple[list[ChangedSymbol], list[_RankedSnippet], list[_RankedSnippet]]:
        symbols: list[ChangedSymbol] = []
        snippets: list[_RankedSnippet] = []
        imports: list[_RankedSnippet] = []
        for changed_file in changed_files:
            if not changed_file.is_python:
                continue
            use_base = changed_file.status is ChangeStatus.DELETED
            revision = base_sha if use_base else head_sha
            path = (
                changed_file.previous_path
                if use_base and changed_file.previous_path
                else changed_file.path
            )
            source = self._read_source(revision, path)
            if source is None:
                continue
            tree = self._parse_python(source, path)
            if tree is None:
                continue
            line_ranges = self._changed_line_ranges(base_sha, head_sha, changed_file, use_base)
            visitor = _SymbolVisitor()
            visitor.visit(tree)
            intersecting = [
                span
                for span in visitor.spans
                if any(
                    span.start_line <= range_end and range_start <= span.end_line
                    for range_start, range_end in line_ranges
                )
            ]
            if not intersecting:
                start = min((item[0] for item in line_ranges), default=1)
                end = max((item[1] for item in line_ranges), default=start)
                intersecting = [
                    _SymbolSpan(
                        qualified_name="<module>",
                        kind=SymbolKind.MODULE,
                        start_line=start,
                        end_line=end,
                    )
                ]
            for span in intersecting:
                symbol = ChangedSymbol(
                    path=changed_file.path,
                    qualified_name=span.qualified_name,
                    kind=span.kind,
                    start_line=span.start_line,
                    end_line=span.end_line,
                )
                if symbol not in symbols:
                    symbols.append(symbol)
                    snippets.append(
                        _RankedSnippet(
                            score=120 if not changed_file.is_test else 110,
                            snippet=self._source_snippet(
                                kind=SnippetKind.CHANGED_SYMBOL,
                                path=changed_file.path,
                                source=source,
                                start_line=max(1, span.start_line - 2),
                                end_line=span.end_line + 2,
                                reason=f"changed {span.kind.value.lower()} {span.qualified_name}",
                            ),
                        )
                    )
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports.append(
                        _RankedSnippet(
                            score=55,
                            snippet=self._source_snippet(
                                kind=SnippetKind.IMPORT,
                                path=changed_file.path,
                                source=source,
                                start_line=node.lineno,
                                end_line=node.end_lineno or node.lineno,
                                reason="import used by a changed Python file",
                            ),
                        )
                    )
        symbols.sort(key=lambda item: (item.path, item.start_line, item.qualified_name))
        return symbols, snippets, imports[:4]

    def _changed_line_ranges(
        self,
        base_sha: str,
        head_sha: str,
        changed_file: ChangedFile,
        use_base: bool,
    ) -> list[tuple[int, int]]:
        paths = [changed_file.path]
        if changed_file.previous_path is not None:
            paths.insert(0, changed_file.previous_path)
        output = self._run_git(
            (
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--unified=0",
                base_sha,
                head_sha,
                "--",
                *(f":(literal){path}" for path in paths),
            )
        ).stdout.decode("utf-8", errors="replace")
        ranges: list[tuple[int, int]] = []
        for match in _HUNK_PATTERN.finditer(output):
            start = int(match.group(1 if use_base else 3))
            count_text = match.group(2 if use_base else 4)
            count = int(count_text) if count_text is not None else 1
            anchor = max(1, start)
            ranges.append((anchor, anchor if count == 0 else anchor + count - 1))
        return ranges or [(1, 1)]

    def _python_paths(self, head_sha: str) -> tuple[list[str], bool]:
        output = self._run_git(("ls-tree", "-r", "-z", "--name-only", head_sha)).stdout
        paths: list[str] = []
        for raw_path in output.rstrip(b"\0").split(b"\0") if output else []:
            path = self._decode_path(raw_path)
            if path.endswith(".py"):
                paths.append(path)
        paths.sort()
        truncated = len(paths) > self.budget.max_python_paths
        return paths[: self.budget.max_python_paths], truncated

    def _likely_test_snippets(
        self,
        head_sha: str,
        python_paths: list[str],
        changed_files: list[ChangedFile],
        changed_symbols: list[ChangedSymbol],
    ) -> tuple[list[_RankedSnippet], int]:
        changed_paths = {item.path for item in changed_files}
        symbol_names = self._symbol_terms(changed_symbols)
        module_terms = {
            PurePosixPath(item.path).stem for item in changed_symbols if item.path.endswith(".py")
        }
        test_paths = [path for path in python_paths if self._is_test_path(path)]
        test_paths.sort(key=lambda path: (path not in changed_paths, path))
        ranked: list[_RankedSnippet] = []
        scanned = 0
        for path in test_paths[: self.budget.max_test_files_scanned]:
            source = self._read_source(head_sha, path)
            scanned += 1
            if source is None:
                continue
            score = 80 if path in changed_paths else 0
            matched = [term for term in symbol_names if self._contains_word(source, term)]
            score += 12 * len(matched)
            score += 3 * sum(term in path for term in module_terms)
            if score == 0:
                continue
            start, end = self._best_test_span(source, path, matched)
            reason = (
                f"changed test file referencing {', '.join(matched[:3])}"
                if path in changed_paths and matched
                else "changed test file"
                if path in changed_paths
                else f"test references changed symbol(s): {', '.join(matched[:3])}"
            )
            ranked.append(
                _RankedSnippet(
                    score=score,
                    snippet=self._source_snippet(
                        kind=SnippetKind.LIKELY_TEST,
                        path=path,
                        source=source,
                        start_line=start,
                        end_line=end,
                        reason=reason,
                    ),
                )
            )
        return ranked, scanned

    def _reference_snippets(
        self,
        head_sha: str,
        python_paths: list[str],
        changed_files: list[ChangedFile],
        changed_symbols: list[ChangedSymbol],
    ) -> tuple[list[_RankedSnippet], int]:
        changed_paths = {item.path for item in changed_files}
        terms = self._symbol_terms(changed_symbols)
        if not terms:
            return [], 0
        candidates = [
            path
            for path in python_paths
            if path not in changed_paths and not self._is_test_path(path)
        ]
        stem_terms = {PurePosixPath(item.path).stem for item in changed_symbols}
        candidates.sort(key=lambda path: (-sum(term in path for term in stem_terms), path))
        ranked: list[_RankedSnippet] = []
        scanned = 0
        for path in candidates[: self.budget.max_reference_files_scanned]:
            source = self._read_source(head_sha, path)
            scanned += 1
            if source is None:
                continue
            for line_number, line in enumerate(source.splitlines(), start=1):
                matched = [term for term in terms if self._contains_word(line, term)]
                if not matched:
                    continue
                ranked.append(
                    _RankedSnippet(
                        score=35 + 5 * len(matched),
                        snippet=self._source_snippet(
                            kind=SnippetKind.SYMBOL_REFERENCE,
                            path=path,
                            source=source,
                            start_line=max(1, line_number - 2),
                            end_line=line_number + 2,
                            reason=f"source references changed symbol(s): {', '.join(matched[:3])}",
                        ),
                    )
                )
                break
        return ranked, scanned

    def _read_source(self, revision: str, path: str) -> str | None:
        key = (revision, path)
        if key in self._source_cache:
            return self._source_cache[key]
        completed = self._run_git(("show", f"{revision}:{path}"), check=False)
        if completed.returncode != 0 or len(completed.stdout) > self.budget.max_source_file_bytes:
            self._source_cache[key] = None
            return None
        try:
            encoding, _ = tokenize.detect_encoding(BytesIO(completed.stdout).readline)
            source = completed.stdout.decode(encoding)
        except (LookupError, SyntaxError, UnicodeDecodeError):
            source = None
        self._source_cache[key] = source
        return source

    @staticmethod
    def _parse_python(source: str, path: str) -> ast.Module | None:
        try:
            return ast.parse(source, filename=path)
        except (SyntaxError, ValueError):
            return None

    def _source_snippet(
        self,
        *,
        kind: SnippetKind,
        path: str,
        source: str,
        start_line: int,
        end_line: int,
        reason: str,
    ) -> ContextSnippet:
        lines = source.splitlines()
        bounded_start = min(max(1, start_line), max(1, len(lines)))
        bounded_end = min(max(bounded_start, end_line), max(1, len(lines)))
        content = "\n".join(lines[bounded_start - 1 : bounded_end]) or "# empty source line"
        content, _ = self._truncate(content, self.budget.max_snippet_chars)
        return ContextSnippet(
            kind=kind,
            path=path,
            start_line=bounded_start,
            end_line=bounded_end,
            content=content,
            reason=reason,
        )

    @staticmethod
    def _best_test_span(source: str, path: str, terms: list[str]) -> tuple[int, int]:
        tree = DeterministicContextRetriever._parse_python(source, path)
        if tree is not None:
            candidates = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ]
            for node in sorted(candidates, key=lambda item: item.lineno):
                segment = ast.get_source_segment(source, node) or ""
                if not terms or any(
                    DeterministicContextRetriever._contains_word(segment, term) for term in terms
                ):
                    return max(1, node.lineno - 1), (node.end_lineno or node.lineno) + 1
        return 1, min(40, max(1, len(source.splitlines())))

    @staticmethod
    def _symbol_terms(changed_symbols: list[ChangedSymbol]) -> list[str]:
        terms = {
            symbol.qualified_name.rsplit(".", maxsplit=1)[-1]
            for symbol in changed_symbols
            if symbol.kind is not SymbolKind.MODULE
        }
        return sorted(term for term in terms if len(term) >= 2)

    @staticmethod
    def _contains_word(text: str, term: str) -> bool:
        return re.search(_WORD_BOUNDARY_TEMPLATE.format(re.escape(term)), text) is not None

    def _fit_json_budget(self, context: PullRequestContext) -> PullRequestContext:
        candidate = context
        while len(candidate.model_dump_json()) > self.budget.max_context_json_chars:
            if len(candidate.diff) > 2_000:
                bounded, _ = self._truncate(candidate.diff, max(2_000, len(candidate.diff) - 2_000))
                candidate = candidate.model_copy(update={"diff": bounded})
            elif candidate.snippets:
                candidate = candidate.model_copy(update={"snippets": candidate.snippets[:-1]})
            elif len(candidate.changed_files) > 1:
                candidate = candidate.model_copy(
                    update={"changed_files": candidate.changed_files[:-1]}
                )
            else:
                raise ContextRetrievalError("minimum context exceeds configured JSON budget")
            candidate = candidate.model_copy(
                update={"stats": candidate.stats.model_copy(update={"truncated": True})}
            )
        return candidate

    def _run_git(
        self, arguments: tuple[str, ...], *, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        command = ("git", "-C", str(self.source_repository), *arguments)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=self.budget.git_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ContextRetrievalError(f"Git command could not complete: {command!r}") from error
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ContextRetrievalError(
                f"Git command failed with exit code {completed.returncode}: {detail}"
            )
        return completed

    @staticmethod
    def _normalize_status(value: str) -> ChangeStatus:
        return {
            "A": ChangeStatus.ADDED,
            "M": ChangeStatus.MODIFIED,
            "D": ChangeStatus.DELETED,
            "R": ChangeStatus.RENAMED,
            "C": ChangeStatus.COPIED,
            "T": ChangeStatus.TYPE_CHANGED,
            "U": ChangeStatus.UNMERGED,
        }.get(value[:1], ChangeStatus.UNKNOWN)

    @staticmethod
    def _decode_path(value: bytes) -> str:
        try:
            path = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextRetrievalError("Git path is not valid UTF-8") from error
        try:
            return _validate_repository_path(path)
        except ValueError as error:
            raise ContextRetrievalError(str(error)) from error

    @staticmethod
    def _is_test_path(path: str) -> bool:
        pure_path = PurePosixPath(path)
        return (
            "tests" in pure_path.parts
            or pure_path.name.startswith("test_")
            or pure_path.name.endswith("_test.py")
        )

    @staticmethod
    def _truncate(value: str, maximum: int) -> tuple[str, bool]:
        if len(value) <= maximum:
            return value, False
        if maximum <= len(_TRUNCATION_MARKER):
            return _TRUNCATION_MARKER[:maximum], True
        return value[: maximum - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER, True
