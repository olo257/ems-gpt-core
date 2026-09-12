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


if __name__ == "__main__":
    unittest.main()
