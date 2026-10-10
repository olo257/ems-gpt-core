import json
import threading
import logging
import sys
import unittest
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_service import ApiAdapters, build_handler


class RejectedPlansApiTests(unittest.TestCase):
    def test_latest_rejected_attempt_is_flattened_for_read_only_view(self):
        attempt = {
            "attempt_id": "attempt-1",
            "run_id": "run-1",
            "run_type": "scheduled",
            "status": "REJECTED",
            "attempted_at": datetime(2026, 10, 10, 5, 51),
            "failed_stage": "DISPATCH",
            "failure_index": 0,
            "failed_slot": datetime(2026, 10, 10, 5, 45),
            "initial_soc_pct": 26.0,
            "failure_reason": "SOC_REQUIRED_VIOLATION:0:24.2<26.75",
            "candidate_rows_json": json.dumps([
                {"slot_start": "2026-10-10 05:45:00", "soc_end_plan_pct": 24.2}
            ]),
        }

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, *args): pass
            def fetchone(self): return attempt

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
            app_name="EMS-GPT Core", app_version="0.40.3", state={},
            lock=threading.RLock(), log=logging.getLogger("test"),
            db=db, local_now=lambda: datetime(2026, 10, 10, 5, 51),
            slot_start=lambda: datetime(2026, 10, 10, 5, 45), html="",
        )
        base_handler = build_handler(ApiAdapters(**values))

        class CaptureHandler(base_handler):
            def __init__(self):
                self.path = "/api/rejected-plans"

            def json(self, payload, status=200):
                self.payload = payload
                self.status = status
                return payload

        handler = CaptureHandler()
        handler.do_GET()
        payload = handler.payload

        self.assertEqual(handler.status, 200)
        self.assertEqual(payload["view"], "rejected-plans")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["rows"][0]["status"], "REJECTED")
        self.assertEqual(payload["rows"][0]["failed_stage"], "DISPATCH")
        self.assertEqual(payload["rows"][0]["failure_reason"], attempt["failure_reason"])
        self.assertEqual(payload["rows"][0]["soc_end_plan_pct"], 24.2)


if __name__ == "__main__":
    unittest.main()
