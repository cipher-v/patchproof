"""The exception-to-observable repair strategy must not degrade into fishing.

The repair agent is taught to convert a one-sided escaping exception into an explicit
observed value, because that turns a real behavioral difference into admissible
assertion evidence (see Starlette #3317 in the sealed unseen holdout, where the
candidate had already found the discriminating trigger and was discarded only because
of the shape of its observation).

That instruction is safe only because the degenerate forms of it are mechanically
impossible. These tests pin the guardrails.
"""

from __future__ import annotations

import pytest

from patchproof.context_retrieval import (
    ChangedFile,
    ChangeStatus,
    PullRequestContext,
    RetrievalStats,
)
from patchproof.evidence_workflow import _EXCEPTION_TO_OBSERVABLE_GUIDANCE
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


def _context() -> PullRequestContext:
    return PullRequestContext(
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff="",
        changed_files=(
            ChangedFile(
                path="package/urls.py",
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


def _validate(source: str):
    return CandidateTestValidator().validate(
        proposal=CandidateTestProposal(
            candidate_id="candidate-repair-1",
            target_path="tests/patchproof_generated/test_patchproof_generated_repair_1.py",
            test_function="test_patchproof_generated_behavior",
            source=source,
            rationale="converts the old failure into an observed value",
        ),
        context=_context(),
        contract=_CONTRACT,
        existing_paths=frozenset(),
    )


def _expect(source: str, code: CandidateIssueCode) -> None:
    with pytest.raises(CandidateValidationError) as rejected:
        _validate(source)
    assert rejected.value.issues[0].code is code


THE_INTENDED_SHAPE = """import package.urls


def test_patchproof_generated_behavior():
    try:
        observed = ("ok", str(package.urls.replace("file:///tmp/report.txt", port=None)))
    except IndexError as error:
        observed = ("error", type(error).__name__)
    assert observed == ("ok", "file:///tmp/report.txt")
"""


def test_the_intended_transformation_is_accepted() -> None:
    """A named exception bound to an asserted observation is exactly what we want."""
    validated = _validate(THE_INTENDED_SHAPE)
    assert validated.artifact.node_id.endswith("::test_patchproof_generated_behavior")


def test_bare_except_is_rejected() -> None:
    _expect(
        """def test_patchproof_generated_behavior():
    try:
        observed = ("ok", 1)
    except:  # noqa: E722
        observed = ("error", "x")
    assert observed == ("ok", 1)
""",
        CandidateIssueCode.BROAD_EXCEPTION_HANDLER,
    )


@pytest.mark.parametrize("name", ["Exception", "BaseException"])
def test_catching_everything_is_rejected(name: str) -> None:
    _expect(
        f"""def test_patchproof_generated_behavior():
    try:
        observed = ("ok", 1)
    except {name} as error:
        observed = ("error", type(error).__name__)
    assert observed == ("ok", 1)
""",
        CandidateIssueCode.BROAD_EXCEPTION_HANDLER,
    )


@pytest.mark.parametrize("name", ["Exception", "BaseException"])
def test_pytest_raises_with_an_overbroad_type_is_rejected(name: str) -> None:
    _expect(
        f"""import pytest


def test_patchproof_generated_behavior():
    with pytest.raises({name}):
        int("not a number")
""",
        CandidateIssueCode.BROAD_EXCEPTION_HANDLER,
    )


def test_catching_a_type_the_test_never_imported_is_rejected() -> None:
    _expect(
        """def test_patchproof_generated_behavior():
    try:
        observed = ("ok", 1)
    except DomainValidationError as error:
        observed = ("error", type(error).__name__)
    assert observed == ("ok", 1)
""",
        CandidateIssueCode.UNGROUNDED_EXCEPTION_TYPE,
    )


def test_discarding_the_outcome_is_rejected() -> None:
    _expect(
        """def test_patchproof_generated_behavior():
    try:
        assert 1 == 2
    except ValueError:
        pass
""",
        CandidateIssueCode.SWALLOWED_OUTCOME,
    )


def test_builtin_exceptions_are_grounded_without_an_import() -> None:
    validated = _validate(
        """def test_patchproof_generated_behavior():
    try:
        observed = ("ok", int("12"))
    except ValueError as error:
        observed = ("error", type(error).__name__)
    assert observed == ("ok", 12)
"""
    )
    assert validated.behavior_fingerprint


@pytest.mark.parametrize(
    "exception_expression",
    ["pickle.PicklingError", "asyncio.TimeoutError"],
)
def test_module_qualified_exception_is_grounded_by_its_imported_root(
    exception_expression: str,
) -> None:
    root = exception_expression.split(".", maxsplit=1)[0]
    validated = _validate(
        f"""import {root}


def test_patchproof_generated_behavior():
    try:
        observed = ("ok", 1)
    except {exception_expression} as error:
        observed = ("error", type(error).__name__)
    assert observed == ("ok", 1)
"""
    )
    assert validated.behavior_fingerprint


def test_module_qualified_exception_with_unimported_root_is_rejected() -> None:
    _expect(
        """def test_patchproof_generated_behavior():
    try:
        observed = ("ok", 1)
    except pickle.PicklingError as error:
        observed = ("error", type(error).__name__)
    assert observed == ("ok", 1)
""",
        CandidateIssueCode.UNGROUNDED_EXCEPTION_TYPE,
    )


def test_pytest_raises_accepts_a_module_qualified_imported_exception() -> None:
    validated = _validate(
        """import pickle
import pytest


def test_patchproof_generated_behavior():
    with pytest.raises(pickle.PicklingError):
        raise pickle.PicklingError("expected observation")
"""
    )
    assert validated.behavior_fingerprint


def test_module_qualified_overbroad_exception_remains_rejected() -> None:
    _expect(
        """import builtins


def test_patchproof_generated_behavior():
    try:
        observed = ("ok", 1)
    except builtins.Exception as error:
        observed = ("error", type(error).__name__)
    assert observed == ("ok", 1)
""",
        CandidateIssueCode.BROAD_EXCEPTION_HANDLER,
    )


def test_guidance_states_the_guardrails_it_relies_on() -> None:
    """The prompt text and the validator must not drift apart."""
    assert "Never catch Exception or BaseException" in _EXCEPTION_TO_OBSERVABLE_GUIDANCE
    assert "bare except" in _EXCEPTION_TO_OBSERVABLE_GUIDANCE
    assert "without asserting on it" in _EXCEPTION_TO_OBSERVABLE_GUIDANCE
