import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from component_forecast import (split_slot, complete_cycles, build_component_forecast,
                                heating_profile_for_plan, mean_profile)
from materialization_service import build_materializations, MaterializationAdapters

SLOT = timedelta(minutes=15)
NOW = datetime(2026, 10, 16, 6, 15)


def raw(when, house=.2, dhw=0, heat=0, **changes):
    row = dict(slot_start=when, actual_load_kwh=house+dhw+heat,
               actual_heating_consumed_kwh=heat,actual_dhw_consumed_kwh=dhw,
               actual_cooling_consumed_kwh=0,actual_ev_kwh=0,pv_cwu_on=0,
               actual_mode='AUTO',execution_reason='CORE_TELEMETRY_15_SAMPLES',
               sample_count=15,component_samples=15,coverage_pct=100)
    row.update(changes)
    return row


def history():
    rows=[]
    for days_ago in range(7,0,-1):
        day=(NOW-timedelta(days=days_ago)).replace(hour=0,minute=0)
        for i in range(96):
            minute=i*15
            dhw=0
            for start in (360,510,660,810,960,1110):
                if start <= minute < start+30:
                    dhw=(.4,.2)[(minute-start)//15]
            heat=(.5,.3,.2,.1)[(i-28)%4] if 28 <= i < 40 else 0
            rows.append(raw(day+SLOT*i,dhw=dhw,heat=heat))
    return rows


class ComponentTests(unittest.TestCase):
    def test_energy_conservation_and_flexible_exclusion(self):
        row=raw(NOW,actual_load_kwh=2,actual_ev_kwh=.3,pv_cwu_on=1,
                pv_cwu_kwh=.4,actual_dhw_consumed_kwh=.5,
                actual_heating_consumed_kwh=.2)
        result=split_slot(row)
        self.assertAlmostEqual(result['house_kwh'],.6)
        self.assertAlmostEqual(sum(result[k] for k in ('house_kwh','heat_kwh','dhw_kwh')),2-.3-.4)

    def test_missing_quality_and_unmetered_flexible_load_do_not_become_zero(self):
        for changes in ({'actual_ev_kwh':None},{'pv_cwu_on':1}, {'pv_cwu_on':None},
                        {'actual_dhw_consumed_kwh':None},{'sample_count':3},
                        {'coverage_pct':20},{'component_samples':2},{'actual_mode':'MISSING_OUTAGE'},
                        {'actual_load_kwh':float('nan')}, {'execution_reason':'LEGACY'},
                        {'actual_heating_consumed_kwh':100}):
            with self.subTest(changes=changes):
                self.assertIsNone(split_slot(raw(NOW,**changes)))

    def test_circulation_losses_stay_in_dhw_without_an_extra_energy_margin(self):
        result=split_slot(raw(NOW))
        self.assertEqual(result['house_kwh'],.2)
        self.assertNotIn('circulation_kwh',result)
        model=build_component_forecast(history(),[NOW],now=NOW)
        self.assertEqual(model['circulation_effect'],'IN_MEASURED_DHW_CYCLES')

    def test_gap_fragments_and_unclosed_cycles_are_excluded(self):
        def part(i,kwh): return {'slot_start':NOW+SLOT*i,'dhw_kwh':kwh}
        rows=[part(0,.5),part(1,0),part(2,.4),part(4,.3),part(5,0),part(6,.2)]
        self.assertEqual(complete_cycles(rows,'dhw_kwh'),[])
        self.assertEqual(complete_cycles([part(0,0),part(1,.4),part(2,.2),part(3,0)],'dhw_kwh')[0]['profile'],[.4,.2])

    def test_variable_duration_profiles_preserve_mean_whole_cycle_energy(self):
        profile=mean_profile([{'profile':[.4,.2]},{'profile':[.6,.4,.2]}])
        self.assertAlmostEqual(sum(profile),.9)

    def test_full_dhw_cycle_is_forecast_during_heating_and_recent_execution_learns(self):
        start=NOW.replace(hour=6,minute=0)
        horizon=[start+SLOT*i for i in range(8)]
        result=build_component_forecast(history(),horizon,now=start)
        self.assertAlmostEqual(sum(row['dhw_kwh'] for row in result['slots'].values()),.6)
        self.assertEqual(result['slots'][start]['house_kwh'],.2)
        self.assertEqual(result['dhw_interval_slots'],10)
        self.assertIsNotNone(result['heat_profile'])
        raised=history()
        for row in raised: row['actual_load_kwh']+=.1
        changed=build_component_forecast(raised,horizon,now=start)
        self.assertEqual(changed['slots'][start]['house_kwh'],.3)

    def test_replan_forecasts_only_remaining_dhw_cycle(self):
        rows=history()+[raw(NOW-SLOT,dhw=.4)]
        result=build_component_forecast(rows,[NOW,NOW+SLOT],now=NOW)
        self.assertEqual(result['slots'][NOW]['dhw_kwh'],.2)
        self.assertEqual(result['slots'][NOW+SLOT]['dhw_kwh'],0)

    def test_missing_history_preserves_input(self):
        result=build_component_forecast([], [NOW],now=NOW)
        self.assertIsNone(result['slots'][NOW]['house_kwh'])
        self.assertIn('INSUFFICIENT_COMPLETE_DHW_CYCLES',result['reasons'])

    def test_heating_pause_does_not_repeat_startup(self):
        future=[NOW,NOW+SLOT*4,NOW+SLOT*20]
        result=heating_profile_for_plan(future,[.5,.3,.1],
                [{'slot_start':NOW-SLOT,'heat_kwh':.5}])
        self.assertEqual([result[when] for when in future],[.3,.1,.5])

    def test_short_heat_break_is_one_training_session(self):
        rows=[{'slot_start':NOW+SLOT*i,'heat_kwh':value}
              for i,value in enumerate([0,.5,.3,0,0,.2,.1]+[0]*13)]
        cycles=complete_cycles(rows,'heat_kwh',max_idle_slots=12)
        self.assertEqual(len(cycles),1)
        self.assertEqual(cycles[0]['profile'],[.5,.3,.2,.1])

    def test_component_energy_raises_bridge_without_pv_flexible_loads(self):
        from planner_service import replenishment_soc_requirements
        start=NOW.replace(hour=6,minute=0)
        times=[start+SLOT*i for i in range(8)]
        model=build_component_forecast(history(),times,now=start)
        def bridge(include_dhw):
            rows=[dict(forecast_load_kwh=0,forecast_heat_pump_load_kwh=0,
                       forecast_pv_total_kwh=0,buy_window=False,sale_window=False)]
            rows += [dict(forecast_load_kwh=model['slots'][when]['house_kwh']+
                          (model['slots'][when]['dhw_kwh'] if include_dhw else 0),
                          forecast_heat_pump_load_kwh=0,forecast_pv_total_kwh=0,
                          buy_window=False,sale_window=False) for when in times]
            return replenishment_soc_requirements(rows,15,15,2,.9,.95,5,15,0)[0]
        self.assertAlmostEqual(bridge(True)-bridge(False),.6/.95/15*100)
        # A meter for PV-only flexible loads changes neither learned house nor SOC.
        metered=history()
        for row in metered:
            row.update(actual_load_kwh=row['actual_load_kwh']+1.4,
                       actual_ev_kwh=.8,pv_cwu_on=1,pv_cwu_kwh=.6)
        other=build_component_forecast(metered,times,now=start)
        self.assertEqual(model['slots'],other['slots'])

    def test_new_rce_horizon_in_same_slot_is_not_skipped(self):
        cur=MagicMock()
        future=[{'slot_start':NOW+SLOT,'forecast_load_kwh':1}]
        cur.fetchall.side_effect=[future,[]]
        cur.fetchone.return_value={'generated_at':NOW,'last_slot':NOW,'slot_count':1}
        conn=MagicMock();conn.cursor.return_value.__enter__.return_value=cur
        context=MagicMock();context.__enter__.return_value=conn
        service=build_materializations(MaterializationAdapters(options={},app_version='test',
                db=lambda:context,local_now=lambda:NOW,slot_start=lambda:NOW,
                record_event=lambda *a:None))
        service.learn_missing_load()
        self.assertTrue(any('INSERT INTO ems_gpt_core_consumption_forecasts' in call.args[0]
                            for call in cur.execute.call_args_list))

    def test_materialization_writes_same_batch_and_preserves_missing_rows(self):
        valid_time=NOW.replace(minute=0)
        missing_time=valid_time+SLOT
        samples=history()
        for row in samples:
            if row['slot_start'].hour==6 and row['slot_start'].minute==15:
                row['sample_count']=0
        # 06:30 stays valid; later cycles supply complete DHW training examples.
        valid_time=NOW.replace(minute=30)
        future=[{'slot_start':valid_time,'forecast_load_kwh':9},
                {'slot_start':missing_time,'forecast_load_kwh':8}]
        cur=MagicMock();cur.fetchall.side_effect=[future,samples];cur.fetchone.return_value=None
        conn=MagicMock();conn.cursor.return_value.__enter__.return_value=cur
        context=MagicMock();context.__enter__.return_value=conn
        service=build_materializations(MaterializationAdapters(options={},app_version='test',
                db=lambda:context,local_now=lambda:NOW,slot_start=lambda:NOW,
                record_event=lambda *a:None))
        service.learn_missing_load()
        writes=[call.args[1] for call in cur.execute.call_args_list
                if 'SET forecast_load_kwh=' in call.args[0]]
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0][1],valid_time)
        self.assertAlmostEqual(writes[0][0],.2)
        batches=[call.args[1][1] for call in cur.execute.call_args_list
                 if 'INSERT INTO ems_gpt_core_consumption_forecasts' in call.args[0]]
        self.assertEqual(len(batches),2)
        self.assertEqual(batches[0],batches[1])

    def test_incomplete_batch_retries_and_restores_last_published_same_slot(self):
        future=[{'slot_start':NOW,'forecast_load_kwh':None}]
        cur=MagicMock()
        cur.fetchall.side_effect=[future,[]]
        cur.fetchone.side_effect=[
            {'generated_at':NOW,'last_slot':NOW,'slot_count':1,'unresolved':1},
            {'forecast_load_kwh':.73},
        ]
        cur.rowcount=1
        conn=MagicMock();conn.cursor.return_value.__enter__.return_value=cur
        context=MagicMock();context.__enter__.return_value=conn
        events=[]
        service=build_materializations(MaterializationAdapters(options={},app_version='test',
                db=lambda:context,local_now=lambda:NOW,slot_start=lambda:NOW,
                record_event=lambda *args:events.append(args)))

        service.learn_missing_load()

        fallback_query=next(call for call in cur.execute.call_args_list
                            if 'JOIN ems_gpt_plan_runs' in call.args[0])
        self.assertEqual(fallback_query.args[1],(NOW,))
        restore=next(call for call in cur.execute.call_args_list
                     if 'SET forecast_load_kwh=%s' in call.args[0])
        self.assertEqual(restore.args[1],(.73,NOW))
        batch=next(call for call in cur.execute.call_args_list
                   if 'INSERT INTO ems_gpt_core_consumption_forecasts' in call.args[0])
        self.assertEqual(batch.args[1][8],'PRESERVED_LAST_PUBLISHED_PLAN')
        self.assertIn('PUBLISHED_PLAN_SAME_SLOT',batch.args[1][9])
        self.assertTrue(any("status='PRESERVED_INCOMPLETE_HISTORY'" in call.args[0]
                            for call in cur.execute.call_args_list))

    def test_missing_history_without_published_fallback_stays_blocked(self):
        future=[{'slot_start':NOW,'forecast_load_kwh':None}]
        cur=MagicMock()
        cur.fetchall.side_effect=[future,[]]
        cur.fetchone.side_effect=[None,{}]
        cur.rowcount=0
        conn=MagicMock();conn.cursor.return_value.__enter__.return_value=cur
        context=MagicMock();context.__enter__.return_value=conn
        service=build_materializations(MaterializationAdapters(options={},app_version='test',
                db=lambda:context,local_now=lambda:NOW,slot_start=lambda:NOW,
                record_event=lambda *args:None))

        service.learn_missing_load()

        batch=next(call for call in cur.execute.call_args_list
                   if 'INSERT INTO ems_gpt_core_consumption_forecasts' in call.args[0])
        self.assertEqual(batch.args[1][8],'BLOCKED_NO_ACCEPTED_FALLBACK')
        self.assertFalse(any('SET forecast_load_kwh=%s' in call.args[0]
                             for call in cur.execute.call_args_list))


if __name__=='__main__': unittest.main()
