import pathlib
import sys
import unittest
from datetime import datetime,timedelta
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from planner_policy_service import solve_buy_policy, EnergyInfeasible
from planner_diagnostics_service import PlannerTrace


def rows(spec):
    start=datetime(2026,11,1)
    return [dict(slot_start=start+timedelta(minutes=15*i),
        slot_end=start+timedelta(minutes=15*(i+1)),
        buy_window=window=='BUY',sale_window=window=='SELL',market_window=window,
        price_buy_pln_kwh=price,price_sell_pln_kwh=max(0,price-.59),
        forecast_load_kwh=load,forecast_pv_total_kwh=pv)
        for i,(window,price,load,pv) in enumerate(spec)]


def solve(data,**kwargs):
    defaults=dict(initial_soc_pct=20,capacity_kwh=15,reserve_pct=12,
        eta_c=.98,eta_d=.98,degradation=.08,min_margin=.05,max_power_kw=5,
        slot_minutes=15,terminal_soc_pct=12,required_soc_pcts=[12]*len(data),
        sale_floor_pcts=[100]*len(data),soc_step_pct=.5)
    defaults.update(kwargs)
    return solve_buy_policy(data,**defaults)


class PrimaryBuyPolicyTests(unittest.TestCase):
    def test_feasible_primary_never_considers_cheaper_neutral(self):
        trace=PlannerTrace('test')
        result=solve(rows([('BUY',.5,.1,0),('NEUTRAL',.1,.5,0)]),trace=trace)
        self.assertFalse(result['expanded'])
        self.assertEqual(result['promoted_indices'],set())
        self.assertFalse(any(e['stage'].startswith('NEUTRAL') for e in trace.entries))

    def test_primary_deficit_opens_only_used_neutral(self):
        data=rows([('NEUTRAL',.8,0,0),('NEUTRAL',.4,0,0),('SELL',2,1,0),('NEUTRAL',3,.1,0)])
        result=solve(data,initial_soc_pct=12)
        self.assertTrue(result['expanded'])
        self.assertEqual(result['promoted_indices'],{1})
        self.assertFalse(result['rows'][0]['buy_window'])
        self.assertFalse(result['rows'][2]['buy_window'])
        self.assertGreater(result['optimization']['flows'][1]['grid_charge_kwh'],0)

    def test_cheaper_primary_preloads_for_dearer_primary(self):
        data=rows([('BUY',.5,.1,0),('NEUTRAL',2,.9,0),('BUY',1.5,.1,0),('SELL',2,.7,0)])
        result=solve(data,initial_soc_pct=12)
        flows=result['optimization']['flows']
        self.assertGreater(flows[0]['grid_charge_kwh'],1)
        self.assertLess(flows[2]['grid_charge_kwh'],.8)
        for i,f in enumerate(flows):
            self.assertGreaterEqual(f['soc_end_pct']+.001,12)
            if f['grid_charge_kwh']>.02:
                self.assertAlmostEqual(f['soc_end_pct'],result['targets'][i])

    def test_neutral_after_deficit_cannot_replace_required_earlier_charge(self):
        result=solve(rows([('NEUTRAL',1,0,0),('SELL',2,.8,0),('NEUTRAL',.01,0,0)]),initial_soc_pct=12)
        self.assertIn(0,result['promoted_indices'])
        self.assertGreater(result['optimization']['flows'][0]['grid_charge_kwh'],0)

    def test_no_permission_to_replace_sell_or_lower_hard_goal(self):
        with self.assertRaises(EnergyInfeasible):
            solve(rows([('SELL',2,1,0)]),initial_soc_pct=12)

    def test_programming_error_does_not_open_neutral(self):
        def broken(*a,**k): raise RuntimeError('BUG')
        with self.assertRaisesRegex(RuntimeError,'BUG'):
            solve(rows([('BUY',1,0,0),('NEUTRAL',.5,0,0)]),optimizer=broken)

    def test_pv_charges_above_grid_target_and_minimum_is_unchanged(self):
        result=solve(rows([('NEUTRAL',1,0,2),('NEUTRAL',2,.5,0)]),initial_soc_pct=50)
        self.assertFalse(result['expanded'])
        self.assertGreater(result['optimization']['flows'][0]['soc_end_pct'],50)
        self.assertEqual(result['required'],[12,12])

    def test_pv_not_limited_by_grid_target_cap(self):
        result=solve(rows([('NEUTRAL',1,0,2)]),initial_soc_pct=50,max_soc_pct=40)
        self.assertGreater(result['optimization']['flows'][0]['soc_end_pct'],50)

    def test_measured_soc_below_minimum_recovers_in_neutral(self):
        result=solve(rows([('NEUTRAL',1,.2,0),('BUY',.2,.2,0)]),initial_soc_pct=11.5)
        self.assertTrue(result['expanded'])
        self.assertIn(0,result['promoted_indices'])
        self.assertGreaterEqual(result['optimization']['flows'][0]['soc_end_pct'],12)

    def test_second_attempt_keeps_daily_target(self):
        trace=PlannerTrace('test')
        result=solve(rows([('BUY',1,0,0),('NEUTRAL',.5,0,0),('NEUTRAL',2,.8,0)]),
            initial_soc_pct=12,terminal_soc_pct=20,required_soc_pcts=[12,12,20],trace=trace)
        self.assertTrue(result['expanded'])
        self.assertGreaterEqual(result['optimization']['flows'][-1]['soc_end_pct'],20)
        calls=[e['optimizer_inputs'] for e in trace.entries if 'optimizer_inputs' in e]
        self.assertTrue(all(c['required_soc_pcts']==[12,12,20] for c in calls))
        self.assertTrue(all(c['terminal_soc_pct']==20 for c in calls))

    def test_full_planner_assembles_and_publishes_selected_policy(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import patch
        from planner_service import build_planner,PlannerAdapters
        now=datetime(2026,11,1)
        data=rows([('BUY',.5,.2,0),('NEUTRAL',2,.2,0)])
        for row in data:
            row['slot_start']+=timedelta(minutes=15)
            row['slot_end']+=timedelta(minutes=15)
            row.update(price_source='PSE_API',forecast_pv1_kwh=0,forecast_pv2_kwh=0,
                local_day=now.date(),heat_pump_window=False)
        executed=[]
        class Cursor:
            rowcount=2
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def execute(self,sql,params=()):
                self.sql=sql
                executed.append(sql)
                # All assembled SQL must have a parameter for every placeholder.
                assert sql.count('%s') == len(params), (sql,len(params))
                if 'soc_target_source=%s' in sql:
                    # Mirror the actual legacy MariaDB contract, not a
                    # permissive mock accepting an arbitrarily long label.
                    import re
                    app_source=(pathlib.Path(__file__).resolve().parents[1]/'app.py').read_text()
                    width=int(re.search(r'soc_target_source VARCHAR\((\d+)\)',app_source).group(1))
                    index=sql[:sql.index('soc_target_source=%s')].count('%s')
                    if len(params[index]) > width:
                        raise ValueError('DATA_TOO_LONG:soc_target_source')

            def fetchall(self):
                if ('FROM ems_gpt_rce_windows' in self.sql
                        or 'SELECT * FROM ems_gpt_slots' in self.sql
                        or 'SELECT * FROM ems_gpt_plan_stage_rows' in self.sql
                        or 'SELECT slot_start,price_buy_pln_kwh' in self.sql):
                    return [dict(r) for r in data]
                return []
            def fetchone(self):
                if 'SELECT COUNT(*) n' in self.sql:
                    return {'n':2,'bad_price':0,'bad_soc':0,'bad_ppd':0}
                return None
        @contextmanager
        def db(): yield SimpleNamespace(cursor=Cursor)
        noop=lambda *a,**k:None
        service=build_planner(PlannerAdapters(
            options={'slot_minutes':15,**{f'deye_program_{i}_soc_pct':10 for i in range(1,7)}},
            db=db,local_now=lambda:now,slot_start=lambda:now,setting=lambda *a:40,
            audit_stage=noop,active_tou_program=noop,tou_program_snapshot=lambda:[],record_event=noop))
        with patch('planner_service.persist_independent_hp_schedule',return_value={'status':'COMMITTED'}), \
             patch('planner_service.current_soc_for_replan',return_value=40), \
             patch('planner_service.project_soc_to_horizon_start',return_value={'horizon_initial_soc_pct':40}), \
             patch('planner_service.planning_tou_programs',return_value=[]), \
             patch('planner_service.prepare_heat_pump_plan',return_value={
                'dhw_profile':{},'hp_fallback_kw':1.5,'planned_hp_kw_by_day':{},
                'hp_selected_indices':set(),'hp_shortfalls':[], 'component_rows':{},
                'previous_loads':{},'component_heat':{}}):
            result=service.run_planner('test')
        self.assertEqual(result['rows'],2)
        self.assertTrue(any('p.buy_window=s.buy_window' in sql for sql in executed))

    def test_replan_does_not_inherit_previous_neutral_promotions(self):
        from ingestion_service import original_rce_windows
        original=rows([('BUY',.5,0,0),('NEUTRAL',1,.8,0),('SELL',2,.8,0)])
        first=solve(original,initial_soc_pct=12)
        self.assertTrue(first['expanded'])
        class Cursor:
            def execute(self,sql,params=()):
                self_sql=sql
            def fetchall(self):
                return [{'slot_start':r['slot_start'],'buy_window':r['buy_window'],
                         'sale_window':r['sale_window']} for r in original]
        restored=original_rce_windows(Cursor(),first['rows'],{})
        self.assertEqual([r['buy_window'] for r in restored],[True,False,False])
        second=solve(restored,initial_soc_pct=80)
        self.assertFalse(second['expanded'])
        self.assertEqual(second['promoted_indices'],set())

    def test_optional_sale_fault_preserves_accepted_no_sale_plan(self):
        from planner_dispatch_service import optimize_energy_horizon
        attempted=[]
        def optimizer(*args,**kwargs):
            attempted.append(kwargs.get('battery_sales_enabled'))
            if kwargs.get('battery_sales_enabled'):
                raise RuntimeError('OPTIONAL_SOLVER_FAULT')
            return optimize_energy_horizon(*args,**kwargs)
        result=solve(rows([('SELL',3,.1,0),('BUY',.5,.1,0)]),
            initial_soc_pct=80,sale_floor_pcts=[12,100],optimizer=optimizer)
        self.assertIn(True,attempted)
        self.assertEqual(sum(f['battery_sell_kwh'] for f in result['optimization']['flows']),0)
