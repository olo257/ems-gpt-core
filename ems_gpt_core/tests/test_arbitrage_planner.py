import pathlib
import sys
import unittest
from decimal import Decimal


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planner_service import (
    allocate_slot_discharge,
    cheapest_recovery_indices,
    economic_sell_indices,
    paired_arbitrage_buy_indices,
    optimize_energy_horizon,
    planning_tou_programs,
    strict_database_bool,
    bridge_soc_commitments,
    derive_soc_commitments,
    soc_bridge_envelopes,
)
from ingestion_service import derive_price_windows


class PairedArbitrageTests(unittest.TestCase):
    def test_database_flags_are_strict_true_false_for_historical_rows(self):
        self.assertIs(strict_database_bool(False, "buy_window"), False)
        self.assertIs(strict_database_bool(True, "buy_window"), True)
        self.assertIs(strict_database_bool(Decimal("0"), "buy_window"), False)
        self.assertIs(strict_database_bool(Decimal("1"), "buy_window"), True)
        self.assertIs(strict_database_bool("false", "buy_window"), False)
        self.assertIs(strict_database_bool("true", "buy_window"), True)
        with self.assertRaisesRegex(RuntimeError, "INVALID_BOOLEAN:buy_window"):
            strict_database_bool(2, "buy_window")

    def test_planning_uses_configured_soc_not_temporary_live_target(self):
        live = [
            {"program": 4, "soc": 100, "time": "19:30"},
            {"program": 5, "soc": 40, "time": "20:30"},
        ]
        result = planning_tou_programs(live, '{"4":40,"5":40}')
        self.assertEqual([row["soc"] for row in result], [40.0, 40.0])
        self.assertEqual([row["time"] for row in result], ["19:30", "20:30"])

    def test_missing_planning_baseline_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "DEYE_PROGRAM_SOC_BASELINE_MISSING:4"):
            planning_tou_programs([{"program": 4, "soc": 100}], '{}')

    @staticmethod
    def optimize(rows, initial_soc=40.0, terminal_soc=40.0, floors=None):
        return optimize_energy_horizon(rows, initial_soc, 15.0, 15.0, 0.90, 0.95,
            0.08, 0.05, 5.0, 15, floors or [40.0]*len(rows), terminal_soc, 0.25, 95.0)

    def test_full_horizon_links_distant_purchase_and_sale(self):
        rows=[{"price_buy_pln_kwh":2.0,"price_sell_pln_kwh":1.0,"forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0} for _ in range(100)]
        rows[1]["price_buy_pln_kwh"]=1.0
        rows[1]["buy_window"]=True
        rows[85]["price_sell_pln_kwh"]=3.0
        rows[85]["sale_window"]=True
        result=self.optimize(rows)
        self.assertGreater(result["flows"][1]["grid_charge_kwh"],0.0)
        self.assertGreater(result["flows"][85]["battery_sell_kwh"],0.0)

    def test_full_horizon_buys_for_distant_load_without_sale(self):
        rows=[{"price_buy_pln_kwh":3.0,"price_sell_pln_kwh":0.0,"forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0} for _ in range(72)]
        rows[2]["price_buy_pln_kwh"]=0.5
        rows[2]["buy_window"]=True
        rows[60]["forecast_load_kwh"]=1.0
        result=self.optimize(rows,15.0,15.0,[15.0]*len(rows))
        self.assertGreater(result["flows"][2]["grid_charge_kwh"],0.0)
        self.assertGreater(result["flows"][60]["battery_to_load_kwh"],0.0)
        self.assertFalse(any(flow["battery_sell_kwh"]>0 for flow in result["flows"]))

    def test_target_covers_future_load_and_is_not_the_floor(self):
        rows=[{"price_buy_pln_kwh":3.0,"price_sell_pln_kwh":0.0,"forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0} for _ in range(8)]
        rows[0]["price_buy_pln_kwh"]=0.5
        rows[0]["buy_window"]=True
        rows[6]["forecast_load_kwh"]=1.0
        result=self.optimize(rows,15.0,15.0,[15.0]*len(rows))
        floors,targets=derive_soc_commitments(result["flows"],15.0,15.0,15.0,90.0,95.0)
        self.assertEqual(floors[0],15.0)
        self.assertGreater(targets[0],floors[0])
        self.assertAlmostEqual(targets[0],result["flows"][0]["soc_end_pct"])
        self.assertEqual(targets[5],targets[0])
        self.assertEqual(targets[6],15.0)

    def test_sale_floor_and_terminal_soc_are_protected(self):
        rows=[{"price_buy_pln_kwh":4.0,"price_sell_pln_kwh":10.0,"sale_window":True,"forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0} for _ in range(8)]
        result=self.optimize(rows,60.0,40.0)
        self.assertGreaterEqual(result["flows"][-1]["soc_end_pct"],40.0)
        self.assertGreaterEqual(min(flow["soc_end_pct"] for flow in result["flows"] if flow["battery_sell_kwh"]>0),40.0)

    def test_sale_slot_native_load_cannot_push_soc_below_floor(self):
        rows=[{"price_buy_pln_kwh":4.0,"price_sell_pln_kwh":10.0,
               "sale_window":True,
               "forecast_load_kwh":0.30,"forecast_pv_total_kwh":0.0}]
        result=self.optimize(rows,46.5,40.0,[40.0])
        flow=result["flows"][0]
        self.assertGreater(flow["battery_sell_kwh"],0.0)
        self.assertGreaterEqual(flow["soc_end_pct"],40.0)

    def test_grid_only_load_is_rejected_without_profitable_future_sale(self):
        rows=[
            {"price_buy_pln_kwh":1.0,"price_sell_pln_kwh":0.0,
             "forecast_load_kwh":0.30,"forecast_pv_total_kwh":0.0},
            {"price_buy_pln_kwh":2.0,"price_sell_pln_kwh":0.0,
             "forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0},
        ]
        result=self.optimize(rows,40.0,15.0,[15.0,15.0])
        self.assertGreater(result["flows"][0]["battery_to_load_kwh"],0.0)
        self.assertLessEqual(result["flows"][0]["grid_load_kwh"],0.04)

    def test_grid_load_may_hold_soc_for_materially_better_sale(self):
        rows=[
            {"price_buy_pln_kwh":1.0,"price_sell_pln_kwh":0.0,
             "forecast_load_kwh":0.30,"forecast_pv_total_kwh":0.0},
            {"price_buy_pln_kwh":4.0,"price_sell_pln_kwh":5.0,
             "sale_window":True,
             "forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0},
        ]
        result=self.optimize(rows,17.0,15.0,[15.0,15.0])
        self.assertGreater(result["flows"][0]["grid_load_kwh"],0.0)
        self.assertGreater(result["flows"][1]["battery_sell_kwh"],0.0)

    def test_sale_recovery_uses_pv_before_grid_buy(self):
        rows=[
            {"forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0},
            {"forecast_load_kwh":0.2,"forecast_pv_total_kwh":1.2},
            {"forecast_load_kwh":0.3,"forecast_pv_total_kwh":0.0},
        ]
        flows=[
            {"grid_charge_kwh":0.0,"soc_end_pct":30.0},
            {"grid_charge_kwh":0.0,"soc_end_pct":36.0},
            {"grid_charge_kwh":0.0,"soc_end_pct":34.0},
        ]
        floors,targets=bridge_soc_commitments(
            rows,flows,15.0,15.0,0.98,0.98,1.25,0.0,90.0,100.0)
        self.assertEqual(targets[0],15.0)
        self.assertEqual(floors[0],15.0)

    def test_soc_controls_are_results_of_backward_pass(self):
        flows=[
            {"soc_start_pct":15.0,"soc_end_pct":35.0,"battery_charge_internal_kwh":3.0,"battery_discharge_internal_kwh":0.0,"battery_sell_kwh":0.0},
            {"soc_start_pct":35.0,"soc_end_pct":20.0,"battery_charge_internal_kwh":0.0,"battery_discharge_internal_kwh":3.0,"battery_sell_kwh":2.0},
        ]
        floors,targets=derive_soc_commitments(flows,15.0,15.0,15.0,90.0,95.0)
        self.assertEqual(targets,[35.0,15.0])
        self.assertEqual(floors,[15.0,20.0])
        self.assertNotEqual(floors,targets)
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

    def test_target_stays_above_sale_floor_and_drives_morning_recovery(self):
        rows = [
            {"price_buy_pln_kwh": 1.0 if index == 0 else 4.0,
             "price_sell_pln_kwh": 2.0,
             "buy_window": index == 0,
             "forecast_load_kwh": 0.30,
             "forecast_pv_total_kwh": 0.80 if 6 <= index < 12 else 0.0}
            for index in range(20)
        ]
        sale_floors = [20.0] * len(rows)
        seed = [{"grid_charge_kwh": 0.0} for _ in rows]
        floors, targets = bridge_soc_commitments(
            rows, seed, 15.0, 15.0, 0.90, 0.95, 1.25, 1.0,
            90.0, 100.0, sale_floors)

        self.assertTrue(all(floor == 20.0 for floor in floors))
        self.assertGreater(targets[0], floors[0])
        self.assertGreater(targets[6], floors[6])

        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, sale_floors, 15.0, 0.25, 100.0, targets)
        flows = result["flows"]

        self.assertGreater(flows[0]["grid_charge_kwh"], 0.0)
        self.assertTrue(all(
            flow["soc_end_pct"] + 1e-9 >= target
            or flow["battery_charge_internal_kwh"] > 1e-9
            for row, flow, target in zip(rows, flows, targets)
            if row["buy_window"] or row["forecast_pv_total_kwh"] > row["forecast_load_kwh"]
        ))
        self.assertFalse(any(
            flow["pv_export_kwh"] > 1e-9 and flow["soc_end_pct"] < target
            for flow, target in zip(flows, targets)
        ))

    def test_bridge_target_is_rounded_up_to_executable_soc_step(self):
        rows = [
            {
                "buy_window": True,
                "forecast_load_kwh": 0.0,
                "forecast_pv_total_kwh": 0.0,
            },
            {
                "buy_window": False,
                "forecast_load_kwh": 0.027,
                "forecast_pv_total_kwh": 0.0,
            },
        ]
        flows = [{"grid_charge_kwh": 0.0}, {"grid_charge_kwh": 0.0}]

        _, targets = bridge_soc_commitments(
            rows, flows, 15.0, 15.0, 0.90, 0.95, 1.25, 0.0,
            90.0, 100.0, [15.0, 15.0], 35.0, 0.25)

        self.assertEqual(targets[0], 35.25)

    def test_unmet_target_charges_only_in_buy_or_from_pv(self):
        rows = [
            {"price_buy_pln_kwh": 4.0, "price_sell_pln_kwh": 2.0,
             "buy_window": Decimal("0"), "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0},
            {"price_buy_pln_kwh": 1.0, "price_sell_pln_kwh": 1.0,
             "buy_window": Decimal("1"), "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0},
        ]
        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [20.0, 20.0], 20.0, 0.25, 100.0, [20.0, 27.5])

        self.assertEqual(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertGreater(result["flows"][1]["grid_charge_kwh"], 0.0)

    def test_unmet_target_between_replenishment_windows_remains_feasible(self):
        rows = [
            {"price_buy_pln_kwh": 4.0, "price_sell_pln_kwh": 0.0,
             "buy_window": Decimal("0"), "forecast_load_kwh": 0.30,
             "forecast_pv_total_kwh": 0.0},
            {"price_buy_pln_kwh": 1.0, "price_sell_pln_kwh": 0.0,
             "buy_window": Decimal("1"), "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0},
        ]
        result = optimize_energy_horizon(
            rows, 25.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [20.0, 20.0], 20.0, 0.25, 100.0, [30.0, 30.0])

        self.assertEqual(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertGreater(result["flows"][0]["battery_to_load_kwh"], 0.0)
        self.assertGreater(result["flows"][1]["grid_charge_kwh"], 0.0)

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

    def test_falling_evening_price_is_not_buy_before_overnight_trough(self):
        prices = [
            {"sell": 2.568, "buy": 3.168},
            {"sell": 2.138, "buy": 2.738},
            {"sell": 1.545, "buy": 2.145},
            {"sell": 1.153, "buy": 1.753},
            {"sell": 0.807, "buy": 1.407},
            {"sell": 0.787, "buy": 1.387},
            {"sell": 1.812, "buy": 2.412},
        ]

        windows = derive_price_windows(prices, 0.90, 0.95, 0.08, 0.05, 0.05)

        self.assertFalse(windows[0][1])
        self.assertFalse(windows[1][1])
        self.assertTrue(windows[4][1])
        self.assertTrue(windows[5][1])

    def test_small_local_price_noise_does_not_expand_buy_over_whole_day(self):
        buys = [1.50 + (0.01 if index % 2 else 0.0) for index in range(40)]
        buys[20] = 1.00
        prices = [{"buy": price, "sell": 0.0} for price in buys]

        windows = derive_price_windows(prices, 0.90, 0.95, 0.08, 0.05, 0.05)

        self.assertEqual([i for i, (_, buy) in enumerate(windows) if buy], [20])

    def test_buy_and_sale_permissions_never_overlap(self):
        prices = [
            {"sell": 3.0, "buy": 1.0},
            {"sell": 0.0, "buy": 2.0},
        ]

        windows = derive_price_windows(prices, 1.0, 1.0, 0.0, 0.0, 0.05)

        self.assertEqual(windows[0], (True, False))

    def test_sale_window_false_blocks_battery_export_but_not_native_load(self):
        rows = [{"price_buy_pln_kwh": 4.0, "price_sell_pln_kwh": 10.0,
                 "sale_window": False, "buy_window": False,
                 "forecast_load_kwh": 0.30, "forecast_pv_total_kwh": 0.0}]

        result = self.optimize(rows, 41.0, 15.0, [40.0])
        flow = result["flows"][0]

        self.assertEqual(flow["battery_sell_kwh"], 0.0)
        self.assertGreater(flow["battery_to_load_kwh"], 0.0)
        self.assertLess(flow["soc_end_pct"], 40.0)

    def test_buy_target_is_a_hard_charge_ceiling(self):
        rows = [{"price_buy_pln_kwh": 0.5, "price_sell_pln_kwh": 0.0,
                 "sale_window": False, "buy_window": True,
                 "forecast_load_kwh": 0.0, "forecast_pv_total_kwh": 0.0}]

        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0], 15.0, 0.25, 100.0, [25.0])

        self.assertGreater(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertEqual(result["flows"][0]["soc_end_pct"], 25.0)

    def test_last_replenishment_target_includes_terminal_soc(self):
        rows = [
            {"buy_window": True, "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0},
            {"buy_window": False, "forecast_load_kwh": 0.30,
             "forecast_pv_total_kwh": 0.0},
        ]
        flows = [{"battery_sell_kwh": 0.0}, {"battery_sell_kwh": 0.0}]

        _, targets = bridge_soc_commitments(
            rows, flows, 15.0, 15.0, 0.90, 0.95, 1.25, 0.0,
            90.0, 100.0, [40.0, 40.0], 30.0)

        self.assertEqual(targets[0], 32.25)

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
