from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from marshmallow import Schema, ValidationError, fields


def test_nested_partial_uses_attribute_name_when_data_key_differs() -> None:
    class AddressSchema(Schema):
        postal_code = fields.String(required=True)
        city = fields.String(required=True)

    class PersonSchema(Schema):
        address = fields.Nested(AddressSchema, data_key="homeAddress", required=True)

    try:
        observed = PersonSchema().load(
            {"homeAddress": {"city": "Pune"}},
            partial=("address.postal_code",),
        )
    except ValidationError:
        observed = None

    assert observed == {"address": {"city": "Pune"}}
