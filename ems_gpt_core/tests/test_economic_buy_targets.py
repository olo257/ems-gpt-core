import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_service import (
    deterministic_soc_target_contract, economic_buy_charge_caps,
    optimize_energy_horizon, planned_buy_execution_targets,
    validate_physical_buy_commitments, voluntary_load_without_charging_indices,
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
                              if optional else None),
            minimum_grid_charge_kwh=0.02)
        validate_physical_buy_commitments(
            rows, result['flows'], contract['buy_due_indices'],
            result['effective_target_pcts'], 5.0, 15, 15.0, 0.9)
        return contract, result

    def test_every_buy_window_uses_available_charge_volume(self):
        rows = self.rows()
        contract, result = self.optimize(rows)
        targets = planned_buy_execution_targets(
            rows, result['flows'], result['effective_target_pcts'])
        buying = [i for i, f in enumerate(result['flows'])
                  if rows[i]['buy_window'] and f['grid_charge_kwh'] > 1e-7]
        self.assertEqual(buying, [0, 1, 2, 3, 10, 11])
        self.assertTrue(all(abs(result['flows'][i]['grid_charge_kwh'] - 1.25) < 1e-7
                            for i in buying))
        self.assertEqual(len({targets[i] for i in buying[:4]}), 1)
        self.assertGreaterEqual(result['flows'][-1]['soc_end_pct'], 40.0)
        for i, f in enumerate(result['flows']):
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

    def test_all_buy_slots_allow_full_soc_ceiling(self):
        rows = self.rows(cheap=1.15, dear=1.05)
        targets = [20.0, 25.0, 30.0, 35.0] + [15.0] * 6 + [30.0, 40.0]
        caps = economic_buy_charge_caps(rows, targets)
        self.assertEqual(caps[:4], [100.0] * 4)
        self.assertEqual(caps[4:10], targets[4:10])
        self.assertEqual(caps[10:], [100.0] * 2)

    def test_missing_price_does_not_block_full_volume_in_buy_window(self):
        rows = self.rows(cheap=1.15, dear=1.05)
        rows[0]['price_buy_pln_kwh'] = None
        caps = economic_buy_charge_caps(rows, [15.0] * len(rows))
        self.assertEqual(caps[0], 100.0)

    def test_sell_blocks_buy_but_missing_price_does_not_reduce_buy_volume(self):
        rows = self.rows()
        for row in rows[10:]:
            row['sale_window'] = True
        caps = economic_buy_charge_caps(rows, [30.0] * 12)
        self.assertEqual(caps[:4], [100.0] * 4)
        self.assertEqual(caps[4:], [30.0] * 8)
        for row in rows[10:]:
            row['sale_window'] = False
            row['price_buy_pln_kwh'] = None
        caps = economic_buy_charge_caps(rows, [30.0] * 12)
        self.assertEqual(caps[:4], [100.0] * 4)
        self.assertEqual(caps[4:10], [30.0] * 6)
        self.assertEqual(caps[10:], [100.0] * 2)

    def test_optional_cap_does_not_become_required_deadline(self):
        rows = self.rows()
        contract, result = self.optimize(rows)
        self.assertEqual(contract['charge_targets'][-1], 40.0)
        self.assertLessEqual(result['flows'][3]['soc_end_pct'], 100.0)
        self.assertGreaterEqual(result['flows'][-1]['soc_end_pct'], 40.0)

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

    def test_negative_price_buy_window_still_uses_available_volume(self):
        rows = self.rows(cheap=-0.1, dear=1.15)
        for row in rows:
            row['forecast_load_kwh'] = 0.0
        caps = economic_buy_charge_caps(rows, [40.0] * len(rows))
        self.assertEqual(caps[:4], [100.0] * 4)
        self.assertEqual(caps[4:10], [40.0] * 6)
        self.assertEqual(caps[10:], [100.0] * 2)
        _, result = self.optimize(rows)
        self.assertGreater(sum(f['grid_charge_kwh'] for f in result['flows']), 0.0)

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

    def test_battery_buy_does_not_authorize_home_import(self):
        rows = self.rows()
        for row in rows:
            row['forecast_load_kwh'] = 0.0
        rows[0]['forecast_load_kwh'] = 0.3
        _, result = self.optimize(rows)
        self.assertTrue(any(f['grid_charge_kwh'] > 0.02
                            for f in result['flows']))
        self.assertTrue(all(f['grid_load_kwh'] <= 0.05
                            for f in result['flows']))
        self.assertEqual(voluntary_load_without_charging_indices(
            rows, result['flows'], 0.05, 0.02), set())

    def test_no_plan_imports_house_load_at_minimum_soc(self):
        rows = self.rows()
        rows[0]['forecast_load_kwh'] = 0.3
        with self.assertRaisesRegex(
                RuntimeError, 'No feasible SOC state at horizon slot 0'):
            self.optimize(rows, initial=15.0)

    def test_unbacked_native_import_stays_forbidden(self):
        rows = [dict(buy_window=b, sale_window=s) for b, s in
                [(True, False), (False, False), (True, True), (True, False)]]
        flows = [dict(voluntary_grid_load_kwh=0.3, grid_charge_kwh=q)
                 for q in [0.0, 1.0, 1.0, 0.01]]
        self.assertEqual(voluntary_load_without_charging_indices(
            rows, flows, 0.05, 0.02), {0, 1, 2, 3})

    def test_technical_native_residual_is_not_deliberate_import(self):
        self.assertEqual(voluntary_load_without_charging_indices(
            [dict(buy_window=False, sale_window=False)],
            [dict(voluntary_grid_load_kwh=0.01, grid_charge_kwh=0)],
            0.05, 0.02), set())


if __name__ == '__main__':
    unittest.main()
