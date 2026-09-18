from datetime import datetime
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scheduler_service import (
    publish_current_slot_prices,
    rce_event_keys,
    should_run_slot_replan,
    update_telemetry_health,
)


class ImmediateLock:
    def __enter__(self): return self
    def __exit__(self, *args): return False


class PriceCursor:
    def __init__(self, row): self.row = row
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, query, params): self.query, self.params = query, params
    def fetchone(self): return self.row


class PriceConnection:
    def __init__(self, row): self.row = row
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return PriceCursor(self.row)


class RceRestoreKeysTests(unittest.TestCase):
    def test_current_prices_come_from_the_same_active_slot(self):
        writes = []
        result = publish_current_slot_prices(
            lambda: PriceConnection({
                "price_buy_pln_kwh": 1.1524,
                "price_sell_pln_kwh": 0.5624,
            }),
            datetime(2026, 9, 15, 12, 15),
            lambda domain, service, payload: writes.append(
                (domain, service, payload)) or [],
        )

        self.assertEqual(result["status"], "OK")
        self.assertEqual(writes, [
            ("input_number", "set_value", {
                "entity_id": "input_number.ems_gpt_cena_zakupu_biezaca",
                "value": 1.152,
            }),
            ("input_number", "set_value", {
                "entity_id": "input_number.ems_gpt_cena_sprzedazy_biezaca",
                "value": 0.562,
            }),
        ])

    def test_missing_active_slot_price_does_not_publish_partial_pair(self):
        writes = []
        result = publish_current_slot_prices(
            lambda: PriceConnection({
                "price_buy_pln_kwh": 1.152,
                "price_sell_pln_kwh": None,
            }),
            datetime(2026, 9, 15, 12, 15),
            lambda *args: writes.append(args),
        )

        self.assertEqual(result["status"], "MISSING_SLOT_PRICE")
        self.assertEqual(writes, [])

    def test_before_14_accepts_previous_day_next_marker(self):
        self.assertEqual(rce_event_keys(datetime(2026, 9, 13, 5, 30)), (
            "RCE_2026-09-13_TODAY", "RCE_2026-09-12_NEXT"))

    def test_after_14_uses_current_day_next_marker(self):
        self.assertEqual(rce_event_keys(datetime(2026, 9, 13, 14, 0)), (
            "RCE_2026-09-13_NEXT",))

    def test_replan_runs_once_for_each_settled_slot_with_complete_rce(self):
        slot = datetime(2026, 9, 15, 15, 15)
        self.assertTrue(should_run_slot_replan(
            datetime(2026, 9, 15, 15, 17), slot,
            datetime(2026, 9, 15, 15, 14), True))
        self.assertFalse(should_run_slot_replan(
            datetime(2026, 9, 15, 15, 18), slot,
            datetime(2026, 9, 15, 15, 17), True))

    def test_replan_waits_for_rce_and_forecast_refresh_blackout(self):
        slot = datetime(2026, 9, 15, 14, 0)
        self.assertFalse(should_run_slot_replan(
            datetime(2026, 9, 15, 14, 2), slot, None, False))
        midnight = datetime(2026, 9, 16, 0, 0)
        self.assertFalse(should_run_slot_replan(
            datetime(2026, 9, 16, 0, 2), midnight, None, True))

    def test_missing_telemetry_is_not_masked_by_engine_heartbeat(self):
        state = {"last_telemetry_success": "2026-09-13T21:40:00+00:00",
                 "telemetry_consecutive_failures": 1}
        result = update_telemetry_health(
            state, ImmediateLock(), False,
            datetime.fromisoformat("2026-09-13T21:45:01+00:00"), 120, 300)
        self.assertEqual(result["readiness"], "STALE_TELEMETRY")
        self.assertEqual(state["status"] if "status" in state else "unchanged", "unchanged")
        self.assertEqual(state["telemetry_consecutive_failures"], 2)

    def test_telemetry_recovery_resets_failure_counter(self):
        state = {"last_telemetry_success": None, "telemetry_consecutive_failures": 9}
        now = datetime.fromisoformat("2026-09-14T04:22:37+00:00")
        result = update_telemetry_health(state, ImmediateLock(), True, now)
        self.assertEqual(result["readiness"], "READY")
        self.assertEqual(state["telemetry_consecutive_failures"], 0)


if __name__ == "__main__":
    unittest.main()
