import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics_service import (
    _flow_metrics, _hp_execution_metrics, _is_core_quality_slot,
    _native_load_kwh, _suggested_scale, _target_history,
    _target_history_metrics, _target_load_kwh,
)
from materialization_service import normalize_hp_mode_energy
from datetime import datetime, timedelta


class AnalyticsQualityWindowTests(unittest.TestCase):
    def test_idle_hp_channel_noise_is_not_reported_as_heating(self):
        # 18 W averaged over a 15-minute slot is 0.0045 kWh.  This is the
        # observed inactive-channel value, not a CO cycle.
        self.assertEqual(normalize_hp_mode_energy(0.0045, 0.0), (0.0, 0.0))

    def test_each_active_hp_mode_keeps_its_own_energy_for_cop(self):
        self.assertEqual(normalize_hp_mode_energy(0.25, 0.75), (0.25, 0.75))
        self.assertEqual(normalize_hp_mode_energy(0.0, 0.10), (0.0, 0.10))

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

    def test_hp_analytics_refilters_historical_idle_heating_noise(self):
        result = _hp_execution_metrics([
            {"actual_heating_consumed_kwh": 0.0045,
             "actual_heating_generated_kwh": 0.0,
             "actual_dhw_consumed_kwh": 0.25,
             "actual_dhw_generated_kwh": 0.75,
             "actual_heat_pump_is_running": 1},
        ])

        self.assertEqual(result["actual_heating_consumed_kwh"], 0.0)
        self.assertEqual(result["actual_heating_generated_kwh"], 0.0)
        self.assertIsNone(result["actual_heating_cop"])
        self.assertEqual(result["actual_dhw_consumed_kwh"], 0.25)
        self.assertEqual(result["actual_dhw_cop"], 3.0)

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

    def test_target_load_excludes_ev_but_keeps_hp_dhw_energy(self):
        self.assertEqual(_target_load_kwh({
            "actual_load_kwh": 2.0,
            "detail_actual_ev_kwh": 0.4,
            "detail_actual_dhw_kwh": 0.6,
            "actual_heat_pump_electric_kwh": 0.5,
        }), 1.6)

    def test_target_history_reconstructs_need_until_confirmed_pv(self):
        start = datetime(2026, 9, 14, 18, 30)
        rows = []
        for index in range(6):
            rows.append({
                "slot_start": start + timedelta(minutes=15 * index),
                "soc_target_pct": 20.0,
                "actual_load_kwh": 0.5,
                "actual_pv_total_kwh": 0.8 if index >= 4 else 0.0,
                "sample_count": 15,
                "plan_published": 1,
                "execution_reason": "CORE_TELEMETRY_15_SAMPLES",
                "market_window": "NEUTRAL",
            })
        samples = _target_history(
            rows, reserve_pct=15.0, capacity_kwh=10.0,
            discharge_efficiency=1.0, pv_threshold_kwh=0.1,
            slot_minutes=15, minimum_samples=10)
        first = samples[0]
        self.assertEqual(first["status"], "VALID")
        self.assertEqual(first["relief_type"], "PV")
        self.assertEqual(first["horizon_slots"], 4)
        self.assertEqual(first["required_energy_kwh"], 2.0)
        self.assertEqual(first["required_target_pct"], 35.0)
        self.assertEqual(first["target_shortfall_pct"], 15.0)

    def test_target_history_does_not_treat_gross_pv_below_load_as_relief(self):
        start = datetime(2026, 9, 17, 6, 0)
        rows = [{
            "slot_start": start + timedelta(minutes=15 * index),
            "soc_target_pct": 30.0, "actual_load_kwh": 0.5,
            "actual_pv_total_kwh": 0.2 if index >= 2 else 0.0,
            "sample_count": 15, "plan_published": 1,
            "execution_reason": "CORE_TELEMETRY_15_SAMPLES",
            "market_window": "NEUTRAL",
        } for index in range(5)]
        samples = _target_history(
            rows, reserve_pct=15.0, capacity_kwh=10.0,
            discharge_efficiency=1.0, pv_threshold_kwh=0.1,
            slot_minutes=15, minimum_samples=10)
        self.assertEqual(samples[0]["status"], "OCZEKUJE")
        self.assertEqual(samples[0]["reason"], "NO_COMPLETE_RELIEF_HORIZON")

    def test_target_history_rejects_incomplete_tail(self):
        start = datetime(2026, 9, 14, 23, 45)
        rows = [{
            "slot_start": start, "soc_target_pct": 15.0,
            "actual_load_kwh": 0.3, "actual_pv_total_kwh": 0.0,
            "sample_count": 15, "plan_published": 1,
            "execution_reason": "CORE_TELEMETRY_15_SAMPLES",
            "market_window": "NEUTRAL",
        }]
        sample = _target_history(
            rows, reserve_pct=15.0, capacity_kwh=15.6,
            discharge_efficiency=.95, pv_threshold_kwh=.1,
            slot_minutes=15, minimum_samples=10)[0]
        self.assertEqual(sample["status"], "OCZEKUJE")
        self.assertEqual(sample["reason"], "NO_COMPLETE_RELIEF_HORIZON")

    def test_discontinuous_target_horizon_is_invalid_not_pending(self):
        start = datetime(2026, 9, 14, 23, 45)
        rows = [{"slot_start": start, "soc_target_pct": 30.0},
                {"slot_start": start + timedelta(minutes=30),
                 "soc_target_pct": 30.0}]
        samples = _target_history(
            rows, reserve_pct=15.0, capacity_kwh=15.0,
            discharge_efficiency=.95, pv_threshold_kwh=.1,
            slot_minutes=15, minimum_samples=10)
        self.assertEqual(samples[0]["status"], "INVALID")
        self.assertEqual(samples[0]["reason"], "NON_CONTIGUOUS_SLOTS")
        self.assertEqual(samples[1]["status"], "OCZEKUJE")

    def test_pending_target_history_does_not_inflate_invalid_count(self):
        samples = [
            {"status": "OCZEKUJE"},
            {"status": "INVALID"},
            {"status": "VALID", "target_error_pct": 2.0,
             "target_shortfall_pct": 2.0,
             "slot_start": datetime(2026, 9, 14, 18, 30)},
        ]
        metrics = _target_history_metrics(
            samples, minimum_samples=1, correction_cap_pct=15.0)
        self.assertEqual(metrics["target_history_samples"], 1)
        self.assertEqual(metrics["target_history_invalid_samples"], 1)

    def test_target_correction_needs_minimum_samples_and_is_capped(self):
        start = datetime(2026, 9, 1, 18, 30)
        samples = [{"status": "VALID", "target_error_pct": value,
                    "target_shortfall_pct": max(0, value),
                    "slot_start": start + timedelta(days=index)}
                   for index, value in enumerate((2, 4, 8, 20))]
        immature = _target_history_metrics(samples, minimum_samples=5, correction_cap_pct=15)
        mature = _target_history_metrics(samples, minimum_samples=4, correction_cap_pct=5)
        self.assertIsNone(immature["target_suggested_correction_pct"])
        self.assertEqual(mature["target_suggested_correction_pct"], 5.0)
        self.assertEqual(mature["target_history_mode"], "SHADOW_READ_ONLY")


if __name__ == "__main__":
    unittest.main()
