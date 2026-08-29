"""Independent regression oracle for python-jsonschema/jsonschema#1208."""

from jsonschema import Draft202012Validator


def test_nested_enum_values_distinguish_booleans_from_integers() -> None:
    validator = Draft202012Validator({"enum": [[0], {"enabled": 1}]})

    assert list(validator.iter_errors([0])) == []
    assert list(validator.iter_errors({"enabled": 1})) == []

    false_errors = list(validator.iter_errors([False]))
    true_errors = list(validator.iter_errors({"enabled": True}))
    assert len(false_errors) == 1
    assert len(true_errors) == 1
