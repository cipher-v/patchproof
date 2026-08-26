from pathlib import Path
import sys
from tokenize import TokenError
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astroid import builder, nodes


def test_position_token_error_degrades_to_missing_position() -> None:
    try:
        with mock.patch(
            "astroid.rebuilder.generate_tokens",
            side_effect=TokenError("unexpected EOF", (1, 0)),
        ):
            observed = builder.extract_node("class Example:  #@\n    ...")
    except TokenError:
        observed = None

    assert isinstance(observed, nodes.ClassDef) and observed.position is None
