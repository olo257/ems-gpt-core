import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics_service import _is_core_quality_slot


class AnalyticsQualityWindowTests(unittest.TestCase):
    def test_core_telemetry_slot_is_in_quality_window(self):
        self.assertTrue(_is_core_quality_slot({
            "plan_published": 1,
            "execution_reason": "CORE_TELEMETRY_15_SAMPLES",
        }))

    def test_legacy_import_is_excluded_from_quality_window(self):
        self.assertFalse(_is_core_quality_slot({"plan_published": None}))
        self.assertFalse(_is_core_quality_slot({}))

    def test_old_published_slot_without_core_execution_is_excluded(self):
        row = {"plan_published": 1, "execution_reason": None}
        self.assertFalse(_is_core_quality_slot(row))

    def test_explicit_outage_stays_in_quality_window(self):
        row = {"plan_published": 1, "actual_mode": "MISSING_OUTAGE"}
        self.assertTrue(_is_core_quality_slot(row))


if __name__ == "__main__":
    unittest.main()
