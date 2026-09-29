"""Authenticated persistence boundary for the external read-only AI worker."""
from __future__ import annotations

import json
import uuid
from typing import Any

from observer_details import format_observer_todo


MAX_FINDINGS = 20
MAX_EVIDENCE = 12
SEVERITIES = {"INFO", "WARNING", "CRITICAL"}


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field}_INVALID")
    return value.strip()


def _validate_finding(item: Any, index: int) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"FINDING_{index}_INVALID")
    severity = str(item.get("severity", "WARNING")).upper()
    if severity not in SEVERITIES:
        raise ValueError(f"FINDING_{index}_SEVERITY_INVALID")
    evidence = item.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise ValueError(f"FINDING_{index}_EVIDENCE_INVALID")
    # Ensure evidence is JSON serializable and bounded before persistence.
    try:
        evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raise ValueError(f"FINDING_{index}_EVIDENCE_INVALID") from None
    if len(evidence_json) > 12_000:
        raise ValueError(f"FINDING_{index}_EVIDENCE_TOO_LARGE")
    return {
        "metric": _text(item.get("metric"), f"FINDING_{index}_METRIC", 120),
        "title": _text(item.get("title"), f"FINDING_{index}_TITLE", 250),
        "severity": severity,
        "error": _text(item.get("error"), f"FINDING_{index}_ERROR", 2000),
        "conclusion": _text(item.get("conclusion"), f"FINDING_{index}_CONCLUSION", 2000),
        "recommendation": _text(item.get("recommendation"), f"FINDING_{index}_RECOMMENDATION", 2000),
        "evidence": evidence,
    }


def submit_observer_result(agent_id: str, payload: dict, *, db, now,
                           create_todo, reconcile_todos, record_event) -> dict:
    """Persist a worker analysis as an Observer run and detailed TODOs only."""
    agent_id = _text(agent_id, "AGENT_ID", 80)
    if not isinstance(payload, dict):
        raise ValueError("PAYLOAD_OBJECT_REQUIRED")
    source_ref = _text(payload.get("source_ref"), "SOURCE_REF", 100)
    summary = _text(payload.get("summary"), "SUMMARY", 4000)
    scope = payload.get("analysis_scope")
    if not isinstance(scope, dict):
        raise ValueError("ANALYSIS_SCOPE_INVALID")
    try:
        scope_json = json.dumps(scope, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raise ValueError("ANALYSIS_SCOPE_INVALID") from None
    if len(scope_json) > 4000:
        raise ValueError("ANALYSIS_SCOPE_TOO_LARGE")
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS:
        raise ValueError("FINDINGS_INVALID")
    findings = [_validate_finding(item, index) for index, item in enumerate(raw_findings)]
    run_id = str(uuid.uuid4())
    decision = "ESCALATE" if any(item["severity"] == "CRITICAL" for item in findings) else \
               "WATCH" if findings else "ACCEPT"
    result = {
        "contract": "EMS_AI_OBSERVER_WORKER_0_39_12",
        "mode": "SHADOW_READ_ONLY",
        "decision": decision,
        "summary": summary,
        "analysis_scope": scope,
        "findings": findings,
        "external_model_called": True,
        "worker_id": agent_id,
        "forbidden_operations": ["PLAN_WRITE", "PPD_WRITE", "COMMAND_WRITE", "HA_SERVICE_CALL", "SETTINGS_WRITE"],
    }
    prompt = {
        "contract": "EMS_AI_OBSERVER_WORKER_0_39_12",
        "mode": "SHADOW_READ_ONLY",
        "source_analytics_run_id": source_ref,
        "analysis_scope": scope,
        "allowed_operations": ["READ_CONTEXT", "WRITE_OBSERVER_FINDINGS"],
    }
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT run_id FROM ems_gpt_core_analytics_runs
          WHERE run_id=%s AND status='COMPLETED'""", (source_ref,))
        if not cur.fetchone():
            conn.rollback()
            raise ValueError("SOURCE_ANALYTICS_RUN_NOT_FOUND")
        cur.execute("""SELECT run_id,result_json FROM ems_gpt_core_ai_runs
          WHERE role_name='EMS_AGENT_OBSERVER' AND source_ref=%s""", (source_ref,))
        existing = cur.fetchone()
        if existing:
            run_id = existing["run_id"]
            try:
                prior = json.loads(existing.get("result_json") or "{}")
                findings = [_validate_finding(item, index)
                            for index, item in enumerate(prior.get("findings", []))]
                scope = prior.get("analysis_scope", scope)
                decision = prior.get("decision", decision)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        else:
            cur.execute("""INSERT INTO ems_gpt_core_ai_runs
              (run_id,role_name,source_ref,started_at,completed_at,status,prompt_json,result_json,auto_score,decision)
              VALUES(%s,'EMS_AGENT_OBSERVER',%s,%s,%s,'COMPLETED',%s,%s,NULL,%s)""",
              (run_id, source_ref, now, now,
               json.dumps(prompt, ensure_ascii=False, default=str),
               json.dumps(result, ensure_ascii=False, default=str), decision))
        conn.commit()

    active_titles = []
    for finding in findings:
        title = "AI Observer: " + finding["title"]
        active_titles.append(title)
        details = format_observer_todo(
            scope=scope, metric=finding["metric"], severity=finding["severity"],
            error=finding["error"], conclusion=finding["conclusion"],
            recommendation=finding["recommendation"], evidence=finding["evidence"],
            run_id=run_id, source_ref=source_ref, worker_id=agent_id)
        create_todo("ai_agent", title, details,
                    finding["severity"], run_id,
                    require_consecutive_days=finding["severity"] != "CRITICAL")
    reconcile_todos(active_titles, module_name="ai_agent")
    record_event("ai_observer_worker_completed", "ai_observer", {
        "run_id": run_id, "source_ref": source_ref, "decision": decision,
        "finding_count": len(findings), "worker_id": agent_id,
    }, "CRITICAL" if decision == "ESCALATE" else "INFO")
    return {"status": "DUPLICATE" if existing else "COMPLETED", "run_id": run_id, "source_ref": source_ref,
            "decision": decision, "finding_count": len(findings)}
