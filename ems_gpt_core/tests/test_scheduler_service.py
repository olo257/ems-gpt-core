from datetime import datetime
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scheduler_service import rce_event_keys


class RceRestoreKeysTests(unittest.TestCase):
    def test_before_14_accepts_previous_day_next_marker(self):
        self.assertEqual(rce_event_keys(datetime(2026, 9, 13, 5, 30)), (
            "RCE_2026-09-13_TODAY", "RCE_2026-09-12_NEXT"))

    def test_after_14_uses_current_day_next_marker(self):
        self.assertEqual(rce_event_keys(datetime(2026, 9, 13, 14, 0)), (
            "RCE_2026-09-13_NEXT",))


if __name__ == "__main__":
    unittest.main()
