import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appliance_service import _number
from config_service import APPLIANCE_DEFAULTS, CONFIG_SETTINGS, DEFAULT_OPTIONS


class ApplianceConfigTests(unittest.TestCase):
    def test_all_six_appliances_are_configurable(self):
        self.assertEqual(set(APPLIANCE_DEFAULTS), {
            "dishwasher", "large_fridge", "small_fridge", "freezer", "washer", "dryer"
        })
        for key in APPLIANCE_DEFAULTS:
            self.assertIn(f"appliance_{key}_energy_entity", CONFIG_SETTINGS)
            self.assertIn(f"appliance_{key}_counter_type", CONFIG_SETTINGS)

    def test_known_entities_are_preconfigured(self):
        self.assertEqual(DEFAULT_OPTIONS["appliance_dishwasher_energy_entity"], "sensor.zmywarka_energy")
        self.assertEqual(DEFAULT_OPTIONS["appliance_dishwasher_power_entity"], "sensor.zmywarka_power")
        self.assertEqual(DEFAULT_OPTIONS["appliance_washer_counter_type"], "daily")

    def test_unavailable_values_are_not_numbers(self):
        self.assertIsNone(_number({"state": "unavailable"}))
        self.assertEqual(_number({"state": "1.25"}), 1.25)


if __name__ == "__main__":
    unittest.main()
