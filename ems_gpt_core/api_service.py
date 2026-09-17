"""HTTP and Ingress API service for EMS-GPT Core."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class ApiAdapters:
    app_name: str
    app_version: str
    state: dict
    lock: Any
    log: Any
    db: Callable
    local_now: Callable
    slot_start: Callable
    settings_payload: Callable
    update_operational_settings: Callable
    update_executor_mode: Callable
    update_process_override: Callable
    stage_executor_commands: Callable
    acknowledge_command: Callable
    run_serialized: Callable
    run_planner: Callable
    run_ppd: Callable
    refresh_rce: Callable
    complete_rce_cycle: Callable
    refresh_pv_forecast: Callable
    refresh_weather_forecast: Callable
    run_analytics: Callable
    run_ai_observer: Callable
    generate_diagnostic_report: Callable
    review_todo: Callable
    database_audit: Callable
    database_catalog: Callable
    slot_column_audit: Callable
    html: str
    icon_path: str = "/app/icon.png"


def build_handler(a: ApiAdapters):
    APP_NAME, APP_VERSION = a.app_name, a.app_version
    STATE, LOCK, LOG = a.state, a.lock, a.log
    db, local_now, slot_start = a.db, a.local_now, a.slot_start
    settings_payload = a.settings_payload
    update_operational_settings = a.update_operational_settings
    update_executor_mode, update_process_override = a.update_executor_mode, a.update_process_override
    stage_executor_commands, acknowledge_command = a.stage_executor_commands, a.acknowledge_command
    run_serialized, run_planner, run_ppd = a.run_serialized, a.run_planner, a.run_ppd
    refresh_rce, complete_rce_cycle = a.refresh_rce, a.complete_rce_cycle
    refresh_pv_forecast, refresh_weather_forecast = a.refresh_pv_forecast, a.refresh_weather_forecast
    run_analytics, run_ai_observer = a.run_analytics, a.run_ai_observer
    generate_diagnostic_report, review_todo = a.generate_diagnostic_report, a.review_todo
    database_audit = a.database_audit
    database_catalog = a.database_catalog
    slot_column_audit = a.slot_column_audit
    HTML, icon_path = a.html, a.icon_path

    class Handler(BaseHTTPRequestHandler):
        def json(self, payload: dict, status=HTTPStatus.OK):
            data = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Pragma", "no-cache"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    
        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path.endswith("/live") or path == "/live":
                return self.json({"ok": True, "app": APP_NAME, "version": APP_VERSION,
                                  "status": "PROCESS_RUNNING"})
            if path.endswith("/health") or path == "/health":
                with LOCK:
                    heartbeat = STATE.get("last_heartbeat")
                    database_ok = STATE["database"] == "CONNECTED"
                    status_ok = STATE["status"] == "RUNNING"
                    readiness = STATE.get("readiness")
                    telemetry_age = STATE.get("telemetry_age_seconds")
                heartbeat_age = None
                if heartbeat:
                    try:
                        heartbeat_age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
                    except (TypeError, ValueError):
                        heartbeat_age = None
                healthy = (database_ok and status_ok and readiness == "READY" and
                           heartbeat_age is not None and heartbeat_age < 180)
                return self.json({"ok": healthy, "app": APP_NAME, "version": APP_VERSION,
                                  "readiness": readiness,
                                  "heartbeat_age_seconds": None if heartbeat_age is None else round(heartbeat_age, 1),
                                  "telemetry_age_seconds": telemetry_age},
                                 HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE)
            if path.endswith("/ready") or path == "/ready":
                with LOCK:
                    readiness = STATE.get("readiness")
                    payload = {"ok": readiness == "READY", "app": APP_NAME,
                               "version": APP_VERSION, "readiness": readiness,
                               "ha_input": STATE.get("ha_input"),
                               "telemetry_age_seconds": STATE.get("telemetry_age_seconds"),
                               "telemetry_consecutive_failures": STATE.get("telemetry_consecutive_failures"),
                               "last_telemetry_success": STATE.get("last_telemetry_success")}
                return self.json(payload, HTTPStatus.OK if payload["ok"] else HTTPStatus.SERVICE_UNAVAILABLE)
            if path.endswith("/api/status") or path == "/api/status":
                with LOCK:
                    payload = dict(STATE)
                try:
                    current = slot_start().replace(tzinfo=None)
                    with db() as conn, conn.cursor() as cur:
                        cur.execute("""SELECT slot_start,forecast_pv_total_kwh,
                          forecast_load_kwh,planned_battery_discharge_kwh,
                          planned_buy_kwh,planned_battery_charge_kwh,planned_sell_kwh,
                          planned_pv_to_bat_kwh
                          FROM ems_gpt_slots WHERE slot_start=%s LIMIT 1""", (current,))
                        row = cur.fetchone()
                    if row:
                        values = {key: max(0.0, float(row.get(key) or 0.0)) for key in (
                            "forecast_pv_total_kwh", "forecast_load_kwh",
                            "planned_battery_discharge_kwh", "planned_buy_kwh",
                            "planned_battery_charge_kwh", "planned_sell_kwh")}
                        pv_to_bat = max(0.0, float(row.get("planned_pv_to_bat_kwh") or 0.0))
                        settings = settings_payload()
                        eta_c = max(0.01, float(settings["battery_charge_efficiency"]["value"]))
                        eta_d = max(0.01, float(settings["battery_discharge_efficiency"]["value"]))
                        pv_balance = min(values["forecast_pv_total_kwh"],
                                         values["forecast_load_kwh"] + pv_to_bat)
                        battery_output = values["planned_battery_discharge_kwh"] * eta_d
                        pv_to_load = min(values["forecast_pv_total_kwh"], values["forecast_load_kwh"])
                        battery_to_load = max(0.0, battery_output - values["planned_sell_kwh"])
                        grid_load = max(0.0, values["forecast_load_kwh"] - pv_to_load - battery_to_load)
                        grid_import = values["planned_buy_kwh"] + grid_load
                        charge_input = values["planned_battery_charge_kwh"] / eta_c
                        supply = pv_balance + battery_output + grid_import
                        demand = (values["forecast_load_kwh"] + charge_input
                                  + values["planned_sell_kwh"])
                        payload["active_slot_balance"] = {
                            "slot_start": row["slot_start"], **values,
                            "planned_pv_to_bat_kwh": round(pv_to_bat, 6),
                            "grid_load_kwh": round(grid_load, 6),
                            "pv_balance_kwh": round(pv_balance, 6),
                            "battery_discharge_output_kwh": round(battery_output, 6),
                            "grid_import_kwh": round(grid_import, 6),
                            "battery_charge_input_kwh": round(charge_input, 6),
                            "supply_kwh": round(supply, 6),
                            "demand_kwh": round(demand, 6),
                            "difference_kwh": round(supply - demand, 6),
                        }
                    else:
                        payload["active_slot_balance"] = None
                except Exception as exc:
                    LOG.warning("active slot balance unavailable: %s", type(exc).__name__)
                    payload["active_slot_balance"] = None
                return self.json(payload)
            if path.endswith("/api/settings") or path == "/api/settings":
                return self.json({"settings": settings_payload()})
            if path.endswith("/api/database-audit") or path == "/api/database-audit":
                try:
                    return self.json(database_audit())
                except Exception as exc:
                    LOG.exception("read-only database audit failed")
                    return self.json({"status":"ERROR","error":type(exc).__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)
            if path.endswith("/api/database-catalog") or path == "/api/database-catalog":
                try:
                    return self.json(database_catalog())
                except Exception as exc:
                    LOG.exception("read-only database catalog failed")
                    return self.json({"status":"ERROR","error":type(exc).__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)
            if path.endswith("/api/slot-column-audit") or path == "/api/slot-column-audit":
                try:
                    return self.json(slot_column_audit())
                except Exception as exc:
                    LOG.exception("read-only slot column audit failed")
                    return self.json({"status":"ERROR","error":type(exc).__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)
            if path.endswith("/api/process-status") or path == "/api/process-status":
                return self.json({})
            if path.endswith("/api/rce-chart") or path == "/api/rce-chart":
                chart_range = "48h"
                now = local_now().replace(tzinfo=None)
                with db() as conn, conn.cursor() as cur:
                    cur.execute("""SELECT slot_start label,price_sell_pln_kwh sell_avg,
                      price_buy_pln_kwh buy_avg,price_sell_pln_kwh sell_min,
                      price_sell_pln_kwh sell_max,
                      (market_window='BUY') buy_window,
                      (market_window='SELL') sale_window
                      FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s
                      AND price_sell_pln_kwh IS NOT NULL ORDER BY slot_start""",
                                (now-timedelta(hours=24), now+timedelta(hours=24)))
                    rows = cur.fetchall()
                return self.json({"range":chart_range,"unit":"PLN/kWh",
                                  "current_slot":slot_start().replace(tzinfo=None),
                                  "count":len(rows),"rows":rows})
            if any(path.endswith(f"/api/{name}") or path == f"/api/{name}" for name in ("plan","execution","hourly","daily","runs","analytics","target-history","diagnostics","processes","overrides","commands","process-execution","todo","ai-runs","appliances")):
                name=path.rsplit("/",1)[-1]; params=parse_qs(urlparse(self.path).query); limit=min(500,max(1,int(params.get("limit",["96"])[0])))
                if name == "plan":
                    limit = 500
                queries={
                  "plan":("SELECT * FROM ems_gpt_slots WHERE actual_recorded_at IS NULL AND slot_start>=%s ORDER BY slot_start LIMIT %s",(slot_start().replace(tzinfo=None),limit)),
                  "execution":("""SELECT s.*,d.actual_grid_export_kwh,d.actual_ev_kwh,d.actual_dhw_kwh,
                    d.soc_start_pct,d.soc_min_pct,d.soc_delta_pct,
                    d.sample_count,d.coverage_pct,d.export_attribution FROM ems_gpt_slots s
                    LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
                    WHERE s.actual_recorded_at IS NOT NULL ORDER BY s.slot_start DESC LIMIT %s""",(limit,)),
                  "hourly":("SELECT * FROM ems_gpt_core_hourly WHERE hour_start<=%s ORDER BY hour_start DESC LIMIT %s",(local_now().replace(tzinfo=None),limit)),
                  "daily":("""SELECT d.*,
                    totals.daily_record_count,totals.learning_record_count,
                    totals.non_learning_record_count
                    FROM ems_gpt_daily d CROSS JOIN (
                      SELECT COUNT(*) daily_record_count,
                        COALESCE(SUM(learning_eligible=1),0) learning_record_count,
                        COALESCE(SUM(learning_eligible=0),0) non_learning_record_count
                      FROM ems_gpt_daily
                    ) totals ORDER BY d.day_date DESC LIMIT %s""",(limit,)),
                  "runs":("SELECT * FROM ems_gpt_plan_runs ORDER BY created_at DESC LIMIT %s",(limit,)),
                  "analytics":("SELECT * FROM ems_gpt_core_analytics_runs ORDER BY started_at DESC LIMIT %s",(limit,)),
                  "target-history":("SELECT * FROM ems_gpt_core_target_history ORDER BY slot_start DESC LIMIT %s",(limit,)),
                  "diagnostics":("SELECT * FROM ems_gpt_core_diagnostic_reports ORDER BY created_at DESC LIMIT %s",(limit,)),
                  "processes":("SELECT * FROM ems_gpt_core_process_decisions WHERE slot_start>=%s ORDER BY slot_start,process_name LIMIT %s",(slot_start().replace(tzinfo=None),limit)),
                  "overrides":("SELECT * FROM ems_gpt_core_process_overrides ORDER BY requested_at DESC LIMIT %s",(limit,)),
                  "commands":("SELECT * FROM ems_gpt_core_commands ORDER BY created_at DESC LIMIT %s",(limit,)),
                  "process-execution":("SELECT * FROM ems_gpt_core_process_execution ORDER BY recorded_at DESC LIMIT %s",(limit,)),
                  "todo":("SELECT * FROM ems_gpt_core_todo ORDER BY created_at DESC LIMIT %s",(limit,)),
                  "ai-runs":("SELECT * FROM ems_gpt_core_ai_runs ORDER BY started_at DESC LIMIT %s",(limit,)),
                  "appliances":("SELECT * FROM ems_gpt_core_appliance_daily ORDER BY local_day DESC,appliance_name LIMIT %s",(limit,)),
                }
                with db() as conn,conn.cursor() as cur: cur.execute(*queries[name]); rows=cur.fetchall()
                return self.json({"view":name,"count":len(rows),"rows":rows})
            if path.endswith("/icon.png") or path == "/icon.png":
                data = Path(icon_path).read_bytes(); self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); return self.wfile.write(data)
            data = HTML.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    
        def do_POST(self):
            path=self.path.split("?",1)[0].rstrip("/")
            if path.endswith("/api/settings") or path=="/api/settings":
                try:
                    length=min(65536,int(self.headers.get("Content-Length","0") or 0))
                    payload=json.loads(self.rfile.read(length) or b"{}")
                    return self.json({"status":"OK",**update_operational_settings(payload)})
                except (ValueError,TypeError,json.JSONDecodeError) as exc:
                    return self.json({"status":"REJECTED","error":str(exc)},HTTPStatus.BAD_REQUEST)
            if path.endswith("/api/executor/mode") or path=="/api/executor/mode":
                try:
                    length=min(65536,int(self.headers.get("Content-Length","0") or 0))
                    payload=json.loads(self.rfile.read(length) or b"{}")
                    actor=self.headers.get("X-Ingress-User") or "operator"
                    return self.json({"status":"OK", **update_executor_mode(payload, actor)})
                except (ValueError,TypeError,json.JSONDecodeError) as exc:
                    return self.json({"status":"REJECTED","error":str(exc)},HTTPStatus.BAD_REQUEST)
            if path.endswith("/api/process/override") or path=="/api/process/override":
                try:
                    length=min(65536,int(self.headers.get("Content-Length","0") or 0))
                    payload=json.loads(self.rfile.read(length) or b"{}")
                    actor=self.headers.get("X-Ingress-User") or "operator"
                    return self.json({"status":"OK", **update_process_override(payload, actor)})
                except (ValueError,TypeError,json.JSONDecodeError) as exc:
                    return self.json({"status":"REJECTED","error":str(exc)},HTTPStatus.BAD_REQUEST)
            if path.endswith("/api/executor/stage") or path=="/api/executor/stage":
                try:
                    return self.json(stage_executor_commands())
                except Exception as exc:
                    return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.CONFLICT)
            if path.endswith("/api/connector/ack") or path=="/api/connector/ack":
                try:
                    length=min(65536,int(self.headers.get("Content-Length","0") or 0))
                    payload=json.loads(self.rfile.read(length) or b"{}")
                    return self.json(acknowledge_command(payload))
                except (ValueError,TypeError,json.JSONDecodeError) as exc:
                    return self.json({"status":"REJECTED","error":str(exc)},HTTPStatus.CONFLICT)
            if path.endswith("/api/planner/run") or path=="/api/planner/run":
                started_at = datetime.now(timezone.utc).isoformat()
                with LOCK:
                    STATE["modules"]["planner"] = "RUNNING"
                    STATE["modules"]["ppd"] = "WAITING"
                    STATE.setdefault("module_details", {})["planner"] = {
                        "activity": "Ręczne przeliczanie planu", "updated_at": started_at}
                    STATE["module_details"]["ppd"] = {
                        "activity": "Oczekiwanie na wynik planera", "updated_at": started_at}
                try:
                    result = run_serialized("planner", run_planner, "manual_api")
                    try:
                        ppd = (run_serialized("ppd", run_ppd, result.get("run_id"), "manual_api")
                               if result.get("status") != "WAITING" else {"status": "WAITING_FOR_PLAN"})
                    except Exception as ppd_exc:
                        ppd = {"status": "ERROR", "error": str(ppd_exc),
                               "plan_run_id": result.get("run_id")}
                        LOG.exception("manual PPD failed after successful planner run")
                    completed_at = datetime.now(timezone.utc).isoformat()
                    health = "RUNNING" if result.get("status") != "WAITING" else "WAITING"
                    with LOCK:
                        STATE["modules"]["planner"] = health
                        STATE["modules"]["ppd"] = ("DEGRADED" if ppd.get("status") == "ERROR" else health)
                        STATE["module_details"]["planner"] = {
                            "activity": "Plan opublikowany" if health == "RUNNING" else "Oczekiwanie na komplet danych",
                            "updated_at": completed_at}
                        STATE["module_details"]["ppd"] = {
                            "activity": (f"Błąd PPD: {ppd.get('error')}" if ppd.get("status") == "ERROR"
                                         else "Decyzje PPD odświeżone" if health == "RUNNING"
                                         else "Oczekiwanie na plan"),
                            "updated_at": completed_at}
                        STATE["planner_failure_latched"] = None
                    return self.json({"status":"ACCEPTED",**result,"ppd":ppd})
                except Exception as exc:
                    failed_at = datetime.now(timezone.utc).isoformat()
                    with LOCK:
                        STATE["modules"]["planner"] = "DEGRADED"
                        STATE["modules"]["ppd"] = "DEGRADED"
                        STATE["module_details"]["planner"] = {
                            "activity": f"Błąd przeliczenia: {exc}", "updated_at": failed_at}
                        STATE["module_details"]["ppd"] = {
                            "activity": "Zachowano ostatnie poprawne decyzje", "updated_at": failed_at}
                        STATE["planner_failure_latched"] = str(exc)
                    LOG.exception("manual planner failed")
                    return self.json({"status":"REJECTED","error":str(exc)},HTTPStatus.CONFLICT)
            if path.endswith("/api/rce/refresh") or path=="/api/rce/refresh":
                try:
                    params=parse_qs(urlparse(self.path).query)
                    requested=params.get("day",[None])[0]
                    target=datetime.strptime(requested,"%Y-%m-%d").date() if requested else None
                    result=refresh_rce(target)
                    return self.json(complete_rce_cycle(result,"manual_rce_refresh"))
                except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.BAD_GATEWAY)
            if path.endswith("/api/forecast/refresh") or path=="/api/forecast/refresh":
                try: return self.json({"status":"ACCEPTED", "pv": refresh_pv_forecast(), "weather": refresh_weather_forecast()})
                except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.INTERNAL_SERVER_ERROR)
            if path.endswith("/api/analytics/run") or path=="/api/analytics/run":
                try:
                    result=run_serialized("analytics", run_analytics)
                    return self.json({"status":"ACCEPTED", **result, "observer":run_ai_observer(result.get("run_id"))})
                except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.INTERNAL_SERVER_ERROR)
            if path.endswith("/api/ai-observer/run") or path=="/api/ai-observer/run":
                try: return self.json(run_ai_observer())
                except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.INTERNAL_SERVER_ERROR)
            if path.endswith("/api/diagnostics/run") or path=="/api/diagnostics/run":
                try: return self.json(run_serialized("diagnostics", generate_diagnostic_report, "manual_api"))
                except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.INTERNAL_SERVER_ERROR)
            if path.endswith("/api/todo/review") or path=="/api/todo/review":
                try:
                    length=min(65536,int(self.headers.get("Content-Length","0") or 0))
                    payload=json.loads(self.rfile.read(length) or b"{}")
                    actor=self.headers.get("X-Ingress-User") or "operator"
                    return self.json({"status":"OK", **review_todo(payload, actor)})
                except (ValueError,TypeError,json.JSONDecodeError) as exc:
                    return self.json({"status":"REJECTED","error":str(exc)},HTTPStatus.BAD_REQUEST)
            return self.json({"error":"not_found"},HTTPStatus.NOT_FOUND)
    
        def log_message(self, fmt, *args):
            LOG.info("http " + fmt, *args)
    
    

    return Handler
