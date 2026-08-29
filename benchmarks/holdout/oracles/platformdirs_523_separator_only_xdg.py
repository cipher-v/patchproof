"""Independent regression oracle for tox-dev/platformdirs#523."""

import os
import sys
from types import ModuleType

# platformdirs normally generates this metadata module while building a wheel.
# The path behavior under test is source-only, so provide deterministic metadata.
version_stub = ModuleType("platformdirs.version")
version_stub.__version__ = "holdout"
version_stub.__version_tuple__ = (0, 0, 0)
sys.modules.setdefault("platformdirs.version", version_stub)

from platformdirs.unix import Unix


def test_separator_only_xdg_site_dirs_fall_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_DIRS", f" {os.pathsep}  {os.pathsep} ")
    directories = Unix(appname="patchproof-holdout", multipath=False)

    try:
        observed = directories.site_data_dir
    except Exception as error:  # Convert the former empty-list crash to an assertion.
        observed = type(error).__name__

    normalized = observed.replace("\\", "/")
    assert normalized.endswith("/usr/local/share/patchproof-holdout")
