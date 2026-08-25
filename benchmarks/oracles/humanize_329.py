"""Hidden reference oracle adapted from the developer regression in PR #329."""

import importlib
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
version_module = ModuleType("humanize._version")
version_module.__version__ = "patchproof-benchmark"
sys.modules["humanize._version"] = version_module
naturalsize = importlib.import_module("humanize.filesize").naturalsize


def test_naturalsize_rounding_rolls_over_to_the_next_unit() -> None:
    assert naturalsize(999999) == "1.0 MB"
    assert naturalsize(999999999) == "1.0 GB"
    assert naturalsize(1024**2 - 1, binary=True) == "1.0 MiB"
