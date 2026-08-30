from pathlib import Path

import pytest

from patchproof.pr_analyze import PrAnalyzeError, find_known_pr, parse_pr_url


def test_parse_pr_url_normalizes_repository() -> None:
    parsed = parse_pr_url("https://github.com/PyPA/packaging/pull/1345")
    assert parsed.repository == "pypa/packaging"
    assert parsed.number == 1345
    assert parsed.url == "https://github.com/pypa/packaging/pull/1345"


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/pypa/packaging/issues/1345",
        "https://example.com/pypa/packaging/pull/1345",
        "github.com/pypa/packaging/pull/1345",
        "https://github.com/pypa/packaging/pull/0",
    ],
)
def test_parse_pr_url_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(PrAnalyzeError):
        parse_pr_url(value)


def test_known_holdout_pr_resolves_from_committed_manifest() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parsed = parse_pr_url("https://github.com/pypa/packaging/pull/1345")
    known = find_known_pr(parsed, project_root=project_root)
    assert known.case.repository == "pypa/packaging"
    assert known.case.pull_request_number == 1345
    assert known.case.base_sha != known.case.head_sha


def test_unknown_pr_fails_before_model_work() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parsed = parse_pr_url("https://github.com/python/cpython/pull/1")
    with pytest.raises(PrAnalyzeError, match="not in the committed historical case set"):
        find_known_pr(parsed, project_root=project_root)
