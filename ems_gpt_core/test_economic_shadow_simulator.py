"""Regression tests for shadow-only simulator."""
import unittest

from economic_shadow import ShadowSlot
from economic_shadow_simulator import (
    ShadowAssumptions, propose_one_slot_arbitrage, simulate,
)


class ShadowSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.slots = [
            ShadowSlot("2026-10-13 17:45", 2.18661, 1.58661, False, True, 0.1, 0),
            ShadowSlot("2026-10-13 23:45", 1.05844, 0.45844, True, False, 0.1, 0),
        ]

    def test_13_october_price_pair_proposed(self):
        sell, buy = propose_one_slot_arbitrage(self.slots)
        self.assertGreater(sell[0], 0)
        self.assertGreater(buy[1], 0)
        self.assertEqual(buy[0], 0)

    def test_arbitrage_validated_with_sufficient_soc(self):
        sell, buy = propose_one_slot_arbitrage(self.slots)
        result = simulate("E", self.slots, 95, sell, buy, terminal_soc_pct=80)
        self.assertTrue(result.feasible, result.reasons)
        self.assertGreater(result.net_pln, 0)

    def test_no_sell_if_not_authorized(self):
        result = simulate("E", self.slots, 95, [0, 0.1], [0, 0])
        self.assertFalse(result.feasible)
        self.assertIn("1:SELL_NOT_ALLOWED", result.reasons)

    def test_terminal_soc_rejected(self):
        result = simulate("E", self.slots, 95, [1, 0], [0, 0], terminal_soc_pct=95)
        self.assertFalse(result.feasible)
        self.assertIn("TERMINAL_SOC", result.reasons)

    def test_buy_disallowed(self):
        result = simulate("E", self.slots, 95, [0, 0], [0.2, 0])
        self.assertFalse(result.feasible)
        self.assertIn("0:BUY_NOT_ALLOWED", result.reasons)

    def test_power_cap(self):
        result = simulate("E", self.slots, 95, [2, 0], [0, 0])
        self.assertIn("0:DISCHARGE_POWER", result.reasons)


if __name__ == "__main__":
    unittest.main()
