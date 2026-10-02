from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ems_gpt_ai_observer_worker"))

from worker import AgentWorker, ChatCompletionsClient, compact_context, supervisory_review


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, messages, max_tokens=1200, json_mode=False):
        self.calls.append((messages, max_tokens, json_mode))
        return self.response


class FakeCore:
    def __init__(self, context):
        self._context = context
        self.saved = []

    def context(self):
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
            "mode": "READ_ONLY_ANALYSIS", "history_days": 28,
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
        context = {"history_days": 28, "completed_slots": [{"slot_start": "2026-09-29T12:00:00"}],
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


if __name__ == "__main__":
    unittest.main()
