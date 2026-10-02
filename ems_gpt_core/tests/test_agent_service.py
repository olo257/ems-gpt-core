from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import datetime

import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_service


class Cursor:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.executed = []
        self.rowcount = 0
        self.row = None

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        if self.responses:
            self.row = self.responses.pop(0)
        self.rowcount = 1

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def fetchall(self):
        value, self.row = self.row or [], None
        return value


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


class AgentServiceTests(unittest.TestCase):
    def test_operator_message_is_bounded_and_queued(self):
        cursor = Cursor()
        result = agent_service.submit_message(
            "  Sprawdź rozbieżność PV  ", "maciek", db=Database(cursor),
            now=datetime(2026, 9, 29, 8, 0),
        )
        sql, values = cursor.executed[0]
        self.assertIn("INSERT INTO ems_gpt_core_agent_messages", sql)
        self.assertEqual(values[3], "Sprawdź rozbieżność PV")
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(result["thread_id"].count("-"), 4)

    def test_oversize_or_blank_messages_are_rejected_before_database_write(self):
        cursor = Cursor()
        with self.assertRaisesRegex(ValueError, "MESSAGE_EMPTY"):
            agent_service.submit_message("  ", "operator", db=Database(cursor), now=datetime.now())
        with self.assertRaisesRegex(ValueError, "MESSAGE_TOO_LONG"):
            agent_service.submit_message("x" * 4001, "operator", db=Database(cursor), now=datetime.now())
        self.assertEqual(cursor.executed, [])

    def test_agent_claim_is_explicit_and_bounded(self):
        cursor = Cursor([[{"message_id": "a", "thread_id": "t", "message_text": "analiza"}]])
        rows = agent_service.claim_messages("agent-1", db=Database(cursor), limit=500)
        self.assertEqual(rows[0]["message_id"], "a")
        self.assertIn("LIMIT %s FOR UPDATE", cursor.executed[0][0])
        self.assertEqual(cursor.executed[0][1], (20,))
        self.assertIn("SET status='CLAIMED',claimed_by=%s,claimed_at=NOW(6)", cursor.executed[1][0])

    def test_context_is_read_only_and_includes_future_history_and_observer(self):
        future = [{"slot_start": "future"}]
        history = [{"slot_start": "past"}]
        analytics = [{"run_id": "analytics-1"}]
        observer = [{"run_id": "observer-1"}]
        todos = [{"todo_id": "todo-1", "status": "ACCEPTED", "details": "evidence"}]
        cursor = Cursor([future, history, analytics, observer, todos])
        result = agent_service.read_context(
            db=Database(cursor), now=datetime(2026, 9, 29, 8, 0),
            state={"modules": {"planner": "RUNNING"}}, limit=1000,
        )
        self.assertEqual(len(cursor.executed), 5)
        self.assertTrue(all(sql.lstrip().startswith("SELECT") for sql, _ in cursor.executed))
        self.assertEqual(result["future_slots"], future)
        self.assertEqual(result["completed_slots"], history)
        self.assertEqual(result["analytics_runs"], analytics)
        self.assertEqual(result["observer_runs"], observer)
        self.assertEqual(result["todo_items"], todos)
        self.assertEqual(result["todo_source_of_truth"], "ems_gpt_core_todo")
        self.assertIn("READ_TODO", result["permissions"])
        self.assertEqual(result["mode"], "READ_ONLY_ANALYSIS")
        self.assertIn("COMMAND_WRITE", result["forbidden"])
        self.assertEqual(cursor.executed[0][1][1], 96)
        self.assertEqual(cursor.executed[1][1][1], 28 * 96)
        self.assertIn("status IN ('WATCHING','OPEN','SUGGESTED','ACCEPTED','REJECTED','RESOLVED')",
                      cursor.executed[4][0])
        self.assertIn("CASE WHEN status='ACCEPTED' THEN 0", cursor.executed[4][0])
        self.assertEqual(cursor.executed[4][1], (25,))

    def test_todo_details_are_bounded_in_project_context(self):
        long_todo = {"todo_id": "todo-1", "status": "OPEN", "details": "x" * 2000}
        cursor = Cursor([[], [], [], [], [long_todo]])
        result = agent_service.read_context(db=Database(cursor), now=datetime.now(),
                                            state={}, limit=1)
        self.assertEqual(len(result["todo_items"][0]["details"]), agent_service.MAX_TODO_DETAIL_CHARS)

    def test_context_can_request_a_seven_day_history_without_changing_default(self):
        cursor = Cursor([[], [], [], []])
        result = agent_service.read_context(
            db=Database(cursor), now=datetime(2026, 10, 2, 12, 0),
            state={}, history_days=7,
        )
        self.assertEqual(result["history_days"], 7)
        self.assertEqual(cursor.executed[1][1][0], datetime(2026, 9, 25, 12, 0))
        self.assertEqual(cursor.executed[1][1][1], 7 * 96)

    def test_reply_requires_matching_claim_and_completes_thread(self):
        cursor = Cursor([{"thread_id": "thread-1"}])
        result = agent_service.submit_reply(
            "message-1", "agent-1", "Dane wskazują na rozbieżność PV.",
            db=Database(cursor), now=datetime(2026, 9, 29, 8, 0),
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertIn("INSERT INTO ems_gpt_core_agent_messages", cursor.executed[1][0])
        self.assertIn("SET status='COMPLETED'", cursor.executed[2][0])

    def test_reply_rejects_message_claimed_by_different_worker(self):
        cursor = Cursor([None])
        db = Database(cursor)
        with self.assertRaisesRegex(PermissionError, "MESSAGE_NOT_CLAIMED_BY_AGENT"):
            agent_service.submit_reply("message-1", "agent-2", "odpowiedź",
                                       db=db, now=datetime.now())
        self.assertEqual(db.connection.rollbacks, 1)
        self.assertEqual(len(cursor.executed), 1)

    def test_agent_module_has_no_control_or_planner_dependencies(self):
        tree = ast.parse((ROOT / "agent_service.py").read_text(encoding="utf-8"))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse({"planner_service", "executor_service", "ppd_service", "ha_gateway_service"} & imports)
        self.assertFalse({"run_planner", "run_ppd", "stage_executor_commands", "ha_service_response"} & calls)


if __name__ == "__main__":
    unittest.main()
