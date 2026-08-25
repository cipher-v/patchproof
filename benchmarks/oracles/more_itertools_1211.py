"""Hidden reference oracle adapted from the developer regressions in PR #1211."""

from fractions import Fraction

from more_itertools import running_max, running_min


def test_running_extrema_preserve_equal_value_stability() -> None:
    data = [0, 0.0, Fraction(0)]
    assert list(map(type, running_min(data, maxlen=2))) == [
        type(min(data[0:1])),
        type(min(data[0:2])),
        type(min(data[1:3])),
    ]
    assert list(map(type, running_max(data, maxlen=2))) == [
        type(max(data[0:1])),
        type(max(data[0:2])),
        type(max(data[1:3])),
    ]
