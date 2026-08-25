"""Intentionally weak candidate for the controlled no-op comparison."""

from more_itertools import running_max, running_min


def test_running_extrema_are_available() -> None:
    assert callable(running_min)
    assert callable(running_max)
