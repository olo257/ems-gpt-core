from datetime import datetime
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scheduler_service import rce_event_keys, update_telemetry_health


class ImmediateLock:
    def __enter__(self): return self
    def __exit__(self, *args): return False


class RceRestoreKeysTests(unittest.TestCase):
    def test_before_14_accepts_previous_day_next_marker(self):
        self.assertEqual(rce_event_keys(datetime(2026, 9, 13, 5, 30)), (
            "RCE_2026-09-13_TODAY", "RCE_2026-09-12_NEXT"))

    def test_after_14_uses_current_day_next_marker(self):
        self.assertEqual(rce_event_keys(datetime(2026, 9, 13, 14, 0)), (
            "RCE_2026-09-13_NEXT",))

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
