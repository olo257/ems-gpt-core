from __future__ import annotations

import json
import logging
import pathlib
import sys
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime
from http.server import ThreadingHTTPServer
from threading import RLock, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api_service import ApiAdapters, build_handler


class TodoCursor:
    def __init__(self):
        self.executed = []
        self.rows = [{"todo_id": "todo-accepted", "status": "ACCEPTED"}]

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TodoDatabase:
    def __init__(self):
        self.cursor_obj = TodoCursor()

    @contextmanager
    def __call__(self):
        yield self

    def cursor(self):
        return self.cursor_obj


class AgentApiAuthTests(unittest.TestCase):
    def setUp(self):
        self.claimed = []
        self.observer_results = []
        self.db = TodoDatabase()
        adapters = ApiAdapters(
            app_name="EMS-GPT Core", app_version="test", state={}, lock=RLock(),
            log=logging.getLogger("agent-api-test"), db=self.db,
            local_now=datetime.now, slot_start=datetime.now,
            settings_payload=lambda: {}, update_operational_settings=lambda *_: {},
            update_executor_mode=lambda *_: {}, update_process_override=lambda *_: {},
            stage_executor_commands=lambda: {}, acknowledge_command=lambda *_: {},
            run_serialized=lambda *_: {}, run_planner=lambda: {}, run_ppd=lambda *_: {},
            refresh_rce=lambda *_: {}, complete_rce_cycle=lambda *_: {},
            refresh_pv_forecast=lambda: {}, refresh_weather_forecast=lambda: {},
            run_analytics=lambda: {}, run_ai_observer=lambda *_: {},
            generate_diagnostic_report=lambda *_: {}, review_todo=lambda *_: {},
            database_audit=lambda: {}, database_catalog=lambda: {},
            slot_column_audit=lambda: {}, html="", agent_api_token="secret-test-token",
            agent_list_messages=lambda *_: [],
            agent_claim_messages=lambda agent_id, limit: self.claimed.append(agent_id) or [],
            agent_read_context=lambda *_: {"mode": "READ_ONLY_ANALYSIS"},
            agent_submit_reply=lambda *_: {}, agent_submit_message=lambda *_: {},
            agent_submit_observer_result=lambda agent_id, payload: self.observer_results.append(
                (agent_id, payload)) or {"status": "COMPLETED", "run_id": "observer-run-1"},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(adapters))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_agent_endpoints_reject_requests_without_exact_bearer_token(self):
        for path in ("/api/agent/inbox", "/api/agent/context"):
            with self.subTest(path=path):
                try:
                    urlopen(self.base + path, timeout=2)
                    self.fail("unauthorized request unexpectedly succeeded")
                except HTTPError as exc:
                    self.assertEqual(exc.code, 401)
        self.assertEqual(self.claimed, [])

    def test_agent_context_is_available_to_authenticated_worker(self):
        req = Request(self.base + "/api/agent/context",
                      headers={"Authorization": "Bearer secret-test-token"})
        with urlopen(req, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"READ_ONLY_ANALYSIS", response.read())

    def test_authenticated_inbox_claims_as_stable_agent_identity(self):
        req = Request(self.base + "/api/agent/inbox",
                      headers={"Authorization": "Bearer secret-test-token",
                               "X-EMS-Agent-ID": "worker-test"})
        with urlopen(req, timeout=2) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.claimed, ["worker-test"])

    def test_project_connector_can_read_accepted_todos_by_status(self):
        with urlopen(self.base + "/api/todo?status=ACCEPTED&limit=4", timeout=2) as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
        self.assertEqual(payload["rows"][0]["todo_id"], "todo-accepted")
        sql, params = self.db.cursor_obj.executed[0]
        self.assertIn("WHERE status=%s ORDER BY created_at DESC LIMIT %s", sql)
        self.assertEqual(params, ("ACCEPTED", 4))

    def test_todo_status_filter_rejects_unknown_status(self):
        try:
            urlopen(self.base + "/api/todo?status=EXECUTE", timeout=2)
            self.fail("unknown TODO status unexpectedly succeeded")
        except HTTPError as exc:
            self.assertEqual(exc.code, 400)
        self.assertEqual(self.db.cursor_obj.executed, [])

    def test_observer_result_endpoint_requires_worker_token_and_saves_run(self):
        payload = {"source_ref": "analytics-1", "summary": "OK", "findings": [],
                   "analysis_scope": {"history_days": 28}}
        try:
            urlopen(Request(self.base + "/api/agent/observer-result", data=b"{}",
                            headers={"Content-Type": "application/json"}, method="POST"), timeout=2)
            self.fail("unauthenticated observer result unexpectedly succeeded")
        except HTTPError as exc:
            self.assertEqual(exc.code, 401)
        req = Request(self.base + "/api/agent/observer-result",
                      data=json.dumps(payload).encode(),
                      headers={"Authorization": "Bearer secret-test-token",
                               "X-EMS-Agent-ID": "observer-worker",
                               "Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=2) as response:
            self.assertEqual(response.status, 201)
        self.assertEqual(self.observer_results, [("observer-worker", payload)])


if __name__ == "__main__":
    unittest.main()
