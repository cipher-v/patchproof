"""Independent regression oracle for dateutil/dateutil#751."""

from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType

import dateutil

# Import only the public isoparser module under test.  The historical package's
# broad parser and timezone initializers depend on ``six``, but this ISO case
# needs only ``six.text_type`` and never constructs a timezone.
six_stub = ModuleType("six")
six_stub.text_type = str
six_stub.raise_from = None
sys.modules.setdefault("six", six_stub)

tz_stub = ModuleType("dateutil.tz")
tz_stub.tzutc = None
tz_stub.tzoffset = None
sys.modules.setdefault("dateutil.tz", tz_stub)

parser_package = ModuleType("dateutil.parser")
parser_package.__path__ = [str(Path(next(iter(dateutil.__path__))) / "parser")]
sys.modules["dateutil.parser"] = parser_package

from dateutil.parser.isoparser import isoparse


def test_iso_24_hour_midnight_advances_to_the_next_calendar_day() -> None:
    assert isoparse("2019-12-31T24:00:00") == datetime(2020, 1, 1, 0, 0, 0)
    assert isoparse("2020-02-29T24:00") == datetime(2020, 3, 1, 0, 0, 0)
