import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_service import (
    deterministic_soc_target_contract, economic_buy_charge_caps,
    optimize_energy_horizon, planned_buy_execution_targets,
    validate_physical_buy_commitments,
)


class EconomicBuyTargetsTests(unittest.TestCase):
    def rows(self, cheap=1.05, dear=1.15, initial_slots=4):
        return [dict(slot_start=i, slot_end=i + 1,
                     price_buy_pln_kwh=cheap if i < initial_slots else dear,
                     price_sell_pln_kwh=0.0,
                     buy_window=i < initial_slots or i >= 10,
                     sale_window=False, forecast_load_kwh=0.55 if 4 <= i < 10 else 0.0,
                     forecast_pv_total_kwh=0.0)
                for i in range(12)]

    def optimize(self, rows, initial=40.0, optional=True):
        contract = deterministic_soc_target_contract(
            rows, [15.0] * len(rows), [15.0] * (len(rows)-1) + [40.0],
            15.0, 15.0, 0.9, 0.95, 5.0, 15, 0.0)
        result = optimize_energy_horizon(
            rows, initial, 15.0, 15.0, 0.9, 0.95, 0.0, 0.0, 5.0, 15,
            [100.0] * len(rows), 40.0, 0.1, 100.0,
            contract['charge_targets'], set(), contract['buy_due_indices'],
            contract['required'], battery_sales_enabled=False,
            grid_charge_caps=(economic_buy_charge_caps(rows, contract['charge_targets'])
                              if optional else None))
        validate_physical_buy_commitments(
            rows, result['flows'], contract['buy_due_indices'],
            result['effective_target_pcts'], 5.0, 15, 15.0, 0.9)
        return contract, result

    def test_cheaper_day_replaces_dearer_night_and_cost_falls(self):
        rows = self.rows()
        _, old = self.optimize(rows, optional=False)
        contract, new = self.optimize(rows)
        old_night = sum(f['grid_charge_kwh'] for f in old['flows'][10:])
        new_night = sum(f['grid_charge_kwh'] for f in new['flows'][10:])
        self.assertGreater(old_night, 0.1)
        self.assertLess(new_night, 1e-7)
        self.assertLess(new['objective_pln'], old['objective_pln'])
        self.assertGreater(new['flows'][3]['soc_end_pct'], old['flows'][3]['soc_end_pct'])
        targets = planned_buy_execution_targets(rows, new['flows'], new['effective_target_pcts'])
        buying = [i for i, f in enumerate(new['flows'][:4]) if f['grid_charge_kwh'] > 1e-7]
        self.assertTrue(buying)
        self.assertEqual(len({targets[i] for i in buying}), 1)
        self.assertLess(targets[buying[0]], 100.0)
        self.assertGreaterEqual(new['flows'][-1]['soc_end_pct'], 40.0)
        for i, f in enumerate(new['flows']):
            self.assertLessEqual(f['soc_end_pct'], 100.0)
            self.assertGreaterEqual(f['soc_end_pct'] + 1e-7, contract['required'][i])
            self.assertLessEqual(f['grid_charge_kwh'] + f['pv_to_bat_kwh'], 1.25 + 1e-7)
            if not rows[i]['buy_window']:
                self.assertLess(f['grid_charge_kwh'], 1e-7)

    def test_insufficient_cheaper_capacity_keeps_needed_night_purchase(self):
        rows = self.rows(initial_slots=1)
        _, result = self.optimize(rows, initial=45.0)
        self.assertGreater(sum(f['grid_charge_kwh'] for f in result['flows'][10:]), 0.1)
        self.assertAlmostEqual(result['flows'][0]['grid_charge_kwh'], 1.25, delta=0.02)

    def test_dearer_early_buy_is_not_extended(self):
        rows = self.rows(cheap=1.15, dear=1.05)
        targets = [20.0, 25.0, 30.0, 35.0] + [15.0] * 6 + [30.0, 40.0]
        caps = economic_buy_charge_caps(rows, targets)
        self.assertEqual(caps[:4], [35.0] * 4)
        self.assertEqual(caps[10:], [40.0] * 2)

    def test_sell_or_missing_price_does_not_offer_later_buy(self):
        rows = self.rows()
        for row in rows[10:]:
            row['sale_window'] = True
        self.assertEqual(economic_buy_charge_caps(rows, [30.0] * 12), [30.0] * 12)
        for row in rows[10:]:
            row['sale_window'] = False
            row['price_buy_pln_kwh'] = None
        self.assertEqual(economic_buy_charge_caps(rows, [30.0] * 12), [30.0] * 12)

    def test_optional_cap_does_not_become_required_deadline(self):
        rows = self.rows()
        contract, result = self.optimize(rows)
        self.assertEqual(contract['charge_targets'][-1], 40.0)
        self.assertLess(result['flows'][3]['soc_end_pct'], 100.0)
        self.assertAlmostEqual(result['flows'][-1]['soc_end_pct'], 40.0)

    def test_target_shared_only_across_actual_consecutive_buys(self):
        rows = self.rows()
        flows = [dict(grid_charge_kwh=q, soc_end_pct=s) for q, s in
                 [(1, 55), (1, 62), (0, 60), (1, 65)] + [(0, 40)] * 8]
        original = [20.0] * 12
        targets = planned_buy_execution_targets(rows, flows, original)
        self.assertEqual(targets[:4], [62.0, 62.0, 20.0, 65.0])
        self.assertEqual(original, [20.0] * 12)

    def test_pv_can_exceed_grid_cap(self):
        rows = [dict(price_buy_pln_kwh=1.0, price_sell_pln_kwh=1.0,
                     buy_window=True, sale_window=False,
                     forecast_load_kwh=0.0, forecast_pv_total_kwh=1.25)]
        result = optimize_energy_horizon(
            rows, 50.0, 15.0, 15.0, 0.9, 0.95, 0.0, 0.0, 5.0, 15,
            [100.0], 15.0, 0.1, 100.0, [20.0], set(), set(), [15.0],
            battery_sales_enabled=False, grid_charge_caps=[20.0])
        self.assertGreater(result['flows'][0]['soc_end_pct'], 50.0)
        self.assertEqual(result['flows'][0]['grid_charge_kwh'], 0.0)

    def test_negative_price_without_future_demand_does_not_create_stockpile(self):
        rows = self.rows(cheap=-0.1, dear=1.15)
        for row in rows:
            row['forecast_load_kwh'] = 0.0
        caps = economic_buy_charge_caps(rows, [40.0] * len(rows))
        self.assertEqual(caps, [40.0] * len(rows))
        _, result = self.optimize(rows)
        self.assertEqual(sum(f['grid_charge_kwh'] for f in result['flows']), 0.0)

    def test_capacity_limit_retains_residual_later_buy(self):
        rows = self.rows()
        for row in rows[4:10]:
            row['forecast_load_kwh'] = 1.25
        contract = deterministic_soc_target_contract(
            rows, [15.0] * 12, [15.0] * 11 + [50.0],
            15.0, 15.0, 0.9, 0.95, 5.0, 15, 0.0)
        caps = economic_buy_charge_caps(rows, contract['charge_targets'])
        result = optimize_energy_horizon(
            rows, 80.0, 15.0, 15.0, 0.9, 0.95, 0.0, 0.0, 5.0, 15,
            [100.0] * 12, 50.0, 0.1, 100.0,
            contract['charge_targets'], set(), contract['buy_due_indices'],
            contract['required'], battery_sales_enabled=False,
            grid_charge_caps=caps)
        self.assertLessEqual(max(f['soc_end_pct'] for f in result['flows']), 100.0)
        self.assertGreater(sum(f['grid_charge_kwh'] for f in result['flows'][10:]), 0.1)


if __name__ == '__main__':
    unittest.main()
