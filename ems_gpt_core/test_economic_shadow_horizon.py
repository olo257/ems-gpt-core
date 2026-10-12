"""Regression cases for full-horizon shadow economic search."""
import unittest

from economic_shadow import ShadowSlot
from economic_shadow_horizon import optimize_horizon


class HorizonTests(unittest.TestCase):
    def test_multiple_buy_and_sell_slots(self):
        slots = [
            ShadowSlot("s1", 2.1, 1.8, False, True, 0, 0),
            ShadowSlot("s2", 2.0, 1.7, False, True, 0, 0),
            ShadowSlot("b1", 0.5, 0.1, True, False, 0, 0),
            ShadowSlot("b2", 0.5, 0.1, True, False, 0, 0),
        ]
        plan = optimize_horizon(slots, 90, [15] * 4, 90)
        self.assertTrue(plan.feasible, plan.reasons)
        self.assertGreater(sum(plan.sell_kwh), 0)
        self.assertGreater(sum(plan.buy_kwh), 0)
        self.assertGreater(plan.net_pln, 0)

    def test_no_sale_when_price_zero(self):
        slots = [ShadowSlot("s", 2, 0, False, True, 0, 0)]
        plan = optimize_horizon(slots, 80, [15], 80)
        self.assertTrue(plan.feasible)
        self.assertEqual(sum(plan.sell_kwh), 0)

    def test_infeasible_load_without_grid_fails(self):
        slots = [ShadowSlot("x", 2, 1, False, False, 3, 0)]
        plan = optimize_horizon(slots, 15, [15], 15)
        self.assertFalse(plan.feasible)

    def test_terminal_soc_preserved(self):
        slots = [ShadowSlot("s", 2, 1.8, False, True, 0, 0)]
        plan = optimize_horizon(slots, 90, [15], 90)
        self.assertTrue(plan.feasible)
        self.assertEqual(sum(plan.sell_kwh), 0)


if __name__ == "__main__":
    unittest.main()
