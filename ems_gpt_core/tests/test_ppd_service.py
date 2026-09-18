import unittest
from datetime import datetime, timedelta

from ppd_service import build_flexible_ppd, plan_bound_decisions


def rows(soc=(60, 70, 80, 80, 80), pv=(0.1, 0.6, 0.1, 0.7, 0.0), target=70):
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
    def test_windows_are_continuous_between_first_and_last_anchor(self):
        result = build_flexible_ppd(rows(), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, True, True, True, False])
        self.assertEqual([x.pv_ev_allowed for x in result], [False, True, True, True, False])
        self.assertFalse(result[2].cwu_anchor)
        self.assertTrue(result[2].pv_cwu_allowed)

    def test_target_is_read_only_and_not_returned_as_a_planner_input(self):
        source = rows(target=75)
        result = build_flexible_ppd(source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, False, False, True, False])
        self.assertFalse(hasattr(result[0], "soc_target_pct"))
        self.assertEqual([row["soc_target_pct"] for row in source], [75] * 5)

    def test_unexpected_soc_gain_moves_window_earlier_on_next_run(self):
        low = build_flexible_ppd(rows(soc=(60, 65, 80, 80, 80)), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        high = build_flexible_ppd(rows(soc=(75, 75, 80, 80, 80)), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertFalse(low[1].pv_cwu_allowed)
        self.assertTrue(high[1].pv_cwu_allowed)

    def test_no_anchor_means_no_window(self):
        result = build_flexible_ppd(rows(pv=(0.1, 0.1, 0.1, 0.1, 0.0)), cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertFalse(any(x.pv_cwu_allowed or x.pv_ev_allowed for x in result))

    def test_windows_do_not_cross_local_day(self):
        source = rows()
        source[3]["local_day"] = datetime(2026, 9, 17).date()
        source[4]["local_day"] = datetime(2026, 9, 17).date()
        result = build_flexible_ppd(source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, True, False, True, False])

    def test_battery_sale_is_a_hard_window_boundary(self):
        source = rows(pv=(0.1, 0.6, 0.6, 0.7, 0.0))
        source[2]["sell_battery"] = True
        result = build_flexible_ppd(source, cwu_threshold_kwh=0.5, ev_threshold_kwh=0.4)
        self.assertEqual([x.pv_cwu_allowed for x in result], [False, False, False, True, False])

    def test_target_affecting_processes_are_copied_from_the_frozen_plan(self):
        decisions = plan_bound_decisions({
            "planned_buy_kwh": 0.75,
            "planned_sell_kwh": 0.50,
            "grid_policy_planned": "BUY_ALLOWED",
            "export_policy_planned": "SELL_BAT",
            "heat_pump_window": 1,
        }, 0.02)
        by_name = {decision[0]: decision for decision in decisions}
        self.assertEqual(by_name["BATTERY_IMPORT"][1:3], (True, "BUY_ALLOWED"))
        self.assertEqual(by_name["BATTERY_EXPORT"][1:3], (True, "SELL_BAT"))
        self.assertEqual(by_name["HP_HEAT_DHW"][1:3], (True, "ON"))

    def test_ppd_does_not_recalculate_planner_owned_battery_economics(self):
        decisions = plan_bound_decisions({
            "planned_buy_kwh": 0.0,
            "planned_sell_kwh": 0.0,
            "grid_policy_planned": "NEUTRAL",
            "export_policy_planned": "NEUTRAL",
            "heat_pump_window": 0,
        }, 0.02)
        self.assertTrue(all(not decision[1] for decision in decisions))

    def test_database_encoded_zero_never_enables_hp(self):
        for stored_zero in (0, False, "0", "false", b"0", b"\x00"):
            decisions = plan_bound_decisions({
                "planned_buy_kwh": 0.0,
                "planned_sell_kwh": 0.0,
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
