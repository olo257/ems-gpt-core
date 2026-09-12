from datetime import datetime, timezone
import pathlib
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from time_service import TimeAdapters, build_time_service


class TimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.warsaw = ZoneInfo("Europe/Warsaw")
        self.service = build_time_service(TimeAdapters(
            timezone=self.warsaw,
            slot_minutes=15,
        ))

    def test_slot_start_floors_to_configured_boundary(self):
        value = datetime(2026, 9, 12, 21, 29, 59, 123456, tzinfo=self.warsaw)
        self.assertEqual(
            self.service.slot_start(value),
            datetime(2026, 9, 12, 21, 15, tzinfo=self.warsaw),
        )

    def test_slot_start_converts_utc_before_flooring(self):
        value = datetime(2026, 1, 15, 20, 14, tzinfo=timezone.utc)
        self.assertEqual(
            self.service.slot_start(value),
            datetime(2026, 1, 15, 21, 0, tzinfo=self.warsaw),
        )

    def test_non_positive_slot_length_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            build_time_service(TimeAdapters(timezone=self.warsaw, slot_minutes=0))


if __name__ == "__main__":
    unittest.main()
