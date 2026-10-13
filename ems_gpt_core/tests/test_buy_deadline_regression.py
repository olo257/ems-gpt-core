"""Regression: a BUY deadline must never silently lower the requested SOC."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_service import optimize_energy_horizon, validate_physical_buy_commitments


def slot(buy, load=0.0):
    return {
        "buy_window": buy, "sale_window": False,
        "forecast_load_kwh": load, "forecast_heat_pump_load_kwh": 0.0,
        "forecast_pv_total_kwh": 0.0,
        "price_buy_pln_kwh": 0.50, "price_sell_pln_kwh": 0.0,
    }


def dispatch(rows, initial, targets, due):
    return optimize_energy_horizon(
        rows, initial, 15.0, 15.0, 1.0, 1.0,
        0.0, 0.0, 5.0, 15, [15.0] * len(rows),
        15.0, 0.1, 100.0,
        minimum_soc_targets=targets,
        hard_target_indices=set(),
        target_due_indices=due,
        required_soc_pcts=[15.0] * len(rows),
        battery_sales_enabled=False,
        max_grid_import_kw=12.0,
    )


class BuyDeadlineRegressionTests(unittest.TestCase):
    def test_buy_21_to_28_percent_is_feasible(self):
        result = dispatch([slot(True)], 21.0, [28.0], {0})
        self.assertGreater(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertGreaterEqual(result["flows"][0]["soc_end_pct"], 28.0)


    def test_unreachable_buy_deadline_keeps_reachable_plan_and_reports_shortfall(self):
        result = dispatch([slot(True)], 21.0, [50.0], {0})
        self.assertLess(result["effective_target_pcts"][0], 50.0)
        self.assertEqual(result["requested_target_pcts"][0], 50.0)
        self.assertGreater(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertLessEqual(result["flows"][0]["grid_charge_kwh"], 1.25 + 1e-9)


    def test_no_buy_outside_buy_window(self):
        result = dispatch([slot(False)], 21.0, [28.0], set())
        self.assertEqual(result["flows"][0]["grid_charge_kwh"], 0.0)

    def test_two_slot_buy_window_covers_physical_deficit(self):
        rows = [slot(True), slot(True)]
        rows[0]["price_buy_pln_kwh"] = 0.25
        rows[1]["price_buy_pln_kwh"] = 0.80
        result = dispatch(rows, 21.0, [35.0, 35.0], {1})
        self.assertGreaterEqual(result["flows"][-1]["soc_end_pct"], 35.0)
        self.assertGreater(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertLessEqual(result["flows"][0]["grid_charge_kwh"], 1.25 + 1e-9)
        self.assertLessEqual(result["flows"][1]["grid_charge_kwh"], 1.25 + 1e-9)

    def test_physical_buy_audit_rejects_paper_only_buy(self):
        rows = [slot(True)]
        fake = [{
            "grid_charge_kwh": 0.0,
            "pv_to_bat_kwh": 0.0,
            "soc_start_pct": 21.0,
            "soc_end_pct": 21.0,
        }]
        with self.assertRaisesRegex(RuntimeError, "BUY_PHYSICAL_DEFICIT"):
            validate_physical_buy_commitments(
                rows, fake, {0}, [28.0], 5.0, 15, 15.0, 1.0)

    def test_physical_buy_audit_rejects_overpower(self):
        rows = [slot(True)]
        fake = [{
            "grid_charge_kwh": 2.0,
            "pv_to_bat_kwh": 0.0,
            "soc_start_pct": 21.0,
            "soc_end_pct": 34.0,
        }]
        with self.assertRaisesRegex(RuntimeError, "CHARGE_POWER_LIMIT"):
            validate_physical_buy_commitments(
                rows, fake, {0}, [28.0], 5.0, 15, 15.0, 1.0)
