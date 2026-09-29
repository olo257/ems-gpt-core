from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observer_worker_service import submit_observer_result


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class Database:
    def __init__(self, cursor):
        self.connection = Connection(cursor)

    def __call__(self):
        return self.connection


class ObserverWorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "source_ref": "analytics-1", "summary": "Wykryto eksport przy cenie zerowej.",
            "analysis_scope": {"history_days": 28, "completed_slots": 500, "future_slots": 96},
            "findings": [{"metric": "export_at_nonpositive_price", "title": "Eksport po cenie 0",
                          "severity": "CRITICAL", "error": "Slot 12:00: cena 0, eksport 0.2 kWh.",
                          "conclusion": "Plan i wykonanie łamały próg ceny.",
                          "recommendation": "Sprawdź ochronę SELL_PV.",
                          "evidence": [{"slot_start": "2026-09-29T12:00:00", "price": 0}]}],
        }

    def test_worker_result_is_saved_to_observer_and_detailed_todo(self):
        cursor = Cursor([{"run_id": "analytics-1"}, None])
        db = Database(cursor)
        todos, reconciled, events = [], [], []
        result = submit_observer_result(
            "observer-worker", self.payload, db=db, now=datetime(2026, 9, 29, 12),
            create_todo=lambda *args, **kwargs: todos.append((args, kwargs)),
            reconcile_todos=lambda titles, **kwargs: reconciled.append((titles, kwargs)),
            record_event=lambda *args: events.append(args),
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIn("INSERT INTO ems_gpt_core_ai_runs", cursor.executed[2][0])
        self.assertEqual(db.connection.commits, 1)
        self.assertEqual(todos[0][0][0], "ai_agent")
        todo_details = todos[0][0][2]
        for section in ("ZAKRES ANALIZY", "WYKRYTY PROBLEM", "DOWODY", "WNIOSEK", "CO SPRAWDZIĆ"):
            self.assertIn(section, todo_details)
        self.assertIn("2026-09-29T12:00:00", todo_details)
        self.assertFalse(todos[0][1]["require_consecutive_days"])
        self.assertEqual(reconciled[0][1], {"module_name": "ai_agent"})
        self.assertEqual(events[0][-1], "CRITICAL")

    def test_missing_analytics_reference_rejects_result_without_writes(self):
        cursor = Cursor([None])
        db = Database(cursor)
        with self.assertRaisesRegex(ValueError, "SOURCE_ANALYTICS_RUN_NOT_FOUND"):
            submit_observer_result("observer-worker", self.payload, db=db,
                                   now=datetime.now(), create_todo=lambda *_a, **_k: None,
                                   reconcile_todos=lambda *_a, **_k: None,
                                   record_event=lambda *_a: None)
        self.assertEqual(len(cursor.executed), 1)
        self.assertEqual(db.connection.rollbacks, 1)

    def test_findings_are_bounded_and_severity_is_allowlisted(self):
        payload = {**self.payload, "findings": [{**self.payload["findings"][0], "severity": "CONTROL"}]}
        with self.assertRaisesRegex(ValueError, "SEVERITY_INVALID"):
            submit_observer_result("observer-worker", payload, db=Database(Cursor([])),
                                   now=datetime.now(), create_todo=lambda *_a, **_k: None,
                                   reconcile_todos=lambda *_a, **_k: None,
                                   record_event=lambda *_a: None)


if __name__ == "__main__":
    unittest.main()
