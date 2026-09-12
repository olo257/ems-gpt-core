import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics_service import _is_core_quality_slot


class AnalyticsQualityWindowTests(unittest.TestCase):
    def test_published_core_slot_is_in_quality_window(self):
        self.assertTrue(_is_core_quality_slot({"plan_published": 1}))

    def test_legacy_import_is_excluded_from_quality_window(self):
        self.assertFalse(_is_core_quality_slot({"plan_published": None}))
        self.assertFalse(_is_core_quality_slot({}))

    def test_failed_or_partial_published_slot_stays_in_window(self):
        row = {"plan_published": 1, "forecast_load_kwh": None}
        self.assertTrue(_is_core_quality_slot(row))


if __name__ == "__main__":
    unittest.main()
