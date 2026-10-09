import unittest
from datetime import datetime, timedelta

from ppd_service import build_flexible_ppd, current_live_flexible_row, plan_bound_decisions


def rows(soc=(60, 100, 100, 100, 80), pv=(0.1, 0.6, 0.1, 0.7, 0.0), target=70):
    start = datetime(2026, 9, 16, 10, 0)
    return [
        {
            "slot_start": start + timedelta(minutes=15 * index),
            "local_day": start.date(),
            "soc_end_pct": soc[index],
            "soc_target_pct": target,
            "pv_flex_kwh": pv[index],
            "sell_battery": False,
            "flexible_is_economic": True,
        }
        for index in range(len(soc))
    ]


class FlexiblePpdTests(unittest.TestCase):
    def test_live_surplus_opens_current_slot_despite_underforecast(self):
        source = rows(soc=(49, 53, 60, 60, 60), pv=(0, 0, 0.4, 0, 0), target=53)
        sample = {"captured_at": source[0]["slot_start"] + timedelta(seconds=30),
                  "soc_pct": 100, "pv_power_w": 4172, "load_power_w": 1228}
        source[0] = current_live_flexible_row(
            source[0], sample, now=sample["captured_at"], stale_seconds=120)
        result = build_flexible_ppd(source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.375)
        self.assertTrue(result[0].pv_cwu_allowed)
        self.assertTrue(result[0].pv_ev_allowed)

    def test_soc_target_below_full_does_not_open_flexible_window(self):
        result = build_flexible_ppd(
            rows(soc=(99.9, 99.9, 99.9, 99.9, 99.9), target=75),
            cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertFalse(any(x.pv_cwu_allowed or x.pv_ev_allowed for x in result))

    def test_live_soc_above_target_but_below_full_cannot_open_corridor(self):
        source = rows(soc=(99.9, 100, 100, 100, 80), target=75)
        sample = {"captured_at": source[0]["slot_start"], "soc_pct": 99.9,
                  "pv_power_w": 6000, "load_power_w": 1000}
        result = current_live_flexible_row(
            source[0], sample, now=sample["captured_at"], stale_seconds=120)
        self.assertIs(result, source[0])

    def test_stale_telemetry_cannot_open_flexible_corridor(self):
        source = rows(soc=(49, 49, 49, 49, 49), pv=(0, 0, 0, 0, 0), target=53)
        sample = {"captured_at": source[0]["slot_start"], "soc_pct": 99,
                  "pv_power_w": 4000, "load_power_w": 1000}
        result = current_live_flexible_row(
            source[0], sample, now=sample["captured_at"] + timedelta(minutes=3),
            stale_seconds=120)
        self.assertIs(result, source[0])

    def test_windows_are_continuous_between_first_and_last_anchor(self):
        result = build_flexible_ppd(rows(), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, True, True, True, False])
        self.assertEqual([x.pv_ev_allowed for x in result], [False, True, True, True, False])
        self.assertFalse(result[2].cwu_anchor)
        self.assertTrue(result[2].pv_cwu_allowed)

    def test_target_is_read_only_and_not_returned_as_a_planner_input(self):
        source = rows(soc=(60, 70, 100, 100, 80), target=75)
        result = build_flexible_ppd(source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, False, True, True, False])
        self.assertFalse(hasattr(result[0], "soc_target_pct"))
        self.assertEqual([row["soc_target_pct"] for row in source], [75] * 5)

    def test_unexpected_soc_gain_moves_window_earlier_on_next_run(self):
        low = build_flexible_ppd(rows(soc=(60, 65, 80, 80, 80)), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        high = build_flexible_ppd(rows(soc=(100, 100, 100, 100, 100)), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertFalse(low[1].pv_cwu_allowed)
        self.assertTrue(high[1].pv_cwu_allowed)

    def test_no_candidate_pv_means_no_window(self):
        result = build_flexible_ppd(rows(pv=(0.0, 0.01, 0.01, 0.01, 0.0)), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertFalse(any(x.pv_cwu_allowed or x.pv_ev_allowed for x in result))

    def test_weak_forecast_keeps_live_surplus_option_open_after_full_soc(self):
        result = build_flexible_ppd(
            rows(soc=(100, 100, 100, 100, 100), pv=(0.0, 0.245, 0.20, 0.0, 0.0), target=95),
            cwu_threshold_kwh=0.5, ev_threshold_kwh=0.375)
        self.assertTrue(result[1].pv_cwu_allowed)
        self.assertTrue(result[1].pv_ev_allowed)
        self.assertFalse(result[1].cwu_anchor)
        self.assertFalse(result[1].ev_anchor)
        self.assertFalse(result[3].pv_cwu_allowed)

    def test_shared_window_allows_runtime_to_reapply_cwu_priority(self):
        result = build_flexible_ppd(
            rows(pv=(0.1, 0.4, 0.4, 0.4, 0.0)),
            cwu_threshold_kwh=0.5,
            ev_threshold_kwh=0.4,
        )
        self.assertEqual(
            [x.pv_cwu_allowed for x in result],
            [False, True, True, True, False],
        )
        self.assertEqual(
            [x.pv_ev_allowed for x in result],
            [False, True, True, True, False],
        )
        self.assertFalse(result[1].cwu_anchor)
        self.assertTrue(result[1].ev_anchor)

    def test_windows_do_not_cross_local_day(self):
        source = rows()
        source[3]["local_day"] = datetime(2026, 9, 17).date()
        source[4]["local_day"] = datetime(2026, 9, 17).date()
        result = build_flexible_ppd(source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, True, True, True, False])

    def test_battery_sale_does_not_block_independent_flexible_pv_window(self):
        source = rows(pv=(0.1, 0.6, 0.6, 0.7, 0.0))
        source[2]["sell_battery"] = True
        result = build_flexible_ppd(source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, True, True, True, False])

    def test_pv_sale_opportunity_cost_never_blocks_cwu_or_ev_priority(self):
        source = rows(pv=(0.1, 0.6, 0.1, 0.7, 0.0))
        for row in source:
            row["flexible_is_economic"] = False
        result = build_flexible_ppd(
            source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual(
            [x.pv_cwu_allowed for x in result],
            [False, True, True, True, False],
        )
        self.assertEqual(
            [x.pv_ev_allowed for x in result],
            [False, True, True, True, False],
        )

    def test_full_day_keeps_flexible_priority_despite_export_opportunity(self):
        start = datetime(2026, 9, 27)
        source = []
        for index in range(96):
            daytime = 32 <= index <= 64
            source.append({
                "slot_start": start + timedelta(minutes=15 * index),
                "local_day": start.date(),
                "soc_end_pct": 100.0,
                "soc_target_pct": 70.0,
                "pv_flex_kwh": 0.75 if daytime else 0.0,
                "sell_battery": False,
                "flexible_is_economic": False,
            })

        result = build_flexible_ppd(
            source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)

        self.assertTrue(all(result[index].pv_cwu_allowed for index in range(32, 65)))
        self.assertTrue(all(result[index].pv_ev_allowed for index in range(32, 65)))
        self.assertFalse(any(result[index].pv_cwu_allowed for index in range(0, 32)))
        self.assertFalse(any(result[index].pv_cwu_allowed for index in range(65, 96)))

    def test_target_affecting_processes_are_copied_from_the_frozen_plan(self):
        decisions = plan_bound_decisions({
            "planned_buy_kwh": 0.75,
            "planned_sell_kwh": 0.50,
            "planned_pv_export_kwh": 0.40,
            "price_sell_pln_kwh": 0.25,
            "sell_bat_policy_allowed": 1,
            "sell_pv_policy_allowed": 1,
            "grid_policy_planned": "BUY_ALLOWED",
            "export_policy_planned": "SELL_BAT",
            "heat_pump_window": 1,
        }, 0.02)
        by_name = {decision[0]: decision for decision in decisions}
        self.assertEqual(by_name["BATTERY_IMPORT"][1:3], (True, "BUY_ALLOWED"))
        self.assertEqual(by_name["SELL_BAT"][1:3], (True, "ALLOWED"))
        self.assertEqual(by_name["SELL_PV"][1:3], (True, "ALLOWED"))
        self.assertEqual(by_name["HP_HEAT_DHW"][1:3], (True, "ON"))

    def test_ppd_rejects_battery_sale_when_policy_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "PPD_SELL_BAT_BLOCKED_PLAN"):
            plan_bound_decisions({
                "slot_start": datetime(2026, 9, 18, 10, 0),
                "planned_buy_kwh": 0.0,
                "planned_sell_kwh": 0.5,
                "planned_pv_export_kwh": 0.0,
                "price_sell_pln_kwh": 1.0,
                "sell_bat_policy_allowed": 0,
                "grid_policy_planned": "NEUTRAL",
                "export_policy_planned": "SELL_BAT",
                "heat_pump_window": 0,
            }, 0.02)

    def test_ppd_does_not_recalculate_planner_owned_battery_economics(self):
        decisions = plan_bound_decisions({
            "planned_buy_kwh": 0.0,
            "planned_sell_kwh": 0.0,
            "planned_pv_export_kwh": 0.0,
            "price_sell_pln_kwh": -0.01,
            "grid_policy_planned": "NEUTRAL",
            "export_policy_planned": "NEUTRAL",
            "heat_pump_window": 0,
        }, 0.02)
        self.assertTrue(all(not decision[1] for decision in decisions))
        by_name = {decision[0]: decision for decision in decisions}
        self.assertEqual(by_name["SELL_PV"][2], "BLOCKED")

    def test_positive_price_allows_sell_pv_without_forecast_export(self):
        decisions = plan_bound_decisions({
            "planned_buy_kwh": 0.0,
            "planned_sell_kwh": 0.0,
            "planned_pv_export_kwh": 0.0,
            "price_sell_pln_kwh": 0.01,
            "sell_pv_policy_allowed": 1,
            "grid_policy_planned": "NEUTRAL",
            "export_policy_planned": "NEUTRAL",
            "heat_pump_window": 0,
        }, 0.02)
        sell_pv = next(item for item in decisions if item[0] == "SELL_PV")
        self.assertEqual(sell_pv[1:3], (True, "ALLOWED"))

    def test_nonpositive_price_blocks_only_sell_pv_process(self):
        decisions = plan_bound_decisions({
            "planned_buy_kwh": 0.0,
            "planned_sell_kwh": 0.0,
            "planned_pv_export_kwh": 0.75,
            "price_sell_pln_kwh": 0.0,
            "grid_policy_planned": "NEUTRAL",
            "export_policy_planned": "NO_SELL_PV",
            "heat_pump_window": 0,
        }, 0.02)
        by_name = {decision[0]: decision for decision in decisions}
        self.assertEqual(by_name["SELL_BAT"][1:3], (False, "BLOCKED"))
        self.assertEqual(by_name["SELL_PV"][1:3], (False, "BLOCKED"))

    def test_database_encoded_zero_never_enables_hp(self):
        for stored_zero in (0, False, "0", "false", b"0", b"\x00"):
            decisions = plan_bound_decisions({
                "planned_buy_kwh": 0.0,
                "planned_sell_kwh": 0.0,
                "planned_pv_export_kwh": 0.0,
                "price_sell_pln_kwh": 0.0,
                "grid_policy_planned": "NEUTRAL",
                "export_policy_planned": "NEUTRAL",
                "heat_pump_window": stored_zero,
            }, 0.02)
            hp = next(decision for decision in decisions
                      if decision[0] == "HP_HEAT_DHW")
            self.assertEqual(hp[1:3], (False, "OFF"))

    def test_ppd_rejects_zero_buy_with_buy_allowed_policy(self):
        with self.assertRaisesRegex(RuntimeError, "PPD_IMPORT_PLAN_MISMATCH"):
            plan_bound_decisions({
                "slot_start": datetime(2026, 9, 18, 10, 0),
                "planned_buy_kwh": 0.0,
                "planned_sell_kwh": 0.0,
                "grid_policy_planned": "BUY_ALLOWED",
                "export_policy_planned": "NEUTRAL",
                "heat_pump_window": 0,
            }, 0.02)


if __name__ == "__main__":
    unittest.main()
