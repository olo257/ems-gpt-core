#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from api_service import ApiAdapters, build_handler
from analytics_service import run_analytics as run_analytics_service
from appliance_service import ApplianceAdapters, build_appliance_meter
from backup_service import BackupAdapters, build_backup_service
from config_service import CONFIG_SETTINGS, OPERATIONAL_SETTINGS, load_options
from database_service import build_database
from database_audit_service import audit_v3_tables, catalog_database_tables
from diagnostics_service import generate_diagnostic_report as run_diagnostics_service
from executor_service import ExecutorAdapters, build_executor
from ha_gateway_service import HomeAssistantAdapters, build_home_assistant_gateway
from ingestion_service import IngestionAdapters, build_ingestion
from materialization_service import MaterializationAdapters, build_materializations
from planner_service import PlannerAdapters, build_planner
from scheduler_service import SchedulerAdapters, run_scheduler
from schema_service import ensure_runtime_schema as ensure_runtime_schema_service
from slot_calendar_service import SlotCalendarAdapters, build_slot_calendar
from observer_service import run_ai_observer as run_observer_service
from recovery_service import RecoveryAdapters, build_recovery
from runtime_service import build_runtime
from todo_service import TodoService
from telemetry_service import TelemetryAdapters, build_telemetry
from time_service import TimeAdapters, build_time_service

APP_NAME = "EMS-GPT Core"
APP_VERSION = "0.30.0"
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
RUNTIME_SETTINGS_PATH = DATA_DIR / "runtime-settings.json"
HA_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("ems-gpt-core")


OPTIONS = load_options(OPTIONS_PATH, RUNTIME_SETTINGS_PATH)
TZ = ZoneInfo(OPTIONS["timezone"])
_RUNTIME = build_runtime(APP_NAME, APP_VERSION, LOG)
LOCK = _RUNTIME.lock
STATE = _RUNTIME.state
run_serialized = _RUNTIME.run_serialized


_DATABASE = build_database(OPTIONS, TZ)
db = _DATABASE.db
qname = _DATABASE.qname


def database_audit() -> dict:
    return audit_v3_tables(
        db=db, qname=qname, schema_name=OPTIONS["db_name"], log=LOG,
    )


def database_catalog() -> dict:
    return catalog_database_tables(db=db, schema_name=OPTIONS["db_name"], log=LOG)


def ensure_runtime_schema() -> None:
    ensure_runtime_schema_service(db=db, app_version=APP_VERSION)


_TIME = build_time_service(TimeAdapters(
    timezone=TZ, slot_minutes=int(OPTIONS["slot_minutes"]),
))
local_now = _TIME.local_now
slot_start = _TIME.slot_start


_SLOT_CALENDAR = build_slot_calendar(SlotCalendarAdapters(timezone=TZ, db=db))
canonical_slots_for_day = _SLOT_CALENDAR.canonical_slots_for_day
ensure_slot_calendar = _SLOT_CALENDAR.ensure_slot_calendar
backfill_slot_relations = _SLOT_CALENDAR.backfill_slot_relations


_HA_GATEWAY = build_home_assistant_gateway(HomeAssistantAdapters(
    supervisor_token=SUPERVISOR_TOKEN, ha_api=HA_API, log=LOG,
))
ha_state = _HA_GATEWAY.ha_state
ha_service_response = _HA_GATEWAY.ha_service_response
number = _HA_GATEWAY.number
tou_program_snapshot = _HA_GATEWAY.tou_program_snapshot
active_tou_program = _HA_GATEWAY.active_tou_program


ENTITIES = {
    "soc": "sensor.inverter_battery",
    "pv": "sensor.inverter_pv_power",
    "pv1": "sensor.inverter_pv1_power",
    "pv2": "sensor.inverter_pv2_power",
    "load": "sensor.inverter_load_power",
    "grid": "sensor.inverter_grid_power",
    "dhw": "sensor.panasonic_heat_pump_main_dhw_temp",
    "battery_direct": "sensor.inverter_battery_power",
    "ev_power": "sensor.sonoff_1002270ef4_power",
    "dhw_power": "sensor.panasonic_heat_pump_main_dhw_power_consumption",
    "hp_outlet": "sensor.panasonic_heat_pump_main_main_outlet_temp",
    "hp_inlet": "sensor.panasonic_heat_pump_main_main_inlet_temp",
    "hp_compressor_freq": "sensor.panasonic_heat_pump_main_compressor_freq",
    "hp_compressor_current": "sensor.panasonic_heat_pump_main_compressor_current",
    "hp_flow": "sensor.panasonic_heat_pump_main_pump_flow",
    "outside_temperature": "sensor.klimat_w_ogrodzie_temperature",
    "hp_heat_consumption": "sensor.panasonic_heat_pump_main_heat_power_consumption",
    "hp_heat_production": "sensor.panasonic_heat_pump_main_heat_power_production",
    "hp_dhw_production": "sensor.panasonic_heat_pump_main_dhw_power_production",
    "hp_cool_consumption": "sensor.panasonic_heat_pump_main_cool_power_consumption",
    "hp_cool_production": "sensor.panasonic_heat_pump_main_cool_power_production",
    # HA keeps these daily meters (and their Recorder history) independently.
    # They are retained as the reconciliation/backfill contract for all HP modes.
    "hp_heat_energy_consumed": "sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_ogrzewanie_pobrana",
    "hp_heat_energy_generated": "sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_ogrzewanie_wytworzona",
    "hp_dhw_energy_consumed": "sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_cwu_pobrana",
    "hp_dhw_energy_generated": "sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_cwu_wytworzona",
    "hp_cool_energy_consumed": "sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_chlodzenie_pobrana",
    "hp_cool_energy_generated": "sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_chlodzenie_wytworzona",
    "hp_operations_counter": "sensor.panasonic_heat_pump_main_operations_counter",
    "hp_operations_hours": "sensor.panasonic_heat_pump_main_operations_hours",
}

PV_FORECAST_ENTITIES = {
    "today": ("sensor.open_meteo_pv1_e_energy_production_today_remaining", "sensor.open_meteo_pv2_w_energy_production_today_remaining"),
    "tomorrow": ("sensor.open_meteo_pv1_e_energy_production_tomorrow", "sensor.open_meteo_pv2_w_energy_production_tomorrow"),
}


_TELEMETRY = build_telemetry(TelemetryAdapters(
    options=OPTIONS, entities=ENTITIES, db=db, local_now=local_now,
    slot_start=slot_start, canonical_slots_for_day=canonical_slots_for_day,
    number=number, ha_state=ha_state,
))
capture_telemetry = _TELEMETRY.capture_telemetry


def setting(entity_id: str, default: float) -> float:
    value = number(ha_state(entity_id))
    return default if value is None else value


# Data-ingestion functions are wired after record_event is defined.


def audit_stage(cur, run_id: str, stage: str, status: str, rows: int, details: str = "") -> None:
    cur.execute(
        """INSERT INTO ems_gpt_plan_stage_audit
        (run_id,stage,procedure_name,status,affected_rows,details,created_at)
        VALUES(%s,%s,%s,%s,%s,%s,NOW(6))""",
        (run_id, stage, f"core_{stage.lower()}", status, rows, details[:4000]),
    )


def run_analytics() -> dict:
    return run_analytics_service(options=OPTIONS, db=db, local_now=local_now, record_event=record_event)


def run_ai_observer(source_ref: str | None = None) -> dict:
    return run_observer_service(
        source_ref, options=OPTIONS, db=db, create_todo=create_todo,
        reconcile_observer_todos=reconcile_observer_todos, record_event=record_event,
    )


def generate_diagnostic_report(trigger_name: str = "scheduled") -> dict:
    return run_diagnostics_service(
        trigger_name, options=OPTIONS, db=db, local_now=local_now, slot_start=slot_start,
        canonical_slots_for_day=canonical_slots_for_day, create_todo=create_todo,
        reconcile_diagnostic_todos=reconcile_diagnostic_todos, record_event=record_event,
    )


def _todo_service() -> TodoService:
    return TodoService(db=db, local_now=local_now, record_event=record_event)


def create_todo(module: str, title: str, details: str, severity: str = "INFO",
                source_ref: str | None = None, require_consecutive_days: bool = False) -> str:
    return _todo_service().create(module, title, details, severity, source_ref, require_consecutive_days)


def reconcile_observer_todos(active_titles: list[str]) -> int:
    return _todo_service().reconcile_observer(active_titles)


def reconcile_diagnostic_todos(active_titles: list[str]) -> int:
    return _todo_service().reconcile_diagnostics(active_titles)


def maintain_todo_archive() -> dict:
    return _todo_service().maintain_archive()


def review_todo(payload: dict, actor: str) -> dict:
    return _todo_service().review(payload, actor)


# Slot materializations are wired after record_event is defined.


def record_event(event_type: str, module: str, payload: dict, severity: str = "INFO") -> None:
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ems_gpt_core_events(created_at,severity,event_type,module_name,slot_start,payload_json) VALUES(NOW(6),%s,%s,%s,%s,%s)",
                        (severity, event_type, module, slot_start().replace(tzinfo=None),
                         json.dumps(payload, ensure_ascii=False, default=str)))
    except Exception as exc:
        LOG.error("event write failed: %s", exc)


_PLANNER = build_planner(PlannerAdapters(
    options=OPTIONS, db=db, local_now=local_now, slot_start=slot_start,
    setting=setting, audit_stage=audit_stage, active_tou_program=active_tou_program,
    tou_program_snapshot=tou_program_snapshot, record_event=record_event,
))
optimize_hp_heating_slots = _PLANNER.optimize_hp_heating_slots
run_planner = _PLANNER.run_planner


_RECOVERY = build_recovery(RecoveryAdapters(
    options=OPTIONS, db=db, qname=qname, record_event=record_event,
))
bootstrap_legacy_tables = _RECOVERY.bootstrap_legacy_tables
recover_interrupted_runs = _RECOVERY.recover_interrupted_runs


_MATERIALIZATIONS = build_materializations(MaterializationAdapters(
    options=OPTIONS, app_version=APP_VERSION, db=db, local_now=local_now,
    slot_start=slot_start, record_event=record_event,
))
close_finished_slots = _MATERIALIZATIONS.close_finished_slots
backfill_execution_details = _MATERIALIZATIONS.backfill_execution_details
aggregate_results = _MATERIALIZATIONS.aggregate_results
rebuild_recovery_materializations = _MATERIALIZATIONS.rebuild_recovery_materializations
learn_missing_load = _MATERIALIZATIONS.learn_missing_load


_INGESTION = build_ingestion(IngestionAdapters(
    options=OPTIONS,
    timezone=TZ,
    pv_forecast_entities=PV_FORECAST_ENTITIES,
    db=db,
    local_now=local_now,
    slot_start=slot_start,
    canonical_slots_for_day=canonical_slots_for_day,
    number=number,
    ha_state=ha_state,
    ha_service_response=ha_service_response,
    record_event=record_event,
))
refresh_pv_forecast = _INGESTION.refresh_pv_forecast
refresh_weather_forecast = _INGESTION.refresh_weather_forecast
refresh_rce = _INGESTION.refresh_rce


_EXECUTOR = build_executor(ExecutorAdapters(
    options=OPTIONS,
    operational_settings=OPERATIONAL_SETTINGS,
    config_settings=CONFIG_SETTINGS,
    runtime_settings_path=RUNTIME_SETTINGS_PATH,
    lock=LOCK,
    state=STATE,
    record_event=record_event,
    local_now=local_now,
    db=db,
    slot_start=slot_start,
    tou_program_snapshot=tou_program_snapshot,
    active_tou_program=active_tou_program,
    number=number,
    ha_state=ha_state,
    ha_service_response=ha_service_response,
))
settings_payload = _EXECUTOR.settings_payload
update_operational_settings = _EXECUTOR.update_operational_settings
update_executor_mode = _EXECUTOR.update_executor_mode
enable_production_on_startup = _EXECUTOR.enable_production_on_startup
update_process_override = _EXECUTOR.update_process_override
expire_process_overrides = _EXECUTOR.expire_process_overrides
expire_stale_commands = _EXECUTOR.expire_stale_commands
externally_started_hp_is_running = _EXECUTOR.externally_started_hp_is_running
stage_executor_commands = _EXECUTOR.stage_executor_commands
dispatch_ready_commands = _EXECUTOR.dispatch_ready_commands
acknowledge_command = _EXECUTOR.acknowledge_command


capture_appliances = build_appliance_meter(ApplianceAdapters(
    options=OPTIONS, db=db, local_now=local_now, ha_state=ha_state, record_event=record_event,
))
maintain_backup = build_backup_service(BackupAdapters(
    options=OPTIONS, local_now=local_now, record_event=record_event, log=LOG,
))


def complete_rce_cycle(result: dict, run_type: str) -> dict:
    """Refresh dependent inputs and atomically publish PPD after a complete RCE day."""
    if result.get("status") != "OK" or int(result.get("rows") or 0) != int(result.get("expected") or 0):
        return {**result, "planner": {"status": "WAITING_FOR_COMPLETE_RCE_DAY"}}
    pv = refresh_pv_forecast()
    weather = refresh_weather_forecast()
    learned = learn_missing_load()
    plan = run_planner(run_type)
    completed = {**result, "pv": pv, "weather": weather,
                 "load_slots_filled": learned, "planner": {"status": "ACCEPTED", **plan}}
    record_event("rce_dependent_cycle_completed", "core", completed)
    return completed


def engine_loop() -> None:
    run_scheduler(SchedulerAdapters(
        options=OPTIONS, state=STATE, lock=LOCK, log=LOG, db=db, local_now=local_now,
        slot_start=slot_start, capture_telemetry=capture_telemetry,
        close_finished_slots=close_finished_slots, backfill_execution_details=backfill_execution_details,
        run_serialized=run_serialized, rebuild_recovery_materializations=rebuild_recovery_materializations,
        learn_missing_load=learn_missing_load, expire_process_overrides=expire_process_overrides,
        expire_stale_commands=expire_stale_commands, maintain_todo_archive=maintain_todo_archive,
        ensure_slot_calendar=ensure_slot_calendar, refresh_pv_forecast=refresh_pv_forecast,
        refresh_weather_forecast=refresh_weather_forecast, record_event=record_event,
        refresh_rce=refresh_rce, complete_rce_cycle=complete_rce_cycle, run_planner=run_planner,
        stage_executor_commands=stage_executor_commands, dispatch_ready_commands=dispatch_ready_commands,
        run_analytics=run_analytics, run_ai_observer=run_ai_observer,
        generate_diagnostic_report=generate_diagnostic_report,
        capture_appliances=capture_appliances, maintain_backup=maintain_backup,
    ))

HTML = Path(__file__).with_name("webui.html").read_text(encoding="utf-8")


Handler = build_handler(ApiAdapters(
    app_name=APP_NAME, app_version=APP_VERSION, state=STATE, lock=LOCK, log=LOG,
    db=db, local_now=local_now, slot_start=slot_start, settings_payload=settings_payload,
    update_operational_settings=update_operational_settings, update_executor_mode=update_executor_mode,
    update_process_override=update_process_override, stage_executor_commands=stage_executor_commands,
    acknowledge_command=acknowledge_command, run_serialized=run_serialized, run_planner=run_planner,
    refresh_rce=refresh_rce, complete_rce_cycle=complete_rce_cycle,
    refresh_pv_forecast=refresh_pv_forecast, refresh_weather_forecast=refresh_weather_forecast,
    run_analytics=run_analytics, run_ai_observer=run_ai_observer,
    generate_diagnostic_report=generate_diagnostic_report, review_todo=review_todo, html=HTML,
    database_audit=database_audit,
    database_catalog=database_catalog,
))


def initialize() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + 120
    while True:
        try:
            ensure_runtime_schema()
            migrated = bootstrap_legacy_tables()
            recovered = recover_interrupted_runs()
            calendar = ensure_slot_calendar(local_now().date()-timedelta(days=1), local_now().date(), local_now().date()+timedelta(days=1))
            slot_relations = backfill_slot_relations()
            with LOCK: STATE["migrated_tables"] = migrated; STATE["database"] = "CONNECTED"
            record_event("application_started", "core", {"version": APP_VERSION, "migrated_tables": migrated,
              "recovered": recovered, "slot_calendar": calendar, "slot_relations": slot_relations})
            return
        except Exception as exc:
            if time.time() >= deadline: raise
            LOG.warning("waiting for MariaDB: %s", exc); time.sleep(5)


def startup_database_audit() -> None:
    try:
        database_audit()
        database_catalog()
    except Exception:
        # An optional read-only inventory must never block or stop normal operation.
        LOG.exception("startup read-only database audit failed")


def main() -> None:
    startup_executor = enable_production_on_startup()
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        initialize()
        record_event("executor_startup_mode", "executor", startup_executor,
                     "INFO" if startup_executor["mode"] == "LIVE" else "WARNING")
        threading.Thread(target=engine_loop, daemon=True).start()
        threading.Thread(target=startup_database_audit, daemon=True).start()
        LOG.info("%s %s started; executor=%s", APP_NAME, APP_VERSION, startup_executor["mode"])
        server_thread.join()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
