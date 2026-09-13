#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, urlencode
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from api_service import ApiAdapters, build_handler
from analytics_service import run_analytics as run_analytics_service
from config_service import load_options
from database_service import build_database
from database_audit_service import audit_v3_tables, catalog_database_tables
from diagnostics_service import generate_diagnostic_report as run_diagnostics_service
from executor_service import ExecutorAdapters, build_executor
from ha_gateway_service import HomeAssistantAdapters, build_home_assistant_gateway
from ingestion_service import IngestionAdapters, build_ingestion
from materialization_service import MaterializationAdapters, build_materializations
from scheduler_service import SchedulerAdapters, run_scheduler
from slot_calendar_service import SlotCalendarAdapters, build_slot_calendar
from observer_service import run_ai_observer as run_observer_service
from recovery_service import RecoveryAdapters, build_recovery
from runtime_service import build_runtime
from todo_service import TodoService
from telemetry_service import TelemetryAdapters, build_telemetry
from time_service import TimeAdapters, build_time_service

APP_NAME = "EMS-GPT Core"
APP_VERSION = "0.27.3"
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
RUNTIME_SETTINGS_PATH = DATA_DIR / "runtime-settings.json"
HA_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("ems-gpt-core")


OPTIONS = load_options(OPTIONS_PATH, RUNTIME_SETTINGS_PATH)
OPERATIONAL_SETTINGS = {
    "purchase_margin_pln_kwh": (0.0, 5.0, "Marża zakupu [PLN/kWh]", "Ceny i ekonomia"),
    "minimum_arbitrage_margin_pln_kwh": (0.0, 5.0, "Minimalna marża arbitrażu [PLN/kWh]", "Ceny i ekonomia"),
    "battery_degradation_cost_pln_kwh": (0.0, 5.0, "Degradacja baterii [PLN/kWh]", "Ceny i ekonomia"),
    "battery_charge_efficiency": (0.01, 1.0, "Sprawność ładowania", "Bateria"),
    "battery_discharge_efficiency": (0.01, 1.0, "Sprawność rozładowania", "Bateria"),
    "battery_capacity_kwh": (1.0, 100.0, "Pojemność baterii [kWh]", "Bateria"),
    "battery_min_soc_pct": (0.0, 90.0, "Minimalny SOC [%]", "Bateria"),
    "battery_max_power_kw": (0.25, 30.0, "Maksymalna moc baterii [kW]", "Bateria"),
    "historical_soc_drop_p80_pct": (0.0, 100.0, "Historyczny spadek SOC P80 [%]", "Prognozy i procesy"),
    "pv_cwu_min_surplus_kw": (0.0, 20.0, "Próg PV→CWU [kW]", "Prognozy i procesy"),
    "pv_ev_min_surplus_kw": (0.0, 20.0, "Próg PV→EV [kW]", "Prognozy i procesy"),
    "evening_soc_target_pct": (15.0, 95.0, "Bazowy cel SOC o 20:00 [%]", "Prognozy i procesy"),
    "forecast_uncertainty_weight": (0.0, 2.0, "Waga niepewności prognozy", "Prognozy i procesy"),
    "terminal_soc_value_weight": (0.0, 2.0, "Waga wartości końcowego SOC", "Prognozy i procesy"),
    "buy_window_tolerance_pln_kwh": (0.0, 2.0, "Tolerancja okna zakupu [PLN/kWh]", "Ceny i ekonomia"),
    "planned_flow_threshold_kwh": (0.0, 1.0, "Próg istotnego przepływu planu [kWh/slot]", "Ceny i ekonomia"),
    "technical_flow_threshold_kwh": (0.0, 1.0, "Próg przepływu technicznego [kWh/slot]", "Ceny i ekonomia"),
    "soc_floor_max_pct": (15.0, 100.0, "Maksymalny SOC floor [%]", "Bateria"),
    "soc_target_max_pct": (15.0, 100.0, "Maksymalny SOC target [%]", "Bateria"),
    "sale_morning_start_hour": (0.0, 24.0, "Sprzedaż rano — początek [h]", "Okna czasowe"),
    "sale_morning_end_hour": (0.0, 24.0, "Sprzedaż rano — koniec [h]", "Okna czasowe"),
    "sale_evening_start_hour": (0.0, 24.0, "Sprzedaż wieczór — początek [h]", "Okna czasowe"),
    "sale_evening_end_hour": (0.0, 24.0, "Sprzedaż wieczór — koniec [h]", "Okna czasowe"),
    "night_heating_threshold_c": (-20.0, 25.0, "Nocny próg ogrzewania [°C]", "Pompa ciepła"),
    "hp_min_heating_hours": (1.0, 24.0, "Minimalne grzanie domu [h/dobę]", "Pompa ciepła"),
    "hp_min_cycle_hours": (0.25, 6.0, "Minimalna długość cyklu HP [h]", "Pompa ciepła"),
    "hp_min_cycle_break_hours": (0.25, 6.0, "Minimalna przerwa między cyklami [h]", "Pompa ciepła"),
    "hp_max_cycle_break_hours": (0.25, 12.0, "Maksymalna przerwa między cyklami [h]", "Pompa ciepła"),
    "hp_planned_power_kw": (0.25, 15.0, "Planowana moc elektryczna HP [kW]", "Pompa ciepła"),
    "hp_cycle_start_penalty_pln": (0.0, 20.0, "Koszt uruchomienia kolejnego cyklu HP [PLN]", "Pompa ciepła"),
    "telemetry_min_samples_per_slot": (1.0, 15.0, "Minimalna liczba próbek slotu", "Jakość danych"),
    "telemetry_learning_coverage_pct": (0.0, 100.0, "Minimalne pokrycie do uczenia [%]", "Jakość danych"),
    "recovery_lookback_days": (1.0, 31.0, "Zakres odtwarzania po awarii [dni]", "Jakość danych"),
    "observer_pv_wape_warn_pct": (0.0, 500.0, "Observer: próg PV WAPE [%]", "AI Observer"),
    "observer_load_wape_warn_pct": (0.0, 500.0, "Observer: próg Load WAPE [%]", "AI Observer"),
    "observer_soc_mae_warn_pct": (0.0, 100.0, "Observer: próg SOC MAE [%]", "AI Observer"),
    "observer_cost_variance_warn_pln": (0.0, 10000.0, "Observer: próg odchylenia kosztu [PLN]", "AI Observer"),
    "observer_min_quality_score_pct": (0.0, 100.0, "Observer: minimalna jakość [%]", "AI Observer"),
}
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
    statements = [
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_migrations (
          migration_key VARCHAR(100) PRIMARY KEY, applied_at DATETIME(6) NOT NULL,
          details_json LONGTEXT NULL) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_events (
          id BIGINT AUTO_INCREMENT PRIMARY KEY, created_at DATETIME(6) NOT NULL,
          severity VARCHAR(12) NOT NULL, event_type VARCHAR(64) NOT NULL,
          module_name VARCHAR(32) NOT NULL, slot_start DATETIME(6) NULL,
          payload_json LONGTEXT NOT NULL, INDEX ix_core_events_time(created_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_module_runs (
          run_id VARCHAR(36) PRIMARY KEY, module_name VARCHAR(32) NOT NULL,
          run_type VARCHAR(32) NOT NULL, status VARCHAR(24) NOT NULL,
          started_at DATETIME(6) NOT NULL, completed_at DATETIME(6) NULL,
          slot_start DATETIME(6) NULL, input_watermark VARCHAR(80) NULL,
          output_version VARCHAR(80) NULL, reason TEXT NULL,
          INDEX ix_module_runs(module_name,started_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_telemetry_snapshots (
          captured_at DATETIME(6) PRIMARY KEY, slot_start DATETIME(6) NOT NULL,
          soc_pct DOUBLE NULL, pv_power_w DOUBLE NULL, load_power_w DOUBLE NULL,
          grid_power_w DOUBLE NULL, rce_sell_pln_kwh DOUBLE NULL,
          rce_buy_pln_kwh DOUBLE NULL, dhw_temperature_c DOUBLE NULL,
          battery_charge_power_w DOUBLE NULL, battery_discharge_power_w DOUBLE NULL,
          ev_power_w DOUBLE NULL, dhw_power_w DOUBLE NULL,
          source_status VARCHAR(24) NOT NULL, payload_json LONGTEXT NOT NULL,
          INDEX ix_telemetry_slot(slot_start,captured_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_hourly (
          hour_start DATETIME(6) PRIMARY KEY, slot_count INT NOT NULL,
          forecast_pv_kwh DOUBLE NULL, actual_pv_kwh DOUBLE NULL,
          forecast_load_kwh DOUBLE NULL, actual_load_kwh DOUBLE NULL,
          planned_import_kwh DOUBLE NULL, actual_import_kwh DOUBLE NULL,
          planned_export_kwh DOUBLE NULL, actual_export_kwh DOUBLE NULL,
          planned_net_pln DOUBLE NULL, actual_net_pln DOUBLE NULL,
          soc_end_pct DOUBLE NULL, updated_at DATETIME(6) NOT NULL,
          quality_status VARCHAR(24) NOT NULL) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_slot_calendar (
          slot_id VARCHAR(32) PRIMARY KEY, slot_start_utc DATETIME(6) NOT NULL,
          slot_end_utc DATETIME(6) NOT NULL, slot_start_local DATETIME(6) NOT NULL,
          utc_offset_minutes SMALLINT NOT NULL, local_fold TINYINT NOT NULL,
          local_day DATE NOT NULL, slot_index_local SMALLINT NOT NULL,
          UNIQUE KEY uq_calendar_utc(slot_start_utc),
          INDEX ix_calendar_local_day(local_day,slot_index_local)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_analytics_runs (
          run_id VARCHAR(36) PRIMARY KEY, started_at DATETIME(6) NOT NULL,
          completed_at DATETIME(6) NULL, status VARCHAR(24) NOT NULL,
          slots_scanned INT NOT NULL DEFAULT 0, complete_slots INT NOT NULL DEFAULT 0,
          pv_wape_pct DOUBLE NULL, load_wape_pct DOUBLE NULL,
          import_wape_pct DOUBLE NULL, export_wape_pct DOUBLE NULL,
          quality_score DOUBLE NULL, details_json LONGTEXT NULL,
          INDEX ix_analytics_runs_time(started_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_slot_quality (
          slot_start DATETIME(6) PRIMARY KEY, sample_count INT NOT NULL,
          completeness_pct DOUBLE NOT NULL, forecast_complete TINYINT(1) NOT NULL,
          actual_complete TINYINT(1) NOT NULL, price_complete TINYINT(1) NOT NULL,
          pv_abs_error_kwh DOUBLE NULL, load_abs_error_kwh DOUBLE NULL,
          import_abs_error_kwh DOUBLE NULL, export_abs_error_kwh DOUBLE NULL,
          status VARCHAR(24) NOT NULL, checked_at DATETIME(6) NOT NULL,
          reasons_json LONGTEXT NOT NULL) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_diagnostic_reports (
          report_id VARCHAR(36) PRIMARY KEY, created_at DATETIME(6) NOT NULL,
          trigger_name VARCHAR(32) NOT NULL, status VARCHAR(24) NOT NULL,
          alert_count INT NOT NULL, summary VARCHAR(500) NOT NULL,
          checks_json LONGTEXT NOT NULL, INDEX ix_diag_time(created_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_load_profiles (
          weekday_no TINYINT NOT NULL, hour_no TINYINT NOT NULL, minute_no TINYINT NOT NULL,
          sample_count INT NOT NULL, mean_kwh DOUBLE NOT NULL, trimmed_mean_kwh DOUBLE NOT NULL,
          p80_kwh DOUBLE NOT NULL, wape_pct DOUBLE NULL, updated_at DATETIME(6) NOT NULL,
          PRIMARY KEY(weekday_no,hour_no,minute_no)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_pv_profiles (
          month_no TINYINT NOT NULL, hour_no TINYINT NOT NULL, minute_no TINYINT NOT NULL,
          sample_days INT NOT NULL, mean_share DOUBLE NOT NULL, updated_at DATETIME(6) NOT NULL,
          PRIMARY KEY(month_no,hour_no,minute_no)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_process_decisions (
          slot_start DATETIME(6) NOT NULL, process_name VARCHAR(32) NOT NULL,
          decision VARCHAR(24) NOT NULL, eligible TINYINT(1) NOT NULL,
          reason VARCHAR(1000) NOT NULL, plan_run_id VARCHAR(36) NOT NULL,
          valid_until DATETIME(6) NOT NULL, connector_required TINYINT(1) NOT NULL DEFAULT 1,
          published_at DATETIME(6) NOT NULL, PRIMARY KEY(slot_start,process_name),
          INDEX ix_process_decisions_run(plan_run_id)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_process_overrides (
          override_id VARCHAR(36) PRIMARY KEY, process_name VARCHAR(32) NOT NULL,
          requested_state VARCHAR(16) NOT NULL, requested_at DATETIME(6) NOT NULL,
          valid_from DATETIME(6) NOT NULL, valid_until DATETIME(6) NOT NULL,
          requested_by VARCHAR(100) NOT NULL, reason VARCHAR(1000) NOT NULL,
          status VARCHAR(24) NOT NULL, cancelled_at DATETIME(6) NULL,
          INDEX ix_process_override_active(process_name,status,valid_from,valid_until)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_commands (
          command_id VARCHAR(36) PRIMARY KEY, slot_start DATETIME(6) NOT NULL,
          process_name VARCHAR(32) NOT NULL, decision VARCHAR(16) NOT NULL,
          plan_version VARCHAR(80) NOT NULL, created_at DATETIME(6) NOT NULL,
          expires_at DATETIME(6) NOT NULL, source VARCHAR(24) NOT NULL,
          status VARCHAR(24) NOT NULL, safety_json LONGTEXT NOT NULL,
          dispatched_at DATETIME(6) NULL, acknowledged_at DATETIME(6) NULL,
          executed_at DATETIME(6) NULL, acknowledgement_json LONGTEXT NULL,
          UNIQUE KEY uq_command_intent(slot_start,process_name,decision,plan_version),
          INDEX ix_commands_status(status,expires_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_process_execution (
          id BIGINT AUTO_INCREMENT PRIMARY KEY, command_id VARCHAR(36) NULL,
          slot_start DATETIME(6) NOT NULL, process_name VARCHAR(32) NOT NULL,
          planned_state VARCHAR(16) NOT NULL, effective_state VARCHAR(16) NOT NULL,
          observed_state VARCHAR(24) NULL, observed_energy_kwh DOUBLE NULL,
          decision_source VARCHAR(24) NOT NULL, reason VARCHAR(1000) NOT NULL,
          recorded_at DATETIME(6) NOT NULL,
          INDEX ix_process_execution_slot(slot_start,process_name)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_todo (
          todo_id VARCHAR(36) PRIMARY KEY, created_at DATETIME(6) NOT NULL,
          local_day DATE NOT NULL, severity VARCHAR(12) NOT NULL,
          module_name VARCHAR(32) NOT NULL, title VARCHAR(300) NOT NULL,
          details LONGTEXT NOT NULL, status VARCHAR(24) NOT NULL DEFAULT 'OPEN',
          source_ref VARCHAR(100) NULL, resolved_at DATETIME(6) NULL,
          INDEX ix_todo_day(local_day,status,severity)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_ai_runs (
          run_id VARCHAR(36) PRIMARY KEY, role_name VARCHAR(32) NOT NULL,
          source_ref VARCHAR(100) NOT NULL, started_at DATETIME(6) NOT NULL,
          completed_at DATETIME(6) NULL, status VARCHAR(24) NOT NULL,
          prompt_json LONGTEXT NOT NULL, result_json LONGTEXT NULL,
          auto_score VARCHAR(24) NULL, decision VARCHAR(24) NULL,
          UNIQUE KEY uq_ai_role_source(role_name,source_ref),
          INDEX ix_ai_runs_time(started_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_execution_details (
          slot_start DATETIME(6) PRIMARY KEY, sample_count INT NOT NULL,
          coverage_pct DOUBLE NOT NULL, first_sample_at DATETIME(6) NULL,
          last_sample_at DATETIME(6) NULL, actual_grid_export_kwh DOUBLE NULL,
          actual_ev_kwh DOUBLE NULL, actual_dhw_kwh DOUBLE NULL,
          complete_source_samples INT NOT NULL DEFAULT 0,
          export_attribution VARCHAR(32) NOT NULL DEFAULT 'UNRESOLVED',
          updated_at DATETIME(6) NOT NULL) ENGINE=InnoDB""",
    ]
    with db() as conn, conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
        for column in (
            "battery_charge_power_w DOUBLE NULL", "battery_discharge_power_w DOUBLE NULL",
            "ev_power_w DOUBLE NULL", "dhw_power_w DOUBLE NULL",
            "pv1_power_w DOUBLE NULL", "pv2_power_w DOUBLE NULL",
            "hp_outlet_temperature_c DOUBLE NULL", "hp_inlet_temperature_c DOUBLE NULL",
            "hp_compressor_frequency_hz DOUBLE NULL", "hp_compressor_current_a DOUBLE NULL",
            "hp_flow_l_min DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_telemetry_snapshots ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "hp_heat_consumption_w DOUBLE NULL", "hp_heat_production_w DOUBLE NULL",
            "hp_dhw_production_w DOUBLE NULL", "hp_cool_consumption_w DOUBLE NULL",
            "hp_cool_production_w DOUBLE NULL", "outside_temperature_c DOUBLE NULL",
            "hp_operations_counter DOUBLE NULL", "hp_operations_hours DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_telemetry_snapshots ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "soc_start_pct DOUBLE NULL", "soc_min_pct DOUBLE NULL", "soc_delta_pct DOUBLE NULL",
            "recovery_status VARCHAR(24) NOT NULL DEFAULT 'OBSERVED'",
            "quality_status VARCHAR(24) NOT NULL DEFAULT 'OPEN'",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_execution_details ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "override_id VARCHAR(36) NULL", "requested_by VARCHAR(100) NULL",
            "override_reason VARCHAR(1000) NULL", "override_valid_until DATETIME(6) NULL",
            "control_origin VARCHAR(32) NOT NULL DEFAULT 'AUTO'",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_process_execution ADD COLUMN IF NOT EXISTS {column}")
        for column in ("pv1_wape_pct DOUBLE NULL", "pv2_wape_pct DOUBLE NULL"):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "pv_bias_kwh DOUBLE NULL", "load_bias_kwh DOUBLE NULL",
            "import_bias_kwh DOUBLE NULL", "export_bias_kwh DOUBLE NULL",
            "soc_mae_pct DOUBLE NULL", "net_cost_variance_pln DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "pv_daylight_slots INT NOT NULL DEFAULT 0", "metric_confidence_pct DOUBLE NULL",
            "import_active_mae_kwh DOUBLE NULL", "export_active_mae_kwh DOUBLE NULL",
            "import_event_f1_pct DOUBLE NULL", "export_event_f1_pct DOUBLE NULL",
            "suggested_pv1_scale DOUBLE NULL", "suggested_pv2_scale DOUBLE NULL",
            "suggested_load_scale DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "first_seen_day DATE NULL", "last_seen_day DATE NULL",
            "occurrence_count INT NOT NULL DEFAULT 1",
            "consecutive_days INT NOT NULL DEFAULT 1",
            "reviewed_at DATETIME(6) NULL", "reviewed_by VARCHAR(100) NULL",
            "review_note VARCHAR(1000) NULL", "archived_at DATETIME(6) NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_todo ADD COLUMN IF NOT EXISTS {column}")
        cur.execute("""UPDATE ems_gpt_core_todo SET first_seen_day=COALESCE(first_seen_day,local_day),
          last_seen_day=COALESCE(last_seen_day,local_day)""")
        ppd_columns = (
            "grid_buy_allowed TINYINT(1) NOT NULL DEFAULT 0",
            "grid_no_buy TINYINT(1) NOT NULL DEFAULT 0",
            "grid_neutral TINYINT(1) NOT NULL DEFAULT 0",
            "sell_bat_allowed TINYINT(1) NOT NULL DEFAULT 0",
            "no_sell_bat TINYINT(1) NOT NULL DEFAULT 0",
            "sell_pv_allowed TINYINT(1) NOT NULL DEFAULT 0",
            "no_sell_pv TINYINT(1) NOT NULL DEFAULT 0",
        )
        for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows"):
            for column in ppd_columns:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
            for column in (
                "soc_start_plan_pct DOUBLE NULL", "soc_end_plan_pct DOUBLE NULL",
                "heat_pump_window TINYINT(1) NOT NULL DEFAULT 0",
                "planned_pv_to_bat_kwh DOUBLE NOT NULL DEFAULT 0",
                "planned_pv_to_cwu_kwh DOUBLE NOT NULL DEFAULT 0",
                "planned_pv_to_ev_kwh DOUBLE NOT NULL DEFAULT 0",
                "planned_pv_curtail_kwh DOUBLE NOT NULL DEFAULT 0",
            ):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
        for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows", "ems_gpt_telemetry_snapshots"):
            for column in (
                "slot_id VARCHAR(32) NULL", "slot_start_utc DATETIME(6) NULL",
                "slot_start_local DATETIME(6) NULL", "utc_offset_minutes SMALLINT NULL",
                "local_fold TINYINT NOT NULL DEFAULT 0", "local_day DATE NULL",
                "slot_index_local SMALLINT NULL",
            ):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "expected_slot_count INT NOT NULL DEFAULT 4", "terminal_slot_count INT NOT NULL DEFAULT 0",
            "recovered_slot_count INT NOT NULL DEFAULT 0", "missing_slot_count INT NOT NULL DEFAULT 0",
            "coverage_pct DOUBLE NOT NULL DEFAULT 0", "completion_status VARCHAR(24) NOT NULL DEFAULT 'OPEN'",
            "source_version VARCHAR(32) NULL", "input_watermark DATETIME(6) NULL",
            "closed_at DATETIME(6) NULL", "learning_eligible TINYINT(1) NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_hourly ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "planned_pv_to_bat_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_to_cwu_kwh DOUBLE NOT NULL DEFAULT 0",
            "planned_pv_to_ev_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_export_kwh DOUBLE NOT NULL DEFAULT 0",
            "planned_pv_curtail_kwh DOUBLE NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_hourly ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "completion_status VARCHAR(24) NOT NULL DEFAULT 'OPEN'",
            "terminal_slot_count INT NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_daily ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "planned_pv_to_bat_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_to_cwu_kwh DOUBLE NOT NULL DEFAULT 0",
            "planned_pv_to_ev_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_curtail_kwh DOUBLE NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_daily ADD COLUMN IF NOT EXISTS {column}")
        cur.execute("ALTER TABLE ems_gpt_daily MODIFY COLUMN closed_at DATETIME(6) NULL")
        # 0.25.11 intentionally removes the short-lived legacy HP decision
        # matrix and retired process history. Telemetry and DHW analytics stay.
        for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows"):
            for column in ("heat_pump_no_buy", "hp_run_preferred", "hp_run_neutral", "hp_run_avoid"):
                cur.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
        for table in ("ems_gpt_core_process_decisions", "ems_gpt_core_process_overrides",
                      "ems_gpt_core_commands", "ems_gpt_core_process_execution"):
            cur.execute(f"DELETE FROM {table} WHERE process_name IN ('MANUAL_CIRCULATION','HP_DHW')")
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_7_0", json.dumps({"version": APP_VERSION})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_21_0", json.dumps({"version": APP_VERSION, "scope": "overrides_commands_process_execution_todo"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_22_0", json.dumps({"version": APP_VERSION, "scope": "explicit_ppd_matrix"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_22_1", json.dumps({"version": APP_VERSION, "scope": "outage_recovery_hour_daily_quality"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_23_0", json.dumps({"version": APP_VERSION, "scope": "canonical_utc_dst_slot_calendar_rce_schedule"})))
        for table in ("ems_gpt_core_events", "ems_gpt_core_module_runs", "ems_gpt_core_slot_quality",
                      "ems_gpt_core_process_decisions", "ems_gpt_core_commands",
                      "ems_gpt_core_process_execution", "ems_gpt_core_execution_details"):
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS slot_id VARCHAR(32) NULL")
            cur.execute(f"""UPDATE {table} t JOIN ems_gpt_core_slot_calendar c
              ON c.slot_start_local=t.slot_start AND c.local_fold=0
              SET t.slot_id=c.slot_id WHERE t.slot_id IS NULL""")
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_24_0", json.dumps({"version": APP_VERSION, "scope": "independent_soc_quantitative_allocator_slot_id_relations"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_24_1", json.dumps({"version": APP_VERSION, "scope": "continuous_slot_relations_diagnostics_todo_threshold"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_24_2", json.dumps({"version": APP_VERSION, "scope": "quantitative_pv_allocator_hour_daily"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_25_0", json.dumps({"version": APP_VERSION, "scope": "v3_analytics_ai_observer_multi_slot_rce_horizon"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_25_11", json.dumps({"version": APP_VERSION, "scope": "binary_hp_window_cost_replan_manual_origin_cleanup"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_25_20", json.dumps({"version": APP_VERSION, "scope": "command_expiry_observer_todo_lifecycle"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_27_0", json.dumps({"version": APP_VERSION, "scope": "analytics_confidence_daylight_intermittent_flows"})))
        # Normalize the historical/UI typo before the 0.24 slot-id cutover.
        for table, column in (
            ("ems_gpt_slots", "grid_policy_planned"),
            ("ems_gpt_slots", "export_policy_planned"),
            ("ems_gpt_plan_stage_rows", "grid_policy_planned"),
            ("ems_gpt_plan_stage_rows", "export_policy_planned"),
            ("ems_gpt_core_process_decisions", "decision"),
        ):
            cur.execute(f"""UPDATE {table} SET {column}='NEUTRAL'
              WHERE UPPER(REPLACE({column},' ','')) IN ('NEURAL','NEUTRAL')
                AND {column}<>'NEUTRAL'""")


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


def optimize_hp_heating_slots(rows: list[dict], past_states: list[bool], required_slots: int,
                              min_cycle_slots: int, min_gap_slots: int,
                              max_gap_slots: int, planned_power_kw: float,
                              cycle_start_penalty: float) -> set[int]:
    """Choose the cheapest remaining HP slots while preserving cycle hygiene."""
    on_count = sum(1 for value in past_states if value)
    phase, run_len, gap_len = "BEFORE", 0, 0
    for value in past_states:
        if value:
            if phase == "ON":
                run_len += 1
            else:
                phase, run_len, gap_len = "ON", 1, 0
        elif phase == "ON":
            phase, run_len, gap_len = "GAP", 0, 1
        elif phase == "GAP":
            gap_len += 1

    # state=(delivered, phase, run_len, gap_len), value=(cost, selected indexes)
    start_state = (min(required_slots, on_count), phase, min(run_len, min_cycle_slots), gap_len)
    states = {start_state: (0.0, ())}
    slot_energy = max(0.0, planned_power_kw) * 0.25
    for index, row in enumerate(rows):
        next_states = {}
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        load = max(0.0, float(row.get("forecast_load_kwh") or 0.0))
        pv_cover = min(slot_energy, max(0.0, pv - load))
        grid_energy = max(0.0, slot_energy - pv_cover)
        buy_price = float(row.get("price_buy_pln_kwh") or 0.0)
        sell_price = max(0.0, float(row.get("price_sell_pln_kwh") or 0.0))
        on_cost = grid_energy * buy_price + pv_cover * sell_price
        for state, (cost, selected) in states.items():
            delivered, current_phase, current_run, current_gap = state
            choices = [False] if delivered >= required_slots else [False, True]
            for turn_on in choices:
                if turn_on:
                    if current_phase == "GAP" and current_gap < min_gap_slots:
                        continue
                    new_phase = "ON"
                    new_run = min(min_cycle_slots, current_run + 1) if current_phase == "ON" else 1
                    new_gap = 0
                    new_delivered = min(required_slots, delivered + 1)
                    start_cost = cycle_start_penalty if current_phase != "ON" else 0.0
                    new_cost, new_selected = cost + on_cost + start_cost, selected + (index,)
                else:
                    if current_phase == "ON" and current_run < min_cycle_slots:
                        continue
                    if current_phase == "GAP" and current_gap >= max_gap_slots and delivered < required_slots:
                        continue
                    new_phase = "GAP" if current_phase in ("ON", "GAP") else "BEFORE"
                    new_run = 0
                    new_gap = (current_gap + 1) if current_phase == "GAP" else (1 if current_phase == "ON" else 0)
                    new_delivered = delivered
                    new_cost, new_selected = cost, selected
                new_state = (new_delivered, new_phase, new_run, new_gap)
                previous = next_states.get(new_state)
                if previous is None or new_cost < previous[0]:
                    next_states[new_state] = (new_cost, new_selected)
        states = next_states

    complete = [(value[0], value[1]) for state, value in states.items()
                if state[0] >= required_slots and (state[1] != "ON" or state[2] >= min_cycle_slots)]
    if complete:
        return set(min(complete, key=lambda value: value[0])[1])
    # A late replan must fail toward comfort: use every feasible future slot.
    fallback = max(states.items(), key=lambda item: (item[0][0], -item[1][0]))[1][1] if states else ()
    return set(fallback)


def run_planner(run_type: str = "scheduled") -> dict:
    """Run the V3 staged planner transactionally in the app-owned database."""
    now = local_now().replace(tzinfo=None)
    cutoff = slot_start().replace(tzinfo=None) + timedelta(minutes=int(OPTIONS["slot_minutes"]))
    capacity = max(1.0, float(OPTIONS.get("battery_capacity_kwh", 15.0)))
    reserve = max(0.0, min(90.0, float(OPTIONS.get("battery_min_soc_pct", 15.0))))
    soc_now = max(reserve, min(100.0, setting("sensor.inverter_battery", reserve)))
    eta_c = max(0.01, min(1.0, float(OPTIONS.get("battery_charge_efficiency", 0.90))))
    eta_d = max(0.01, min(1.0, float(OPTIONS.get("battery_discharge_efficiency", 0.95))))
    degradation = max(0.0, float(OPTIONS.get("battery_degradation_cost_pln_kwh", 0.08)))
    min_margin = max(0.0, float(OPTIONS.get("minimum_arbitrage_margin_pln_kwh", 0.05)))
    p80 = max(0.0, float(OPTIONS.get("historical_soc_drop_p80_pct", 60.0)))
    uncertainty_weight = max(0.0, min(2.0, float(OPTIONS.get("forecast_uncertainty_weight", 1.0))))
    terminal_weight = max(0.0, min(2.0, float(OPTIONS.get("terminal_soc_value_weight", 1.0))))
    floor_cap = max(reserve, min(100.0, float(OPTIONS.get("soc_floor_max_pct", 90.0))))
    target_cap = max(floor_cap, min(100.0, float(OPTIONS.get("soc_target_max_pct", 95.0))))
    configured_evening_target = max(reserve, min(target_cap, float(OPTIONS.get("evening_soc_target_pct", 60.0))))
    flow_threshold = max(0.0, float(OPTIONS.get("planned_flow_threshold_kwh", 0.02)))
    max_kw = max(0.25, float(OPTIONS.get("battery_max_power_kw", 5.0)))
    cwu_threshold = max(0.0, float(OPTIONS.get("pv_cwu_min_surplus_kw", 2.0))) * .25
    ev_threshold = max(0.0, float(OPTIONS.get("pv_ev_min_surplus_kw", 1.5))) * .25
    run_id = str(uuid.uuid4())
    hp_shortfalls = []
    tou_programs = tou_program_snapshot()
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT * FROM ems_gpt_slots
          WHERE slot_start>=%s AND actual_recorded_at IS NULL
            AND price_source='PSE_API' AND price_buy_pln_kwh IS NOT NULL AND price_sell_pln_kwh IS NOT NULL
          ORDER BY slot_start""", (cutoff,))
        source = list(cur.fetchall())
        if not source:
            raise RuntimeError("No open forecast rows for planner horizon")
        continuity = all(
            source[i]["slot_start"] - source[i - 1]["slot_start"] == timedelta(minutes=15)
            for i in range(1, len(source))
        )
        complete_prices = len(source)
        per_day = {}
        for row in source:
            day = row.get("local_day") or row["slot_start"].date()
            per_day[str(day)] = per_day.get(str(day), 0) + 1
        if not continuity or not source:
            details = {"rows": len(source), "continuous": continuity,
                       "pse_prices": complete_prices, "per_day": per_day, "expected": "all contiguous available RCE slots"}
            record_event("planner_waiting_for_inputs", "planner", details, "WARNING")
            return {"status": "WAITING", **details}
        cur.execute("""INSERT INTO ems_gpt_plan_runs
          (run_id,plan_day,run_type,stage_version,expected_slots,status,current_stage,created_at,updated_at)
          VALUES(%s,%s,%s,%s,%s,'RUNNING','RCE_RAW',NOW(6),NOW(6))""",
          (run_id, cutoff.date(), run_type, "CORE_0_4_1", len(source)))
        stage_columns = [
            "slot_start","slot_end","slot_id","slot_start_utc","slot_start_local","utc_offset_minutes",
            "local_fold","local_day","slot_index_local","price_sell_pln_kwh","price_buy_pln_kwh","price_source",
            "price_fetched_at","sale_window","buy_window","forecast_pv1_kwh","forecast_pv2_kwh",
            "forecast_pv_total_kwh","forecast_load_kwh","forecast_temperature_c",
            "forecast_cloud_coverage_pct","forecast_precipitation_mm","pv_correction",
            "load_correction","forecast_pv_source","planned_sell_kwh",
            "heat_pump_window",
        ]
        insert_cols = ["run_id"] + stage_columns
        placeholders = ",".join(["%s"] * len(insert_cols))
        for row in source:
            values = [run_id] + [0 if c == "heat_pump_window" and row.get(c) is None else row.get(c) for c in stage_columns]
            cur.execute(f"INSERT INTO ems_gpt_plan_stage_rows ({','.join(insert_cols)}) VALUES({placeholders})", values)
        audit_stage(cur, run_id, "RCE_RAW", "OK", len(source), "durable prices copied")
        audit_stage(cur, run_id, "FORECAST", "OK", len(source), "PV/LOAD forecast copied from durable inputs")
        cur.execute("""UPDATE ems_gpt_plan_stage_rows SET
          grid_window=CASE WHEN COALESCE(sale_window,0)=1 THEN 'NO_BUY'
            WHEN COALESCE(buy_window,0)=1 THEN 'BUY_ALLOWED' ELSE 'NEUTRAL' END,
          windows_updated_at=NOW(6) WHERE run_id=%s""", (run_id,))
        audit_stage(cur, run_id, "WINDOWS", "OK", len(source))
        cur.execute("SELECT * FROM ems_gpt_plan_stage_rows WHERE run_id=%s ORDER BY slot_start", (run_id,))
        rows = list(cur.fetchall())

        # Keep the daily heating trigger stable across hourly replans by reading
        # the complete 00:00-06:00 forecast, including already closed slots.
        night_threshold = float(OPTIONS.get("night_heating_threshold_c", 10.0))
        day_values = sorted({row.get("local_day") or row["slot_start"].date() for row in rows})
        night_min_by_day = {}
        if day_values:
            markers = ",".join(["%s"] * len(day_values))
            cur.execute(f"""SELECT local_day,MIN(forecast_temperature_c) night_min
              FROM ems_gpt_slots WHERE local_day IN ({markers})
                AND TIME(slot_start)>='00:00:00' AND TIME(slot_start)<'06:00:00'
                AND forecast_temperature_c IS NOT NULL GROUP BY local_day""", tuple(day_values))
            night_min_by_day = {str(value["local_day"]): float(value["night_min"]) for value in cur.fetchall()}

        required_slots = max(1, int(float(OPTIONS.get("hp_min_heating_hours", 10.0)) * 4 + 0.999999))
        min_cycle_slots = max(1, int(float(OPTIONS.get("hp_min_cycle_hours", 2.0)) * 4 + 0.999999))
        min_gap_slots = max(1, int(float(OPTIONS.get("hp_min_cycle_break_hours", 1.0)) * 4 + 0.999999))
        max_gap_slots = max(min_gap_slots, int(float(OPTIONS.get("hp_max_cycle_break_hours", 3.0)) * 4 + 0.999999))
        planned_hp_kw = max(0.0, float(OPTIONS.get("hp_planned_power_kw", 2.5)))
        cycle_penalty = max(0.0, float(OPTIONS.get("hp_cycle_start_penalty_pln", 0.25)))
        hp_selected_indices = set()
        for day_value in day_values:
            day_key = str(day_value)
            night_min = night_min_by_day.get(day_key)
            if night_min is None or night_min >= night_threshold:
                continue
            indexed_rows = [(index, row) for index, row in enumerate(rows)
                            if (row.get("local_day") or row["slot_start"].date()) == day_value]
            day_start = datetime.combine(day_value, datetime.min.time())
            cur.execute("""SELECT d.eligible,
              (SELECT o.requested_state FROM ems_gpt_core_process_overrides o
               WHERE o.process_name=d.process_name AND o.valid_from<d.valid_until
                 AND o.valid_until>d.slot_start AND o.status IN ('ACTIVE','EXPIRED')
               ORDER BY o.requested_at DESC LIMIT 1) requested_state,
              (SELECT MAX(e.control_origin='EXTERNAL_MANUAL' AND e.effective_state='ON')
               FROM ems_gpt_core_process_execution e
               WHERE e.process_name=d.process_name AND e.slot_start=d.slot_start) external_manual_on
              FROM ems_gpt_core_process_decisions d
              WHERE d.process_name='HP_HEAT_DHW' AND d.slot_start>=%s AND d.slot_start<%s
              ORDER BY d.slot_start""", (day_start, min(cutoff, day_start + timedelta(days=1))))
            past_states = [True if value.get("requested_state") == "FORCE_ON" else
                           False if value.get("requested_state") == "FORCE_OFF" else
                           True if value.get("external_manual_on") else bool(value["eligible"])
                           for value in cur.fetchall()]
            selected_local = optimize_hp_heating_slots(
                [row for _, row in indexed_rows], past_states, required_slots,
                min_cycle_slots, min_gap_slots, max_gap_slots, planned_hp_kw, cycle_penalty)
            hp_selected_indices.update(indexed_rows[index][0] for index in selected_local)
            delivered_slots = sum(past_states) + len(selected_local)
            if delivered_slots < required_slots:
                hp_shortfalls.append({"day": day_key, "required_slots": required_slots,
                                      "scheduled_slots": delivered_slots,
                                      "missing_hours": round((required_slots-delivered_slots)/4, 2)})

        # One backward pass replaces repeated 96x96 future-price/weather scans.
        suffix_min_buy = [None] * len(rows)
        suffix_max_sell = [None] * len(rows)
        suffix_bad_weather = [False] * len(rows)
        min_buy = max_sell = None
        bad_weather = False
        for i in range(len(rows)-1, -1, -1):
            row = rows[i]
            suffix_min_buy[i], suffix_max_sell[i] = min_buy, max_sell
            bad = float(row.get("forecast_cloud_coverage_pct") or 0) >= 80 or float(row.get("forecast_precipitation_mm") or 0) > 0
            bad_weather = bad_weather or bad
            suffix_bad_weather[i] = bad_weather
            if row.get("price_buy_pln_kwh") is not None:
                price = float(row["price_buy_pln_kwh"])
                min_buy = price if min_buy is None else min(min_buy, price)
            if row.get("price_sell_pln_kwh") is not None:
                price = float(row["price_sell_pln_kwh"])
                max_sell = price if max_sell is None else max(max_sell, price)

        energy = capacity * soc_now / 100.0
        base = []
        for index, row in enumerate(rows):
            start_pct = energy / capacity * 100
            pv = float(row.get("forecast_pv_total_kwh") or 0)
            native_load = float(row.get("forecast_load_kwh") or 0)
            hp_load = planned_hp_kw * 0.25 if index in hp_selected_indices else 0.0
            load = native_load + hp_load
            legacy_sell = max(0.0, float(row.get("planned_sell_kwh") or 0))
            replacement = suffix_min_buy[index]
            required_sell = replacement/(eta_c*eta_d)+degradation+min_margin if replacement is not None else None
            future_peak = suffix_max_sell[index]
            sell_now = float(row.get("price_sell_pln_kwh") or 0)
            economically_ready = required_sell is not None and sell_now >= required_sell
            # Do not spend stored energy before a materially better selling slot.
            peak_ready = future_peak is None or sell_now >= future_peak-min_margin
            sale_candidate = max(legacy_sell, max_kw*.25 if row.get("sale_window") else 0.0)
            battery_sell_request = sale_candidate if sale_candidate > flow_threshold and economically_ready and peak_ready else 0.0
            balance = pv-load-battery_sell_request
            charge = discharge = 0.0
            if balance > 0:
                charge = min(balance*eta_c, max_kw*.25*eta_c, capacity-energy)
                energy += charge
            elif balance < 0:
                discharge = min((-balance)/eta_d, max_kw*.25/eta_d, max(0.0, energy-capacity*reserve/100))
                energy -= discharge
            pv_surplus = max(0.0, pv-load)
            pv_export = max(0.0, pv_surplus-charge/eta_c)
            served_from_battery = discharge*eta_d
            native_deficit = max(0.0, load-pv)
            battery_export = min(battery_sell_request, max(0.0, served_from_battery-native_deficit))
            base.append({"row":row,"start":start_pct,"end":energy/capacity*100,"charge":charge,
                         "discharge":discharge,"sell":battery_export,"pv_export":pv_export})

        raw_floor = [reserve] * len(base)
        raw_target = [reserve] * len(base)
        terminal_target = min(target_cap, reserve + p80 * 0.25 * terminal_weight)
        required_next = gross_next = max(0.0, (terminal_target - reserve) / 100.0 * capacity)
        grid_recovery = max_kw*.25*eta_c
        evening_target = configured_evening_target
        for i in range(len(base)-1, -1, -1):
            item, row = base[i], base[i]["row"]
            grid = grid_recovery if row.get("grid_window") == "BUY_ALLOWED" else 0.0
            net = item["discharge"]-item["charge"]
            required_start = max(0.0, net+required_next-grid)
            gross_start = max(0.0, net+gross_next)
            uncertainty = min(20.0, uncertainty_weight * (5.0+p80*.10+(3.0 if suffix_bad_weather[i] else 0.0)))
            floor = min(floor_cap,max(reserve,reserve+required_start/capacity*100+3.0))
            target = min(target_cap,max(floor+uncertainty,reserve+gross_next/capacity*100+3.0+uncertainty))
            if row["slot_start"].hour == 19 and row["slot_start"].minute == 45:
                target=max(target,evening_target)
            raw_floor[i], raw_target[i] = floor,target
            required_next,gross_next=required_start,gross_start

        def envelope(values, step):
            out=list(values)
            for i in range(1,len(out)): out[i]=max(out[i],out[i-1]-step)
            for i in range(len(out)-2,-1,-1): out[i]=max(out[i],out[i+1]-step)
            return out
        floors=envelope(raw_floor,max(1.0,max_kw*.25/eta_d/capacity*100))
        targets=envelope(raw_target,max(1.0,grid_recovery/capacity*100))
        energy = capacity * soc_now / 100.0
        for i,item in enumerate(base):
            row=item["row"]
            floor=min(floor_cap,max(reserve,floors[i]))
            tou_program=active_tou_program(row["slot_start"], tou_programs)
            tou_floor=float(tou_program["soc"]) if tou_program else None
            effective_floor=max(floor,reserve,tou_floor) if tou_floor is not None else max(floor,reserve)
            tou_block_reason=None if tou_program else "TOU_FLOOR_UNAVAILABLE"
            target=min(target_cap,max(0.0,targets[i]))
            item["start"] = energy/capacity*100
            pv=float(row.get("forecast_pv_total_kwh") or 0)
            native_load=float(row.get("forecast_load_kwh") or 0)
            hp_load=planned_hp_kw * 0.25 if i in hp_selected_indices else 0.0
            load=native_load+hp_load
            pv_surplus=max(0.0,pv-load)
            native_deficit=max(0.0,load-pv)
            requested_export=max(0.0,float(item.get("sell") or 0))
            discharge=min((native_deficit+requested_export)/eta_d,max_kw*.25/eta_d,
                          max(0.0,energy-capacity*effective_floor/100))
            energy-=discharge
            pv_to_bat=min(pv_surplus,max_kw*.25,max(0.0,(capacity-energy)/eta_c))
            energy+=pv_to_bat*eta_c
            pv_flex=max(0.0,pv_surplus-pv_to_bat)
            buy=0.0
            required_soc=max(effective_floor,target)
            if row.get("grid_window")=="BUY_ALLOWED" and energy/capacity*100 < required_soc:
                buy=min(max_kw*.25,max(0.0,(required_soc-energy/capacity*100)/100*capacity/eta_c))
                energy=min(capacity,energy+buy*eta_c)
            item["charge"],item["discharge"],item["end"] = pv_to_bat*eta_c,discharge,energy/capacity*100
            sell_price=float(row.get("price_sell_pln_kwh") or 0)
            delivered_from_battery=discharge*eta_d
            item["sell"]=max(0.0,delivered_from_battery-native_deficit)
            sell_bat=(tou_program is not None and item["sell"]>flow_threshold
                      and item["discharge"]>flow_threshold and item["end"]>=effective_floor)
            if not sell_bat:
                item["sell"] = 0.0
                if row.get("sale_window") and tou_program is not None and item["start"] <= effective_floor + 0.01:
                    tou_block_reason=f"TOU_FLOOR_BLOCK: program={tou_program['program']}, soc={tou_floor:.0f}%"
            no_sell_pv=sell_price<=0
            pv_cwu=(not row.get("sale_window") and not sell_bat and pv_flex>=cwu_threshold and item["end"]>=target)
            cwu_kwh=min(pv_flex,0.625) if pv_cwu else 0.0
            after_cwu=max(0.0,pv_flex-cwu_kwh)
            pv_ev=(not row.get("sale_window") and not sell_bat and after_cwu>=ev_threshold and item["end"]>=min(100,target+20))
            ev_kwh=after_cwu if pv_ev else 0.0
            after_flex=max(0.0,after_cwu-ev_kwh)
            pv_export_kwh=after_flex if sell_price>0 else 0.0
            pv_curtail_kwh=after_flex-pv_export_kwh
            item["pv_export"]=pv_export_kwh
            grid_policy="BUY_ALLOWED" if buy>flow_threshold else ("NO_BUY" if row.get("sale_window") else "NEUTRAL")
            export_policy="SELL_BAT" if sell_bat else ("NO_SELL_PV" if no_sell_pv else ("SELL_PV" if item["pv_export"]>flow_threshold else "NEUTRAL"))
            recommendation="Zakup ładowanie" if buy>flow_threshold else "Sprzedaż z baterii" if sell_bat else "Sprzedaż PV" if item["pv_export"]>flow_threshold else "Ładowanie PV" if item["charge"]>flow_threshold else "Autokonsumpcja PV" if float(row.get("forecast_pv_total_kwh") or 0)>flow_threshold else "Autokonsumpcja z baterii"
            reason=f"grid={grid_policy}; export={export_policy}; soc={item['end']:.2f}; floor={effective_floor:.2f}; target={target:.2f}; hp_load_kwh={hp_load:.3f}"
            if tou_block_reason:
                reason += f"; {tou_block_reason}"
            cur.execute("""UPDATE ems_gpt_plan_stage_rows SET soc_start_plan_pct=%s,soc_end_plan_pct=%s,
              soc_floor_pct=%s,soc_target_pct=%s,planned_buy_kwh=%s,planned_battery_charge_kwh=%s,
              planned_battery_discharge_kwh=%s,planned_sell_kwh=%s,planned_pv_export_kwh=%s,
              recommendation=%s,grid_policy_planned=%s,export_policy_planned=%s,pv_to_bat_planned=%s,
              pv_to_cwu_planned=%s,pv_to_ev_planned=%s,pv_export_planned=%s,pv_curtail_planned=%s,
              grid_buy_allowed=%s,grid_no_buy=%s,grid_neutral=%s,sell_bat_allowed=%s,no_sell_bat=%s,
              sell_pv_allowed=%s,no_sell_pv=%s,
              planned_pv_to_bat_kwh=%s,planned_pv_to_cwu_kwh=%s,planned_pv_to_ev_kwh=%s,
              planned_pv_curtail_kwh=%s,ppd_reason=%s,soc_updated_at=NOW(6),ppd_updated_at=NOW(6)
              WHERE run_id=%s AND slot_start=%s""",
              (round(item["start"],2),round(item["end"],2),round(effective_floor,2),round(target,2),round(buy,6),
               round(item["charge"],6),round(item["discharge"],6),round(item["sell"],6),round(item["pv_export"],6),
               recommendation,grid_policy,export_policy,item["charge"]>flow_threshold,pv_cwu,pv_ev,
               item["pv_export"]>flow_threshold and not no_sell_pv,no_sell_pv and pv_flex>flow_threshold,
               grid_policy=="BUY_ALLOWED",grid_policy=="NO_BUY",grid_policy=="NEUTRAL",
               sell_bat,not sell_bat,not no_sell_pv,no_sell_pv,
               round(pv_to_bat,6),round(cwu_kwh,6),round(ev_kwh,6),
               round(pv_curtail_kwh,6),reason,run_id,row["slot_start"]))
            hp_window = i in hp_selected_indices
            night_day = str(row.get("local_day") or row["slot_start"].date())
            night_min = night_min_by_day.get(night_day)
            heat_dhw_allowed = hp_window
            cur.execute("""UPDATE ems_gpt_plan_stage_rows SET heat_pump_window=%s
              WHERE run_id=%s AND slot_start=%s""", (hp_window,run_id,row["slot_start"]))
            decisions = (
                ("BATTERY_IMPORT", grid_policy == "BUY_ALLOWED", grid_policy, f"grid={grid_policy}; soc_target={target:.2f}"),
                ("BATTERY_EXPORT", export_policy == "SELL_BAT", export_policy, f"replacement_cost={required_sell}; sell={sell_price:.3f}"),
                ("PV_CWU", pv_cwu, "ALLOW" if pv_cwu else "BLOCK", f"pv_flex={pv_flex:.3f}; priority=BATTERY>CWU>EV"),
                ("PV_EV", pv_ev, "ALLOW" if pv_ev else "BLOCK", f"pv_flex={pv_flex:.3f}; cwu={pv_cwu}"),
                ("HP_HEAT_DHW", heat_dhw_allowed,
                 "ON" if heat_dhw_allowed else "OFF",
                 f"window={hp_window}; night_min={night_min}; threshold={night_threshold}; minimum_hours={OPTIONS.get('hp_min_heating_hours',10.0)}; planned_hp_kwh={hp_load:.3f}"),
            )
            for process_name, eligible, decision, process_reason in decisions:
                cur.execute("""INSERT INTO ems_gpt_core_process_decisions
                  (slot_start,slot_id,process_name,decision,eligible,reason,plan_run_id,valid_until,connector_required,published_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,NOW(6)) ON DUPLICATE KEY UPDATE
                  decision=VALUES(decision),eligible=VALUES(eligible),reason=VALUES(reason),
                  slot_id=COALESCE(slot_id,VALUES(slot_id)),
                  plan_run_id=VALUES(plan_run_id),valid_until=VALUES(valid_until),
                  connector_required=1,published_at=NOW(6)""",
                  (row["slot_start"], row.get("slot_id"), process_name, decision, eligible, process_reason,
                   run_id, row["slot_start"]+timedelta(minutes=16)))
        audit_stage(cur,run_id,"SOC","OK",len(base),"continuous reserve + physical envelope")
        audit_stage(cur,run_id,"PPD","OK",len(base),"O(n) future scan; replacement cost + efficiency + degradation + SOC reserve")
        cur.execute("""SELECT COUNT(*) n,
          SUM(price_buy_pln_kwh IS NULL OR price_sell_pln_kwh IS NULL) bad_price,
          SUM(soc_floor_pct IS NULL OR soc_target_pct IS NULL) bad_soc,
          SUM(grid_policy_planned IS NULL OR export_policy_planned IS NULL
            OR grid_buy_allowed+grid_no_buy+grid_neutral<>1
            OR sell_bat_allowed+no_sell_bat<>1
            OR sell_pv_allowed+no_sell_pv<>1
            OR heat_pump_window NOT IN (0,1)) bad_ppd
          FROM ems_gpt_plan_stage_rows WHERE run_id=%s""",(run_id,))
        checks=cur.fetchone()
        accepted=checks["n"]==len(source) and not any(int(checks[k] or 0) for k in ("bad_price","bad_soc","bad_ppd"))
        if not accepted:
            cur.execute("UPDATE ems_gpt_plan_runs SET status='REJECTED',current_stage='VALIDATE',validated_at=NOW(6),updated_at=NOW(6),validation_status='REJECTED',validation_reason=%s WHERE run_id=%s",(json.dumps(checks,default=str),run_id))
            audit_stage(cur,run_id,"VALIDATE","REJECTED",0,json.dumps(checks,default=str))
            raise RuntimeError(f"planner validation rejected: {checks}")
        cur.execute("""UPDATE ems_gpt_slots p JOIN ems_gpt_plan_stage_rows s
          ON s.slot_start=p.slot_start AND s.run_id=%s SET
          p.forecast_pv1_kwh=s.forecast_pv1_kwh,p.forecast_pv2_kwh=s.forecast_pv2_kwh,
          p.forecast_pv_total_kwh=s.forecast_pv_total_kwh,p.forecast_load_kwh=s.forecast_load_kwh,
          p.soc_start_plan_pct=s.soc_start_plan_pct,p.soc_end_plan_pct=s.soc_end_plan_pct,
          p.soc_floor_pct=s.soc_floor_pct,p.soc_target_pct=s.soc_target_pct,p.planned_buy_kwh=s.planned_buy_kwh,
          p.planned_sell_kwh=s.planned_sell_kwh,p.planned_pv_export_kwh=s.planned_pv_export_kwh,
          p.planned_battery_charge_kwh=s.planned_battery_charge_kwh,
          p.planned_battery_discharge_kwh=s.planned_battery_discharge_kwh,p.recommendation=s.recommendation,
          p.grid_policy_planned=s.grid_policy_planned,p.export_policy_planned=s.export_policy_planned,
          p.pv_to_bat_planned=s.pv_to_bat_planned,p.pv_to_cwu_planned=s.pv_to_cwu_planned,
          p.pv_to_ev_planned=s.pv_to_ev_planned,p.pv_export_planned=s.pv_export_planned,
          p.pv_curtail_planned=s.pv_curtail_planned,
          p.planned_pv_to_bat_kwh=s.planned_pv_to_bat_kwh,
          p.planned_pv_to_cwu_kwh=s.planned_pv_to_cwu_kwh,
          p.planned_pv_to_ev_kwh=s.planned_pv_to_ev_kwh,
          p.planned_pv_curtail_kwh=s.planned_pv_curtail_kwh,
          p.grid_buy_allowed=s.grid_buy_allowed,p.grid_no_buy=s.grid_no_buy,p.grid_neutral=s.grid_neutral,
          p.sell_bat_allowed=s.sell_bat_allowed,p.no_sell_bat=s.no_sell_bat,
          p.sell_pv_allowed=s.sell_pv_allowed,p.no_sell_pv=s.no_sell_pv,
          p.heat_pump_window=s.heat_pump_window,
          p.ppd_reason=s.ppd_reason,p.ppd_run_type=%s,
          p.ppd_version='CORE_0_4_1',p.ppd_locked_at=NOW(6),p.plan_run_id=%s,p.plan_stage='PUBLISHED',
          p.plan_stage_version='CORE_0_4_1',p.plan_stage_updated_at=NOW(6),
          p.plan_validation_status='ACCEPTED',p.plan_validation_reason='OK',
          p.plan_published_at=NOW(6),p.plan_published=1 WHERE p.actual_recorded_at IS NULL AND p.slot_start>=%s""",
          (run_id,run_type,run_id,cutoff))
        published=cur.rowcount
        cur.execute("""UPDATE ems_gpt_plan_runs SET status='PUBLISHED',current_stage='VALIDATE',
          validated_at=NOW(6),published_at=NOW(6),validation_status='ACCEPTED',
          validation_reason='OK',updated_at=NOW(6) WHERE run_id=%s""",(run_id,))
        audit_stage(cur,run_id,"VALIDATE","ACCEPTED",published,"atomic publish")
    record_event("plan_published","planner",{"run_id":run_id,"run_type":run_type,"rows":published})
    for shortfall in hp_shortfalls:
        record_event("hp_minimum_heating_shortfall", "planner", shortfall, "WARNING")
    return {"run_id":run_id,"rows":published}


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
