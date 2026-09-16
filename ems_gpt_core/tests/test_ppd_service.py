import unittest
from datetime import datetime, timedelta

from ppd_service import build_flexible_ppd


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


if __name__ == "__main__":
    unittest.main()
