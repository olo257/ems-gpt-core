"""Read-only production slot bridge tests."""
import unittest
from economic_shadow_live import inspect_production_slots

def row():
    return dict(slot_start="2026-10-13 12:00:00",
        price_buy_pln_kwh=0.7, price_sell_pln_kwh=0.1,
        forecast_pv_total_kwh=0.8, forecast_load_kwh=0.5,
        planned_buy_kwh=0, planned_sell_kwh=0,
        planned_pv_export_kwh=0.1, planned_pv_to_bat_kwh=0.2,
        planned_pv_to_cwu_kwh=0, planned_pv_to_ev_kwh=0,
        planned_pv_curtail_kwh=0, soc_start_plan_pct=50,
        soc_end_plan_pct=51, soc_required_pct=40,
        market_window="NEUTRAL")

class LiveShadowTests(unittest.TestCase):
    def test_real_data_still_not_comparable(self):
        result=inspect_production_slots([row()])
        self.assertEqual(result["status"],"DATA_READY_MODEL_INCOMPLETE")
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["delta_pln"])
        self.assertEqual(result["production_totals_kwh"]["forecast_pv_total_kwh"],0.8)
    def test_missing_data_fails_closed(self):
        r=row();del r["planned_pv_export_kwh"]
        self.assertEqual(inspect_production_slots([r])["status"],"INVALID_SOURCE")
    def test_nan_fails_closed(self):
        r=row();r["forecast_load_kwh"]=float("nan")
        self.assertEqual(inspect_production_slots([r])["status"],"INVALID_SOURCE")
    def test_no_data(self):
        self.assertEqual(inspect_production_slots([])["status"],"NO_DATA")

if __name__=="__main__":
    unittest.main()
