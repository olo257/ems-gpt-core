from __future__ import annotations

import json
import io
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ems_gpt_ai_observer_worker"))

from worker import (AgentWorker, ChatCompletionsClient, compact_context,
                    supervisory_review, _new_completed_slots,
                    suppress_reviewed_findings, CoreClient, JsonHttpClient,
                    HttpRequestError, safe_error_code)


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, messages, max_tokens=1200, json_mode=False):
        self.calls.append((messages, max_tokens, json_mode))
        return self.response


class SequenceModel(FakeModel):
    def __init__(self, responses):
        super().__init__("")
        self.responses = list(responses)

    def complete(self, messages, max_tokens=1200, json_mode=False):
        self.calls.append((messages, max_tokens, json_mode))
        return self.responses.pop(0)


class FakeCore:
    def __init__(self, context):
        self._context = context
        self.saved = []
        self.context_days = []

    def context(self, history_days=28):
        self.context_days.append(history_days)
        return self._context

    def submit_observer_result(self, payload):
        self.saved.append(payload)
        return {"status": "COMPLETED", "run_id": "observer-run-1"}


class FakeHttp:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.result


class AgentWorkerTests(unittest.TestCase):
    def test_context_compaction_preserves_daily_and_current_slot_evidence(self):
        context = {
            "mode": "READ_ONLY_ANALYSIS", "history_days": 7,
            "completed_slots": [
                {"slot_start": datetime(2026, 9, 28, 12), "forecast_pv_total_kwh": 1.0,
                 "actual_pv_total_kwh": 2.0, "price_sell_pln_kwh": 0.0, "planned_sell_kwh": .1},
                {"slot_start": datetime(2026, 9, 29, 12), "forecast_pv_total_kwh": 1.5,
                 "actual_pv_total_kwh": 2.0},
            ],
            "future_slots": [{"slot_start": "future"}],
            "analytics_runs": [{"run_id": "analytics-1"}], "observer_runs": [],
            "todo_items": [{"todo_id": "todo-1", "status": "ACCEPTED"}],
            "todo_count": 1, "todo_source_of_truth": "ems_gpt_core_todo",
        }
        compact = compact_context(context)
        self.assertEqual(len(compact["history_daily_aggregates"]), 2)
        self.assertEqual(compact["history_daily_aggregates"][0]["nonpositive_price_export_slots"][0]["planned_sell_kwh"], .1)
        self.assertEqual(len(compact["latest_completed_day_detail"]), 1)
        self.assertEqual(compact["future_slots"], context["future_slots"])
        self.assertEqual(compact["todo_items"], context["todo_items"])
        self.assertEqual(compact["todo_source_of_truth"], "ems_gpt_core_todo")

    def test_chat_completions_client_uses_separate_model_key(self):
        http = FakeHttp({"choices": [{"message": {"content": "analiza"}}]})
        model = ChatCompletionsClient("http://llm/v1", "local-model", "provider-key", http=http)
        self.assertEqual(model.complete([{"role": "user", "content": "test"}]), "analiza")
        method, url, kwargs = http.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://llm/v1/chat/completions")
        self.assertEqual(kwargs["token"], "provider-key")

    def test_chat_completions_client_requests_json_object_when_needed(self):
        http = FakeHttp({"choices": [{"message": {"content": '{"summary":"ok","findings":[]}'} }]})
        model = ChatCompletionsClient("http://llm/v1", "model", "key", http=http)
        model.complete([{"role": "user", "content": "json"}], json_mode=True)
        self.assertEqual(http.requests[0][2]["payload"]["response_format"], {"type": "json_object"})

    def test_supervisory_response_requires_structured_findings(self):
        model = FakeModel("```json\n" + json.dumps({"summary": "Wszystko zgodne.", "findings": []}) + "\n```")
        context = {"completed_slots": [], "future_slots": [], "analytics_runs": []}
        result = supervisory_review(model, context)
        self.assertEqual(result, {"summary": "Wszystko zgodne.", "findings": []})
        self.assertIn("SHADOW_READ_ONLY", model.calls[0][0][0]["content"])
        self.assertTrue(model.calls[0][2])

    def test_supervisory_review_repairs_malformed_json_once(self):
        model = SequenceModel([
            '{"summary":"Eksport wykryty", "findings":[}',
            json.dumps({"summary": "Eksport wykryty", "findings": []}),
        ])
        result = supervisory_review(model, {"completed_slots": [], "future_slots": [], "analytics_runs": []})
        self.assertEqual(result, {"summary": "Eksport wykryty", "findings": []})
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(model.calls[0][2])
        self.assertTrue(model.calls[1][2])
        self.assertIn("Popraw odpowiedź Observera", model.calls[1][0][1]["content"])

    def test_supervisory_review_reports_invalid_json_after_single_repair(self):
        model = SequenceModel(["not json", "still not json"])
        result = supervisory_review(model, {"completed_slots": [], "future_slots": [], "analytics_runs": []})
        self.assertIsNone(result)
        self.assertEqual(len(model.calls), 2)

    def test_supervisory_review_falls_back_when_provider_rejects_json_mode(self):
        class JsonModeUnsupportedModel(FakeModel):
            def complete(self, messages, max_tokens=1200, json_mode=False):
                self.calls.append((messages, max_tokens, json_mode))
                if json_mode:
                    raise RuntimeError("HTTP_400")
                return json.dumps({"summary": "Przegląd gotowy.", "findings": []})

        model = JsonModeUnsupportedModel("")
        result = supervisory_review(model, {"completed_slots": [], "future_slots": [], "analytics_runs": []})
        self.assertEqual(result, {"summary": "Przegląd gotowy.", "findings": []})
        self.assertEqual([call[2] for call in model.calls], [True, False])

    def test_periodic_analysis_is_saved_to_observer_once_per_analytics_run(self):
        context = {"history_days": 7, "completed_slots": [{"slot_start": "2026-09-29T12:00:00"}],
                   "future_slots": [{}], "analytics_runs": [{"run_id": "analytics-1", "status": "COMPLETED"}]}
        core = FakeCore(context)
        model = FakeModel(json.dumps({"summary": "Wykryto błąd eksportu.", "findings": [
            {"metric": "export_at_nonpositive_price", "title": "Eksport przy cenie zero",
             "severity": "CRITICAL", "error": "Eksport 0.2 kWh przy cenie 0 PLN/kWh.",
             "conclusion": "Warunek ceny nie zablokował eksportu.",
             "recommendation": "Sprawdź SELL_PV i wyłączanie surplus.",
             "evidence": [{"slot_start": "2026-09-29T12:00:00"}]},
        ]}))
        with tempfile.TemporaryDirectory() as temp:
            worker = AgentWorker(core, model, supervision_interval=60,
                                 state_path=str(pathlib.Path(temp) / "worker.json"))
            self.assertTrue(worker.review_once(now=100))
            self.assertFalse(worker.review_once(now=200))
        self.assertEqual(len(core.saved), 1)
        self.assertEqual(core.saved[0]["source_ref"], "analytics-1")
        self.assertEqual(core.saved[0]["analysis_scope"]["completed_slots"], 1)
        self.assertEqual(core.saved[0]["findings"][0]["severity"], "CRITICAL")
        self.assertEqual(len(model.calls), 1)
        self.assertTrue(model.calls[0][2])
        self.assertEqual(core.context_days, [7, 7])
        self.assertEqual(core.saved[0]["analysis_scope"]["history_days"], 7)
        self.assertEqual(core.saved[0]["analysis_scope"]["new_completed_slots"], 1)

    def test_history_watermark_excludes_previously_reviewed_slots(self):
        watermark = {"actual_recorded_at": "2026-10-01T12:05:00",
                     "slot_start": "2026-10-01T12:00:00"}
        rows = [
            {"slot_start": "2026-10-01T12:00:00", "actual_recorded_at": "2026-10-01T12:05:00"},
            {"slot_start": "2026-10-01T12:15:00", "actual_recorded_at": "2026-10-01T12:20:00"},
        ]
        self.assertEqual(_new_completed_slots(rows, watermark), [rows[1]])


class StartupAndRepeatFindingTests(unittest.TestCase):
    def test_rejected_and_resolved_findings_need_post_review_evidence(self):
        todos = [
            {"status": "REJECTED", "reviewed_at": "2026-10-01T12:00:00",
             "details": "Kontrola: błąd planu (planner_fault)"},
            {"status": "ACCEPTED", "reviewed_at": "2026-10-01T12:00:00",
             "details": "Kontrola: inna rzecz (accepted_metric)"},
        ]
        findings = [
            {"metric": "planner_fault", "evidence": [{"slot_start": "2026-09-30T10:00:00"}]},
            {"metric": "planner_fault", "evidence": [{"slot_start": "2026-10-02T10:00:00"}]},
            {"metric": "accepted_metric", "evidence": []},
        ]
        self.assertEqual(suppress_reviewed_findings(findings, todos), findings[1:])

    def test_core_liveness_probe_uses_public_live_endpoint(self):
        http = FakeHttp({"ok": True, "status": "PROCESS_RUNNING"})
        core = CoreClient("http://core:8099", "token", "worker", http=http)
        self.assertTrue(core.live()["ok"])
        self.assertEqual(http.requests[0][1], "http://core:8099/live")
        self.assertIsNone(http.requests[0][2].get("token"))


class WorkerFailureIsolationTests(unittest.TestCase):
    def test_http_diagnostics_preserve_status_and_known_code_without_provider_text(self):
        error = HTTPError("https://llm/v1", 429, "private message", {}, io.BytesIO(
            b'{"error":{"code":"insufficient_quota","message":"secret-key and EMS data"}}'))
        with patch("worker.urlopen", side_effect=error):
            with self.assertRaises(HttpRequestError) as caught:
                JsonHttpClient().request("POST", "https://llm/v1", token="secret-key")
        self.assertEqual(str(caught.exception), "HTTP_429")
        self.assertEqual(safe_error_code(caught.exception), "HTTP_429:insufficient_quota")

    def test_untrusted_error_codes_and_bodies_are_not_logged(self):
        for body in (b'{"error":{"code":"secret-key","message":"private"}}',
                     b'not json: secret-key', b'[]'):
            error = HTTPError("https://llm/v1", 401, "private", {}, io.BytesIO(body))
            with patch("worker.urlopen", side_effect=error):
                with self.assertRaises(HttpRequestError) as caught:
                    JsonHttpClient().request("GET", "https://llm/v1")
            self.assertEqual(safe_error_code(caught.exception), "HTTP_401")
        self.assertEqual(safe_error_code(RuntimeError("secret-key")), "RuntimeError")

    def test_mailbox_failure_does_not_skip_review_and_obeys_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = AgentWorker(None, None, state_path=str(pathlib.Path(temp) / "state.json"))
            with patch.object(worker, "process_one", side_effect=RuntimeError("HTTP_429")) as mailbox, \
                 patch.object(worker, "review_once", return_value=False) as review:
                with self.assertLogs("ems_agent_worker", level="ERROR") as logs:
                    worker.run_cycle(now=1000)
                worker.run_cycle(now=1030)
                self.assertEqual(mailbox.call_count, 1)
                self.assertEqual(review.call_count, 2)
                self.assertIn("mailbox failed: HTTP_429", logs.output[0])
                self.assertEqual(worker.retry_after["mailbox"], 1060)
                worker.run_cycle(now=1060)
                self.assertEqual(mailbox.call_count, 2)
                self.assertEqual(worker.retry_after["mailbox"], 1180)

    def test_review_failure_does_not_skip_mailbox_or_advance_interval(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = AgentWorker(None, None, state_path=str(pathlib.Path(temp) / "state.json"))
            worker.last_supervision = 123
            def failed_review(now):
                worker.last_supervision = now
                raise RuntimeError("HTTP_500")
            with patch.object(worker, "process_one", return_value=False) as mailbox, \
                 patch.object(worker, "review_once", side_effect=failed_review) as review:
                worker.run_cycle(now=1000)
                worker.run_cycle(now=1030)
                self.assertEqual(mailbox.call_count, 2)
                self.assertEqual(review.call_count, 1)
                self.assertEqual(worker.last_supervision, 123)
                review.side_effect = None
                review.return_value = False
                worker.run_cycle(now=1060)
                self.assertEqual(worker.failures["supervision"], 0)
                self.assertEqual(worker.retry_after["supervision"], 0)

    def test_invalid_observer_json_does_not_advance_persisted_watermark(self):
        context = {"completed_slots": [], "analytics_runs": [
            {"run_id": "analytics-2", "status": "COMPLETED"}]}
        with tempfile.TemporaryDirectory() as temp:
            worker = AgentWorker(FakeCore(context), SequenceModel(["bad", "bad"]),
                                 state_path=str(pathlib.Path(temp) / "state.json"))
            worker.state = {"last_supervision_source_ref": "analytics-1"}
            with self.assertRaisesRegex(RuntimeError, "OBSERVER_RESPONSE_INVALID"):
                worker.review_once(now=1000)
            self.assertEqual(worker.state, {"last_supervision_source_ref": "analytics-1"})
            self.assertEqual(worker.core.saved, [])

    def test_http_400_still_uses_json_mode_fallback_with_typed_error(self):
        class Model(FakeModel):
            def complete(self, messages, max_tokens=1200, json_mode=False):
                self.calls.append(json_mode)
                if json_mode:
                    raise HttpRequestError(400, "invalid_request_error")
                return '{"summary":"ok","findings":[]}'
        model = Model("")
        self.assertEqual(supervisory_review(model, {}), {"summary": "ok", "findings": []})
        self.assertEqual(model.calls, [True, False])


if __name__ == "__main__":
    unittest.main()
