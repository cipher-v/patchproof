"""Independent regression oracle for pypa/packaging#1345."""

import pytest
from packaging.requirements import InvalidRequirement, Requirement


def test_requirement_parser_rejects_a_trailing_line_feed() -> None:
    assert Requirement("widget>=2").name == "widget"

    with pytest.raises(InvalidRequirement):
        Requirement("widget>=2\n")
