def test_zero_length_suffix_range_is_rejected_for_arbitrary_units():
    from werkzeug.http import parse_range_header

    assert parse_range_header("records=-0") is None
