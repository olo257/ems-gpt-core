import logging
import sys
import threading
import unittest
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_service import ApiAdapters, build_handler


class ActiveSlotBalanceApiTests(unittest.TestCase):
    def test_status_includes_active_slot_balance(self):
        row = {
            "slot_start": datetime(2026, 10, 10, 11, 45),
            "forecast_pv_total_kwh": 1.0,
            "forecast_load_kwh": 0.5,
            "forecast_heat_pump_load_kwh": 0.2,
            "planned_battery_discharge_kwh": 0.0,
            "planned_buy_kwh": 0.0,
            "planned_battery_charge_kwh": 0.0,
            "planned_sell_kwh": 0.0,
            "planned_pv_to_bat_kwh": 0.3,
            "planned_pv_to_cwu_kwh": 0.0,
            "planned_pv_to_ev_kwh": 0.0,
            "planned_pv_export_kwh": 0.0,
        }

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, *args): pass
            def fetchone(self): return row

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): return Cursor()

        @contextmanager
        def db():
            yield Connection()

        values = {}
        for item in fields(ApiAdapters):
            if item.name == "icon_path":
                values[item.name] = item.default
            else:
                values[item.name] = lambda *args, **kwargs: {}
        values.update(
            app_name="EMS-GPT Core", app_version="0.40.6", state={},
            lock=threading.RLock(), log=logging.getLogger("test"),
            db=db, local_now=lambda: datetime(2026, 10, 10, 11, 45),
            slot_start=lambda: datetime(2026, 10, 10, 11, 45), html="",
            settings_payload=lambda: {
                "battery_charge_efficiency": {"value": 0.9},
                "battery_discharge_efficiency": {"value": 0.98},
            },
        )
        handler_type = build_handler(ApiAdapters(**values))

        class CaptureHandler(handler_type):
            def __init__(self):
                self.path = "/api/status"

            def json(self, payload, status=200):
                self.payload = payload
                self.status = status
                return payload

        handler = CaptureHandler()
        handler.do_GET()

        self.assertEqual(handler.status, 200)
        balance = handler.payload["active_slot_balance"]
        self.assertIsNotNone(balance)
        self.assertAlmostEqual(balance["difference_kwh"], 0.0)
        self.assertEqual(balance["total_load_kwh"], 0.7)


if __name__ == "__main__":
    unittest.main()
