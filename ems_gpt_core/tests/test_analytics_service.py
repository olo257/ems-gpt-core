import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics_service import (
    _flow_metrics, _hp_execution_metrics, _is_core_quality_slot,
    _native_load_kwh, _suggested_scale,
)


class AnalyticsQualityWindowTests(unittest.TestCase):
    def test_hp_execution_metrics_keep_modes_separate(self):
        result = _hp_execution_metrics([
            {"actual_heating_consumed_kwh": 1.0, "actual_heating_generated_kwh": 4.0,
             "actual_dhw_consumed_kwh": .5, "actual_dhw_generated_kwh": 1.5,
             "actual_cooling_consumed_kwh": 0.0, "actual_cooling_generated_kwh": 0.0,
             "actual_heat_pump_is_running": 1},
            {"actual_cooling_consumed_kwh": .25, "actual_cooling_generated_kwh": .75,
             "actual_heat_pump_is_running": 1},
        ])
        self.assertEqual(result["actual_heating_cop"], 4.0)
        self.assertEqual(result["actual_dhw_cop"], 3.0)
        self.assertEqual(result["actual_cooling_cop"], 3.0)
        self.assertEqual(result["actual_heat_pump_electric_kwh"], 1.75)
        self.assertEqual(result["actual_heat_pump_thermal_kwh"], 6.25)
        self.assertEqual(result["actual_heat_pump_running_slot_count"], 2)

    def test_intermittent_flow_metrics_ignore_inactive_slots(self):
        rows = [
            {"plan": 0.0, "actual": 0.0},
            {"plan": 0.4, "actual": 0.5},
            {"plan": 0.3, "actual": 0.0},
            {"plan": 0.0, "actual": 0.2},
        ]
        result = _flow_metrics(rows, "plan", "actual", 0.05)
        self.assertEqual(result["active_slots"], 3)
        self.assertEqual(result["mae_kwh"], 0.2)
        self.assertEqual(result["event_f1_pct"], 50.0)

    def test_intermittent_flow_without_events_is_explicitly_empty(self):
        result = _flow_metrics([{"plan": 0.0, "actual": 0.0}], "plan", "actual", 0.05)
        self.assertEqual(result, {"active_slots": 0, "mae_kwh": None, "event_f1_pct": None})

    def test_suggested_scale_is_bounded_and_needs_energy(self):
        self.assertEqual(_suggested_scale([{"f": 2.0, "a": 1.6}], "f", "a"), 0.8)
        self.assertEqual(_suggested_scale([{"f": 1.0, "a": 9.0}], "f", "a"), 1.5)
        self.assertIsNone(_suggested_scale([{"f": 0.2, "a": 0.3}], "f", "a"))

    def test_native_load_excludes_separately_planned_consumers(self):
        row = {
            "actual_load_kwh": 1.25,
            "detail_actual_ev_kwh": 0.50,
            "actual_heat_pump_electric_kwh": 0.25,
        }
        self.assertEqual(_native_load_kwh(row), 0.5)

    def test_native_load_never_becomes_negative(self):
        self.assertEqual(_native_load_kwh({
            "actual_load_kwh": 0.2,
            "detail_actual_ev_kwh": 0.4,
            "actual_heat_pump_electric_kwh": 0.1,
        }), 0.0)
        self.assertIsNone(_native_load_kwh({"actual_load_kwh": None}))

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
