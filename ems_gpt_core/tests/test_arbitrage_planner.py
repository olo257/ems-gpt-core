import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planner_service import cheapest_recovery_indices, economic_sell_indices, paired_arbitrage_buy_indices
from ingestion_service import derive_price_windows


class PairedArbitrageTests(unittest.TestCase):
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
