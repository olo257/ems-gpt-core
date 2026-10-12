"""Unit tests for isolated shadow economics primitives."""
import unittest

from economic_shadow import (
    ShadowCandidate, ShadowSlot, allowed_rebuy_slots,
    compare_candidates, sale_rebuy_unit_margin,
)


class EconomicShadowTests(unittest.TestCase):
    def test_13_october_evening_arbitrage_positive(self):
        margin = sale_rebuy_unit_margin(1.58661, 1.05844, 0.95, 0.95, 0.08)
        self.assertGreater(margin, 0.30)
        self.assertLess(margin, 0.35)

    def test_rebuy_only_in_authorized_windows(self):
        slots = [
            ShadowSlot("17:45", 2.18661, 1.58661, False, True, 0, 0),
            ShadowSlot("22:45", 1.142, 0.542, False, False, 0, 0),
            ShadowSlot("23:30", 1.074, 0.474, True, False, 0, 0),
            ShadowSlot("23:45", 1.05844, 0.45844, True, False, 0, 0),
        ]
        self.assertEqual(allowed_rebuy_slots(slots, 0), [2, 3])

    def test_infeasible_variant_never_selected(self):
        results = compare_candidates([
            ShadowCandidate("P", 0, 0, 0, True, ()),
            ShadowCandidate("E", 3, 3.33, 1, False, ("SOC_UNSAFE",)),
        ])
        self.assertEqual(results["recommended_shadow_only"], "P")

    def test_invalid_efficiency(self):
        with self.assertRaises(ValueError):
            sale_rebuy_unit_margin(1.5, 1.0, 0, 0.95, 0.08)


if __name__ == "__main__":
    unittest.main()
