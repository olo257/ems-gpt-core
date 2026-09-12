"""Read-only EMS diagnostic service."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta


def generate_diagnostic_report(trigger_name: str = "scheduled", *, options, db, local_now, slot_start, canonical_slots_for_day, create_todo, reconcile_diagnostic_todos, record_event) -> dict:
    """Check freshness, completeness, stuck runs and module/database health without device writes."""
    report_id = str(uuid.uuid4())
    now = local_now().replace(tzinfo=None)
    checks = []
    def add(name, ok, value, expected):
        checks.append({"name": name, "ok": bool(ok), "value": value, "expected": expected})
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(captured_at) v FROM ems_gpt_telemetry_snapshots")
        telemetry_at = cur.fetchone()["v"]
        age = (now-telemetry_at).total_seconds() if telemetry_at else None
        add("telemetry_fresh", age is not None and age <= 90, age, "<=90s")
        cutoff = slot_start().replace(tzinfo=None) + timedelta(minutes=15)
        horizon_end = datetime.combine(now.date() + timedelta(days=2 if now.hour >= 14 else 1), datetime.min.time())
        cur.execute("""SELECT COUNT(*) n, SUM(price_source='PSE_API' AND price_buy_pln_kwh IS NOT NULL
          AND price_sell_pln_kwh IS NOT NULL) prices FROM ems_gpt_slots
          WHERE slot_start>=%s AND slot_start<%s AND actual_recorded_at IS NULL""", (cutoff,horizon_end))
        horizon = cur.fetchone()
        expected_horizon = sum(1 for day_offset in range(2 if now.hour >= 14 else 1)
                               for item in canonical_slots_for_day(now.date()+timedelta(days=day_offset))
                               if cutoff <= item["slot_start_local"] < horizon_end)
        add("planner_horizon", int(horizon["n"] or 0) >= expected_horizon, int(horizon["n"] or 0), f">={expected_horizon}")
        add("pse_prices", int(horizon["prices"] or 0) >= expected_horizon, int(horizon["prices"] or 0), f">={expected_horizon}")
        cur.execute("SELECT COUNT(*) n FROM ems_gpt_plan_runs WHERE status='RUNNING' AND updated_at<%s", (now-timedelta(minutes=10),))
        add("no_stuck_plan_runs", int(cur.fetchone()["n"] or 0) == 0, "checked", 0)
        cur.execute("SELECT COUNT(*) n FROM ems_gpt_slots WHERE actual_recorded_at IS NOT NULL AND slot_start>=%s", (now-timedelta(hours=24),))
        actual_count = int(cur.fetchone()["n"] or 0)
        add("execution_24h", actual_count >= 92, actual_count, ">=92")
        cur.execute("""SELECT AVG(coverage_pct) avg_coverage,
          SUM(coverage_pct<80) low_coverage FROM ems_gpt_core_execution_details
          WHERE slot_start>=%s""", (now-timedelta(hours=24),))
        coverage = cur.fetchone()
        average_coverage = float(coverage["avg_coverage"] or 0)
        add("telemetry_coverage_24h", average_coverage >= 90, round(average_coverage, 2), ">=90%")
        add("low_coverage_slots_24h", int(coverage["low_coverage"] or 0) <= 4,
            int(coverage["low_coverage"] or 0), "<=4")
        cur.execute("SELECT COUNT(*) n FROM ems_gpt_slots GROUP BY slot_start HAVING COUNT(*)>1")
        add("no_duplicate_slots", cur.fetchone() is None, "checked", 0)
        cur.execute("SELECT COUNT(*) n FROM ems_gpt_core_commands WHERE status IN ('READY_FOR_CONNECTOR','DISPATCHED','ACCEPTED') AND expires_at<=NOW(6)")
        expired_commands = int(cur.fetchone()["n"] or 0)
        add("no_expired_active_commands", expired_commands == 0, expired_commands, 0)
        cur.execute("""SELECT process_name,COUNT(*) n FROM ems_gpt_core_process_overrides
          WHERE status='ACTIVE' AND valid_from<=NOW(6) AND valid_until>NOW(6)
          GROUP BY process_name HAVING COUNT(*)>1""")
        add("single_active_override_per_process", cur.fetchone() is None, "checked", 1)
        executor_guard = (not bool(options.get('executor_enabled', False))) or bool(options.get('executor_dry_run', True)) or \
            options.get('executor_activation_ack') == 'EMS_CONNECTOR_ACCEPTED'
        add("executor_guard", executor_guard,
            {"enabled": bool(options.get('executor_enabled', False)), "dry_run": bool(options.get('executor_dry_run', True)),
             "activation_ack": options.get('executor_activation_ack') == 'EMS_CONNECTOR_ACCEPTED'},
            "disabled, dry-run, or explicitly accepted")
        cur.execute("""SELECT run_id,completed_at,quality_score,metric_confidence_pct
          FROM ems_gpt_core_analytics_runs WHERE status='COMPLETED'
          ORDER BY completed_at DESC LIMIT 1""")
        analytics = cur.fetchone()
        analytics_age = (now-analytics["completed_at"]).total_seconds() if analytics and analytics.get("completed_at") else None
        add("analytics_fresh", analytics_age is not None and analytics_age <= 5400,
            None if analytics_age is None else round(analytics_age, 1), "<=5400s")
        minimum_quality = float(options.get("observer_min_quality_score_pct", 80.0))
        add("analytics_quality", analytics is not None and float(analytics.get("quality_score") or 0) >= minimum_quality,
            None if not analytics else analytics.get("quality_score"), f">={minimum_quality}%")
        if bool(options.get("ai_observer_enabled", False)) and analytics:
            cur.execute("""SELECT source_ref,completed_at FROM ems_gpt_core_ai_runs
              WHERE role_name='EMS_OBSERVER' AND status='COMPLETED'
              ORDER BY completed_at DESC LIMIT 1""")
            observer = cur.fetchone()
            add("observer_tracks_latest_analytics",
                observer is not None and observer.get("source_ref") == analytics.get("run_id"),
                None if not observer else observer.get("source_ref"), analytics.get("run_id"))
        alerts = [c for c in checks if not c["ok"]]
        status = "OK" if not alerts else ("WARNING" if len(alerts) <= 2 else "ERROR")
        summary = "Wszystkie kontrole zakończone poprawnie" if not alerts else "; ".join(c["name"] for c in alerts)
        cur.execute("INSERT INTO ems_gpt_core_diagnostic_reports VALUES(%s,NOW(6),%s,%s,%s,%s,%s)",
                    (report_id, trigger_name, status, len(alerts), summary[:500], json.dumps(checks, ensure_ascii=False, default=str)))
    result = {"report_id": report_id, "status": status, "alert_count": len(alerts), "summary": summary, "checks": checks}
    record_event("diagnostic_report", "diagnostics", result, "INFO" if not alerts else "WARNING")
    for alert in alerts:
        create_todo("diagnostics", f"Diagnostyka: {alert['name']}",
                    json.dumps(alert, ensure_ascii=False, default=str), "WARNING", report_id)
    reconcile_diagnostic_todos([f"Diagnostyka: {alert['name']}" for alert in alerts])
    return result
