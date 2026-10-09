import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_service import (
    build_soc_contracts, daily_close_soc_requirements,
    end_of_day_soc_target, optimize_energy_horizon,
    replenishment_soc_requirements, validate_recoverable_soc_requirements,
)


class SocReplenishmentSafetyTests(unittest.TestCase):
    def rows(self, count, start=datetime(2026, 10, 2, 23, 45)):
        return [dict(slot_start=start + timedelta(minutes=15*i),
                     local_day=(start + timedelta(minutes=15*i)).date(),
                     forecast_load_kwh=0.0, forecast_heat_pump_load_kwh=0.0,
                     forecast_pv_total_kwh=0.0, buy_window=False,
                     sale_window=False, price_buy_pln_kwh=1.0,
                     price_sell_pln_kwh=0.0) for i in range(count)]

    def safety(self, rows, uncertainty=0.0):
        return replenishment_soc_requirements(
            rows, 15.0, 15.0, 2.0, 0.9, 0.95, 5.0, 15, uncertainty)

    def optimize(self, rows, required, initial=50.0, terminal=17.0):
        return optimize_energy_horizon(
            rows, initial, 15.0, 15.0, 0.9, 0.95, 0.08, 0.05,
            5.0, 15, [15.0]*len(rows), terminal, 0.1, 100.0,
            required_soc_pcts=required)

    def test_daily_tolerance_is_percentage_points(self):
        self.assertAlmostEqual(end_of_day_soc_target(47.7, 15.0, 100.0), 42.7)
        self.assertEqual(end_of_day_soc_target(17.0, 15.0, 100.0), 15.0)

    def test_history_applies_at_midnight_and_never_at_morning_sell(self):
        rows = self.rows(28)
        rows[25]['sale_window'] = True
        safety = self.safety(rows)
        required, closes = daily_close_soc_requirements(
            rows, safety, {rows[0]['local_day']: 42.7, rows[-1]['local_day']: 42.7})
        self.assertEqual(closes, {0, 27})
        self.assertEqual(required[0], 42.7)
        self.assertEqual(required[24], 17.0)
        self.assertEqual(required[25], 17.0)

    def test_overnight_load_can_raise_daily_close_above_history(self):
        rows = self.rows(6)
        for row in rows[1:]:
            row['forecast_load_kwh'] = 1.0
        safety = self.safety(rows)
        required, _ = daily_close_soc_requirements(
            rows, safety, {rows[0]['local_day']: 42.7, rows[-1]['local_day']: 42.7})
        self.assertGreater(required[0], 42.7)
        self.assertAlmostEqual(required[0], 17.0 + 5.0/0.95/15.0*100.0)

    def test_short_buy_only_offsets_finite_capacity(self):
        rows = self.rows(6)
        rows[1]['buy_window'] = True
        for row in rows[2:]:
            row['forecast_load_kwh'] = 0.8
        with_buy = self.safety(rows)
        rows[1]['buy_window'] = False
        without_buy = self.safety(rows)
        self.assertAlmostEqual(without_buy[0] - with_buy[0], 7.5)
        self.assertGreater(with_buy[0], 17.0)

    def test_buy_in_sell_does_not_count_as_supply(self):
        rows = self.rows(3)
        rows[1].update(buy_window=True, sale_window=True)
        rows[2]['forecast_load_kwh'] = 0.8
        overlap = self.safety(rows)
        rows[1]['buy_window'] = False
        self.assertEqual(overlap, self.safety(rows))

    def test_pv_start_with_no_surplus_cannot_reset_reserve(self):
        rows = self.rows(4)
        rows[1].update(forecast_load_kwh=0.5, forecast_pv_total_kwh=0.2)
        rows[2]['forecast_load_kwh'] = 0.8
        self.assertGreater(self.safety(rows)[0], 17.0 + 0.8/0.95/15*100)

    def test_large_pv_is_limited_by_charge_power(self):
        rows = self.rows(4)
        rows[1]['forecast_pv_total_kwh'] = 10.0
        rows[2]['forecast_load_kwh'] = 2.0
        self.assertAlmostEqual(self.safety(rows)[0], 17 + 2/0.95/15*100 - 7.5)

    def test_hp_and_uncertainty_raise_bridge(self):
        rows = self.rows(4)
        rows[2]['forecast_load_kwh'] = 0.5
        baseline = self.safety(rows)[0]
        rows[2]['forecast_heat_pump_load_kwh'] = 0.5
        self.assertGreater(self.safety(rows)[0], baseline)
        self.assertGreater(self.safety(rows, 1.0)[0], self.safety(rows)[0])

    def test_optional_sale_cannot_use_morning_reserve(self):
        rows = self.rows(6)
        rows[0].update(sale_window=True, price_sell_pln_kwh=3.0)
        for row in rows[1:]:
            row['forecast_load_kwh'] = 0.5
        safety = self.safety(rows, 1.0)
        plan = self.optimize(rows, safety)
        self.assertGreater(plan['flows'][0]['battery_sell_kwh'], 0)
        for flow, required in zip(plan['flows'], safety):
            self.assertGreaterEqual(flow['soc_end_pct'] + 1e-9, required)
        self.assertGreaterEqual(plan['flows'][-1]['soc_end_pct'], 17.0)
        self.assertLess(sum(f['grid_load_kwh'] for f in plan['flows']), 0.05)

    def test_omitted_buy_and_delayed_pv_raise_earlier_requirement(self):
        rows = self.rows(8)
        rows[2]['buy_window'] = True
        rows[4]['forecast_pv_total_kwh'] = 1.2
        for row in rows[5:]:
            row['forecast_load_kwh'] = 0.6
        original = self.safety(rows)[0]
        rows[2]['buy_window'] = False
        rows[4]['forecast_pv_total_kwh'] = 0.0
        self.assertGreater(self.safety(rows)[0], original)

    def test_breached_safety_bridge_recovers_at_next_buy_window(self):
        rows = self.rows(4)
        rows[1]['buy_window'] = True
        from planner_service import recoverable_soc_requirements
        recovery = recoverable_soc_requirements(
            rows, [17.0, 17.0, 17.0, 17.0], 13.0, 15.0)
        self.assertEqual(recovery['recovery_buy_index'], 1)
        self.assertEqual(recovery['relaxed_indices'], [0])
        self.assertEqual(recovery['required_soc_pcts'], [15.0, 17.0, 17.0, 17.0])
        plan = self.optimize(rows, recovery['required_soc_pcts'],
                             initial=13.0, terminal=17.0)
        self.assertGreater(plan['flows'][1]['grid_charge_kwh'], 0.0)
        self.assertGreaterEqual(plan['flows'][0]['soc_end_pct'], 15.0)

    def test_insufficient_initial_soc_is_not_silently_waived(self):
        rows = self.rows(4)
        for row in rows[1:]:
            row['forecast_load_kwh'] = 1.0
        with self.assertRaisesRegex(RuntimeError, 'No feasible SOC state'):
            self.optimize(rows, self.safety(rows), initial=20.0)

    def test_over_capacity_bridge_is_bounded_and_disables_sales(self):
        rows = self.rows(4)
        rows[2]['buy_window'] = True
        # Requirements above physical capacity never enter the optimizer as SOC.
        result = validate_recoverable_soc_requirements(
            rows, [105.0, 105.0, 17.0, 17.0], 13.0, 15.0, 100.0)
        self.assertEqual(result['required_soc_pcts'], [15.0, 15.0, 17.0, 17.0])
        self.assertEqual(result['capacity_limited_indices'], [])
        # An over-cap bridge after BUY is bounded and disables battery export.
        post_buy = validate_recoverable_soc_requirements(
            rows, [17.0, 17.0, 105.0, 105.0], 50.0, 15.0, 100.0)
        self.assertEqual(post_buy['required_soc_pcts'], [17.0, 17.0, 15.0, 15.0])
        self.assertEqual(post_buy['capacity_limited_indices'], [2, 3])
        self.assertTrue(post_buy['disable_battery_sales'])

    def test_no_buy_over_capacity_bridge_uses_reserve_fallback(self):
        rows = self.rows(3)
        result = validate_recoverable_soc_requirements(
            rows, [105.0, 17.0, 17.0], 13.0, 15.0, 100.0)
        self.assertLessEqual(max(result['required_soc_pcts']), 100.0)
        self.assertEqual(result['required_soc_pcts'][0], 15.0)
        self.assertTrue(result['disable_battery_sales'])

    def test_requirements_do_not_clip_an_impossible_bridge(self):
        rows = self.rows(20)
        for row in rows:
            row['forecast_load_kwh'] = 1.0
        self.assertGreater(self.safety(rows)[0], 100.0)

    def test_repeated_calculation_does_not_ratchet_target(self):
        rows = self.rows(5)
        rows[1]['buy_window'] = True
        rows[3]['forecast_load_kwh'] = 0.5
        original = self.safety(rows, 1.0)
        for _ in range(5):
            self.assertEqual(original, self.safety(rows, 1.0))

    def test_final_contract_preserves_safety_with_actual_buy(self):
        rows = self.rows(8)
        rows[1]['buy_window'] = True
        for row in rows[3:]:
            row['forecast_load_kwh'] = 0.5
        safety = self.safety(rows)
        economic = self.optimize(rows, safety, initial=30.0)
        contracts = build_soc_contracts(
            rows, economic['flows'], 15, 15, 0.9, 0.95, 0,
            17, 100, 0.1, safety)
        required = [max(a, b) for a, b in zip(contracts['required'], safety)]
        targets = [max(a, b) for a, b in zip(contracts['charge_targets'], required)]
        final = optimize_energy_horizon(
            rows, 30, 15, 15, 0.9, 0.95, 0.08, 0.05, 5, 15,
            [15]*len(rows), 17, 0.1, 100, targets, set(),
            contracts['buy_due_indices'], required)
        self.assertGreater(final['flows'][1]['grid_charge_kwh'], 0)
        for flow, bound in zip(final['flows'], safety):
            self.assertGreaterEqual(flow['soc_end_pct'] + 1e-9, bound)


if __name__ == '__main__':
    unittest.main()
