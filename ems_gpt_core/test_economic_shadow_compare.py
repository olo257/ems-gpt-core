"""Tests for explicit P/E comparison without production integration."""
import unittest

from economic_shadow import ShadowSlot
from economic_shadow_compare import evaluate_shadow


class EconomicShadowComparisonTests(unittest.TestCase):
    def setUp(self):
        self.slots = [
            ShadowSlot("2026-10-13 17:45", 2.18661, 1.58661, False, True, 0.1, 0),
            ShadowSlot("2026-10-13 23:45", 1.05844, 0.45844, True, False, 0.1, 0),
        ]

    def test_missing_production_baseline_is_explicit(self):
        result = evaluate_shadow(self.slots, 95, [80, 80], 80)
        self.assertIsNone(result["production"])
        self.assertIsNone(result["delta_vs_production_pln"])
        self.assertEqual(result["mode"], "SHADOW_READ_ONLY")

    def test_production_baseline_comparison(self):
        result = evaluate_shadow(self.slots, 95, [80, 80], 80,
                                 production_sell_kwh=[0, 0],
                                 production_buy_kwh=[0, 0])
        self.assertTrue(result["production"]["feasible"])
        self.assertIsNotNone(result["best"])
        self.assertGreaterEqual(result["delta_vs_production_pln"], 0)

    def test_missing_half_production_flows_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_shadow(self.slots, 95, [80, 80], 80,
                            production_sell_kwh=[0, 0])


if __name__ == "__main__":
    unittest.main()
