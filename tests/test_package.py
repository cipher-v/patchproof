"""Smoke tests for the Phase 0 package foundation."""

from importlib.metadata import version

import patchproof


def test_package_version_matches_installed_metadata() -> None:
    """The src-layout package is importable and exposes consistent metadata."""
    assert patchproof.__version__ == version("patchproof")
