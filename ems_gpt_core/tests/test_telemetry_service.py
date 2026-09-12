import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telemetry_service import TelemetryAdapters, build_telemetry


class TelemetryServiceTests(unittest.TestCase):
    def test_power_units_are_normalized_to_watts(self):
        service = build_telemetry(TelemetryAdapters(
            options={}, entities={}, db=lambda: None, local_now=lambda: None,
            slot_start=lambda value: value, canonical_slots_for_day=lambda day: [],
            number=lambda state: float(state["state"]) if state else None,
            ha_state=lambda entity: None,
        ))
        self.assertEqual(service.power_w({"state": "2.5", "attributes": {"unit_of_measurement": "kW"}}), 2500.0)
        self.assertEqual(service.power_w({"state": "0.002", "attributes": {"unit_of_measurement": "MW"}}), 2000.0)
        self.assertEqual(service.power_w({"state": "350", "attributes": {"unit_of_measurement": "W"}}), 350.0)
        self.assertIsNone(service.power_w(None))


if __name__ == "__main__":
    unittest.main()
