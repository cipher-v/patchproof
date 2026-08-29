"""Tests for bounded static repository callable-signature grounding."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import ContextRepositoryHistory

from patchproof.context_retrieval import ContextBudget, DeterministicContextRetriever


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _add_signature_fixture(history: ContextRepositoryHistory) -> str:
    (history.path / "api.py").write_text(
        """class Widget:
    def __init__(
        self, name, parent, /, enabled=True, *, mode="safe", limit=None, **kwargs
    ):
        self.name = name


class InheritedWidget(Widget):
    pass


class Service:
    @classmethod
    def create(cls, value, *, strict=True):
        return cls()

    def run(self, task, timeout=None):
        return task


def build(left, right=2, *items, flag=False, **kwargs):
    return left, right, items, flag, kwargs
""",
        encoding="utf-8",
    )
    (history.path / "malformed.py").write_text("def broken(:\n", encoding="utf-8")
    _git(history.path, "add", "api.py", "malformed.py")
    _git(history.path, "commit", "-m", "add callable signature fixture")
    return _git(history.path, "rev-parse", "HEAD")


def test_static_signature_context_covers_functions_methods_and_local_constructor(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    head_sha = _add_signature_fixture(history)
    retriever = DeterministicContextRetriever(source_repository=history.path)
    context = retriever.retrieve(base_sha=head_sha, head_sha=head_sha)

    signatures = retriever.retrieve_callable_signatures(
        head_sha=head_sha,
        context=context,
        affected_symbols=(
            ("api.py", "Widget"),
            ("api.py", "Service.create"),
            ("api.py", "Service.run"),
            ("api.py", "build"),
        ),
    )
    rendered = {item.qualified_name: item.signature for item in signatures.signatures}

    assert rendered["Widget"] == (
        "Widget.__init__(name, parent, /, enabled=True, *, mode='safe', limit=None, **kwargs)"
    )
    assert rendered["Service.create"] == "Service.create(value, *, strict=True)"
    assert rendered["Service.run"] == "Service.run(task, timeout=None)"
    assert rendered["build"] == "build(left, right=2, *items, flag=False, **kwargs)"
    assert signatures.count == 4
    assert len(signatures.sha256) == 64


def test_signature_context_omits_inherited_unparseable_and_missing_definitions(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    head_sha = _add_signature_fixture(history)
    retriever = DeterministicContextRetriever(source_repository=history.path)
    context = retriever.retrieve(base_sha=head_sha, head_sha=head_sha)

    signatures = retriever.retrieve_callable_signatures(
        head_sha=head_sha,
        context=context,
        affected_symbols=(
            ("api.py", "InheritedWidget"),
            ("malformed.py", "broken"),
            ("missing.py", "NotFound"),
        ),
    )

    assert signatures.signatures == ()
    assert signatures.truncated is True


def test_signature_context_is_count_and_character_bounded(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    head_sha = _add_signature_fixture(history)
    retriever = DeterministicContextRetriever(
        source_repository=history.path,
        budget=ContextBudget(max_signature_count=1, max_signature_context_chars=180),
    )
    context = retriever.retrieve(base_sha=history.head_sha, head_sha=head_sha)

    signatures = retriever.retrieve_callable_signatures(
        head_sha=head_sha,
        context=context,
        affected_symbols=(("api.py", "Widget"), ("api.py", "build")),
    )

    assert signatures.count == 1
    assert len(signatures.signatures[0].signature) <= 300
    assert signatures.truncated is True


def test_signature_context_never_reads_excluded_or_other_test_files(
    context_repository_history: ContextRepositoryHistory,
) -> None:
    history = context_repository_history
    hidden_path = "tests/test_hidden_signature.py"
    (history.path / hidden_path).write_text(
        "class HiddenOracleConstructor:\n"
        "    def __init__(self, secret_solution):\n"
        "        self.secret_solution = secret_solution\n",
        encoding="utf-8",
    )
    _git(history.path, "add", hidden_path)
    _git(history.path, "commit", "-m", "add excluded signature source")
    head_sha = _git(history.path, "rev-parse", "HEAD")
    retriever = DeterministicContextRetriever(
        source_repository=history.path,
        excluded_paths=frozenset({hidden_path}),
    )
    context = retriever.retrieve(base_sha=history.head_sha, head_sha=head_sha)

    signatures = retriever.retrieve_callable_signatures(
        head_sha=head_sha,
        context=context,
        affected_symbols=((hidden_path, "HiddenOracleConstructor"),),
    )
    serialized = signatures.model_dump_json()

    assert signatures.signatures == ()
    assert hidden_path not in serialized
    assert "secret_solution" not in serialized
