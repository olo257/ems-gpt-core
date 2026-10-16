import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_service import (
    deterministic_soc_target_contract, economic_buy_charge_caps,
    optimize_energy_horizon, planned_buy_execution_targets,
    validate_physical_buy_commitments, voluntary_load_without_charging_indices,
    validated_buy_deadline_targets, validate_active_soc_target_deadlines,
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
        contract, result = self.optimize(rows, initial=20.0)
        targets = planned_buy_execution_targets(
            rows, result['flows'], result['effective_target_pcts'])
        buying = [i for i, f in enumerate(result['flows'])
                  if rows[i]['buy_window'] and f['grid_charge_kwh'] > 1e-7]
        self.assertEqual(buying, [0, 1, 2, 3, 10, 11])
        for i in buying:
            self.assertLessEqual(result['flows'][i]['soc_end_pct'],
                                 contract['charge_targets'][i] + 1e-7)
            self.assertLessEqual(result['flows'][i]['grid_charge_kwh'], 1.25 + 1e-7)
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
        self.assertGreater(result['flows'][0]['grid_charge_kwh'], 0.0)
        self.assertLess(result['flows'][0]['grid_charge_kwh'], 1.25)

    def test_buy_window_end_target_is_available_from_its_first_slot(self):
        rows = [dict(slot_start=i, slot_end=i + 1,
                     price_buy_pln_kwh=1.0 + i * 0.01,
                     price_sell_pln_kwh=0.0, buy_window=True,
                     sale_window=False, forecast_load_kwh=0.0,
                     forecast_pv_total_kwh=0.0) for i in range(4)]
        contract = deterministic_soc_target_contract(
            rows, [15.0] * 4, [15.0, 15.0, 15.0, 40.0],
            15.0, 15.0, 0.9, 0.95, 5.0, 15, 0.0)

        self.assertLess(contract['required'][0], 40.0)
        self.assertEqual(contract['required'][-1], 40.0)
        self.assertEqual(contract['charge_targets'], [40.0] * 4)
        caps = economic_buy_charge_caps(rows, contract['charge_targets'])
        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.9, 0.95, 0.0, 0.0, 5.0, 15,
            [100.0] * 4, 40.0, 0.1, 100.0,
            contract['charge_targets'], set(), contract['buy_due_indices'],
            contract['required'], battery_sales_enabled=False,
            grid_charge_caps=caps)

        self.assertGreater(result['flows'][0]['grid_charge_kwh'], 0.0)
        self.assertEqual(result['flows'][-1]['soc_end_pct'], 40.0)
        self.assertTrue(all(flow['soc_end_pct'] <= 40.0 + 1e-9
                            for flow in result['flows']))

    def test_future_house_load_preserves_headroom_for_window_end_target(self):
        rows = [dict(slot_start=i, slot_end=i + 1,
                     price_buy_pln_kwh=1.0,
                     price_sell_pln_kwh=0.0, buy_window=True,
                     sale_window=False,
                     forecast_load_kwh=0.5 if i == 1 else 0.0,
                     forecast_pv_total_kwh=0.0) for i in range(2)]
        targets = [40.0, 40.0]
        result = optimize_energy_horizon(
            rows, 32.5, 15.0, 15.0, 0.9, 0.95, 0.0, 0.0, 5.0, 15,
            [15.0] * 2, 40.0, 0.1, 100.0,
            targets, set(), {1}, [15.0] * 2,
            battery_sales_enabled=False, grid_charge_caps=targets,
            minimum_grid_charge_kwh=0.02)

        self.assertLess(result['flows'][0]['soc_end_pct'], 40.0)
        self.assertEqual(result['flows'][1]['soc_end_pct'], 40.0)
        self.assertGreater(result['flows'][1]['grid_charge_kwh'], 0.02)
        self.assertGreater(result['flows'][1]['grid_load_kwh'], 0.0)

    def shortage_replay(self, *, allowed=True, sales=False, required_end=15):
        # 16.10.2026: measured start 67%, evening BUY ceiling 33.3%.
        deficits=[0.337163, 0.293873, 0.236572, 0.448578, 0.63833, 0.484972, 0.320481, 0.276097, 0.263886, 0.302336, 0.272137, 0.256359, 0.538451, 0.594371, 0.386951, 0.292991, 0.230025, 0.190658, 0.177747]
        rows=[dict(slot_start=i,slot_end=i+1,price_buy_pln_kwh=1.5,
                   price_sell_pln_kwh=0,buy_window=i>=17,sale_window=False,
                   forecast_load_kwh=load,forecast_pv_total_kwh=0)
              for i,load in enumerate(deficits)]
        required=[15]*18+[required_end]
        result=optimize_energy_horizon(
            rows,67,15,15,.98,.98,.08,.05,5,15,[100]*19,15,.1,100,
            minimum_soc_targets=[33.3]*19,required_soc_pcts=required,
            battery_sales_enabled=sales,grid_charge_caps=[33.3]*19,
            minimum_grid_charge_kwh=.02,allow_buy_target_shortfall=allowed)
        return rows,required,result

    def test_live_047_failure_reports_shortfall_without_lowering_target(self):
        with self.assertRaisesRegex(RuntimeError,'requested=33.300:reachable=33.000'):
            self.shortage_replay(allowed=False)
        rows,required,result=self.shortage_replay()
        self.assertAlmostEqual(result['flows'][-1]['soc_end_pct'],33.0)
        self.assertAlmostEqual(result['effective_target_pcts'][-1],33.3)
        self.assertAlmostEqual(result['buy_target_shortfalls'][0]['shortfall_pct'],.3)
        deadlines=validated_buy_deadline_targets(result,result['effective_target_pcts'],required,False)
        validate_active_soc_target_deadlines(result,{18},deadlines)
        validate_physical_buy_commitments(rows,result['flows'],{18},deadlines,5,15,15,.98)
        self.assertTrue(all(flow['soc_end_pct']>=floor for flow,floor in zip(result['flows'],required)))
        self.assertTrue(all(flow['battery_sell_kwh']==0 for flow in result['flows']))
        # The selected purchase still uses the full physically available volume.
        self.assertAlmostEqual(result['flows'][-1]['grid_charge_kwh'],1.215/.98)

    def test_shortage_cannot_bypass_mandatory_bridge(self):
        with self.assertRaises(RuntimeError):
            self.shortage_replay(required_end=33.3)

    def test_shortage_is_forbidden_when_battery_sales_enabled(self):
        with self.assertRaisesRegex(RuntimeError,'BUY_TARGET_UNREACHABLE'):
            self.shortage_replay(sales=True)
        rows,required,result=self.shortage_replay()
        with self.assertRaisesRegex(RuntimeError,'UNSAFE_BUY_TARGET_SHORTFALL'):
            validated_buy_deadline_targets(result,result['effective_target_pcts'],required,True)
        with self.assertRaisesRegex(RuntimeError,'UNSAFE_BUY_TARGET_SHORTFALL'):
            validated_buy_deadline_targets(result,result['effective_target_pcts'],[34]*19,False)

    def test_selected_buy_is_full_even_with_future_house_load(self):
        rows = [dict(slot_start=i, slot_end=i+1,price_buy_pln_kwh=1.2,
                     price_sell_pln_kwh=0.0,buy_window=True,sale_window=False,
                     forecast_load_kwh=.1,forecast_pv_total_kwh=0.0)
                for i in range(13)]
        targets=[40.0]*len(rows)
        result=optimize_energy_horizon(
            rows,18.3,15,15,.98,.98,.08,.05,5,15,[100]*len(rows),40,.1,100,
            minimum_soc_targets=targets,required_soc_pcts=[15]*len(rows),
            battery_sales_enabled=False,grid_charge_caps=targets,
            minimum_grid_charge_kwh=.02)
        selected=0
        for flow in result['flows']:
            if flow['grid_charge_kwh'] > 1e-9:
                selected+=1
                full_internal=min(1.215,(40-flow['soc_start_pct'])*15/100)
                self.assertAlmostEqual(flow['battery_charge_internal_kwh'],full_internal)
                self.assertLessEqual(flow['soc_end_pct'],40+1e-9)
        self.assertGreater(selected,0)
        self.assertAlmostEqual(result['flows'][-1]['soc_end_pct'],40)

    def test_full_buy_respects_site_import_limit_after_house_load(self):
        rows=[dict(slot_start=0,slot_end=1,price_buy_pln_kwh=1.2,
                   price_sell_pln_kwh=0,buy_window=True,sale_window=False,
                   forecast_load_kwh=.2,forecast_pv_total_kwh=0)]
        result=optimize_energy_horizon(
            rows,20,15,15,.98,.98,0,0,5,15,[100],15,.1,100,
            minimum_soc_targets=[40],required_soc_pcts=[15],target_due_indices=set(),
            battery_sales_enabled=False,grid_charge_caps=[40],max_grid_import_kw=2,
            minimum_grid_charge_kwh=.02)
        flow=result['flows'][0]
        self.assertAlmostEqual(flow['battery_charge_internal_kwh'],.285)
        self.assertLessEqual(flow['grid_charge_kwh']+flow['grid_load_kwh'],.5)

    def test_at_target_load_uses_battery_then_next_buy_refills_to_target(self):
        rows=[dict(slot_start=i,slot_end=i+1,price_buy_pln_kwh=1.2,
                   price_sell_pln_kwh=0,buy_window=True,sale_window=False,
                   forecast_load_kwh=.1,forecast_pv_total_kwh=0) for i in range(2)]
        result=optimize_energy_horizon(
            rows,40,15,15,.98,.98,0,0,5,15,[100]*2,40,.1,100,
            minimum_soc_targets=[40]*2,required_soc_pcts=[15]*2,
            battery_sales_enabled=False,grid_charge_caps=[40]*2,minimum_grid_charge_kwh=.02)
        self.assertEqual(result['flows'][0]['grid_charge_kwh'],0)
        self.assertGreater(result['flows'][0]['battery_to_load_kwh'],0)
        self.assertAlmostEqual(result['flows'][1]['soc_end_pct'],40)
        self.assertTrue(all(flow['soc_end_pct']<=40 for flow in result['flows']))

    def test_small_buy_is_valid_only_for_remaining_target_headroom(self):
        rows=[dict(slot_start=0,slot_end=1,price_buy_pln_kwh=1.2,
                   price_sell_pln_kwh=0,buy_window=True,sale_window=False,
                   forecast_load_kwh=.1,forecast_pv_total_kwh=0)]
        result=optimize_energy_horizon(
            rows,39.8,15,15,.98,.98,0,0,5,15,[100],40,.1,100,
            minimum_soc_targets=[40],required_soc_pcts=[15],
            battery_sales_enabled=False,grid_charge_caps=[40],minimum_grid_charge_kwh=.02)
        self.assertAlmostEqual(result['flows'][0]['grid_charge_kwh'],.03/.98)
        self.assertAlmostEqual(result['flows'][0]['soc_end_pct'],40)

    def test_buy_slot_caps_follow_the_target_instead_of_soc_100(self):
        rows = self.rows(cheap=1.15, dear=1.05)
        targets = [20.0, 25.0, 30.0, 35.0] + [15.0] * 6 + [30.0, 40.0]
        caps = economic_buy_charge_caps(rows, targets)
        self.assertEqual(caps[:4], targets[:4])
        self.assertEqual(caps[4:10], targets[4:10])
        self.assertEqual(caps[10:], targets[10:])

    def test_target_40_limits_full_buy_volume_and_stops_further_charging(self):
        rows = [dict(slot_start=i, slot_end=i + 1, price_buy_pln_kwh=1.0,
                     price_sell_pln_kwh=0.0, buy_window=True,
                     sale_window=False, forecast_load_kwh=0.0,
                     forecast_pv_total_kwh=0.0) for i in range(4)]
        targets = [40.0] * len(rows)
        caps = economic_buy_charge_caps(rows, targets)
        result = optimize_energy_horizon(
            rows, 20.0, 15.0, 15.0, 0.9, 0.95, 0.0, 0.0, 5.0, 15,
            [15.0] * len(rows), 15.0, 0.1, 100.0,
            targets, set(), {3}, [15.0] * len(rows),
            battery_sales_enabled=False, grid_charge_caps=caps)

        self.assertEqual(result["flows"][-1]["soc_end_pct"], 40.0)
        self.assertTrue(all(flow["soc_end_pct"] <= 40.0 + 1e-9
                            for flow in result["flows"]))
        self.assertEqual(result["flows"][-1]["grid_charge_kwh"], 0.0)

    def test_missing_price_does_not_create_an_unpriced_charge_cap(self):
        rows = self.rows(cheap=1.15, dear=1.05)
        rows[0]['price_buy_pln_kwh'] = None
        caps = economic_buy_charge_caps(rows, [15.0] * len(rows))
        self.assertEqual(caps[0], 15.0)

    def test_sell_blocks_buy_and_missing_price_keeps_contract_cap(self):
        rows = self.rows()
        for row in rows[10:]:
            row['sale_window'] = True
        caps = economic_buy_charge_caps(rows, [30.0] * 12)
        self.assertEqual(caps[:4], [30.0] * 4)
        self.assertEqual(caps[4:], [30.0] * 8)
        for row in rows[10:]:
            row['sale_window'] = False
            row['price_buy_pln_kwh'] = None
        caps = economic_buy_charge_caps(rows, [30.0] * 12)
        self.assertEqual(caps[:4], [30.0] * 4)
        self.assertEqual(caps[4:10], [30.0] * 6)
        self.assertEqual(caps[10:], [30.0] * 2)

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
        self.assertEqual(caps[:4], [40.0] * 4)
        self.assertEqual(caps[4:10], [40.0] * 6)
        self.assertEqual(caps[10:], [40.0] * 2)
        contract, result = self.optimize(rows, initial=20.0)
        self.assertGreater(sum(f['grid_charge_kwh'] for f in result['flows']), 0.0)
        for index, flow in enumerate(result['flows']):
            if rows[index]['buy_window'] and flow['grid_charge_kwh'] > 1e-7:
                self.assertLessEqual(flow['soc_end_pct'],
                                     contract['charge_targets'][index] + 1e-7)

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

    def test_buy_charge_keeps_battery_and_house_energy_separate(self):
        row = dict(slot_start=0, slot_end=1, price_buy_pln_kwh=1.0,
                   price_sell_pln_kwh=0.0, buy_window=True,
                   sale_window=False, forecast_load_kwh=0.3,
                   forecast_pv_total_kwh=0.0)
        result = optimize_energy_horizon(
            [row], 21.0, 15.0, 15.0, 0.9, 0.95, 0.0, 0.0, 5.0, 15,
            [15.0], 15.0, 0.1, 100.0, [28.0], set(), {0}, [15.0],
            battery_sales_enabled=False, grid_charge_caps=[100.0])
        flow = result['flows'][0]
        self.assertAlmostEqual(flow['soc_end_pct'], 28.0)
        self.assertGreater(flow['grid_charge_kwh'], 0.0)
        self.assertAlmostEqual(flow['grid_load_kwh'], 0.3)
        self.assertAlmostEqual(flow['voluntary_grid_load_kwh'], 0.3)
        self.assertEqual(voluntary_load_without_charging_indices(
            [row], result['flows'], 0.05, 0.02), set())

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
