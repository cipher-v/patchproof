"""Integration tests for deterministic bounded Git context selection."""

from __future__ import annotations

import subprocess
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


def test_default_budget_retains_symbols_from_a_changed_oversized_python_file(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    module = history.path / "workspace.py"
    module.write_text(
        module.read_text(encoding="utf-8") + "# padding\n" * 21_000,
        encoding="utf-8",
    )
    assert 192 * 1_024 < module.stat().st_size < ContextBudget().max_source_file_bytes
    subprocess.run(
        ("git", "-C", str(history.path), "add", "workspace.py"),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "-C", str(history.path), "commit", "-m", "pad changed module"),
        check=True,
        capture_output=True,
    )
    large_head_sha = subprocess.run(
        ("git", "-C", str(history.path), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    context = DeterministicContextRetriever(source_repository=history.path).retrieve(
        base_sha=history.base_sha,
        head_sha=large_head_sha,
    )

    assert any(
        symbol.path == "workspace.py"
        and symbol.qualified_name == "WorkspaceResolver.choose_workspace"
        for symbol in context.changed_symbols
    )
    assert any(
        snippet.kind is SnippetKind.CHANGED_SYMBOL and snippet.path == "workspace.py"
        for snippet in context.snippets
    )


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


def test_exact_excluded_path_is_absent_from_every_prompt_bound_context_section(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    excluded_path = "tests/test_workspace.py"
    excluded_marker = "ORACLE_ONLY_EXPECTATION"
    test_path = history.path / excluded_path
    test_path.write_text(
        test_path.read_text(encoding="utf-8")
        + f"\n\ndef test_{excluded_marker.lower()}() -> None:\n"
        + f"    assert {excluded_marker!r}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ("git", "-C", str(history.path), "add", excluded_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "-C", str(history.path), "commit", "-m", "add hidden oracle marker"),
        check=True,
        capture_output=True,
    )
    hidden_test_head = subprocess.run(
        ("git", "-C", str(history.path), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    context = DeterministicContextRetriever(
        source_repository=history.path,
        excluded_paths=frozenset({excluded_path}),
    ).retrieve(base_sha=history.base_sha, head_sha=hidden_test_head)
    serialized = context.model_dump_json()

    assert excluded_path not in serialized
    assert excluded_marker not in serialized
    assert context.stats.changed_file_count == 2
    assert context.stats.excluded_changed_files == 1
    assert context.stats.excluded_python_paths == 1
    assert [changed.path for changed in context.changed_files] == ["workspace.py"]


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
