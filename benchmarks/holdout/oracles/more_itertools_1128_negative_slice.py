"""Independent regression oracle for more-itertools/more-itertools#1128."""

from more_itertools import numeric_range


def test_numeric_range_negative_step_slice_matches_sequence_semantics() -> None:
    values = numeric_range(10, 20)

    assert list(values[8:2:-2]) == [18, 16, 14]
    assert list(values[::-3]) == [19, 16, 13, 10]
