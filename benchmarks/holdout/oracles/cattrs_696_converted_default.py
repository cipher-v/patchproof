"""Independent regression oracle for python-attrs/cattrs#696."""

from attrs import define, field
from cattrs import Converter


@define
class RetryPolicy:
    attempts: int = field(default="3", converter=int)
    label: str = "standard"


def test_omit_if_default_compares_against_the_converted_default() -> None:
    converter = Converter(omit_if_default=True)

    assert converter.unstructure(RetryPolicy()) == {}
    assert converter.unstructure(RetryPolicy(attempts=5)) == {"attempts": 5}
