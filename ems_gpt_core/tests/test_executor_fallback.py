import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from executor_fallback_service import fallback_import_reason


class FallbackAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,1,1,12)
        self.now=self.start+timedelta(minutes=3)
        self.plan=dict(plan_run_id='accepted',plan_stage='PUBLISHED',
            published_run_status='PUBLISHED',plan_validation_status='ACCEPTED',
            plan_published_at=self.start,slot_start=self.start,
            slot_end=self.start+timedelta(minutes=15),buy_window=1,sale_window=0,
            grid_policy_planned='BUY_ALLOWED',planned_buy_kwh=1.0,
            soc_charge_target_pct=40,soc_target_pct=35)
        self.telemetry=dict(soc_pct=30,captured_at=self.now)

    def reason(self,plan=None,telemetry=None,now=None):
        return fallback_import_reason(self.plan if plan is None else plan,
            self.telemetry if telemetry is None else telemetry,
            now or self.now,self.start,15)

    def test_published_current_buy_survives_missing_ppd(self):
        self.assertIsNone(self.reason())
        self.plan['requested_state']='FORCE_ON'
        self.assertIsNone(self.reason())

    def test_no_permission_from_rejected_missing_neutral_sell_or_override(self):
        self.assertIsNotNone(self.reason(plan={}))
        for key,value in [('plan_run_id',None),('plan_stage','REJECTED'),
            ('published_run_status','FAILED'),('plan_validation_status','REJECTED'),
            ('plan_published_at',None),('plan_published_at',self.now+timedelta(seconds=1)),
            ('buy_window',0),('buy_window',None),('sale_window',1),('sale_window',None),
            ('grid_policy_planned','NO_BUY'),('requested_state','FORCE_OFF'),
            ('planned_buy_kwh',0),('planned_buy_kwh',float('nan')),
            ('soc_charge_target_pct',101)]:
            with self.subTest(key=key,value=value):
                self.assertIsNotNone(self.reason(plan={**self.plan,key:value}))

    def test_never_reuses_a_previous_or_future_slot(self):
        for offset in (-15,15):
            self.assertIsNotNone(self.reason(plan={**self.plan,
                'slot_start':self.start+timedelta(minutes=offset)}))
        self.assertIsNotNone(self.reason(now=self.plan['slot_end']))
        self.assertIsNotNone(self.reason(now=self.start-timedelta(seconds=1)))

    def test_telemetry_and_soc_are_required(self):
        for telemetry in ({},{'soc_pct':None,'captured_at':self.now},
            {'soc_pct':float('nan'),'captured_at':self.now},
            {'soc_pct':30,'captured_at':self.now-timedelta(seconds=121)},
            {'soc_pct':30,'captured_at':self.now+timedelta(seconds=1)},
            {'soc_pct':40,'captured_at':self.now}):
            with self.subTest(telemetry=telemetry):
                self.assertIsNotNone(self.reason(telemetry=telemetry))

    def test_target_uses_published_fallback_only(self):
        self.plan['soc_charge_target_pct']=None
        self.assertIsNone(self.reason())
        self.plan['soc_target_pct']=None
        self.assertIsNotNone(self.reason())
