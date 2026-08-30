from jsonschema._keywords import enum

def test_patchproof_generated_behavior():
    errors = list(enum(None, [[False]], [0], {}))
    assert len(errors) == 1
