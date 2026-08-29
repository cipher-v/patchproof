"""Independent regression oracle for agronholm/anyio#1200."""

import pytest
from anyio import Path


def test_with_stem_rejects_empty_stem_when_suffix_is_present() -> None:
    assert str(Path("report.txt").with_stem("summary")) == "summary.txt"

    with pytest.raises(ValueError):
        Path("report.txt").with_stem("")
