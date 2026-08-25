"""Intentionally weak candidate for the controlled no-op comparison."""

import importlib
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
version_module = ModuleType("humanize._version")
version_module.__version__ = "patchproof-benchmark"
sys.modules["humanize._version"] = version_module
fractional = importlib.import_module("humanize.number").fractional


def test_fractional_is_available() -> None:
    assert callable(fractional)
