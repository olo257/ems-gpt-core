"""Read-only EMS Observer service."""
from __future__ import annotations

import json
import uuid


def run_ai_observer(source_ref: str | None = None, *, options, db, create_todo, reconcile_observer_todos, record_event) -> dict:
    """Read-only shadow observer: persist evidence and suggestions, never mutate plan or controls."""
    if not bool(options.get("ai_observer_enabled", False)):
        return {"status": "DISABLED"}
    with db() as conn, conn.cursor() as cur:
        if source_ref:
            cur.execute("SELECT * FROM ems_gpt_core_analytics_runs WHERE run_id=%s AND status='COMPLETED'", (source_ref,))
        else:
            cur.execute("SELECT * FROM ems_gpt_core_analytics_runs WHERE status='COMPLETED' ORDER BY completed_at DESC LIMIT 1")
        analytics = cur.fetchone()
        if not analytics:
            return {"status": "WAITING_FOR_ANALYTICS"}
        source_ref = analytics["run_id"]
        cur.execute("SELECT * FROM ems_gpt_core_ai_runs WHERE role_name='EMS_OBSERVER' AND source_ref=%s", (source_ref,))
        existing = cur.fetchone()
        if existing:
            return {"status": existing["status"], "run_id": existing["run_id"], "source_ref": source_ref, "deduplicated": True}
        prompt = {"contract": "EMS_AI_OBSERVER_0_25_0", "mode": "SHADOW_READ_ONLY",
                  "forbidden": ["PLAN_WRITE", "PPD_WRITE", "COMMAND_WRITE", "HA_SERVICE_CALL"],
                  "analytics": {k: analytics.get(k) for k in (
                      "slots_scanned", "complete_slots", "quality_score", "pv1_wape_pct", "pv2_wape_pct",
                      "pv_wape_pct", "load_wape_pct", "import_wape_pct", "export_wape_pct",
                      "pv_bias_kwh", "load_bias_kwh", "import_bias_kwh", "export_bias_kwh",
                      "soc_mae_pct", "net_cost_variance_pln", "pv_daylight_slots",
                      "metric_confidence_pct", "import_active_mae_kwh", "export_active_mae_kwh",
                      "import_event_f1_pct", "export_event_f1_pct")}}
        prompt["analytics"].update({k: analytics.get(k) for k in (
            "suggested_pv1_scale", "suggested_pv2_scale", "suggested_load_scale")})
        suggestions = []
        def flag(metric, value, threshold, message, severity="WARNING"):
            if value is not None and abs(float(value)) > threshold:
                suggestions.append({"metric": metric, "value": float(value), "threshold": threshold,
                                    "severity": severity, "suggestion": message})
        flag("pv_wape_pct", analytics.get("pv_wape_pct"), float(options.get("observer_pv_wape_warn_pct", 30.0)), "Sprawdź profil rozdziału prognozy PV i różnice PV1/PV2.")
        flag("load_wape_pct", analytics.get("load_wape_pct"), float(options.get("observer_load_wape_warn_pct", 35.0)), "Zwiększ liczbę próbek profilu zużycia przed zmianą planera.")
        flag("soc_mae_pct", analytics.get("soc_mae_pct"), float(options.get("observer_soc_mae_warn_pct", 8.0)), "Zweryfikuj sprawności baterii oraz znak i źródło mocy baterii.")
        flag("net_cost_variance_pln", analytics.get("net_cost_variance_pln"), float(options.get("observer_cost_variance_warn_pln", 10.0)), "Przeanalizuj koszt planowany względem wykonania bez automatycznej korekty PPD.")
        quality_threshold = float(options.get("observer_min_quality_score_pct", 80.0))
        if float(analytics.get("quality_score") or 0) < quality_threshold:
            suggestions.append({"metric": "quality_score", "value": float(analytics.get("quality_score") or 0),
                                "threshold": quality_threshold, "severity": "WARNING",
                                "suggestion": "Nie używaj tego przebiegu do uczenia; popraw kompletność telemetrii."})
        decision = "WATCH" if suggestions else "ACCEPT"
        auto_score = max(0.0, min(100.0, float(analytics.get("quality_score") or 0)))
        result = {"contract": "EMS_AI_OBSERVER_0_25_0", "mode": "SHADOW_READ_ONLY",
                  "decision": decision, "auto_score": auto_score, "suggestions": suggestions,
                  "external_model_called": False}
        run_id = str(uuid.uuid4())
        cur.execute("""INSERT INTO ems_gpt_core_ai_runs
          (run_id,role_name,source_ref,started_at,completed_at,status,prompt_json,result_json,auto_score,decision)
          VALUES(%s,'EMS_OBSERVER',%s,NOW(6),NOW(6),'COMPLETED',%s,%s,%s,%s)""",
          (run_id, source_ref, json.dumps(prompt, ensure_ascii=False, default=str),
           json.dumps(result, ensure_ascii=False, default=str), str(round(auto_score, 2)), decision))
    active_titles = []
    for item in suggestions:
        title = f"Obserwator: {item['metric']}"
        active_titles.append(title)
        create_todo("ai_observer", title, json.dumps(item, ensure_ascii=False),
                    item["severity"], run_id, require_consecutive_days=True)
    reconcile_observer_todos(active_titles)
    record_event("ai_observer_completed", "ai_observer",
                 {"run_id": run_id, "source_ref": source_ref, "decision": decision, "suggestions": len(suggestions)})
    return {"status": "COMPLETED", "run_id": run_id, "source_ref": source_ref,
            "decision": decision, "auto_score": auto_score, "suggestions": suggestions}
