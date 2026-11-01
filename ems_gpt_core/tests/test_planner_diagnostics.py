import pathlib
import sys
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_diagnostics_service import PlannerTrace, diagnostic_rows
from planner_hp_service import hp_heating_window_indices
from energy_balance_service import calculate_slot_energy_balance


class TraceTests(unittest.TestCase):
    def test_failed_optimizer_keeps_full_inputs_before_rollback(self):
        def solve(rows, required_soc_pcts=None):
            raise RuntimeError('SOC_SAFETY_BRIDGE_UNREACHABLE:0')
        trace = PlannerTrace('attempt')
        rows = [{'slot_start': datetime(2026, 11, 1, 7), 'forecast_load_kwh': 1}]
        with self.assertRaises(RuntimeError):
            trace.optimizer(solve, 'DISPATCH', rows, required_soc_pcts=[12])
        rows[0]['forecast_load_kwh'] = 9
        self.assertEqual(trace.entries[0]['optimizer_inputs']['rows'][0]['forecast_load_kwh'], 1)
        self.assertEqual(trace.entries[0]['status'], 'REJECTED')
        payload = trace.payload({'started_at':'now','run_type':'test'}, 'REJECTED', error='bad')
        self.assertFalse(payload['complete'])
        self.assertIn('VALIDATE', payload['missing_stages'])
        self.assertEqual(diagnostic_rows(payload)[0]['complete'], 'NIE')

    def test_pv_charge_counted_once_in_balance(self):
        result = calculate_slot_energy_balance({
            'forecast_pv_total_kwh':1, 'planned_pv_to_bat_kwh':1,
            'planned_battery_charge_kwh':0.98}, 0.98, 0.98)
        self.assertAlmostEqual(result['difference_kwh'], 0)

    def test_comfort_not_blocked_by_sell_window(self):
        day = datetime(2026,11,1)
        rows = [{'slot_start':day.replace(hour=7), 'sale_window':True}]
        self.assertEqual(hp_heating_window_indices(rows, day.date()), {0})

    def test_hp_commit_and_diagnostic_survive_energy_rollback(self):
        import json
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import patch
        from planner_service import build_planner, PlannerAdapters
        transactions = []
        now = datetime(2026,11,1,7)
        source = {'slot_start':now, 'slot_end':now.replace(minute=15),
            'forecast_load_kwh':None, 'forecast_pv1_kwh':0,
            'forecast_pv2_kwh':0, 'forecast_pv_total_kwh':0,
            'price_buy_pln_kwh':1, 'price_sell_pln_kwh':0.5}

        class Cursor:
            def __init__(self, tx): self.tx = tx
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql, params=()): self.tx['queries'].append((sql, params))
            def fetchone(self): return None
            def fetchall(self): return [dict(source)]

        @contextmanager
        def db():
            tx = {'queries':[]}
            transactions.append(tx)
            try:
                yield SimpleNamespace(cursor=lambda:Cursor(tx))
            except Exception:
                tx['outcome'] = 'ROLLBACK'
                raise
            else:
                tx['outcome'] = 'COMMIT'

        noop = lambda *a, **kw: None
        service = build_planner(PlannerAdapters(
            options={'slot_minutes':15, **{f'deye_program_{i}_soc_pct':10 for i in range(1,7)}}, db=db, local_now=lambda:now,
            slot_start=lambda:now, setting=lambda *args:50,
            audit_stage=noop, active_tou_program=noop,
            tou_program_snapshot=lambda:[], record_event=noop))
        with patch('planner_hp_service.prepare_heat_pump_plan', return_value={
                'hp_selected_indices':{0}, 'hp_shortfalls':[]}), \
             patch('planner_service.current_soc_for_replan', return_value=50), \
             patch('planner_service.planning_tou_programs', return_value=[]), \
             patch('planner_service.project_soc_to_horizon_start', return_value={
                 'horizon_initial_soc_pct':50}):
            with self.assertRaisesRegex(RuntimeError, 'MISSING_LOAD_FORECAST'):
                service.run_planner('test')
        self.assertEqual([t['outcome'] for t in transactions],
                         ['COMMIT','ROLLBACK','COMMIT','COMMIT'])
        self.assertTrue(any('INSERT INTO ems_gpt_hp_schedule' in q[0]
                            for q in transactions[0]['queries']))
        traces = [params for sql,params in transactions[-1]['queries']
                  if 'INSERT INTO ems_gpt_planner_traces' in sql]
        payload = json.loads(traces[0][-1])
        self.assertEqual(payload['status'], 'REJECTED')
        self.assertEqual(payload['hp']['status'], 'COMMITTED')
        self.assertIn('MISSING_LOAD_FORECAST', payload['traceback'])
        self.assertTrue(payload['stages'])

    def test_later_optimizer_exposes_lost_contract(self):
        def solve(rows, minimum_soc_targets=None):
            return {'flows':[], 'objective_pln':1}
        trace = PlannerTrace('test')
        trace.optimizer(solve, 'SEED', [], minimum_soc_targets=[40])
        trace.optimizer(solve, 'LATER', [])
        self.assertEqual(trace.entries[-1]['comparison']['weakened_contracts'],
                         ['minimum_soc_targets'])
        self.assertIn('Utracono ograniczenia', diagnostic_rows(trace.payload(
            {'started_at':'now','run_type':'test'}, 'PUBLISHED'))[-1]['explanation'])
