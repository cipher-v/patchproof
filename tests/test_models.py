"""Unit tests for immutable Phase 1 domain models."""

from __future__ import annotations

import hashlib

import pytest

from patchproof.models import Revision, RevisionRole, TestArtifact


def test_revision_normalizes_a_full_sha() -> None:
    revision = Revision(role=RevisionRole.BASE, sha="A" * 40)

    assert revision.sha == "a" * 40


@pytest.mark.parametrize("sha", ["main", "a" * 39, "g" * 40, "a" * 41, ""])
def test_revision_rejects_mutable_or_malformed_refs(sha: str) -> None:
    with pytest.raises(ValueError, match="full 40- or 64-character"):
        Revision(role=RevisionRole.HEAD, sha=sha)


def test_artifact_preserves_exact_bytes_and_hash() -> None:
    content = b"def test_example():\n    assert True\n"
    artifact = TestArtifact(
        relative_path="tests/test_patchproof_generated.py",
        node_id="tests/test_patchproof_generated.py::test_example",
        content=content,
    )

    assert artifact.content is content
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    "path",
    [
        "../test_escape.py",
        "/tests/test_absolute.py",
        "tests\\test_windows.py",
        "tests/./test_unnormalized.py",
        "tests//test_unnormalized.py",
        "tests/not_python.txt",
    ],
)
def test_artifact_rejects_unsafe_or_non_python_paths(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        TestArtifact(relative_path=path, node_id=f"{path}::test_example", content=b"pass\n")


def test_artifact_requires_node_id_inside_its_own_file() -> None:
    with pytest.raises(ValueError, match="node ID"):
        TestArtifact(
            relative_path="tests/test_generated.py",
            node_id="tests/test_other.py::test_example",
            content=b"def test_example():\n    pass\n",
        )


def test_artifact_requires_immutable_nonempty_bytes() -> None:
    with pytest.raises(TypeError, match="immutable bytes"):
        TestArtifact(
            relative_path="tests/test_generated.py",
            node_id="tests/test_generated.py::test_example",
            content=bytearray(b"pass\n"),  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="must not be empty"):
        TestArtifact(
            relative_path="tests/test_generated.py",
            node_id="tests/test_generated.py::test_example",
            content=b"",
        )
