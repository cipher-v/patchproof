def test_literal_search_continues_past_equal_integer():
    from typing import Literal

    from typeguard import TypeCheckError, check_type

    try:
        observed = check_type(True, Literal[1, "one", True])
    except TypeCheckError as error:
        raise AssertionError(f"an exact boolean Literal member was rejected: {error}") from error

    assert observed is True
