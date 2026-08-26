from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from attr import validators


def test_nested_disabled_context_preserves_outer_disabled_state() -> None:
    original = validators.get_run_validators()
    validators.set_run_validators(True)
    try:
        with validators.disabled():
            with validators.disabled():
                pass
            observed = validators.get_run_validators()
    finally:
        validators.set_run_validators(original)

    assert observed is False
