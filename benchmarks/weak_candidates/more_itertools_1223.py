"""Intentionally weak candidate for the controlled no-op comparison."""

from more_itertools import chunked


def test_chunked_is_available() -> None:
    assert callable(chunked)
