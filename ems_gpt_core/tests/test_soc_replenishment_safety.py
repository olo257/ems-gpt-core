import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_service import (
    _soc_reachability_profile, build_soc_contracts, daily_close_soc_requirements,
    end_of_day_soc_target, optimize_energy_horizon,
    relax_soc_requirement_to_reachable,
    deterministic_soc_target_contract, preserve_target_contract_after_reachability,
    replenishment_soc_requirements, recoverable_soc_requirements,
    target_commitment_required_fallback, effective_required_soc_for_dispatch,
    validate_recoverable_soc_requirements,
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

    def optimize(self, rows, required, initial=50.0, terminal=17.0,
                 battery_sales_enabled=True):
        return optimize_energy_horizon(
            rows, initial, 15.0, 15.0, 0.9, 0.95, 0.08, 0.05,
            5.0, 15, [15.0]*len(rows), terminal, 0.1, 100.0,
            required_soc_pcts=required,
            battery_sales_enabled=battery_sales_enabled)

    def test_unreachable_close_uses_best_reachable_soc_and_keeps_reserve(self):
        rows = self.rows(2)
        requested_close = 60.0
        requested_contract = {
            "required": [requested_close, requested_close],
            "charge_targets": [requested_close, requested_close],
        }
        reachability = validate_recoverable_soc_requirements(
            rows, requested_contract["required"], 20.0, 15.0, 100.0,
            capacity_kwh=15.0, eta_c=0.9, eta_d=0.95,
            max_power_kw=5.0, slot_minutes=15, soc_step_pct=0.1)
        contract = preserve_target_contract_after_reachability(
            requested_contract, reachability, {0})
        best_effort = relax_soc_requirement_to_reachable(
            contract["required"][0], contract["reachable_required"][0], 15.0)

        self.assertLess(contract["reachable_required"][0], requested_close)
        self.assertEqual(contract["required"][0], requested_close)
        self.assertEqual(contract["charge_targets"][0], requested_close)
        self.assertLess(best_effort, requested_close)
        self.assertGreaterEqual(best_effort, 15.0)
        plan = self.optimize(
            rows, [best_effort, best_effort], initial=20.0, terminal=17.0,
            battery_sales_enabled=False)
        self.assertGreaterEqual(plan["flows"][0]["soc_end_pct"] + 1e-9, best_effort)

    def test_reachability_does_not_lower_daily_close_or_buy_target(self):
        rows = self.rows(2)
        requested = deterministic_soc_target_contract(
            rows, [17.0, 17.0], [17.0, 60.0], 15.0, 15.0,
            0.9, 0.95, 5.0, 15, 0.0)
        recovery = validate_recoverable_soc_requirements(
            rows, requested["required"], 20.0, 15.0, 100.0,
            capacity_kwh=15.0, eta_c=0.9, eta_d=0.95,
            max_power_kw=5.0, slot_minutes=15, soc_step_pct=0.1)
        reconciled = preserve_target_contract_after_reachability(
            requested, recovery, {1})
        self.assertLess(reconciled["reachable_required"][1], 60.0)
        self.assertEqual(reconciled["required"][1], 60.0)
        self.assertEqual(reconciled["charge_targets"], requested["charge_targets"])

    def test_historical_terminal_soc_is_not_discounted(self):
        self.assertAlmostEqual(end_of_day_soc_target(47.7, 15.0, 100.0), 47.7)
        self.assertEqual(end_of_day_soc_target(17.0, 15.0, 100.0), 17.0)
        self.assertEqual(end_of_day_soc_target(105.0, 15.0, 100.0), 100.0)

    def test_contract_is_not_clamped_to_economic_path(self):
        rows = self.rows(1)
        contract = build_soc_contracts(
            rows,
            [{"soc_end_pct": 20.0, "battery_sell_kwh": 0.0,
              "battery_to_load_kwh": 0.0, "pv_to_bat_kwh": 0.0,
              "grid_charge_kwh": 0.0}],
            15.0, 15.0, 0.9, 0.95, 0.0, 60.0, 100.0, 0.1)
        self.assertEqual(contract["required"], [60.0])

    def test_history_applies_at_midnight_and_never_at_morning_sell(self):
        rows = self.rows(28)
        rows[25]['sale_window'] = True
        safety = self.safety(rows)
        required, closes = daily_close_soc_requirements(
            rows, safety, {rows[0]['local_day']: 47.7, rows[-1]['local_day']: 47.7})
        self.assertEqual(closes, {0, 27})
        self.assertEqual(required[0], 47.7)
        self.assertEqual(required[24], 17.0)
        self.assertEqual(required[25], 17.0)

    def test_overnight_load_can_raise_daily_close_above_history(self):
        rows = self.rows(6)
        for row in rows[1:]:
            row['forecast_load_kwh'] = 1.0
        safety = self.safety(rows)
        required, _ = daily_close_soc_requirements(
            rows, safety, {rows[0]['local_day']: 47.7, rows[-1]['local_day']: 47.7})
        self.assertGreater(required[0], 47.7)
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

    def test_recovery_skips_overlapping_buy_and_sell_slot(self):
        rows = self.rows(3)
        rows[0].update(buy_window=True, sale_window=True)
        rows[1].update(buy_window=True, sale_window=False)
        result = recoverable_soc_requirements(
            rows, [17.0, 17.0, 17.0], 13.0, 15.0)
        self.assertEqual(result["recovery_buy_index"], 1)
        self.assertEqual(result["relaxed_indices"], [0])

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
        recovery = validate_recoverable_soc_requirements(
            rows, recovery['required_soc_pcts'], 13.0, 15.0, 100.0,
            capacity_kwh=15.0, eta_c=0.9, eta_d=0.95,
            max_power_kw=5.0, slot_minutes=15, soc_step_pct=0.1)
        plan = self.optimize(rows, recovery['required_soc_pcts'],
                             initial=13.0, terminal=17.0)
        self.assertGreater(plan['flows'][1]['grid_charge_kwh'], 0.0)
        self.assertAlmostEqual(plan['flows'][0]['soc_end_pct'], 13.0)
        self.assertGreaterEqual(plan['flows'][1]['soc_end_pct'], 17.0)

    def test_final_soc_check_uses_effective_fallback_and_keeps_safety_floor(self):
        rows = self.rows(1)
        rows[0]["forecast_load_kwh"] = 0.25
        initial_soc = 26.0
        reachability = validate_recoverable_soc_requirements(
            rows, [26.75], initial_soc, 15.0, 100.0, capacity_kwh=15.0,
            eta_c=0.9, eta_d=0.95, max_power_kw=5.0,
            slot_minutes=15, soc_step_pct=0.1)
        safety_floor = reachability["required_soc_pcts"]
        dispatch_floor = target_commitment_required_fallback(safety_floor)
        accepted_floor = effective_required_soc_for_dispatch(
            safety_floor, dispatch_floor)
        plan = self.optimize(
            rows, dispatch_floor, initial=initial_soc, terminal=15.0,
            battery_sales_enabled=False)
        flow_end = plan["flows"][0]["soc_end_pct"]
        self.assertLess(flow_end, 26.75)
        self.assertGreaterEqual(flow_end + 0.01, accepted_floor[0])
        self.assertGreaterEqual(accepted_floor[0], safety_floor[0])

    def test_dispatch_required_floor_length_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "DISPATCH_SOC_REQUIREMENT_LENGTH_MISMATCH"):
            effective_required_soc_for_dispatch([15.0, 20.0], [15.0])

    def test_insufficient_initial_soc_is_not_silently_waived(self):
        rows = self.rows(4)
        for row in rows[1:]:
            row['forecast_load_kwh'] = 1.0
        with self.assertRaisesRegex(RuntimeError, 'No feasible SOC state'):
            self.optimize(rows, self.safety(rows), initial=20.0)

    def test_reachability_does_not_hide_house_import_inside_buy_charge(self):
        rows = self.rows(1)
        rows[0].update(forecast_load_kwh=0.5, buy_window=True)
        profile = _soc_reachability_profile(
            rows, 20.0, 15.0, 15.0, 0.98, 0.98, 5.0, 15, 0.1,
            [28.0])
        self.assertLess(profile['feasible_soc_path_pcts'][0], 20.0)
        validated = validate_recoverable_soc_requirements(
            rows, [28.0], 20.0, 15.0, 100.0, capacity_kwh=15.0,
            eta_c=0.98, eta_d=0.98, max_power_kw=5.0,
            slot_minutes=15, soc_step_pct=0.1)
        plan = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.98, 0.98, 0.08, 0.05,
            5.0, 15, [15.0], 15.0, 0.1, 100.0,
            required_soc_pcts=validated['required_soc_pcts'],
            battery_sales_enabled=False)
        self.assertAlmostEqual(plan['flows'][0]['grid_charge_kwh'], 0.0)
        self.assertGreater(plan['flows'][0]['battery_discharge_internal_kwh'], 0.0)

    def test_invalid_measured_soc_above_100_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'INVALID_INITIAL_SOC'):
            self.optimize(self.rows(1), [15.0], initial=100.1)

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

    def test_live_reachability_does_not_turn_over_100_bridge_into_buy_to_full(self):
        rows = self.rows(3)
        rows[0]['buy_window'] = True
        result = validate_recoverable_soc_requirements(
            rows, [105.0, 105.0, 17.0], 50.0, 15.0, 100.0,
            capacity_kwh=15.0, eta_c=0.9, eta_d=0.95,
            max_power_kw=5.0, slot_minutes=15, soc_step_pct=0.1)
        self.assertEqual(result['required_soc_pcts'], [15.0, 15.0, 17.0])
        self.assertEqual(result['unreachable_indices'], [0, 1])
        self.assertTrue(result['disable_battery_sales'])
        plan = optimize_energy_horizon(
            rows, 50.0, 15.0, 15.0, 0.9, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 15.0, 0.1, 100.0,
            [15.0] * len(rows), set(), {0},
            result['required_soc_pcts'], battery_sales_enabled=False)
        self.assertAlmostEqual(plan['flows'][0]['grid_charge_kwh'], 0.0)
        self.assertLess(max(flow['soc_end_pct'] for flow in plan['flows']), 100.0)

    def test_unreachable_floor_is_bounded_to_physical_reachability(self):
        rows = self.rows(4)
        rows[1].update(forecast_load_kwh=0.5, buy_window=False)
        rows[2].update(forecast_load_kwh=0.0, buy_window=True)
        result = validate_recoverable_soc_requirements(
            rows, [50.0, 80.0, 80.0, 50.0], 50.0, 15.0, 100.0,
            capacity_kwh=15.0, eta_c=0.9, eta_d=0.95,
            max_power_kw=5.0, slot_minutes=15, soc_step_pct=0.1)
        self.assertEqual(result['unreachable_indices'], [1, 2])
        self.assertLessEqual(result['required_soc_pcts'][1],
                             result['reachable_soc_ceiling_pcts'][1])
        self.assertTrue(result['disable_battery_sales'])
        plan = self.optimize(rows, result['required_soc_pcts'], initial=50.0)
        self.assertLessEqual(max(flow['soc_end_pct'] for flow in plan['flows']), 100.0)
        for flow, floor in zip(plan['flows'], result['required_soc_pcts']):
            self.assertGreaterEqual(flow['soc_end_pct'] + 1e-9, floor)

    def test_joint_reachability_relaxes_earlier_floor_for_later_buy(self):
        rows = self.rows(6)
        rows[0]['buy_window'] = True
        rows[1]['forecast_load_kwh'] = 1.4523
        rows[2]['forecast_pv_total_kwh'] = 4.6066
        rows[3]['buy_window'] = True
        rows[4].update(buy_window=True, forecast_load_kwh=2.5365)
        rows[5]['forecast_load_kwh'] = 3.4881
        requested = [92.9, 84.2, 91.7, 99.2, 100.0, 91.3]
        result = validate_recoverable_soc_requirements(
            rows, requested, 85.4201, 15.0, 100.0,
            capacity_kwh=15.0, eta_c=0.9, eta_d=0.95,
            max_power_kw=5.0, slot_minutes=15, soc_step_pct=0.1)
        self.assertEqual(result['unreachable_indices'], [4, 5])
        self.assertLess(result['required_soc_pcts'][4], requested[4])
        self.assertLess(result['required_soc_pcts'][4], 100.0)
        self.assertLessEqual(result['required_soc_pcts'][4],
                             result['reachable_soc_ceiling_pcts'][4])
        plan = self.optimize(rows, result['required_soc_pcts'],
                             initial=85.4201, terminal=15.0,
                             battery_sales_enabled=False)
        for flow, floor in zip(plan['flows'], result['required_soc_pcts']):
            self.assertGreaterEqual(flow['soc_end_pct'] + 1e-9, floor)

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

    def test_multislot_buy_target_propagates_back_to_whole_bridge(self):
        rows = self.rows(8)
        for index in range(4):
            rows[index].update(buy_window=True, price_buy_pln_kwh=0.1)
        rows[4]['buy_window'] = True
        rows[5]['buy_window'] = True
        rows[4]['price_buy_pln_kwh'] = 1.0
        rows[5]['price_buy_pln_kwh'] = 1.0
        flows = [{
            'battery_to_load_kwh': 0.0,
            'pv_to_bat_kwh': 0.0,
            'grid_charge_kwh': 0.5 if index in {4, 5} else 0.0,
            'battery_sell_kwh': 0.0,
        } for index in range(len(rows))]
        daily_target = [15.0] * len(rows)
        daily_target[-1] = 35.0

        contracts = build_soc_contracts(
            rows, flows, 15.0, 15.0, 0.9, 0.95, 0.0,
            35.0, 100.0, 0.1, daily_target)

        self.assertEqual(contracts['buy_due_indices'], {5})
        self.assertLess(contracts['required'][0], 35.0)
        self.assertEqual(contracts['charge_targets'][:4], [35.0] * 4)

        # The second pass can now use the earlier, cheaper BUY slots to meet
        # the exact daily amount instead of waiting for the late selected run.
        daily_floor = [15.0] * len(rows)
        daily_floor[-1] = 35.0
        final = optimize_energy_horizon(
            rows, 15.0, 15.0, 15.0, 0.9, 0.95, 0.08, 0.05,
            5.0, 15, [15.0] * len(rows), 35.0, 0.1, 100.0,
            contracts['charge_targets'], set(),
            contracts['buy_due_indices'], daily_floor,
            battery_sales_enabled=False)
        charged_early = sum(
            flow['grid_charge_kwh'] for flow in final['flows'][:4])
        charged_late = sum(
            flow['grid_charge_kwh'] for flow in final['flows'][4:6])
        self.assertGreater(charged_early, 0.0)
        self.assertAlmostEqual(charged_late, 0.0, places=6)
        self.assertAlmostEqual(charged_early, 15.0 * 0.20 / 0.9, places=5)
        self.assertAlmostEqual(final['flows'][-1]['soc_end_pct'], 35.0)


if __name__ == '__main__':
    unittest.main()
