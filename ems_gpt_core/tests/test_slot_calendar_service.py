import pathlib
import sys
import unittest
from datetime import date
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slot_calendar_service import SlotCalendarAdapters, build_slot_calendar


class SlotCalendarServiceTests(unittest.TestCase):
    def test_warsaw_dst_days_have_real_slot_counts(self):
        service = build_slot_calendar(SlotCalendarAdapters(
            timezone=ZoneInfo("Europe/Warsaw"), db=lambda: None
        ))
        self.assertEqual(len(service.canonical_slots_for_day(date(2026, 3, 29))), 92)
        self.assertEqual(len(service.canonical_slots_for_day(date(2026, 9, 12))), 96)
        autumn = service.canonical_slots_for_day(date(2026, 10, 25))
        self.assertEqual(len(autumn), 100)
        self.assertEqual(len({row["slot_id"] for row in autumn}), 100)


if __name__ == "__main__":
    unittest.main()
