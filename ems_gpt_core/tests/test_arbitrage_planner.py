import pathlib
import sys
import unittest
from decimal import Decimal
from datetime import date, datetime, time, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planner_service import (
    allocate_slot_discharge,
    backward_target_commitments,
    build_soc_contracts,
    battery_sale_economics,
    cheapest_recovery_indices,
    economic_sell_indices,
    paired_arbitrage_buy_indices,
    optimize_energy_horizon,
    pv_first_target_caps,
    next_replenishment_prices,
    planning_tou_programs,
    strict_database_bool,
    historical_terminal_soc,
    hp_heating_window_indices,
    bridge_soc_commitments,
    derive_soc_commitments,
    soc_bridge_envelopes,
)
from ingestion_service import derive_price_windows


class PairedArbitrageTests(unittest.TestCase):
    def test_hp_window_starts_after_morning_sell_and_ignores_buy(self):
        day = date(2026, 9, 18)
        start = datetime.combine(day, datetime.min.time())
        rows = []
        for index in range(96):
            slot_start = start + timedelta(minutes=15 * index)
            rows.append({
                "slot_start": slot_start,
                "slot_end": slot_start + timedelta(minutes=15),
                "buy_window": time(4, 0) <= slot_start.time() < time(5, 0),
                "sale_window": time(7, 0) <= slot_start.time() < time(8, 0),
            })

        allowed = hp_heating_window_indices(rows, day)

        self.assertNotIn(2, allowed)
        self.assertNotIn(28, allowed)
        self.assertIn(32, allowed)
        self.assertIn(75, allowed)
        self.assertNotIn(76, allowed)

    def test_hp_window_without_sell_is_7_to_19(self):
        day = date(2026, 9, 18)
        start = datetime.combine(day, datetime.min.time())
        rows = [{
            "slot_start": start + timedelta(minutes=15 * index),
            "slot_end": start + timedelta(minutes=15 * (index + 1)),
            "buy_window": False,
            "sale_window": False,
        } for index in range(96)]

        allowed = hp_heating_window_indices(rows, day)

        self.assertNotIn(27, allowed)
        self.assertIn(28, allowed)
        self.assertIn(75, allowed)
        self.assertNotIn(76, allowed)

    def test_weekend_uses_the_same_7_to_19_window(self):
        day = date(2026, 9, 19)
        start = datetime.combine(day, datetime.min.time())
        rows = [{
            "slot_start": start + timedelta(minutes=15 * index),
            "slot_end": start + timedelta(minutes=15 * (index + 1)),
            "buy_window": index < 8,
            "sale_window": False,
        } for index in range(96)]

        allowed = hp_heating_window_indices(rows, day)

        self.assertNotIn(27, allowed)
        self.assertIn(28, allowed)

    def test_hp_window_stops_before_evening_sell(self):
        day = date(2026, 9, 18)
        start = datetime.combine(day, datetime.min.time())
        rows = []
        for index in range(96):
            slot_start = start + timedelta(minutes=15 * index)
            rows.append({
                "slot_start": slot_start,
                "slot_end": slot_start + timedelta(minutes=15),
                "buy_window": False,
                "sale_window": time(17, 30) <= slot_start.time() < time(19, 0),
            })

        allowed = hp_heating_window_indices(rows, day)

        self.assertIn(69, allowed)
        self.assertNotIn(70, allowed)

    def test_48h_soc_contract_closes_every_slot_without_hidden_grid_hold(self):
        rows = []
        for index in range(192):
            quarter = index % 96
            pv = 0.55 if 40 <= quarter < 64 else 0.0
            buy = 8 <= quarter < 12
            rows.append({
                "price_buy_pln_kwh": 0.55 if buy else 1.40,
                "price_sell_pln_kwh": 0.15,
                "buy_window": buy,
                "sale_window": False,
                "forecast_load_kwh": 0.11,
                "forecast_heat_pump_load_kwh": 0.0,
                "forecast_pv_total_kwh": pv,
                "slot_start": index,
                "slot_end": index + 1,
            })
        economic = optimize_energy_horizon(
            rows, 55.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 35.0, 0.10, 100.0)
        contract = build_soc_contracts(
            rows, economic["flows"], 15.0, 15.0, 0.90, 0.95,
            0.0, 35.0, 100.0)
        result = optimize_energy_horizon(
            rows, 55.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 35.0, 0.10, 100.0,
            contract["charge_targets"], set(), contract["buy_due_indices"],
            contract["required"])

        for index, (row, flow, required) in enumerate(
                zip(rows, result["flows"], contract["required"])):
            self.assertGreaterEqual(flow["soc_end_pct"] + 1e-9, required)
            if index:
                self.assertAlmostEqual(
                    result["flows"][index - 1]["soc_end_pct"],
                    flow["soc_start_pct"])
            if flow["grid_charge_kwh"] > 1e-9:
                self.assertTrue(row["buy_window"])
            if (flow["soc_start_pct"] > 15.01
                    and flow["grid_charge_kwh"] <= 0.02):
                self.assertLessEqual(flow["grid_load_kwh"], 0.05)

    def test_soc_contract_subtracts_only_allocated_buy_energy(self):
        rows = [
            {"buy_window": False, "forecast_load_kwh": 0.20,
             "forecast_heat_pump_load_kwh": 0.0, "forecast_pv_total_kwh": 0.0,
             "slot_start": i, "slot_end": i + 1}
            for i in range(4)
        ]
        rows[1]["buy_window"] = True
        flows = [{"grid_charge_kwh": 0.0, "battery_to_load_kwh": 0.20,
                  "pv_to_bat_kwh": 0.0, "soc_end_pct": 20.0} for _ in rows]
        flows[1] = {"grid_charge_kwh": 0.10, "battery_to_load_kwh": 0.20,
                    "pv_to_bat_kwh": 0.0, "soc_end_pct": 21.0}

        contract = build_soc_contracts(
            rows, flows, 15.0, 15.0, 0.90, 0.95, 0.0, 20.0, 100.0)

        self.assertGreater(contract["required"][0], 15.0)
        self.assertGreater(contract["required"][1], 20.0)
        self.assertEqual(contract["buy_due_indices"], {1})
        self.assertGreaterEqual(contract["charge_targets"][1],
                                contract["required"][1])

    def test_unavoidable_grid_load_at_reserve_does_not_inflate_required_soc(self):
        rows = [{
            "buy_window": False,
            "forecast_load_kwh": 0.30,
            "forecast_heat_pump_load_kwh": 0.0,
            "forecast_pv_total_kwh": 0.0,
            "slot_start": 0,
            "slot_end": 1,
        }]
        economic_flows = [{
            "grid_charge_kwh": 0.0,
            "grid_load_kwh": 0.30,
            "battery_to_load_kwh": 0.0,
            "pv_to_bat_kwh": 0.0,
            "soc_end_pct": 15.0,
        }]

        contract = build_soc_contracts(
            rows, economic_flows, 15.0, 15.0, 0.90, 0.95,
            0.0, 15.0, 100.0)

        self.assertEqual(contract["required"], [15.0])
        result = optimize_energy_horizon(
            rows, 15.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0], 15.0, 0.10, 100.0,
            contract["charge_targets"], set(),
            contract["buy_due_indices"], contract["required"])
        self.assertEqual(result["flows"][0]["soc_end_pct"], 15.0)
        self.assertGreater(result["flows"][0]["grid_load_kwh"], 0.0)

    def test_required_soc_is_enforced_in_every_slot(self):
        rows = [
            {"price_buy_pln_kwh": 1.0, "price_sell_pln_kwh": 0.0,
             "buy_window": False, "sale_window": False,
             "forecast_load_kwh": 0.10, "forecast_pv_total_kwh": 0.0}
            for _ in range(3)
        ]
        result = optimize_energy_horizon(
            rows, 30.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * 3, 20.0, 0.25, 100.0,
            [15.0] * 3, set(), set(), [25.0, 24.0, 20.0])
        self.assertTrue(all(
            flow["soc_end_pct"] + 1e-9 >= required
            for flow, required in zip(result["flows"], [25.0, 24.0, 20.0])
        ))

    def test_pv_first_caps_expensive_buy_displacing_cheap_pv_export(self):
        rows=[]; flows=[]
        for index in range(8):
            rows.append({
                "buy_window": index < 2,
                "forecast_load_kwh": 0.10,
                "forecast_heat_pump_load_kwh": 0.0,
                "forecast_pv_total_kwh": 1.0 if index >= 5 else 0.0,
                "price_buy_pln_kwh": 1.10,
                "price_sell_pln_kwh": 0.12,
            })
            flows.append({
                "soc_end_pct": [35,45,43,41,39,45,50,55][index],
                "grid_charge_kwh": 1.0 if index < 2 else 0.0,
                "pv_export_kwh": 0.8 if index >= 5 else 0.0,
            })
        caps=pv_first_target_caps(rows,flows,{0,1},15.0,15.0,0.90,0.05)
        self.assertIn(1,caps)
        self.assertLess(caps[1],flows[1]["soc_end_pct"])

    def test_pv_first_does_not_cap_energy_needed_to_reach_pv(self):
        rows=[{"buy_window":i==0,"forecast_load_kwh":0.2,
               "forecast_heat_pump_load_kwh":0.0,
               "forecast_pv_total_kwh":1.0 if i==3 else 0.0,
               "price_buy_pln_kwh":1.1,"price_sell_pln_kwh":0.1}
              for i in range(4)]
        flows=[{"soc_end_pct":15.0,"grid_charge_kwh":1.0,"pv_export_kwh":0.0},
               {"soc_end_pct":15.0,"grid_charge_kwh":0.0,"pv_export_kwh":0.0},
               {"soc_end_pct":15.0,"grid_charge_kwh":0.0,"pv_export_kwh":0.0},
               {"soc_end_pct":20.0,"grid_charge_kwh":0.0,"pv_export_kwh":0.5}]
        self.assertEqual(pv_first_target_caps(rows,flows,{0},15,15,.9,.05),{})

    def test_historical_terminal_soc_uses_overlapping_weighted_windows(self):
        terminal_day = date(2026, 9, 18)
        rows = []
        for age in range(1, 29):
            value = 70.0 if age <= 7 else 40.0 if age <= 14 else 20.0
            rows.append({"local_day": terminal_day-timedelta(days=age), "soc_end_pct": value})
        result = historical_terminal_soc(
            rows, terminal_day, {7: 50.0, 14: 25.0, 28: 25.0}, 15.0)
        self.assertAlmostEqual(result["means"][7], 70.0)
        self.assertAlmostEqual(result["means"][14], 55.0)
        self.assertAlmostEqual(result["means"][28], 37.5)
        self.assertAlmostEqual(result["soc_pct"], 58.125)
        self.assertEqual(result["source"], "WEIGHTED_ACTUAL_CLOSE_7_14_28D")

    def test_historical_terminal_soc_falls_back_without_complete_days(self):
        result = historical_terminal_soc([], date(2026, 9, 18),
                                         {7: 50.0, 14: 25.0, 28: 25.0}, 15.0)
        self.assertEqual(result["soc_pct"], 15.0)
        self.assertEqual(result["source"], "FALLBACK_RESERVE")

    def test_flexible_surplus_uses_nearest_buy_window_opportunity_cost(self):
        rows = [
            {"buy_window": False, "price_buy_pln_kwh": 9.0},
            {"buy_window": True, "price_buy_pln_kwh": 2.0},
            {"buy_window": True, "price_buy_pln_kwh": 1.5},
            {"buy_window": False, "price_buy_pln_kwh": 9.0},
            {"buy_window": True, "price_buy_pln_kwh": 0.5},
        ]
        prices = next_replenishment_prices(rows)
        self.assertEqual(prices[0], 1.5)
        self.assertEqual(prices[1], 1.5)
        self.assertEqual(prices[2], 0.5)
        self.assertEqual(prices[3], 0.5)
        self.assertIsNone(prices[4])

    def test_backward_target_reserves_future_pv_before_flexible_surplus(self):
        rows = [
            {"buy_window": False, "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0, "slot_start": index,
             "slot_end": index + 1}
            for index in range(3)
        ]
        rows[1]["forecast_pv_total_kwh"] = 1.0
        rows[2]["forecast_load_kwh"] = 0.5

        contract = backward_target_commitments(
            rows, 15.0, 15.0, 0.90, 0.95, 0.0, 100.0, 15.0)

        self.assertEqual(contract["targets"][0], contract["targets"][1])
        self.assertGreater(contract["targets"][1], 15.0)
        self.assertGreater(contract["reserved_pv_kwh"][1], 0.5)
        self.assertEqual(contract["source"][0], "PV")

    def test_early_pv_in_same_replenishment_window_inherits_closing_target(self):
        rows = [
            {"buy_window": False, "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0, "slot_start": index,
             "slot_end": index + 1}
            for index in range(4)
        ]
        rows[1]["forecast_pv_total_kwh"] = 0.20
        rows[2]["forecast_pv_total_kwh"] = 1.00
        rows[3]["forecast_load_kwh"] = 0.50

        contract = backward_target_commitments(
            rows, 15.0, 15.0, 0.90, 0.95, 0.0, 100.0, 15.0)

        self.assertEqual(contract["source"][1], "PV")
        closing = next(i for i, row in enumerate(rows)
                       if row["slot_end"] == contract["due"][1])
        self.assertGreater(contract["targets"][1], 15.0)
        self.assertEqual(contract["targets"][1], contract["targets"][closing])

    def test_continuous_pv_window_uses_its_largest_target_from_first_light(self):
        rows = [
            {"buy_window": False, "forecast_load_kwh": 0.10,
             "forecast_pv_total_kwh": 0.0, "slot_start": index,
             "slot_end": index + 1}
            for index in range(10)
        ]
        for index, pv in enumerate((0.02, 0.20, 0.50, 0.80, 0.70, 0.30), 2):
            rows[index]["forecast_pv_total_kwh"] = pv
        rows[8]["forecast_load_kwh"] = 1.0
        rows[9]["forecast_load_kwh"] = 1.0

        contract = backward_target_commitments(
            rows, 15.0, 15.0, 0.90, 0.95, 1.0, 100.0, 30.0)

        daylight_targets = contract["targets"][2:8]
        self.assertGreater(daylight_targets[0], 15.0)
        self.assertEqual(len(set(daylight_targets)), 1)

    def test_backward_target_buy_boundary_covers_later_load(self):
        rows = [
            {"buy_window": True, "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0, "slot_start": 0, "slot_end": 1},
            {"buy_window": False, "forecast_load_kwh": 0.95,
             "forecast_pv_total_kwh": 0.0, "slot_start": 1, "slot_end": 2},
        ]
        contract = backward_target_commitments(
            rows, 15.0, 15.0, 1.0, 0.95, 0.0, 100.0, 15.0)

        self.assertEqual(contract["targets"][0], 21.75)
        self.assertEqual(contract["targets"][1], 15.0)

    def test_selected_buy_closes_earlier_energy_bridge(self):
        rows = [
            {"buy_window": index == 2, "forecast_load_kwh": 0.30,
             "forecast_pv_total_kwh": 0.0, "slot_start": index,
             "slot_end": index + 1}
            for index in range(6)
        ]
        contract = backward_target_commitments(
            rows, 15.0, 15.0, 1.0, 1.0, 0.0, 100.0, 15.0,
            0.25, {2})

        self.assertEqual(contract["source"][1], "BUY")
        self.assertEqual(contract["due"][1], 3)
        # BUY closes the earlier energy bridge, while its target remains the
        # charge ceiling on the approach so preceding PV can displace import.
        self.assertEqual(contract["targets"][1], contract["targets"][2])

    def test_sufficient_pv_closes_bridge_before_later_load(self):
        rows = [
            {"buy_window": False, "forecast_load_kwh": 0.30,
             "forecast_pv_total_kwh": 0.0, "slot_start": index,
             "slot_end": index + 1}
            for index in range(5)
        ]
        rows[2]["forecast_load_kwh"] = 0.0
        rows[2]["forecast_pv_total_kwh"] = 1.20
        contract = backward_target_commitments(
            rows, 15.0, 15.0, 1.0, 1.0, 0.0, 100.0, 15.0)

        self.assertEqual(contract["source"][1], "PV")
        self.assertEqual(contract["due"][1], 3)
        self.assertEqual(contract["targets"][1], contract["targets"][2])
        self.assertGreater(contract["targets"][1], 15.0)

    def test_buy_boundary_keeps_requirement_above_its_power_limit(self):
        rows = [
            {"buy_window": index == 2, "forecast_load_kwh": 0.0,
             "forecast_pv_total_kwh": 0.0, "slot_start": index,
             "slot_end": index + 1}
            for index in range(5)
        ]
        rows[3]["forecast_load_kwh"] = 1.50
        rows[4]["forecast_load_kwh"] = 1.50
        contract = backward_target_commitments(
            rows, 15.0, 15.0, 1.0, 1.0, 0.0, 100.0, 15.0,
            0.25, {2}, 5.0, 15)

        self.assertGreater(contract["targets"][1], 15.0)
        self.assertEqual(contract["due"][1], 3)

    def test_unselected_expensive_buy_does_not_reset_cheap_buy_target(self):
        rows = []
        for index in range(16):
            rows.append({
                "price_buy_pln_kwh": 0.50 if index < 4 else (4.00 if index == 8 else 3.00),
                "price_sell_pln_kwh": 0.0,
                "buy_window": index < 4 or index == 8,
                "sale_window": False,
                "forecast_load_kwh": 0.25 if index >= 4 else 0.0,
                "forecast_pv_total_kwh": 0.0,
                "slot_start": index,
                "slot_end": index + 1,
            })
        seed = optimize_energy_horizon(
            rows, 15.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 15.0, 0.25, 100.0)
        selected = {i for i, flow in enumerate(seed["flows"])
                    if flow["grid_charge_kwh"] > 0.02}
        contract = backward_target_commitments(
            rows, 15.0, 15.0, 0.90, 0.95, 0.0, 100.0, 15.0,
            0.25, selected)
        due = {i for i in selected if i + 1 not in selected}
        result = optimize_energy_horizon(
            rows, 15.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 15.0, 0.25, 100.0,
            contract["targets"], due, due)

        self.assertGreater(sum(result["flows"][i]["grid_charge_kwh"] for i in range(4)), 3.0)
        self.assertEqual(result["flows"][8]["grid_charge_kwh"], 0.0)
        self.assertGreater(contract["targets"][3], contract["targets"][8])

    def test_partial_pv_reduces_target_but_does_not_erase_remaining_need(self):
        rows = [
            {"buy_window": index == 0, "forecast_load_kwh": 0.30,
             "forecast_pv_total_kwh": 0.0, "slot_start": index,
             "slot_end": index + 1}
            for index in range(6)
        ]
        rows[3]["forecast_pv_total_kwh"] = 0.60
        no_pv = [dict(row, forecast_pv_total_kwh=0.0) for row in rows]
        with_pv = backward_target_commitments(
            rows, 15.0, 15.0, 0.90, 0.95, 0.0, 100.0, 15.0,
            0.25, {0})
        without_pv = backward_target_commitments(
            no_pv, 15.0, 15.0, 0.90, 0.95, 0.0, 100.0, 15.0,
            0.25, {0})

        self.assertGreater(with_pv["targets"][0], 15.0)
        self.assertLess(with_pv["targets"][0], without_pv["targets"][0])

    def test_pv_before_selected_buy_can_displace_more_expensive_grid_energy(self):
        rows = [
            {"price_buy_pln_kwh": 0.90, "price_sell_pln_kwh": 0.35,
             "buy_window": index == 2, "sale_window": False,
             "forecast_load_kwh": 0.285,
             "forecast_pv_total_kwh": 0.80 if index < 2 else 0.0,
             "slot_start": index, "slot_end": index + 1}
            for index in range(5)
        ]
        selected = {2}
        contract = backward_target_commitments(
            rows, 15.0, 15.0, 0.90, 0.95, 0.0, 100.0, 35.0,
            0.25, selected)
        result = optimize_energy_horizon(
            rows, 30.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 35.0, 0.25, 100.0,
            contract["targets"], {2}, {2})

        self.assertGreater(result["flows"][0]["pv_to_bat_kwh"], 0.0)
        self.assertGreater(result["flows"][1]["pv_to_bat_kwh"], 0.0)
        self.assertEqual(result["flows"][0]["pv_export_kwh"], 0.0)
        self.assertEqual(result["flows"][1]["pv_export_kwh"], 0.0)
        self.assertLess(result["flows"][2]["grid_charge_kwh"], 0.75)

    def test_hard_target_rejects_unfunded_discharge(self):
        rows = [{"price_buy_pln_kwh": 2.0, "price_sell_pln_kwh": 0.0,
                 "buy_window": False, "sale_window": False,
                 "forecast_load_kwh": 0.3, "forecast_pv_total_kwh": 0.0}]
        with self.assertRaisesRegex(RuntimeError, "No feasible SOC state"):
            optimize_energy_horizon(
                rows, 30.0, 15.0, 15.0, 0.9, 0.95, 0.08, 0.05,
                5.0, 15, [15.0], 15.0, 0.25, 100.0, [30.0], {0})

    def test_due_target_is_capped_to_reachable_soc_in_rolling_replan(self):
        rows = [
            {"price_buy_pln_kwh": 0.50, "price_sell_pln_kwh": 0.0,
             "buy_window": False, "sale_window": False,
             "forecast_load_kwh": 0.0, "forecast_pv_total_kwh": 0.0},
            {"price_buy_pln_kwh": 0.40, "price_sell_pln_kwh": 0.0,
             "buy_window": True, "sale_window": False,
             "forecast_load_kwh": 0.0, "forecast_pv_total_kwh": 0.0},
        ]

        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.95, 0.95, 0.08, 0.05,
            5.0, 15, [15.0, 15.0], 15.0, 0.25, 100.0,
            [20.0, 80.0], {1}, {1})

        self.assertLess(result["effective_target_pcts"][1], 80.0)
        self.assertEqual(
            result["flows"][1]["soc_end_pct"],
            result["effective_target_pcts"][1],
        )
        self.assertGreater(result["flows"][1]["grid_charge_kwh"], 0.0)

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

    def test_battery_sale_uses_required_without_redefining_sale_floor(self):
        rows = [{
            "price_buy_pln_kwh": 4.0,
            "price_sell_pln_kwh": 10.0,
            "sale_window": True,
            "forecast_load_kwh": 0.0,
            "forecast_pv_total_kwh": 0.0,
        }]
        result = optimize_energy_horizon(
            rows, 95.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [40.0], 15.0, 0.25, 100.0,
            [100.0], set(), set(), [90.0])
        flow = result["flows"][0]

        self.assertGreater(flow["battery_sell_kwh"], 0.0)
        self.assertGreaterEqual(flow["soc_end_pct"], 90.0)

    def test_battery_sale_is_zero_when_soc_is_below_required(self):
        rows = [{
            "price_buy_pln_kwh": 4.0,
            "price_sell_pln_kwh": 10.0,
            "sale_window": True,
            "forecast_load_kwh": 0.0,
            "forecast_pv_total_kwh": 0.0,
        }]
        with self.assertRaisesRegex(RuntimeError, "No feasible SOC state"):
            optimize_energy_horizon(
                rows, 40.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
                5.0, 15, [20.0], 15.0, 0.25, 100.0,
                [100.0], set(), set(), [90.0])

    def test_grid_only_load_is_rejected_without_profitable_future_sale(self):
        rows=[
            {"price_buy_pln_kwh":1.0,"price_sell_pln_kwh":0.0,
             "forecast_load_kwh":0.30,"forecast_pv_total_kwh":0.0},
            {"price_buy_pln_kwh":2.0,"price_sell_pln_kwh":0.0,
             "forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0},
        ]
        result=self.optimize(rows,40.0,15.0,[15.0,15.0])
        self.assertGreater(result["flows"][0]["battery_to_load_kwh"],0.0)
        self.assertLessEqual(result["flows"][0]["grid_load_kwh"],0.06)

    def test_minimum_soc_uses_only_unavoidable_grid_load_and_remains_feasible(self):
        rows = [{
            "price_buy_pln_kwh": 3.0,
            "price_sell_pln_kwh": 0.0,
            "buy_window": False,
            "sale_window": False,
            "forecast_load_kwh": 0.30,
            "forecast_pv_total_kwh": 0.0,
        }]
        result = self.optimize(rows, 15.0, 15.0, [15.0])
        flow = result["flows"][0]

        self.assertAlmostEqual(flow["soc_end_pct"], 15.0)
        self.assertAlmostEqual(flow["grid_load_kwh"], 0.30)
        self.assertEqual(flow["grid_charge_kwh"], 0.0)

    def test_future_sale_never_allows_grid_hold_for_native_load(self):
        rows=[
            {"price_buy_pln_kwh":1.0,"price_sell_pln_kwh":0.0,
             "buy_window":False,"sale_window":False,
             "forecast_load_kwh":0.30,"forecast_pv_total_kwh":0.0},
            {"price_buy_pln_kwh":4.0,"price_sell_pln_kwh":5.0,
             "buy_window":False,"sale_window":True,
             "forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0},
        ]
        result=self.optimize(rows,17.0,15.0,[15.0,15.0])
        self.assertGreater(result["flows"][0]["battery_to_load_kwh"],0.0)
        self.assertLessEqual(result["flows"][0]["grid_load_kwh"],0.06)

    def test_cheap_buy_precharges_for_load_before_profitable_sale(self):
        rows = [
            {"price_buy_pln_kwh": 0.50, "price_sell_pln_kwh": 0.0,
             "buy_window": True, "sale_window": False,
             "forecast_load_kwh": 0.0, "forecast_pv_total_kwh": 0.0},
            {"price_buy_pln_kwh": 2.00, "price_sell_pln_kwh": 0.0,
             "buy_window": False, "sale_window": False,
             "forecast_load_kwh": 0.60, "forecast_pv_total_kwh": 0.0},
            {"price_buy_pln_kwh": 2.00, "price_sell_pln_kwh": 3.00,
             "buy_window": False, "sale_window": True,
             "forecast_load_kwh": 0.0, "forecast_pv_total_kwh": 0.0},
        ]
        result = self.optimize(rows, 20.0, 15.0, [15.0, 15.0, 15.0])
        buy, load, sale = result["flows"]

        self.assertGreater(buy["grid_charge_kwh"], 0.60)
        self.assertGreater(load["battery_to_load_kwh"], 0.50)
        self.assertLessEqual(load["grid_load_kwh"], 0.04)
        self.assertGreater(sale["battery_sell_kwh"], 0.0)

    def test_voluntary_grid_hold_is_rejected_in_every_pass(self):
        rows=[
            {"price_buy_pln_kwh":1.0,"price_sell_pln_kwh":0.0,
             "forecast_load_kwh":0.30,"forecast_pv_total_kwh":0.0},
            {"price_buy_pln_kwh":4.0,"price_sell_pln_kwh":5.0,
             "sale_window":True,"forecast_load_kwh":0.0,"forecast_pv_total_kwh":0.0},
        ]
        result = optimize_energy_horizon(
            rows, 40.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0, 15.0], 15.0, 0.25, 100.0)
        self.assertGreater(result["flows"][0]["battery_to_load_kwh"], 0.0)
        self.assertLessEqual(result["flows"][0]["grid_load_kwh"], 0.04)

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

    def test_terminal_price_valley_is_wide_enough_for_physical_recovery(self):
        prices = [
            {"sell": 0.0, "buy": 2.0},
            {"sell": 0.0, "buy": 1.40},
            {"sell": 0.0, "buy": 1.31},
            {"sell": 0.0, "buy": 1.35},
            {"sell": 0.0, "buy": 1.36},
        ]

        windows = derive_price_windows(
            prices, 0.90, 0.95, 0.08, 0.05, 0.05, minimum_buy_slots=4)

        self.assertEqual([i for i, (_, buy) in enumerate(windows) if buy], [1, 2, 3, 4])

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

    def test_token_charge_cannot_unlock_purchase_for_native_load(self):
        rows = [
            {"price_buy_pln_kwh": price, "price_sell_pln_kwh": 0.0,
             "sale_window": False, "buy_window": True,
             "forecast_load_kwh": 0.10, "forecast_pv_total_kwh": 0.0}
            for price in (0.80, 0.70, 0.20, 0.30)
        ]

        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 30.0, 0.25, 100.0)

        charging = [flow for flow in result["flows"]
                    if flow["grid_charge_kwh"] > 1e-9]
        self.assertTrue(charging)
        self.assertTrue(all(
            flow["grid_charge_kwh"] + 1e-9 >= flow["grid_load_kwh"]
            for flow in charging
        ))
        self.assertEqual(result["flows"][0]["grid_charge_kwh"], 0.0)
        self.assertEqual(result["flows"][1]["grid_charge_kwh"], 0.0)

    def test_grid_target_does_not_cap_pv_charging(self):
        rows = [{"price_buy_pln_kwh": 5.0, "price_sell_pln_kwh": 10.0,
                 "sale_window": False, "buy_window": False,
                 "forecast_load_kwh": 0.0, "forecast_pv_total_kwh": 2.0}]

        result = optimize_energy_horizon(
            rows, 50.0, 10.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [100.0], 15.0, 0.25, 100.0, [55.0], set(),
            set())
        flow = result["flows"][0]

        self.assertGreater(flow["soc_end_pct"], 55.0)
        self.assertGreater(flow["pv_to_bat_kwh"], 0.0)
        self.assertEqual(flow["grid_charge_kwh"], 0.0)

    def test_partial_pv_window_does_not_require_unreachable_full_target(self):
        rows = [{"price_buy_pln_kwh": 1.0, "price_sell_pln_kwh": 0.5,
                 "sale_window": False, "buy_window": False,
                 "forecast_load_kwh": 0.0, "forecast_pv_total_kwh": 0.10}]

        result = optimize_energy_horizon(
            rows, 15.0, 15.0, 15.0, 0.90, 0.95, 0.08, 0.05,
            5.0, 15, [15.0], 15.0, 0.25, 100.0, [50.0])

        self.assertLess(result["flows"][0]["soc_end_pct"], 50.0)
        self.assertGreater(result["flows"][0]["battery_charge_internal_kwh"], 0.0)

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

    def test_battery_sale_requires_profitable_future_energy_replacement(self):
        rows = [
            {"price_sell_pln_kwh": 1.90, "price_buy_pln_kwh": 2.49},
            {"price_sell_pln_kwh": 0.80, "price_buy_pln_kwh": 1.60},
        ]

        economics = battery_sale_economics(rows, 0, 0.90, 0.95, 0.08, 0.05)

        self.assertEqual(economics["replacement_buy_price"], 1.60)
        self.assertAlmostEqual(economics["required_sell_price"], 2.001345, places=6)
        self.assertAlmostEqual(economics["expected_margin"], 1.90 - 1.60 / 0.855 - 0.08)
        self.assertFalse(economics["eligible"])

        rows[0]["price_sell_pln_kwh"] = 2.10
        self.assertTrue(
            battery_sale_economics(rows, 0, 0.90, 0.95, 0.08, 0.05)["eligible"])

    def test_battery_sale_is_blocked_without_future_replacement_price(self):
        rows = [{"price_sell_pln_kwh": 4.00, "price_buy_pln_kwh": 4.50}]

        economics = battery_sale_economics(rows, 0, 0.90, 0.95, 0.08, 0.05)

        self.assertIsNone(economics["replacement_buy_price"])
        self.assertFalse(economics["eligible"])

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
