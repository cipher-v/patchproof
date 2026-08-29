"""Independent regression oracle for pallets/jinja#2029."""

import copy
from pathlib import Path
import pickle
import sys
from types import ModuleType

# Load the public utility module without importing the rest of Jinja.  Only the
# Markup type is needed while its annotations are defined; the sentinel behavior
# itself has no MarkupSafe dependency.
jinja_source = next(
    Path(entry) / "jinja2" for entry in sys.path if (Path(entry) / "jinja2/utils.py").is_file()
)
jinja_package = ModuleType("jinja2")
jinja_package.__path__ = [str(jinja_source)]
sys.modules["jinja2"] = jinja_package

markupsafe_stub = ModuleType("markupsafe")
markupsafe_stub.Markup = str
markupsafe_stub.escape = str
sys.modules.setdefault("markupsafe", markupsafe_stub)

from jinja2.utils import missing


def test_missing_remains_a_singleton_across_copy_and_pickle() -> None:
    try:
        observations = (
            copy.copy(missing) is missing,
            copy.deepcopy(missing) is missing,
            pickle.loads(pickle.dumps(missing)) is missing,
        )
    except Exception as error:  # Convert the former pickle error into an assertion.
        observations = (type(error).__name__,)

    assert observations == (True, True, True)
