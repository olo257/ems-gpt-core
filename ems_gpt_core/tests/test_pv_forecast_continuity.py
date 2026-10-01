import pathlib
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ingestion_service import IngestionAdapters, build_ingestion, select_pv_profile, preserve_stored_pv_forecasts
from planner_service import validate_pv_forecasts


def forecast(slot, a=1.0, b=2.0):
    return {"slot_start": slot, "forecast_pv1_kwh": a, "forecast_pv2_kwh": b,
            "forecast_pv_total_kwh": a+b if a is not None and b is not None else None}


class Cursor:
    def __init__(self, slots, profiles=(), snapshots=()):
        self.slots, self.profiles, self.snapshots = slots, list(profiles), list(snapshots)
        self.writes = []

    def execute(self, sql, params=None):
        if sql.lstrip().startswith("UPDATE"):
            self.writes.append(params)
        elif "FROM ems_gpt_core_pv_profiles" in sql:
            self.result = self.profiles
        elif "JOIN ems_gpt_plan_runs" in sql:
            self.result = self.snapshots
            if "r.status='PUBLISHED'" not in sql or "r.validation_status='ACCEPTED'" not in sql:
                raise AssertionError("Must use accepted plans only")
        elif "FROM ems_gpt_slots" in sql:
            self.result = [r for r in self.slots if params[0] <= r['slot_start'] < params[1]]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.result

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PVForecastContinuityTests(unittest.TestCase):
    def test_every_month_rollover_can_use_preceding_learned_month(self):
        for month in range(1, 13):
            with self.subTest(month=month):
                previous = (month-2) % 12 + 1
                slots = [{'slot_start': datetime(2026, month, 1, 10, m)} for m in (0, 15)]
                profiles = [{"month_no": previous, "hour_no": 10, "minute_no": m,
                             "mean_share": w} for m, w in ((0, .1), (15, .2))]
                weights, selected = select_pv_profile(slots, profiles, month)
                self.assertEqual(selected, previous)
                self.assertEqual(weights, [.1, .2])
                own = [{**r, 'month_no': month} for r in profiles]
                self.assertEqual(select_pv_profile(slots, profiles+own, month)[1], month)

    def test_incomplete_month_never_invents_zero_for_missing_quarter(self):
        slots = [{'slot_start': datetime(2026, 10, 1, 10, m)} for m in (0, 15)]
        profiles = [{'month_no': 10, 'hour_no': 10, 'minute_no': 0, 'mean_share': .1}]
        self.assertEqual(select_pv_profile(slots, profiles, 10), ([], None))

    def service(self, cur, sources):
        @contextmanager
        def db():
            yield cur
        return build_ingestion(IngestionAdapters(
            options={'forecast_history_corrections_enabled': False}, timezone=ZoneInfo('Europe/Warsaw'),
            pv_forecast_entities={'today': ('a','b'), 'tomorrow': ('c','d')}, db=db,
            local_now=lambda: datetime(2026,10,1,10), slot_start=lambda: datetime(2026,10,1,10),
            canonical_slots_for_day=lambda day: [], number=lambda value: value,
            ha_state=lambda entity: sources.get(entity), ha_service_response=lambda *args: None,
            record_event=lambda *args: None))

    def test_october_refresh_conserves_separate_pv1_pv2_totals(self):
        slots = [forecast(datetime(2026,10,1,10,m), None, None) for m in (0,15)]
        profiles = [{'month_no':9,'hour_no':10,'minute_no':m,'mean_share':w}
                    for m,w in ((0,.1),(15,.2))]
        cur = Cursor(slots, profiles)
        result = self.service(cur, {'a':15.0,'b':9.0,'c':10.0,'d':5.0}).refresh_pv_forecast()
        self.assertEqual(result['today']['profile_source'], 'NEAREST_LEARNED_MONTH')
        self.assertEqual(result['today']['profile_month'], 9)
        self.assertEqual(sum(p[0] for p in cur.writes), 15)
        self.assertEqual(sum(p[1] for p in cur.writes), 9)
        self.assertEqual(sum(p[2] for p in cur.writes), 24)

    def test_source_outage_preserves_existing_sql_forecast(self):
        cur = Cursor([forecast(datetime(2026,10,1,10))])
        result = self.service(cur, {}).refresh_pv_forecast()
        self.assertEqual(result['today']['status'], 'STORED_FORECAST')
        self.assertEqual(cur.writes, [])

    def test_recovers_same_slot_latest_valid_plan_without_overwriting_known_forecast(self):
        start = datetime(2026,10,1,10)
        missing = start+timedelta(minutes=15)
        cur = Cursor([forecast(start), forecast(missing,None,None)], snapshots=[
            forecast(missing,None,None), forecast(start,9,9), forecast(missing,4,5),
            forecast(missing,8,9), forecast(start-timedelta(days=1),6,7)])
        result = preserve_stored_pv_forecasts(cur,start,start+timedelta(hours=1))
        self.assertEqual(result['status'],'STORED_FORECAST')
        self.assertEqual(result['recovered_slots'],1)
        self.assertEqual(cur.writes[0][:3], (4,5,9))
        self.assertEqual(len(cur.writes),1)

    def test_explicit_source_zero_is_valid_without_profiles(self):
        cur = Cursor([forecast(datetime(2026,10,1,10),None,None)])
        result = self.service(cur, {'a':0.0,'b':0.0,'c':0.0,'d':0.0}).refresh_pv_forecast()
        self.assertEqual(result['today']['profile_source'],'SOURCE_ZERO')
        self.assertEqual(cur.writes[0][:3], (0,0,0))

    def test_planner_rejects_unknown_pv_but_accepts_real_zero(self):
        slot = datetime(2026,10,1,10)
        validate_pv_forecasts([forecast(slot,0,0)])
        for row, error in ((forecast(slot,None,None),'MISSING'),
                           (forecast(slot,-1,2),'INVALID'),
                           (forecast(slot,float('nan'),2),'INVALID'),
                           ({**forecast(slot),'forecast_pv_total_kwh':8},'INCONSISTENT')):
            with self.subTest(error=error):
                with self.assertRaisesRegex(RuntimeError,error+'_PV_FORECAST'):
                    validate_pv_forecasts([row])


if __name__ == '__main__':
    unittest.main()
