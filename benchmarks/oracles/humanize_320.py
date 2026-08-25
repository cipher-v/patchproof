"""Hidden reference oracle adapted from the developer regression in PR #320."""

import importlib
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
version_module = ModuleType("humanize._version")
version_module.__version__ = "patchproof-benchmark"
sys.modules["humanize._version"] = version_module
fractional = importlib.import_module("humanize.number").fractional


def test_negative_mixed_fraction_has_one_minus_sign() -> None:
    assert fractional(-1.3) == "-1 3/10"
    assert fractional(-2.5) == "-2 1/2"
    assert fractional(-0.5) == "-1/2"
