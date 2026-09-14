import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planner_service import (
    allocate_slot_discharge,
    cheapest_recovery_indices,
    economic_sell_indices,
    paired_arbitrage_buy_indices,
    soc_bridge_envelopes,
)
from ingestion_service import derive_price_windows


class PairedArbitrageTests(unittest.TestCase):
    def test_native_load_can_discharge_below_sale_floor_to_technical_reserve(self):
        sale, native = allocate_slot_discharge(
            energy_kwh=6.0, capacity_kwh=15.0, reserve_pct=15.0,
            sale_floor_pct=40.0, native_deficit_kwh=0.30,
            sale_request_kwh=1.0, max_slot_output_kwh=1.25, eta_d=0.95)

        self.assertEqual(sale, 0.0)
        self.assertAlmostEqual(native * 0.95, 0.30)

    def test_sale_floor_blocks_only_export_energy(self):
        sale, native = allocate_slot_discharge(
            energy_kwh=6.15, capacity_kwh=15.0, reserve_pct=15.0,
            sale_floor_pct=40.0, native_deficit_kwh=0.30,
            sale_request_kwh=1.0, max_slot_output_kwh=1.25, eta_d=0.95)

        self.assertAlmostEqual(sale, 0.15)
        self.assertAlmostEqual(native * 0.95, 0.30)

    def test_soc_target_bridges_only_to_finite_next_grid_replenishment(self):
        rows = [
            {"forecast_load_kwh": 0.30, "forecast_pv_total_kwh": 0.0},
            {"forecast_load_kwh": 0.30, "forecast_pv_total_kwh": 0.0},
            {"forecast_load_kwh": 0.10, "forecast_pv_total_kwh": 0.0},
            {"forecast_load_kwh": 0.40, "forecast_pv_total_kwh": 0.0},
        ]

        floors, targets = soc_bridge_envelopes(
            rows, {2}, 15.0, 15.0, 1.0, 1.0, 0.50, 0.0, 90.0, 95.0)

        self.assertAlmostEqual(targets[2], 15.0 + 0.40 / 15.0 * 100.0)
        self.assertEqual(targets[1], 15.0)
        self.assertEqual(floors, targets)

    def test_forecast_pv_reduces_bridge_target_without_clock_rule(self):
        rows = [
            {"forecast_load_kwh": 0.30, "forecast_pv_total_kwh": 0.0},
            {"forecast_load_kwh": 0.10, "forecast_pv_total_kwh": 0.60},
            {"forecast_load_kwh": 0.20, "forecast_pv_total_kwh": 0.0},
        ]

        floors, targets = soc_bridge_envelopes(
            rows, set(), 15.0, 15.0, 1.0, 1.0, 1.25, 0.0, 90.0, 95.0)

        self.assertEqual(floors[0], 15.0)
        self.assertEqual(targets[0], 15.0)

    def test_soc_bridge_has_no_special_evening_hour(self):
        evening = [
            {"slot_start_local": "2026-09-14 19:45:00", "forecast_load_kwh": 0.4,
             "forecast_pv_total_kwh": 0.0},
            {"slot_start_local": "2026-09-14 20:00:00", "forecast_load_kwh": 0.2,
             "forecast_pv_total_kwh": 0.0},
        ]
        morning = [
            {**row, "slot_start_local": f"2026-09-15 0{3 + index}:15:00"}
            for index, row in enumerate(evening)
        ]

        evening_result = soc_bridge_envelopes(
            evening, set(), 15.0, 15.0, 0.9, 0.95, 1.25, 1.0, 90.0, 95.0)
        morning_result = soc_bridge_envelopes(
            morning, set(), 15.0, 15.0, 0.9, 0.95, 1.25, 1.0, 90.0, 95.0)

        self.assertEqual(evening_result, morning_result)

    def test_price_windows_have_no_clock_dependency(self):
        prices = [
            {"sell": 3.039, "buy": 3.629},
            {"sell": 2.865, "buy": 3.455},
            {"sell": 1.160, "buy": 1.750},
            {"sell": 1.077, "buy": 1.667},
        ]

        windows = derive_price_windows(prices, 0.90, 0.95, 0.08, 0.05, 0.05)

        self.assertTrue(windows[0][0])
        self.assertFalse(windows[0][1])

    def test_ppd_sell_peak_uses_complete_horizon_not_named_sessions(self):
        rows = [
            {"price_sell_pln_kwh": 2.70, "price_buy_pln_kwh": 3.29},
            {"price_sell_pln_kwh": 3.04, "price_buy_pln_kwh": 3.63},
            {"price_sell_pln_kwh": 2.87, "price_buy_pln_kwh": 3.46},
            {"price_sell_pln_kwh": 1.08, "price_buy_pln_kwh": 1.67},
        ]

        self.assertEqual(economic_sell_indices(rows, 0.90, 0.95, 0.08, 0.05), {1, 2})

    def test_post_sale_recovery_can_span_multiple_profitable_slots(self):
        rows = [
            {"price_sell_pln_kwh": 2.70, "price_buy_pln_kwh": 3.29, "sale_window": True},
            {"price_sell_pln_kwh": 3.04, "price_buy_pln_kwh": 3.63, "sale_window": True},
            {"price_sell_pln_kwh": 2.20, "price_buy_pln_kwh": 2.79, "sale_window": False},
            {"price_sell_pln_kwh": 1.16, "price_buy_pln_kwh": 1.75, "sale_window": False},
            {"price_sell_pln_kwh": 1.08, "price_buy_pln_kwh": 1.67, "sale_window": False},
            {"price_sell_pln_kwh": 1.15, "price_buy_pln_kwh": 1.74, "sale_window": False},
        ]

        eligible = paired_arbitrage_buy_indices(rows, 0.90, 0.95, 0.08, 0.05)

        self.assertEqual(eligible, {3, 4, 5})

    def test_no_recovery_window_exists_without_prior_sale_candidate(self):
        rows = [
            {"price_sell_pln_kwh": 1.00, "price_buy_pln_kwh": 1.59, "sale_window": False},
            {"price_sell_pln_kwh": 0.50, "price_buy_pln_kwh": 1.09, "sale_window": False},
        ]

        self.assertEqual(paired_arbitrage_buy_indices(rows, 0.90, 0.95, 0.08, 0.05), set())

    def test_recovery_waits_for_cheapest_slots_and_expands_with_energy(self):
        rows = [
            {"price_buy_pln_kwh": 2.15},
            {"price_buy_pln_kwh": 2.13},
            {"price_buy_pln_kwh": 1.75},
            {"price_buy_pln_kwh": 1.67},
            {"price_buy_pln_kwh": 1.74},
        ]
        eligible = set(range(len(rows)))

        self.assertEqual(cheapest_recovery_indices(rows, eligible, 0, 1.0, 1.25, 0.90), {3})
        self.assertEqual(cheapest_recovery_indices(rows, eligible, 0, 3.0, 1.25, 0.90), {2, 3, 4})


if __name__ == "__main__":
    unittest.main()
