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
    def test_buy_window_stops_at_target_even_when_grid_cap_is_higher(self):
        rows = [slot(True), slot(True)]
        result = optimize_energy_horizon(
            rows, 40.0, 15.0, 15.0, 0.9, 0.95,
            0.0, 0.0, 5.0, 15, [15.0, 15.0], 15.0, 0.1, 100.0,
            minimum_soc_targets=[40.0, 40.0],
            hard_target_indices=set(), target_due_indices=set(),
            required_soc_pcts=[15.0, 15.0], battery_sales_enabled=False,
            grid_charge_caps=[100.0, 100.0], max_grid_import_kw=12.0,
        )
        self.assertEqual(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertEqual(result["flows"][1]["grid_charge_kwh"], 0.0)
        self.assertEqual(result["flows"][1]["soc_end_pct"], 40.0)

    def test_buy_uses_maximum_slot_volume_but_never_exceeds_dynamic_target(self):
        rows = [slot(True) for _ in range(3)]
        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.9, 0.95,
            0.0, 0.0, 5.0, 15, [15.0] * 3, 15.0, 0.1, 100.0,
            minimum_soc_targets=[40.0] * 3,
            hard_target_indices=set(), target_due_indices=set(),
            required_soc_pcts=[15.0] * 3, battery_sales_enabled=False,
            grid_charge_caps=[100.0] * 3, max_grid_import_kw=12.0,
        )
        self.assertEqual([f["soc_end_pct"] for f in result["flows"]],
                         [27.5, 35.0, 40.0])
        self.assertAlmostEqual(result["flows"][0]["grid_charge_kwh"], 1.25)
        self.assertAlmostEqual(result["flows"][1]["grid_charge_kwh"], 1.25)
        self.assertLess(result["flows"][2]["grid_charge_kwh"], 1.25)

    def test_target_quantization_does_not_make_a_reachable_buy_unreachable(self):
        result = optimize_energy_horizon(
            [slot(True)], 40.0, 15.0, 15.0, 0.9, 0.95,
            0.0, 0.0, 5.0, 15, [15.0], 15.0, 0.25, 100.0,
            minimum_soc_targets=[40.05], hard_target_indices=set(),
            target_due_indices={0}, required_soc_pcts=[15.0],
            battery_sales_enabled=False, grid_charge_caps=[100.0],
        )
        self.assertGreaterEqual(result["flows"][0]["soc_end_pct"], 40.05)
        self.assertLessEqual(result["flows"][0]["soc_end_pct"], 40.30)

    def test_buy_21_to_28_percent_is_feasible(self):
        result = dispatch([slot(True)], 21.0, [28.0], {0})
        self.assertGreater(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertGreaterEqual(result["flows"][0]["soc_end_pct"], 28.0)

    def test_buy_charges_battery_while_grid_supplies_house_load(self):
        row = slot(True, load=0.20)
        result = optimize_energy_horizon(
            [row], 21.0, 15.0, 15.0, 1.0, 1.0,
            0.0, 0.0, 5.0, 15, [15.0], 15.0, 0.1, 100.0,
            minimum_soc_targets=[28.0], hard_target_indices=set(),
            target_due_indices={0}, required_soc_pcts=[15.0],
            battery_sales_enabled=False, max_grid_import_kw=12.0,
            grid_charge_caps=[100.0],
        )
        flow = result["flows"][0]
        self.assertAlmostEqual(flow["grid_charge_kwh"], 1.05)
        self.assertEqual(flow["soc_end_pct"], 28.0)
        self.assertAlmostEqual(flow["grid_load_kwh"], 0.20)
        self.assertAlmostEqual(flow["voluntary_grid_load_kwh"], 0.20)

    def test_overnight_buy_reaches_daily_close_after_load_before_dhw(self):
        rows = [slot(False, load=0.20) for _ in range(4)]
        rows += [slot(True, load=0.20) for _ in range(2)]
        result = optimize_energy_horizon(
            rows, 36.0, 15.0, 15.0, 0.9, 0.95,
            0.0, 0.0, 5.0, 15, [15.0] * len(rows), 40.0, 0.1, 100.0,
            minimum_soc_targets=[40.0] * len(rows), hard_target_indices=set(),
            target_due_indices={5}, required_soc_pcts=[15.0] * len(rows),
            battery_sales_enabled=False, max_grid_import_kw=12.0,
            grid_charge_caps=[15.0] * 4 + [100.0] * 2,
            minimum_grid_charge_kwh=0.02,
        )
        self.assertGreaterEqual(result["flows"][5]["soc_end_pct"], 40.0)
        for flow in result["flows"][4:]:
            self.assertGreater(flow["grid_charge_kwh"], 0.0)
            self.assertAlmostEqual(flow["grid_load_kwh"], 0.20)
        # Retain charging headroom before the second slot: charging to 40% in
        # slot 4 would leave no battery charge in slot 5 to qualify its separate
        # house-load import, so the window-end target would become unreachable.
        self.assertGreater(result["flows"][4]["grid_charge_kwh"], 0.0)
        self.assertLess(result["flows"][4]["grid_charge_kwh"], 1.2)
        self.assertGreater(result["flows"][5]["grid_charge_kwh"], 0.0)


    def test_unreachable_buy_deadline_rejects_instead_of_silently_lowering_target(self):
        with self.assertRaisesRegex(RuntimeError, "BUY_TARGET_UNREACHABLE:slot=0"):
            dispatch([slot(True)], 21.0, [50.0], {0})


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
