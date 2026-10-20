"""Boundary scheduling, persistent events, telemetry timing and minute dispatch."""
import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from dataclasses import fields

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from replan_policy_service import replan_reasons, inspect_replan_inputs
from scheduler_service import should_run_slot_replan, run_scheduler, SchedulerAdapters


class ReplanPolicyTests(unittest.TestCase):
    start = datetime(2026, 10, 20, 4, 30)
    published = datetime(2026, 10, 19, 14)

    def row(self, offset=0, kind='NEUTRAL', **kwargs):
        return dict(slot_start=self.start+timedelta(minutes=offset),
                    buy_window=kind=='BUY', sale_window=kind=='SELL', plan_published=1,
                    soc_start_plan_pct=50., soc_end_plan_pct=50., **kwargs)

    def check(self, rows=None, sample=None, changes=None, heating=None, **kwargs):
        return replan_reasons(self.start+timedelta(minutes=2), self.start, self.published,
                              rows if rows is not None else [self.row(), self.row(15)],
                              sample, changes or [], heating or [], {}, **kwargs)

    def test_neutral_slots_do_not_replan(self):
        result = self.check()
        self.assertEqual(result['reasons'], [])
        self.assertFalse(should_run_slot_replan(self.start+timedelta(minutes=2),
                                               self.start, self.published, True, result['reasons']))

    def test_buy_and_sell_start_are_prepared_in_preceding_slot(self):
        for kind in ('BUY', 'SELL'):
            result = self.check([self.row(), self.row(15, kind)])
            self.assertEqual(result['reasons'], ['BEFORE_'+kind])
            self.assertTrue(should_run_slot_replan(self.start+timedelta(minutes=2),
                                                  self.start, self.published, True, result['reasons']))

    def test_contiguous_buy_or_sell_is_not_replanned(self):
        for kind in ('BUY', 'SELL'):
            self.assertEqual(self.check([self.row(0,kind), self.row(15,kind)])['reasons'], [])

    def test_direct_buy_to_sell_transition_prepares_sell(self):
        self.assertIn('BEFORE_SELL', self.check([self.row(0,'BUY'), self.row(15,'SELL')])['reasons'])

    def test_boundary_only_in_settling_interval(self):
        for minute in (0, 1, 7, 14):
            report = replan_reasons(self.start+timedelta(minutes=minute), self.start,
                                    self.published, [self.row(),self.row(15,'BUY')], None, [], [], {})
            self.assertEqual(report['reasons'], [])

    def test_no_repeated_publication_in_same_slot(self):
        now = self.start+timedelta(minutes=3)
        self.assertFalse(should_run_slot_replan(now,self.start,self.start+timedelta(minutes=2),
                                               True,['BEFORE_BUY','SOC_DEVIATION']))

    def test_persistent_event_cooldown_and_boundary_exception(self):
        now = self.start+timedelta(minutes=2)
        recent = self.start-timedelta(minutes=1)
        self.assertFalse(should_run_slot_replan(now,self.start,recent,True,['SOC_DEVIATION']))
        self.assertTrue(should_run_slot_replan(now,self.start,recent,True,['BEFORE_BUY']))
        self.assertTrue(should_run_slot_replan(now,self.start,recent-timedelta(minutes=15),True,['SOC_DEVIATION']))

    def test_rce_is_required_and_emergency_is_allowed_at_midnight(self):
        now = self.start.replace(hour=0, minute=2)
        slot = now.replace(minute=0)
        self.assertFalse(should_run_slot_replan(now,slot,None,False,['NO_PUBLISHED_PLAN']))
        self.assertTrue(should_run_slot_replan(now,slot,self.published,True,['SOC_RESERVE_AT_RISK']))

    def test_live_soc_compares_at_sample_time_not_plan_end(self):
        row = self.row(); row.update(soc_start_plan_pct=20.,soc_end_plan_pct=32.)
        sample = dict(captured_at=self.start+timedelta(minutes=1),soc_pct=20.8)
        self.assertEqual(self.check([row,self.row(15)],sample)['reasons'], [])
        sample['soc_pct']=14.
        result=self.check([row,self.row(15)],sample)
        self.assertIn('SOC_DEVIATION',result['reasons'])
        self.assertIn('SOC_RESERVE_AT_RISK',result['reasons'])

    def test_stale_missing_or_nan_soc_cannot_trigger_a_false_soc_event(self):
        for sample in (None,dict(captured_at=self.start-timedelta(minutes=5),soc_pct=5.),
                       dict(captured_at=self.start,soc_pct=float('nan'))):
            self.assertEqual(self.check(sample=sample)['reasons'], [])

    def test_projected_discharge_to_reserve_is_detected(self):
        result=self.check(sample=dict(captured_at=self.start,soc_pct=16.,battery_discharge_power_w=2000.))
        self.assertIn('SOC_RESERVE_AT_RISK',result['reasons'])

    def test_forecast_changes_require_absolute_and_relative_thresholds(self):
        for base,changed,wanted in ((1.,2.1,True),(10.,11.1,False),(1.,1.9,False)):
            result=self.check(changes=[dict(current_load=changed,planned_load=base,
                                             current_pv=1.,planned_pv=1.)])
            self.assertEqual('LOAD_FORECAST_CHANGED' in result['reasons'],wanted)

    def test_shifted_pv_is_detected_even_if_total_is_unchanged(self):
        result=self.check(changes=[dict(planned_pv=2.,current_pv=0.),
                                   dict(planned_pv=0.,current_pv=2.)])
        self.assertIn('PV_FORECAST_CHANGED',result['reasons'])

    def test_hp_third_reading_triggers_without_waiting_for_buy(self):
        self.assertIn('HP_HEATING_QUALIFIED',self.check(heating=[dict(
            below_now=3,below_at_publication=2)])['reasons'])
        self.assertNotIn('HP_HEATING_QUALIFIED',self.check(heating=[dict(
            below_now=4,below_at_publication=3)])['reasons'])

    def test_buy_end_checks_battery_execution_and_soc_and_replans_only_shortfall(self):
        previous=self.row(-15,'BUY',planned_battery_charge_kwh=1.2,actual_battery_charge_kwh=.6,
                           soc_end_pct=46.)
        report=self.check([previous,self.row(),self.row(15)])
        self.assertEqual(report['buy_completion']['status'],'SHORTFALL')
        self.assertIn('BUY_EXECUTION_SHORTFALL',report['reasons'])
        previous.update(actual_battery_charge_kwh=1.2,soc_end_pct=50.)
        report=self.check([previous,self.row(),self.row(15)])
        self.assertEqual(report['buy_completion']['status'],'ON_PLAN')
        self.assertEqual(report['reasons'],[])

    def test_buy_end_reports_energy_gap_even_when_lower_plan_was_executed(self):
        previous=self.row(-15,'BUY',planned_battery_charge_kwh=1.2,actual_battery_charge_kwh=1.2,
                          soc_end_pct=50.,soc_required_pct=55.)
        report=self.check([previous,self.row(),self.row(15)])
        self.assertEqual(report['buy_completion']['coverage_shortfall_kwh'],.75)
        self.assertIn('BUY_EXECUTION_SHORTFALL',report['reasons'])

    def test_buy_end_unknown_execution_is_not_zero_and_buy_continuation_not_end(self):
        previous=self.row(-15,'BUY',planned_battery_charge_kwh=1.2)
        report=self.check([previous,self.row(),self.row(15)])
        self.assertEqual(report['buy_completion']['status'],'WAITING_TELEMETRY')
        self.assertNotIn('BUY_EXECUTION_SHORTFALL',report['reasons'])
        self.assertIsNone(self.check([previous,self.row(0,'BUY'),self.row(15,'BUY')])['buy_completion'])

    def test_missing_plan_recovery_and_failure_triggers(self):
        following=self.row(15);following['plan_published']=0
        self.assertIn('MISSING_FUTURE_PLAN',self.check([self.row(),following])['reasons'])
        self.assertIn('TELEMETRY_RECOVERED',self.check(recovered=True)['reasons'])
        self.assertIn('PLANNER_RETRY',self.check(planner_failed=True)['reasons'])

    def test_db_adapter_uses_durable_same_slot_plan_and_published_heating_baseline(self):
        class Cursor:
            queries=[]
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(inner,sql,params):inner.queries.append((sql,params));inner.sql=sql
            def fetchall(inner):
                if 'BETWEEN %s AND %s ORDER BY' in inner.sql:return [self.row(),self.row(15,'BUY')]
                return []
            def fetchone(inner):return None
        cursor=Cursor()
        conn=Mock();conn.__enter__=Mock(return_value=conn);conn.__exit__=Mock(return_value=False)
        conn.cursor=Mock(return_value=cursor)
        report=inspect_replan_inputs(lambda:conn,self.start+timedelta(minutes=2),self.start,self.published,{})
        self.assertEqual(report['reasons'],['BEFORE_BUY'])
        self.assertIn('s.run_id=p.plan_run_id',cursor.queries[2][0])
        self.assertEqual(cursor.queries[3][1][2],self.published)
        self.assertIn('actual_recorded_at<=%s',cursor.queries[3][0])


class SchedulerMinuteDispatchTests(unittest.TestCase):
    def run_cycles(self, reasons):
        publication={'at':datetime(2026,10,20,14)}
        class Lock:
            def __enter__(self):return self
            def __exit__(self,*args):pass
        class Cursor:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,sql,*args):self.sql=sql
            def fetchone(self):
                if 'last_run' in self.sql:return {'last_run':publication['at']}
                return {'created_at':datetime(2026,10,20,14),'payload_json':'{}'}
        class Connection:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def cursor(self):return Cursor()
        args={f.name:Mock(return_value={}) for f in fields(SchedulerAdapters)}
        args.update(options={},state={'modules':{}},lock=Lock(),db=lambda:Connection(),
                    local_now=lambda:datetime(2026,10,20,15,32),
                    slot_start=lambda _:datetime(2026,10,20,15,30),
                    capture_telemetry=Mock(return_value=True),
                    publish_current_prices=Mock(return_value={'status':'OK'}))
        args['run_serialized']=lambda _,fn,*values:fn(*values)
        def publish(*_args):
            publication['at']=datetime(2026,10,20,15,32)
            return {'run_id':'new-plan'}
        args['run_planner'].side_effect=publish
        args['run_ppd'].return_value={'status':'COMPLETED'}
        monitor=dict(reasons=reasons,buy_completion=None)
        with patch('scheduler_service.inspect_replan_inputs',return_value=monitor),patch(
                'scheduler_service.time.sleep',side_effect=[None,StopIteration]):
            with self.assertRaises(StopIteration):run_scheduler(SchedulerAdapters(**args))
        return args

    def test_idle_monitor_does_not_stop_minute_executor(self):
        args=self.run_cycles([])
        self.assertEqual(args['dispatch_ready_commands'].call_count,2)
        self.assertEqual(args['stage_executor_commands'].call_count,2)
        args['run_planner'].assert_not_called()
        args['run_ppd'].assert_not_called()

    def test_boundary_calls_serialized_planner_then_ppd_and_still_dispatches(self):
        args=self.run_cycles(['BEFORE_BUY'])
        self.assertEqual(args['run_planner'].call_count,1)
        args['run_planner'].assert_called_with('event_replan')
        args['run_ppd'].assert_called_with('new-plan','event_replan')
        self.assertEqual(args['dispatch_ready_commands'].call_count,2)


if __name__=='__main__':unittest.main()
