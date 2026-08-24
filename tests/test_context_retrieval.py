"""Integration tests for deterministic bounded Git context selection."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ContextRepositoryHistory

from patchproof.context_retrieval import (
    ContextBudget,
    ContextRetrievalError,
    DeterministicContextRetriever,
    SnippetKind,
)


def test_retriever_selects_diff_symbol_test_import_and_reference(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    context = DeterministicContextRetriever(source_repository=history.path).retrieve(
        base_sha=history.base_sha,
        head_sha=history.head_sha,
    )

    assert context.base_sha == history.base_sha
    assert context.head_sha == history.head_sha
    assert [changed.path for changed in context.changed_files] == ["workspace.py"]
    assert "return max(candidates" in context.diff
    assert any(
        symbol.path == "workspace.py"
        and symbol.qualified_name == "WorkspaceResolver.choose_workspace"
        for symbol in context.changed_symbols
    )
    assert any(
        snippet.kind is SnippetKind.LIKELY_TEST and snippet.path == "tests/test_workspace.py"
        for snippet in context.snippets
    )
    assert any(
        snippet.kind is SnippetKind.SYMBOL_REFERENCE and snippet.path == "consumer.py"
        for snippet in context.snippets
    )
    assert any(snippet.kind is SnippetKind.IMPORT for snippet in context.snippets)
    assert context.stats.test_files_scanned == 1
    assert context.stats.reference_files_scanned >= 1


def test_retriever_reads_commits_not_uncommitted_worktree_content(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    (history.path / "workspace.py").write_text(
        "SECRET_UNCOMMITTED_WORKTREE_CONTENT = True\n",
        encoding="utf-8",
    )

    context = DeterministicContextRetriever(source_repository=history.path).retrieve(
        base_sha=history.base_sha,
        head_sha=history.head_sha,
    )

    assert "SECRET_UNCOMMITTED_WORKTREE_CONTENT" not in context.model_dump_json()
    assert "return max(candidates" in context.diff


def test_context_json_and_individual_sections_obey_small_budgets(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    budget = ContextBudget(
        max_changed_files=5,
        max_diff_chars=220,
        max_diff_chars_per_file=220,
        max_symbols=2,
        max_snippets=2,
        max_snippet_chars=180,
        max_source_file_bytes=10_000,
        max_python_paths=20,
        max_test_files_scanned=5,
        max_reference_files_scanned=5,
        max_context_json_chars=2_000,
    )

    context = DeterministicContextRetriever(
        source_repository=history.path,
        budget=budget,
    ).retrieve(base_sha=history.base_sha, head_sha=history.head_sha)

    assert len(context.model_dump_json()) <= budget.max_context_json_chars
    assert len(context.diff) <= budget.max_diff_chars
    assert all(len(snippet.content) <= budget.max_snippet_chars for snippet in context.snippets)
    assert context.stats.truncated is True


def test_same_revision_produces_empty_but_valid_context(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history

    context = DeterministicContextRetriever(source_repository=history.path).retrieve(
        base_sha=history.head_sha,
        head_sha=history.head_sha,
    )

    assert context.diff == ""
    assert context.changed_files == ()
    assert context.changed_symbols == ()
    assert context.snippets == ()


def test_partial_or_unknown_revision_is_rejected(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    retriever = DeterministicContextRetriever(source_repository=context_repository_history.path)

    with pytest.raises(ValueError, match="full 40- or 64-character"):
        retriever.retrieve(base_sha="abc123", head_sha=context_repository_history.head_sha)

    with pytest.raises(ContextRetrievalError, match="Git command failed"):
        retriever.retrieve(base_sha="f" * 40, head_sha=context_repository_history.head_sha)


def test_missing_repository_is_rejected(writable_test_directory: Path) -> None:
    with pytest.raises(ContextRetrievalError, match="does not exist"):
        DeterministicContextRetriever(source_repository=writable_test_directory / "does-not-exist")
