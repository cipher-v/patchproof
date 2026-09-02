"""Import grounding must follow the prepared environment, not the context bundle.

Regression coverage for `docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md`: in the sealed
unseen holdout the cattrs initial candidate was rejected with `UNGROUNDED_IMPORT`
because the root ``attrs`` did not appear in the deterministic context, even though
``attrs`` is a hard runtime dependency of the project. The candidate never executed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from patchproof.context_retrieval import (
    ChangedFile,
    ChangeStatus,
    PullRequestContext,
    RetrievalStats,
)
from patchproof.environment_introspection import (
    installed_import_roots,
    standard_library_roots,
    virtual_environment_site_packages,
)
from patchproof.execution_contract import ExecutionContract
from patchproof.test_generation import (
    CandidateIssueCode,
    CandidateTestProposal,
    CandidateTestValidator,
    CandidateValidationError,
)

_CONTRACT = ExecutionContract.model_validate(
    {
        "version": 1,
        "python": "3.12",
        "install": [["uv", "sync", "--frozen"]],
        "test": {"command": ["python", "-m", "pytest"]},
        "allowed_test_paths": ["tests/patchproof_generated/"],
        "timeout_seconds": 30,
    }
)


def _make_site_packages(workspace: Path) -> Path:
    if os.name == "nt":
        site_packages = workspace / ".venv" / "Lib" / "site-packages"
    else:
        site_packages = workspace / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    return site_packages


def _context() -> PullRequestContext:
    return PullRequestContext(
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff="",
        changed_files=(
            ChangedFile(
                path="src/cattrs/converters.py",
                status=ChangeStatus.MODIFIED,
                is_python=True,
                is_test=False,
            ),
        ),
        changed_symbols=(),
        snippets=(),
        stats=RetrievalStats(
            changed_file_count=1,
            changed_python_file_count=1,
            python_path_count=1,
            test_files_scanned=0,
            reference_files_scanned=0,
            omitted_changed_files=0,
            truncated=False,
        ),
    )


def _proposal(source: str) -> CandidateTestProposal:
    return CandidateTestProposal(
        candidate_id="candidate-initial",
        target_path="tests/patchproof_generated/test_patchproof_generated_initial.py",
        test_function="test_patchproof_generated_behavior",
        source=source,
        rationale="checks the claimed behavior",
    )


def test_no_virtual_environment_yields_no_roots(tmp_path: Path) -> None:
    """Absence must leave existing grounding in force rather than widen or narrow it."""
    assert installed_import_roots(tmp_path) == frozenset()
    assert virtual_environment_site_packages(tmp_path) == ()


def test_packages_modules_and_metadata_are_all_discovered(tmp_path: Path) -> None:
    site_packages = _make_site_packages(tmp_path)
    (site_packages / "attrs").mkdir()
    (site_packages / "six.py").write_text("", encoding="utf-8")
    distribution = site_packages / "markupsafe-3.0.0.dist-info"
    distribution.mkdir()
    (distribution / "top_level.txt").write_text("markupsafe\n", encoding="utf-8")

    roots = installed_import_roots(tmp_path)

    assert {"attrs", "six", "markupsafe"} <= roots
    assert not any(name.endswith(".dist-info") for name in roots)


def test_introspection_never_imports_or_executes_package_code(tmp_path: Path) -> None:
    """A package that would explode on import must still be discoverable."""
    site_packages = _make_site_packages(tmp_path)
    hostile = site_packages / "hostile"
    hostile.mkdir()
    (hostile / "__init__.py").write_text("raise SystemExit('boom')\n", encoding="utf-8")

    assert "hostile" in installed_import_roots(tmp_path)


def test_installed_dependency_is_no_longer_rejected_as_ungrounded(tmp_path: Path) -> None:
    """The cattrs failure shape: import a real runtime dependency of the project."""
    source = (
        "import attrs\n\n\ndef test_patchproof_generated_behavior():\n"
        "    assert attrs is not None\n"
    )
    context = _context()

    with pytest.raises(CandidateValidationError) as rejected:
        CandidateTestValidator().validate(
            proposal=_proposal(source),
            context=context,
            contract=_CONTRACT,
            existing_paths=frozenset(),
        )
    assert rejected.value.issues[0].code is CandidateIssueCode.UNGROUNDED_IMPORT

    validated = CandidateTestValidator(installed_import_roots=frozenset({"attrs"})).validate(
        proposal=_proposal(source),
        context=context,
        contract=_CONTRACT,
        existing_paths=frozenset(),
    )
    assert "attrs" in validated.imported_roots


def test_installed_roots_do_not_bypass_the_blocked_import_list(tmp_path: Path) -> None:
    """Being installed is not permission: network and subprocess roots stay blocked."""
    source = (
        "import requests\n\n\ndef test_patchproof_generated_behavior():\n"
        "    assert requests is not None\n"
    )
    with pytest.raises(CandidateValidationError) as rejected:
        CandidateTestValidator(installed_import_roots=frozenset({"requests"})).validate(
            proposal=_proposal(source),
            context=_context(),
            contract=_CONTRACT,
            existing_paths=frozenset(),
        )
    assert rejected.value.issues[0].code is CandidateIssueCode.BLOCKED_IMPORT


def test_standard_library_roots_include_pytest() -> None:
    roots = standard_library_roots()
    assert "pytest" in roots
    assert "json" in roots
