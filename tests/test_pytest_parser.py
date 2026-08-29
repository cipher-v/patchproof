"""Focused tests for conservative JUnit normalization."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from patchproof.models import TestExecutionStatus
from patchproof.pytest_runner import PytestJUnitParser


def _parse(
    directory: Path,
    report: str | None,
    *,
    exit_code: int,
    stdout: str = "",
):
    report_path = directory / "pytest.xml"
    if report is not None:
        report_path.write_text(report, encoding="utf-8")
    return PytestJUnitParser().parse(
        report_path=report_path,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
    )


@pytest.mark.parametrize(
    ("element", "exit_code", "expected_status", "expected_count"),
    [
        (
            '<failure message="assert 1 == 2">AssertionError</failure>',
            1,
            TestExecutionStatus.ASSERTION_FAILED,
            1,
        ),
        (
            '<failure message="RuntimeError: boom">RuntimeError: boom</failure>',
            1,
            TestExecutionStatus.TEST_ERROR,
            1,
        ),
        (
            '<error message="collection failure">ERROR collecting generated.py</error>',
            2,
            TestExecutionStatus.COLLECTION_ERROR,
            0,
        ),
        (
            '<skipped type="pytest.xfail" message="expected failure" />',
            0,
            TestExecutionStatus.XFAILED,
            1,
        ),
        (
            '<skipped type="pytest.skip" message="not applicable" />',
            0,
            TestExecutionStatus.SKIPPED,
            1,
        ),
    ],
)
def test_parser_distinguishes_failure_error_skip_and_xfail(
    writable_test_directory: Path,
    element: str,
    exit_code: int,
    expected_status: TestExecutionStatus,
    expected_count: int,
) -> None:
    report = (
        '<testsuites><testsuite tests="1"><testcase classname="generated" name="test_one">'
        f"{element}</testcase></testsuite></testsuites>"
    )

    parsed = _parse(writable_test_directory, report, exit_code=exit_code)

    assert parsed.status is expected_status
    assert parsed.collected_count == expected_count


def test_parser_detects_non_strict_xpass_from_pytest_summary(
    writable_test_directory: Path,
) -> None:
    report = (
        '<testsuites><testsuite tests="1"><testcase classname="generated" '
        'name="test_one" /></testsuite></testsuites>'
    )

    parsed = _parse(writable_test_directory, report, exit_code=0, stdout="1 XPASS in 0.01s")

    assert parsed.status is TestExecutionStatus.XPASSED
    assert parsed.collected_count == 1


def test_parser_rejects_a_node_that_selects_multiple_tests(
    writable_test_directory: Path,
) -> None:
    report = (
        '<testsuites><testsuite tests="2">'
        '<testcase classname="generated" name="test_one" />'
        '<testcase classname="generated" name="test_two" />'
        "</testsuite></testsuites>"
    )

    parsed = _parse(writable_test_directory, report, exit_code=0)

    assert parsed.status is TestExecutionStatus.MULTIPLE_TESTS_COLLECTED
    assert parsed.collected_count == 2


def test_parser_handles_no_collected_test_without_a_report(
    writable_test_directory: Path,
) -> None:
    parsed = _parse(writable_test_directory, None, exit_code=5, stdout="no tests ran")

    assert parsed.status is TestExecutionStatus.NOT_COLLECTED
    assert parsed.collected_count == 0


@pytest.mark.parametrize(
    ("message", "expected_detail"),
    [
        (
            "Failed: DID NOT WARN. No warnings of type UserWarning were emitted.",
            "PYTEST_EXPECTATION_NOT_MET: expected warning was not emitted",
        ),
        (
            "Failed: DID NOT RAISE ValueError",
            "PYTEST_EXPECTATION_NOT_MET: expected exception was not raised",
        ),
    ],
)
def test_parser_normalizes_only_stable_pytest_expectation_failures(
    writable_test_directory: Path,
    message: str,
    expected_detail: str,
) -> None:
    report = (
        '<testsuites><testsuite tests="1"><testcase classname="generated" name="test_one">'
        f"<failure message={quoteattr(message)}>{escape(message)}</failure>"
        "</testcase></testsuite></testsuites>"
    )

    parsed = _parse(writable_test_directory, report, exit_code=1)

    assert parsed.status is TestExecutionStatus.ASSERTION_FAILED
    assert parsed.detail == expected_detail


@pytest.mark.parametrize(
    "message",
    [
        "RuntimeError: unexpected exception inside pytest.warns",
        "LookupError: wrong exception escaped from pytest.raises",
        "ImportError: generated import is unavailable",
        "TypeError: Constructor() missing required argument",
        "SyntaxError: invalid syntax",
    ],
)
def test_parser_keeps_unexpected_generated_test_failures_as_test_errors(
    writable_test_directory: Path,
    message: str,
) -> None:
    report = (
        '<testsuites><testsuite tests="1"><testcase classname="generated" name="test_one">'
        f"<failure message={quoteattr(message)}>{escape(message)}</failure>"
        "</testcase></testsuite></testsuites>"
    )

    parsed = _parse(writable_test_directory, report, exit_code=1)

    assert parsed.status is TestExecutionStatus.TEST_ERROR


def test_did_not_warn_with_an_inner_exception_remains_a_test_error(
    writable_test_directory: Path,
) -> None:
    message = "Failed: DID NOT WARN. No warnings were emitted."
    body = "TypeError: unexpected constructor failure"
    report = (
        '<testsuites><testsuite tests="1"><testcase classname="generated" name="test_one">'
        f"<failure message={quoteattr(message)}>{escape(body)}</failure>"
        "</testcase></testsuite></testsuites>"
    )

    parsed = _parse(writable_test_directory, report, exit_code=1)

    assert parsed.status is TestExecutionStatus.TEST_ERROR
