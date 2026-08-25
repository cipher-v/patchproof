"""Hidden reference oracle adapted from the developer regression in PR #1223."""

import pytest
from more_itertools import chunked


def test_negative_chunk_size_has_clear_error() -> None:
    with pytest.raises(ValueError, match="n must be at least 0"):
        list(chunked("ABCDE", -1))
