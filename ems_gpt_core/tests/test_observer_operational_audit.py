from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observer_service import audit_operational_rows


class ObserverOperationalAuditTests(unittest.TestCase):
    def setUp(self):
        self.options = {"technical_flow_threshold_kwh": 0.05}

    def test_nonpositive_price_export_is_critical_and_evidence_based(self):
        rows = [{"slot_start": datetime(2026, 9, 29, 12), "price_sell_pln_kwh": 0,
                 "planned_pv_export_kwh": 0.2, "actual_pv_export_kwh": 0.1}]
        findings, summary = audit_operational_rows(rows, [], options=self.options)
        finding = next(item for item in findings if item["metric"] == "export_at_nonpositive_price")
        self.assertEqual(finding["severity"], "CRITICAL")
        self.assertEqual(finding["evidence"][0]["slot_start"], rows[0]["slot_start"])
        self.assertEqual(summary["audited_history_slots"], 1)

    def test_actual_import_outside_buy_window_over_technical_noise_is_critical(self):
        rows = [{"slot_start": datetime(2026, 9, 29, 3), "market_window": "NEUTRAL",
                 "actual_buy_kwh": 0.08, "planned_buy_kwh": 0}]
        findings, _ = audit_operational_rows(rows, [], options=self.options)
        finding = next(item for item in findings if item["metric"] == "import_outside_buy_window")
        self.assertEqual(finding["severity"], "CRITICAL")
        self.assertEqual(finding["evidence"][0]["market_window"], "NEUTRAL")

    def test_import_below_technical_threshold_is_ignored(self):
        rows = [{"slot_start": datetime(2026, 9, 29, 3), "market_window": "NEUTRAL",
                 "actual_buy_kwh": 0.049, "planned_buy_kwh": 0}]
        findings, _ = audit_operational_rows(rows, [], options=self.options)
        self.assertFalse(any(item["metric"] == "import_outside_buy_window" for item in findings))

    def test_pv_underforecast_requires_repeated_daily_evidence(self):
        rows = []
        for day in range(3):
            for slot in range(8):
                rows.append({"slot_start": datetime(2026, 9, 20, 10, 0) + timedelta(days=day, minutes=15 * slot),
                             "forecast_pv_total_kwh": 0.125, "actual_pv_total_kwh": 0.25})
        findings, _ = audit_operational_rows(rows, [], options=self.options)
        finding = next(item for item in findings if item["metric"] == "pv_forecast_underestimation_7d")
        self.assertEqual(finding["value"], 100.0)
        self.assertEqual(len(finding["evidence"]["underestimated_days"]), 3)

    def test_single_pv_deviation_does_not_create_forecast_finding(self):
        rows = [{"slot_start": datetime(2026, 9, 29, 12),
                 "forecast_pv_total_kwh": 1.0, "actual_pv_total_kwh": 2.0}]
        findings, _ = audit_operational_rows(rows, [], options=self.options)
        self.assertFalse(any(item["metric"] == "pv_forecast_underestimation_7d" for item in findings))

    def test_hp_plan_outside_allowed_window_is_reported(self):
        rows = [{"slot_start": datetime(2026, 9, 29, 6, 45), "market_window": "NEUTRAL",
                 "forecast_heat_pump_load_kwh": 0.3}]
        findings, _ = audit_operational_rows([], rows, options=self.options)
        finding = next(item for item in findings if item["metric"] == "heat_pump_outside_window")
        self.assertEqual(finding["evidence"][0]["forecast_heat_pump_load_kwh"], 0.3)

    def test_planned_export_at_negative_price_in_future_is_critical(self):
        rows = [{"slot_start": datetime(2026, 9, 29, 12), "price_sell_pln_kwh": -0.01,
                 "planned_pv_export_kwh": 0.2}]
        findings, _ = audit_operational_rows([], rows, options=self.options)
        finding = next(item for item in findings if item["metric"] == "export_at_nonpositive_price")
        self.assertEqual(finding["severity"], "CRITICAL")

    def test_end_of_day_soc_uses_measured_start_plus_measured_delta(self):
        rows = [{"slot_start": datetime(2026, 9, 28, 23, 45), "soc_start_pct": 34,
                 "soc_delta_pct": -1.5}]
        findings, _ = audit_operational_rows(rows, [], options=self.options)
        finding = next(item for item in findings if item["metric"] == "end_of_day_soc_below_target_range")
        self.assertEqual(finding["value"], 32.5)


if __name__ == "__main__":
    unittest.main()
