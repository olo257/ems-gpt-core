import pathlib
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion_service import IngestionAdapters, build_ingestion


class EmptyConnection:
    def execute(self, sql, params=None):
        if not sql.lstrip().startswith("SELECT"):
            raise AssertionError("Missing sources must not overwrite forecasts")

    def fetchall(self):
        return []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class IngestionServiceTests(unittest.TestCase):
    def test_missing_sources_are_reported_without_writes(self):
        events = []

        @contextmanager
        def db():
            yield EmptyConnection()

        service = build_ingestion(IngestionAdapters(
            options={},
            timezone=ZoneInfo("Europe/Warsaw"),
            pv_forecast_entities={"today": ("pv1", "pv2"), "tomorrow": ("pv1_next", "pv2_next")},
            db=db,
            local_now=lambda: datetime(2026, 9, 12),
            slot_start=lambda: datetime(2026, 9, 12),
            canonical_slots_for_day=lambda day: [],
            number=lambda value: value,
            ha_state=lambda entity: None,
            ha_service_response=lambda *args, **kwargs: None,
            record_event=lambda *args: events.append(args),
        ))

        pv = service.refresh_pv_forecast()
        weather = service.refresh_weather_forecast()

        self.assertEqual(pv["today"]["status"], "WAITING_SOURCE")
        self.assertEqual(pv["tomorrow"]["status"], "WAITING_SOURCE")
        self.assertEqual(weather["status"], "WAITING_SOURCE")
        self.assertEqual([event[0] for event in events], [
            "pv_forecast_refreshed", "weather_forecast_refreshed"
        ])


    def test_rce_import_persists_price_windows_and_market_enum(self):
        import io
        import json
        from contextlib import contextmanager
        from datetime import date, timedelta
        from unittest.mock import patch

        target = date(2026, 9, 12)
        local_starts = [datetime(2026, 9, 12, 0, 0), datetime(2026, 9, 12, 0, 15)]
        calendar = [
            {
                "slot_start_local": start,
                "slot_id": f"slot-{index}",
                "slot_start_utc": start.replace(tzinfo=ZoneInfo("Europe/Warsaw")).astimezone(
                    ZoneInfo("UTC")).replace(tzinfo=None),
                "utc_offset_minutes": 120,
                "local_fold": 0,
                "slot_index_local": index,
            }
            for index, start in enumerate(local_starts)
        ]
        payload = {"value": [
            {"dtime": (start + timedelta(minutes=15)).replace(
                tzinfo=ZoneInfo("Europe/Warsaw")).isoformat(),
             "rce_pln": 1000 + index * 100, "publication_ts": None}
            for index, start in enumerate(local_starts)
        ]}

        class RecordingConnection:
            def __init__(self):
                self.statements = []
                self.rows = [
                    {"slot_start": start, "price_sell_pln_kwh": 1.0 + index * .1,
                     "price_buy_pln_kwh": 1.59 + index * .1}
                    for index, start in enumerate(local_starts)
                ]
                self.rowcount = 1

            def cursor(self):
                return self

            def execute(self, sql, params=None):
                self.statements.append((sql, params))
                self.rowcount = 1

            def fetchall(self):
                return self.rows

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        connection = RecordingConnection()

        @contextmanager
        def db():
            yield connection

        service = build_ingestion(IngestionAdapters(
            options={
                "purchase_margin_pln_kwh": .59,
                "battery_capacity_kwh": 15,
                "battery_min_soc_pct": 15,
                "battery_max_power_kw": 5,
                "slot_minutes": 15,
                "battery_charge_efficiency": .9,
                "battery_discharge_efficiency": .95,
                "deye_program_1_soc_pct": 10,
                "deye_program_2_soc_pct": 10,
                "deye_program_3_soc_pct": 10,
                "deye_program_4_soc_pct": 10,
                "deye_program_5_soc_pct": 10,
                "deye_program_6_soc_pct": 30,
            },
            timezone=ZoneInfo("Europe/Warsaw"),
            pv_forecast_entities={},
            db=db,
            local_now=lambda: datetime(2026, 9, 12, 12, 0),
            slot_start=lambda: datetime(2026, 9, 12, 12, 0),
            canonical_slots_for_day=lambda _day: calendar,
            number=lambda value: value,
            ha_state=lambda _entity: None,
            ha_service_response=lambda *args, **kwargs: None,
            record_event=lambda *args: None,
        ))

        response = io.BytesIO(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=response):
            result = service.refresh_rce(target)

        inserts = [item for item in connection.statements if "INSERT INTO ems_gpt_slots" in item[0]]
        window_updates = [item for item in connection.statements if "SET sale_window=%s,buy_window=%s,market_window=%s" in item[0]]
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(inserts), 2)
        self.assertEqual(len(window_updates), 2)
        self.assertIn("market_window", inserts[0][0])
        self.assertIn(inserts[0][1][6], {"BUY", "SELL", "NEUTRAL"})
        self.assertEqual(len(window_updates[0][1]), 4)


if __name__ == "__main__":
    unittest.main()
