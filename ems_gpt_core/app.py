#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, urlencode
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql
from pymysql.cursors import DictCursor

from api_service import ApiAdapters, build_handler
from analytics_service import run_analytics as run_analytics_service
from diagnostics_service import generate_diagnostic_report as run_diagnostics_service
from executor_service import ExecutorAdapters, build_executor
from ingestion_service import IngestionAdapters, build_ingestion
from scheduler_service import SchedulerAdapters, run_scheduler
from observer_service import run_ai_observer as run_observer_service
from todo_service import TodoService

APP_NAME = "EMS-GPT Core"
APP_VERSION = "0.26.4"
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
RUNTIME_SETTINGS_PATH = DATA_DIR / "runtime-settings.json"
HA_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("ems-gpt-core")


def load_options() -> dict:
    result = {
        "timezone": "Europe/Warsaw",
        "slot_minutes": 15,
        "command_ttl_seconds": 960,
        "purchase_margin_pln_kwh": 0.59,
        "battery_charge_efficiency": 0.90,
        "battery_discharge_efficiency": 0.95,
        "battery_degradation_cost_pln_kwh": 0.08,
        "battery_capacity_kwh": 15.0,
        "battery_min_soc_pct": 15.0,
        "battery_max_power_kw": 5.0,
        "minimum_arbitrage_margin_pln_kwh": 0.05,
        "historical_soc_drop_p80_pct": 60.0,
        "pv_cwu_min_surplus_kw": 2.0,
        "pv_ev_min_surplus_kw": 1.5,
        "evening_soc_target_pct": 60.0,
        "forecast_uncertainty_weight": 1.0,
        "terminal_soc_value_weight": 1.0,
        "buy_window_tolerance_pln_kwh": 0.05,
        "planned_flow_threshold_kwh": 0.02,
        "technical_flow_threshold_kwh": 0.05,
        "soc_floor_max_pct": 90.0,
        "soc_target_max_pct": 95.0,
        "sale_morning_start_hour": 6.0,
        "sale_morning_end_hour": 10.0,
        "sale_evening_start_hour": 17.0,
        "sale_evening_end_hour": 23.0,
        "night_heating_threshold_c": 10.0,
        "hp_min_heating_hours": 10.0,
        "hp_min_cycle_hours": 2.0,
        "hp_min_cycle_break_hours": 1.0,
        "hp_max_cycle_break_hours": 3.0,
        "hp_planned_power_kw": 2.5,
        "hp_cycle_start_penalty_pln": 0.25,
        "telemetry_min_samples_per_slot": 10.0,
        "telemetry_learning_coverage_pct": 80.0,
        "observer_pv_wape_warn_pct": 30.0,
        "observer_load_wape_warn_pct": 35.0,
        "observer_soc_mae_warn_pct": 8.0,
        "observer_cost_variance_warn_pln": 10.0,
        "observer_min_quality_score_pct": 80.0,
        "recovery_lookback_days": 7.0,
        "db_host": "core-mariadb",
        "db_port": 3306,
        "db_name": "ems_gpt",
        "db_user": "ems_gpt_app",
        "db_password": "",
        "source_db": "ems_gpt",
        "bootstrap_from_source": False,
        "direct_battery_power_mode": "discharge_positive",
        "executor_enabled": False,
        "executor_dry_run": True,
        "executor_activation_ack": "",
        "connector_service_map_json": "{}",
        "ai_observer_enabled": True,
    }
    try:
        result.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    try:
        result.update(json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    return result


OPTIONS = load_options()
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
LOCK = threading.Lock()
HEAVY_JOB_LOCK = threading.RLock()


def run_serialized(job_name: str, func, *args, **kwargs):
    """Serialize heavy database jobs across engine and HTTP worker threads."""
    started = time.monotonic()
    with HEAVY_JOB_LOCK:
        waited = round(time.monotonic() - started, 3)
        if waited >= 1.0:
            LOG.warning("heavy job %s waited %.3fs for lock", job_name, waited)
        return func(*args, **kwargs)


STATE = {
    "app": APP_NAME,
    "version": APP_VERSION,
    "status": "STARTING",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "database": "CONNECTING",
    "ha_input": "CONNECTING",
    "executor": "CONNECTOR_REQUIRED",
    "active_slot": None,
    "last_heartbeat": None,
    "last_error": None,
    "migrated_tables": 0,
    "modules": {"core": "STARTING", "planner": "STARTING", "ppd": "STARTING", "analytics": "STARTING", "ai_observer": "SHADOW_READ_ONLY", "executor": "CONNECTOR_REQUIRED"},
    "recovery_contract": "CORE_RECOVERY_0_24_2_R6",
}


@contextmanager
def db(database: str | None = None):
    conn = pymysql.connect(
        host=OPTIONS["db_host"], port=int(OPTIONS["db_port"]),
        user=OPTIONS["db_user"], password=OPTIONS["db_password"],
        database=database or OPTIONS["db_name"], charset="utf8mb4",
        autocommit=False, cursorclass=DictCursor, connect_timeout=10,
        read_timeout=30, write_timeout=30,
    )
    try:
        with conn.cursor() as cur:
            offset = datetime.now(TZ).utcoffset() or timedelta(0)
            minutes = int(offset.total_seconds() // 60)
            sign = "+" if minutes >= 0 else "-"
            hours, mins = divmod(abs(minutes), 60)
            cur.execute("SET time_zone=%s", (f"{sign}{hours:02d}:{mins:02d}",))
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def qname(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError("Invalid SQL identifier")
    return f"`{value}`"


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


def bootstrap_legacy_tables() -> int:
    if not OPTIONS.get("bootstrap_from_source", False):
        return 0
    source, target = OPTIONS["source_db"], OPTIONS["db_name"]
    if source == target:
        return 0
    migrated = 0
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ems_gpt_core_migrations WHERE migration_key=%s", ("legacy_tables_1_to_1",))
        if cur.fetchone():
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=%s AND table_name LIKE 'ems_gpt_%%'", (target,))
            return int(cur.fetchone()["n"])
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name LIKE 'ems_gpt_%%' ORDER BY table_name", (source,))
        tables = [row["table_name"] for row in cur.fetchall()]
        for table in tables:
            src = f"{qname(source)}.{qname(table)}"
            dst = f"{qname(target)}.{qname(table)}"
            cur.execute(f"CREATE TABLE IF NOT EXISTS {dst} LIKE {src}")
            cur.execute(f"INSERT IGNORE INTO {dst} SELECT * FROM {src}")
            migrated += 1
        cur.execute("INSERT INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("legacy_tables_1_to_1", json.dumps({"source": source, "target": target, "tables": tables}, ensure_ascii=False)))
    return migrated


def recover_interrupted_runs() -> dict:
    """Idempotently close runs that could not survive an application restart."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE ems_gpt_plan_runs SET status='ABORTED_RECOVERED',
          current_stage='RECOVERY',updated_at=NOW(6),validation_status='REJECTED',
          validation_reason='application restart interrupted the run'
          WHERE status='RUNNING' AND updated_at<NOW(6)-INTERVAL 10 MINUTE""")
        plans = cur.rowcount
        cur.execute("""UPDATE ems_gpt_core_analytics_runs SET status='ABORTED_RECOVERED',
          completed_at=NOW(6),details_json=JSON_OBJECT('reason','application restart interrupted the run')
          WHERE status='RUNNING' AND started_at<NOW(6)-INTERVAL 10 MINUTE""")
        analytics = cur.rowcount
    result = {"plan_runs": plans, "analytics_runs": analytics}
    if plans or analytics:
        record_event("interrupted_runs_recovered", "diagnostics", result, "WARNING")
    return result


def local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(TZ)


def slot_start(now: datetime | None = None) -> datetime:
    value = (now or local_now()).astimezone(TZ)
    minutes = int(OPTIONS["slot_minutes"])
    return value.replace(minute=(value.minute // minutes) * minutes, second=0, microsecond=0)


def canonical_slots_for_day(day) -> list[dict]:
    """Return every real 15-minute instant of a Warsaw civil day (92/96/100)."""
    local_begin = datetime.combine(day, datetime.min.time(), TZ)
    local_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ)
    cursor = local_begin.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    rows = []
    while cursor < end_utc:
        local = cursor.astimezone(TZ)
        rows.append({
            "slot_id": cursor.strftime("%Y%m%dT%H%MZ"),
            "slot_start_utc": cursor.replace(tzinfo=None),
            "slot_end_utc": (cursor + timedelta(minutes=15)).replace(tzinfo=None),
            "slot_start_local": local.replace(tzinfo=None),
            "utc_offset_minutes": int((local.utcoffset() or timedelta()).total_seconds() // 60),
            "local_fold": int(local.fold), "local_day": day, "slot_index_local": len(rows),
        })
        cursor += timedelta(minutes=15)
    return rows


def ensure_slot_calendar(*days) -> dict:
    inserted = 0
    counts = {}
    with db() as conn, conn.cursor() as cur:
        for day in days:
            rows = canonical_slots_for_day(day)
            counts[str(day)] = len(rows)
            for row in rows:
                values = tuple(row[k] for k in ("slot_id","slot_start_utc","slot_end_utc","slot_start_local",
                    "utc_offset_minutes","local_fold","local_day","slot_index_local"))
                cur.execute("""INSERT INTO ems_gpt_core_slot_calendar
                  (slot_id,slot_start_utc,slot_end_utc,slot_start_local,utc_offset_minutes,
                   local_fold,local_day,slot_index_local) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                  ON DUPLICATE KEY UPDATE slot_start_local=VALUES(slot_start_local),
                  utc_offset_minutes=VALUES(utc_offset_minutes),local_fold=VALUES(local_fold),
                  local_day=VALUES(local_day),slot_index_local=VALUES(slot_index_local)""", values)
                inserted += cur.rowcount
                if not row["local_fold"]:
                    for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows"):
                        cur.execute(f"""UPDATE {table} SET slot_id=%s,slot_start_utc=%s,
                          slot_start_local=%s,utc_offset_minutes=%s,local_fold=%s,
                          local_day=%s,slot_index_local=%s WHERE slot_start=%s AND slot_id IS NULL""",
                          (row["slot_id"],row["slot_start_utc"],row["slot_start_local"],
                           row["utc_offset_minutes"],row["local_fold"],row["local_day"],
                           row["slot_index_local"],row["slot_start_local"]))
    return {"days": counts, "writes": inserted}


SLOT_RELATION_TABLES = (
    "ems_gpt_core_events", "ems_gpt_core_module_runs", "ems_gpt_core_slot_quality",
    "ems_gpt_core_process_decisions", "ems_gpt_core_commands",
    "ems_gpt_core_process_execution", "ems_gpt_core_execution_details",
)


def backfill_slot_relations() -> dict:
    """Populate canonical calendar and slot_id for every historical relation."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT DATE(slot_start) day FROM ems_gpt_slots WHERE slot_start IS NOT NULL ORDER BY day")
        days = [row["day"] for row in cur.fetchall()]
    if days:
        ensure_slot_calendar(*days)
    unresolved = {}
    updated = 0
    with db() as conn, conn.cursor() as cur:
        for table in SLOT_RELATION_TABLES:
            cur.execute(f"""UPDATE {table} t JOIN ems_gpt_core_slot_calendar c
              ON c.slot_start_local=t.slot_start AND c.local_fold=0
              SET t.slot_id=c.slot_id WHERE t.slot_start IS NOT NULL AND t.slot_id IS NULL""")
            updated += cur.rowcount
            cur.execute(f"SELECT COUNT(*) n FROM {table} WHERE slot_start IS NOT NULL AND slot_id IS NULL")
            unresolved[table] = int(cur.fetchone()["n"] or 0)
    return {"days": len(days), "updated": updated, "unresolved": unresolved,
            "ready": not any(unresolved.values())}


def ha_state(entity_id: str) -> dict | None:
    if not SUPERVISOR_TOKEN:
        return None
    req = urllib.request.Request(f"{HA_API}/states/{entity_id}", headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)
    except Exception:
        return None


def ha_service_response(domain: str, service: str, payload: dict, *, return_response: bool = False) -> dict | None:
    if not SUPERVISOR_TOKEN:
        return None
    service_url = f"{HA_API}/services/{domain}/{service}"
    if return_response:
        service_url += "?return_response"
    req = urllib.request.Request(
        service_url,
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    except Exception as exc:
        LOG.warning("HA service response failed: %s.%s: %s", domain, service, exc)
        return None


def number(state: dict | None, attribute: str | None = None) -> float | None:
    if not isinstance(state, dict):
        return None
    try:
        value = state.get("attributes", {}).get(attribute) if attribute else state.get("state")
        return float(value) if value not in (None, "unknown", "unavailable", "") else None
    except (TypeError, ValueError):
        return None


def tou_program_snapshot() -> list[dict] | None:
    """Read the six Deye TOU boundaries and SOC floors without writing them."""
    programs = []
    for index in range(1, 7):
        time_state = ha_state(f"time.inverter_program_{index}_time")
        soc_state = ha_state(f"number.inverter_program_{index}_soc")
        raw_time = str((time_state or {}).get("state") or "")
        try:
            hour, minute = [int(value) for value in raw_time.split(":")[:2]]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (TypeError, ValueError):
            return None
        soc = number(soc_state)
        if soc is None or not 0 <= soc <= 100:
            return None
        programs.append({"program": index, "minute": hour * 60 + minute, "soc": float(soc)})
    programs.sort(key=lambda item: (item["minute"], item["program"]))
    return programs


def active_tou_program(moment: datetime, programs: list[dict] | None) -> dict | None:
    """Resolve the Deye wall-clock TOU program for a local slot or live instant."""
    if not programs:
        return None
    minute = moment.hour * 60 + moment.minute
    active = programs[-1]
    for program in programs:
        if program["minute"] <= minute:
            active = program
        else:
            break
    return active


def power_w(state: dict | None) -> float | None:
    """Normalize HA power sensors to watts before telemetry persistence."""
    value = number(state)
    if value is None:
        return None
    unit = str((state or {}).get("attributes", {}).get("unit_of_measurement") or "W").lower()
    if unit == "kw":
        return value * 1000.0
    if unit == "mw":
        return value * 1_000_000.0
    return value


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


def capture_telemetry() -> bool:
    with ThreadPoolExecutor(max_workers=min(8, len(ENTITIES))) as pool:
        states = dict(zip(ENTITIES, pool.map(ha_state, ENTITIES.values())))
    available = sum(value is not None for value in states.values())
    clock = local_now()
    active = slot_start(clock)
    now = clock.replace(tzinfo=None)
    start = active.replace(tzinfo=None)
    start_utc = active.astimezone(timezone.utc)
    slot_id = start_utc.strftime("%Y%m%dT%H%MZ")
    day_slots = canonical_slots_for_day(active.date())
    slot_index = next((r["slot_index_local"] for r in day_slots if r["slot_id"] == slot_id), None)
    payload = {key: value for key, value in states.items() if value is not None}
    configured_direct_mode = str(OPTIONS.get("direct_battery_power_mode", "discharge_positive"))
    # Existing installations may retain the retired value in Supervisor options.
    # Deye reports charging as negative and discharging as positive; migrate the
    # old selection without reading V1/V2/V3 helper entities.
    direct_mode = "discharge_positive" if configured_direct_mode == "legacy_helpers" else configured_direct_mode
    direct_power = power_w(states.get("battery_direct"))
    if direct_mode == "charge_positive" and direct_power is not None:
        battery_charge_w, battery_discharge_w = max(0.0, direct_power), max(0.0, -direct_power)
        battery_source = "INVERTER_DIRECT_CHARGE_POSITIVE"
    elif direct_mode == "discharge_positive" and direct_power is not None:
        battery_charge_w, battery_discharge_w = max(0.0, -direct_power), max(0.0, direct_power)
        battery_source = "INVERTER_DIRECT_DISCHARGE_POSITIVE"
    else:
        battery_charge_w = None
        battery_discharge_w = None
        battery_source = "DIRECT_SENSOR_UNAVAILABLE_OR_MODE_INVALID"
    payload["battery_power_resolution"] = {"configured_mode": configured_direct_mode,
                                           "mode": direct_mode, "source": battery_source,
                                           "direct_power_w": direct_power}
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT price_sell_pln_kwh,price_buy_pln_kwh FROM ems_gpt_slots
          WHERE slot_start=%s LIMIT 1""", (start,))
        price_row = cur.fetchone() or {}
        rce_sell = price_row.get("price_sell_pln_kwh")
        rce_buy = price_row.get("price_buy_pln_kwh")
        payload["rce_price_resolution"] = {"source": "EMS_GPT_SLOTS", "slot_id": slot_id,
                                           "sell": rce_sell, "buy": rce_buy}
        cur.execute("""INSERT INTO ems_gpt_telemetry_snapshots
          (captured_at,slot_start,soc_pct,pv_power_w,load_power_w,grid_power_w,
           rce_sell_pln_kwh,rce_buy_pln_kwh,dhw_temperature_c,battery_charge_power_w,
           battery_discharge_power_w,ev_power_w,dhw_power_w,pv1_power_w,pv2_power_w,
           hp_outlet_temperature_c,hp_inlet_temperature_c,hp_compressor_frequency_hz,
           hp_compressor_current_a,hp_flow_l_min,hp_heat_consumption_w,hp_heat_production_w,
           hp_dhw_production_w,hp_cool_consumption_w,hp_cool_production_w,outside_temperature_c,
           hp_operations_counter,hp_operations_hours,source_status,payload_json,
           slot_id,slot_start_utc,slot_start_local,utc_offset_minutes,local_fold,local_day,slot_index_local)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  %s,%s,%s,%s,%s,%s,%s)""",
          (now, start, number(states["soc"]), power_w(states["pv"]), power_w(states["load"]),
           power_w(states["grid"]), rce_sell, rce_buy, number(states["dhw"]),
           battery_charge_w, battery_discharge_w,
           power_w(states["ev_power"]), power_w(states["dhw_power"]),
           power_w(states["pv1"]), power_w(states["pv2"]),
           number(states["hp_outlet"]), number(states["hp_inlet"]),
           number(states["hp_compressor_freq"]), number(states["hp_compressor_current"]),
           number(states["hp_flow"]),
           power_w(states["hp_heat_consumption"]), power_w(states["hp_heat_production"]),
           power_w(states["hp_dhw_production"]), power_w(states["hp_cool_consumption"]),
           power_w(states["hp_cool_production"]), number(states["outside_temperature"]),
           number(states["hp_operations_counter"]), number(states["hp_operations_hours"]),
           "COMPLETE" if available == len(states) else "PARTIAL", json.dumps(payload, ensure_ascii=False),
           slot_id,start_utc.replace(tzinfo=None),start,
           int((active.utcoffset() or timedelta()).total_seconds()//60),int(active.fold),active.date(),slot_index))
    return available > 0


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


def close_finished_slots() -> int:
    current = slot_start().replace(tzinfo=None)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT slot_start,slot_end FROM ems_gpt_slots
          WHERE slot_end<=%s AND actual_recorded_at IS NULL
          ORDER BY slot_start ASC LIMIT 2688""",(current,))
        pending=list(cur.fetchall())
        closed=0
        for row in reversed(pending):
            cur.execute("""SELECT AVG(pv_power_w) pv,AVG(pv1_power_w) pv1,AVG(pv2_power_w) pv2,
              AVG(load_power_w) load_kwh,AVG(grid_power_w) grid,
              AVG(battery_charge_power_w) battery_charge,AVG(battery_discharge_power_w) battery_discharge,
              AVG(ev_power_w) ev_power,AVG(dhw_power_w) dhw_power,
              AVG(hp_outlet_temperature_c) hp_outlet,AVG(hp_inlet_temperature_c) hp_inlet,
              AVG(hp_compressor_frequency_hz) hp_freq,AVG(hp_compressor_current_a) hp_current,
              AVG(hp_flow_l_min) hp_flow,
              AVG(hp_heat_consumption_w) hp_heat_cons,AVG(hp_heat_production_w) hp_heat_prod,
              AVG(hp_dhw_production_w) hp_dhw_prod,AVG(hp_cool_consumption_w) hp_cool_cons,
              AVG(hp_cool_production_w) hp_cool_prod,AVG(outside_temperature_c) outside_temp,
              SUBSTRING_INDEX(GROUP_CONCAT(hp_operations_counter ORDER BY captured_at DESC),',',1) hp_ops,
              SUBSTRING_INDEX(GROUP_CONCAT(hp_operations_hours ORDER BY captured_at DESC),',',1) hp_hours,
              SUBSTRING_INDEX(GROUP_CONCAT(soc_pct ORDER BY captured_at ASC),',',1) soc_start,
              MIN(soc_pct) soc_min,SUBSTRING_INDEX(GROUP_CONCAT(soc_pct ORDER BY captured_at DESC),',',1) soc,
              SUBSTRING_INDEX(GROUP_CONCAT(dhw_temperature_c ORDER BY captured_at DESC),',',1) dhw,
              COUNT(*) samples,SUM(source_status='COMPLETE') complete_samples,
              MIN(captured_at) first_sample,MAX(captured_at) last_sample
              FROM ems_gpt_telemetry_snapshots
              WHERE captured_at>=%s AND captured_at<%s""",(row["slot_start"],row["slot_end"]))
            m=cur.fetchone(); samples=int(m["samples"] or 0)
            if samples==0:
                cur.execute("""UPDATE ems_gpt_slots SET actual_recorded_at=NOW(6),
                  actual_mode='MISSING_OUTAGE',execution_reason='NO_TELEMETRY_AFTER_HA_OUTAGE',matched=0
                  WHERE slot_start=%s AND actual_recorded_at IS NULL""", (row["slot_start"],))
                slot_closed=cur.rowcount
                if slot_closed:
                    cur.execute("""INSERT INTO ems_gpt_core_execution_details
                      (slot_start,sample_count,coverage_pct,first_sample_at,last_sample_at,
                       actual_grid_export_kwh,actual_ev_kwh,actual_dhw_kwh,complete_source_samples,
                       export_attribution,soc_start_pct,soc_min_pct,soc_delta_pct,
                       recovery_status,quality_status,updated_at)
                      VALUES(%s,0,0,NULL,NULL,NULL,NULL,NULL,0,'UNRESOLVED',NULL,NULL,NULL,
                       'MISSING_OUTAGE','MISSING',NOW(6)) ON DUPLICATE KEY UPDATE
                       sample_count=0,coverage_pct=0,recovery_status='MISSING_OUTAGE',
                       quality_status='MISSING',updated_at=NOW(6)""", (row["slot_start"],))
                    closed += 1
                continue
            pv=float(m["pv"] or 0)*.25/1000; pv1=float(m["pv1"] or 0)*.25/1000; pv2=float(m["pv2"] or 0)*.25/1000
            load=float(m["load_kwh"] or 0)*.25/1000; grid=float(m["grid"] or 0)*.25/1000
            battery_charge=float(m["battery_charge"] or 0)*.25/1000
            battery_discharge=float(m["battery_discharge"] or 0)*.25/1000
            ev_energy=float(m["ev_power"] or 0)*.25/1000
            dhw_energy=float(m["dhw_power"] or 0)*.25/1000
            hp_heat_cons=float(m["hp_heat_cons"] or 0)*.25/1000
            hp_heat_prod=float(m["hp_heat_prod"] or 0)*.25/1000
            hp_dhw_prod=float(m["hp_dhw_prod"] or 0)*.25/1000
            hp_cool_cons=float(m["hp_cool_cons"] or 0)*.25/1000
            hp_cool_prod=float(m["hp_cool_prod"] or 0)*.25/1000
            hp_total_cons=hp_heat_cons+dhw_energy+hp_cool_cons
            hp_total_prod=hp_heat_prod+hp_dhw_prod+hp_cool_prod
            hp_cop=hp_total_prod/hp_total_cons if hp_total_cons>.001 else None
            hp_mode="HEATING" if hp_heat_prod>.001 else "DHW" if hp_dhw_prod>.001 else "COOLING" if hp_cool_prod>.001 else "IDLE"
            grid_export=round(max(0,-grid),6)
            coverage=round(min(100.0, samples/15*100),2)
            cur.execute("""UPDATE ems_gpt_slots SET actual_recorded_at=NOW(6),
              actual_pv_total_kwh=%s,actual_load_kwh=%s,actual_buy_kwh=%s,
              actual_pv_export_kwh=%s,soc_end_pct=%s,actual_dhw_temperature_c=%s,
              actual_battery_charge_kwh=%s,actual_battery_discharge_kwh=%s,
              actual_pv1_kwh=%s,actual_pv2_kwh=%s,
              actual_heat_pump_outlet_temperature_c=%s,actual_heat_pump_inlet_temperature_c=%s,
              actual_heat_pump_compressor_frequency_hz=%s,actual_heat_pump_compressor_current_a=%s,
              actual_heat_pump_flow_l_min=%s,actual_heat_pump_delta_t_c=%s,
              actual_heat_pump_is_running=%s,actual_temperature_c=%s,
              actual_heating_consumed_kwh=%s,actual_heating_generated_kwh=%s,actual_heating_cop=%s,
              actual_dhw_consumed_kwh=%s,actual_dhw_generated_kwh=%s,actual_dhw_cop=%s,
              actual_cooling_consumed_kwh=%s,actual_cooling_generated_kwh=%s,actual_cooling_cop=%s,
              actual_heat_pump_electric_kwh=%s,actual_heat_pump_thermal_kwh=%s,actual_heat_pump_cop=%s,
              actual_heat_pump_mode=%s,actual_heat_pump_operations_counter=%s,
              actual_heat_pump_operations_hours=%s,actual_mode=%s,execution_reason=%s,matched=%s
              WHERE slot_start=%s AND actual_recorded_at IS NULL""",
              (round(pv,6),round(load,6),round(max(0,grid),6),grid_export,
               float(m["soc"]) if m["soc"] not in (None,"") else None,
               float(m["dhw"]) if m["dhw"] not in (None,"") else None,
               round(battery_charge,6),round(battery_discharge,6),
               round(pv1,6),round(pv2,6),m["hp_outlet"],m["hp_inlet"],m["hp_freq"],m["hp_current"],m["hp_flow"],
               (float(m["hp_outlet"])-float(m["hp_inlet"])) if m["hp_outlet"] is not None and m["hp_inlet"] is not None else None,
               1 if float(m["hp_freq"] or 0)>0 else 0,
               m["outside_temp"],round(hp_heat_cons,6),round(hp_heat_prod,6),hp_heat_prod/hp_heat_cons if hp_heat_cons>.001 else None,
               round(dhw_energy,6),round(hp_dhw_prod,6),hp_dhw_prod/dhw_energy if dhw_energy>.001 else None,
               round(hp_cool_cons,6),round(hp_cool_prod,6),hp_cool_prod/hp_cool_cons if hp_cool_cons>.001 else None,
               round(hp_total_cons,6),round(hp_total_prod,6),hp_cop,hp_mode,
               float(m["hp_ops"]) if m["hp_ops"] not in (None,"") else None,
               float(m["hp_hours"]) if m["hp_hours"] not in (None,"") else None,
               "OBSERVED",f"CORE_TELEMETRY_{samples}_SAMPLES",None,row["slot_start"]))
            slot_closed=cur.rowcount
            if slot_closed:
                cur.execute("""INSERT INTO ems_gpt_core_execution_details
                  (slot_start,sample_count,coverage_pct,first_sample_at,last_sample_at,
                   actual_grid_export_kwh,actual_ev_kwh,actual_dhw_kwh,complete_source_samples,
                   export_attribution,soc_start_pct,soc_min_pct,soc_delta_pct,
                   recovery_status,quality_status,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'UNRESOLVED',%s,%s,%s,
                   %s,%s,NOW(6))
                  ON DUPLICATE KEY UPDATE sample_count=VALUES(sample_count),coverage_pct=VALUES(coverage_pct),
                   first_sample_at=VALUES(first_sample_at),last_sample_at=VALUES(last_sample_at),
                   actual_grid_export_kwh=VALUES(actual_grid_export_kwh),actual_ev_kwh=VALUES(actual_ev_kwh),
                   actual_dhw_kwh=VALUES(actual_dhw_kwh),complete_source_samples=VALUES(complete_source_samples),
                   soc_start_pct=VALUES(soc_start_pct),soc_min_pct=VALUES(soc_min_pct),soc_delta_pct=VALUES(soc_delta_pct),
                   recovery_status=VALUES(recovery_status),quality_status=VALUES(quality_status),updated_at=NOW(6)""",
                  (row["slot_start"],samples,coverage,m["first_sample"],m["last_sample"],grid_export,
                   round(ev_energy,6),round(dhw_energy,6),int(m["complete_samples"] or 0),
                   float(m["soc_start"]) if m["soc_start"] not in (None,"") else None,m["soc_min"],
                   float(m["soc"])-float(m["soc_start"]) if m["soc"] not in (None,"") and m["soc_start"] not in (None,"") else None,
                   "RECOVERED" if row["slot_end"] < current-timedelta(minutes=15) else "OBSERVED",
                   "ACCEPTED" if coverage>=float(OPTIONS.get("telemetry_learning_coverage_pct",80.0)) else "PARTIAL"))
                observed_energy = {
                    # Below 50 Wh/slot the inverter flow is technical noise, not an EMS process.
                    "BATTERY_IMPORT": round(battery_charge, 6) if battery_charge >= float(OPTIONS.get("technical_flow_threshold_kwh",0.05)) else 0.0,
                    "BATTERY_EXPORT": round(battery_discharge, 6) if battery_discharge >= float(OPTIONS.get("technical_flow_threshold_kwh",0.05)) else 0.0,
                    "PV_CWU": round(dhw_energy, 6),
                    "PV_EV": round(ev_energy, 6),
                    "HP_HEAT_DHW": round(hp_heat_cons + dhw_energy, 6),
                }
                cur.execute("""SELECT d.process_name,d.eligible,d.decision,o.requested_state,
                  o.override_id,o.requested_by,o.reason override_reason,o.valid_until override_valid_until
                  FROM ems_gpt_core_process_decisions d
                  LEFT JOIN ems_gpt_core_process_overrides o ON o.process_name=d.process_name
                   AND o.valid_from<d.valid_until AND o.valid_until>d.slot_start
                   AND o.status IN ('ACTIVE','EXPIRED')
                  WHERE d.slot_start=%s""", (row["slot_start"],))
                for process in cur.fetchall():
                    planned = "ON" if process["eligible"] else "OFF"
                    requested = process.get("requested_state")
                    energy_value = observed_energy.get(process["process_name"])
                    running_threshold = 0.02 if process["process_name"] == "HP_HEAT_DHW" else 0.001
                    observed = None if energy_value is None else ("RUNNING" if energy_value > running_threshold else "IDLE_OR_DISCONNECTED")
                    external_manual = requested is None and observed == "RUNNING" and planned == "OFF"
                    effective = ("ON" if requested == "FORCE_ON" else
                                 "OFF" if requested == "FORCE_OFF" else
                                 "ON" if external_manual else planned)
                    control_origin = ("MANUAL_FORCE_ON" if requested == "FORCE_ON" else
                                      "MANUAL_BLOCK" if requested == "FORCE_OFF" else
                                      "EXTERNAL_MANUAL" if external_manual else "AUTO")
                    cur.execute("""INSERT INTO ems_gpt_core_process_execution
                      (command_id,slot_start,slot_id,process_name,planned_state,effective_state,observed_state,
                       observed_energy_kwh,decision_source,reason,override_id,requested_by,override_reason,
                       override_valid_until,control_origin,recorded_at)
                      VALUES(NULL,%s,(SELECT slot_id FROM ems_gpt_core_slot_calendar WHERE slot_start_local=%s AND local_fold=0 LIMIT 1),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6))""",
                      (row["slot_start"], row["slot_start"], process["process_name"], planned, effective, observed, energy_value,
                       "OVERRIDE" if requested else "PLAN", process.get("override_reason") or process["decision"],
                       process.get("override_id"), process.get("requested_by"), process.get("override_reason"),
                       process.get("override_valid_until"), control_origin))
            closed+=slot_closed
    if closed: record_event("slots_closed","core",{"count":closed})
    return closed


def backfill_execution_details() -> int:
    """Idempotently reconstruct recent detail rows created before schema 0.17."""
    cutoff = local_now().replace(tzinfo=None) - timedelta(hours=24)
    accepted_samples = max(1, int(15 * float(OPTIONS.get("telemetry_learning_coverage_pct", 80.0)) / 100.0 + 0.999))
    with db() as conn, conn.cursor() as cur:
        cur.execute("""INSERT IGNORE INTO ems_gpt_core_execution_details
          (slot_start,sample_count,coverage_pct,first_sample_at,last_sample_at,
           actual_grid_export_kwh,actual_ev_kwh,actual_dhw_kwh,complete_source_samples,
           export_attribution,recovery_status,quality_status,updated_at)
          SELECT s.slot_start,COUNT(t.captured_at),LEAST(100,COUNT(t.captured_at)/15*100),
           MIN(t.captured_at),MAX(t.captured_at),
           ROUND(GREATEST(0,-AVG(t.grid_power_w))*.25/1000,6),
           ROUND(COALESCE(AVG(t.ev_power_w),0)*.25/1000,6),
           ROUND(CASE WHEN ABS(COALESCE(AVG(t.dhw_power_w),0)) BETWEEN .001 AND 50
             THEN AVG(t.dhw_power_w)*.25 ELSE COALESCE(AVG(t.dhw_power_w),0)*.25/1000 END,6),
           SUM(t.source_status='COMPLETE'),'UNRESOLVED','RECOVERED',
           CASE WHEN COUNT(t.captured_at)>=%s THEN 'ACCEPTED' ELSE 'PARTIAL' END,NOW(6)
          FROM ems_gpt_slots s JOIN ems_gpt_telemetry_snapshots t
            ON t.captured_at>=s.slot_start AND t.captured_at<s.slot_end
          LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
          WHERE s.actual_recorded_at IS NOT NULL AND s.slot_start>=%s AND d.slot_start IS NULL
          GROUP BY s.slot_start""", (accepted_samples, cutoff))
        restored = cur.rowcount
    if restored:
        record_event("execution_details_backfilled", "core", {"rows": restored})
    return restored


def aggregate_results() -> None:
    """Maintain current hourly and daily plan-vs-actual materializations."""
    now=local_now().replace(tzinfo=None)
    day_start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    with db() as conn,conn.cursor() as cur:
        cur.execute("""SELECT DATE_FORMAT(slot_start,'%%Y-%%m-%%d %%H:00:00') hour_start,
          COUNT(*) slots,SUM(actual_recorded_at IS NOT NULL) actual_n,
          SUM(forecast_pv_total_kwh) fpv,SUM(actual_pv_total_kwh) apv,
          SUM(forecast_load_kwh) fload,SUM(actual_load_kwh) aload,
          SUM(planned_buy_kwh) pbuy,SUM(actual_buy_kwh) abuy,
          SUM(COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0)) pexport,
          SUM(COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0)) aexport,
          SUM((COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(planned_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) pnet,
          SUM((COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(actual_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) anet,
          SUBSTRING_INDEX(GROUP_CONCAT(soc_end_pct ORDER BY slot_start DESC),',',1) soc
          FROM ems_gpt_slots WHERE slot_start>=%s
          GROUP BY DATE_FORMAT(slot_start,'%%Y-%%m-%%d %%H:00:00')""",(day_start,))
        hours=cur.fetchall()
        for r in hours:
            cur.execute("""INSERT INTO ems_gpt_core_hourly
              (hour_start,slot_count,forecast_pv_kwh,actual_pv_kwh,forecast_load_kwh,actual_load_kwh,
               planned_import_kwh,actual_import_kwh,planned_export_kwh,actual_export_kwh,
               planned_net_pln,actual_net_pln,soc_end_pct,updated_at,quality_status)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6),%s)
              ON DUPLICATE KEY UPDATE slot_count=VALUES(slot_count),forecast_pv_kwh=VALUES(forecast_pv_kwh),
               actual_pv_kwh=VALUES(actual_pv_kwh),forecast_load_kwh=VALUES(forecast_load_kwh),
               actual_load_kwh=VALUES(actual_load_kwh),planned_import_kwh=VALUES(planned_import_kwh),
               actual_import_kwh=VALUES(actual_import_kwh),planned_export_kwh=VALUES(planned_export_kwh),
               actual_export_kwh=VALUES(actual_export_kwh),planned_net_pln=VALUES(planned_net_pln),
               actual_net_pln=VALUES(actual_net_pln),soc_end_pct=VALUES(soc_end_pct),
               updated_at=NOW(6),quality_status=VALUES(quality_status)""",
              (r["hour_start"],r["slots"],r["fpv"],r["apv"],r["fload"],r["aload"],r["pbuy"],r["abuy"],
               r["pexport"],r["aexport"],r["pnet"],r["anet"],float(r["soc"]) if r["soc"] not in (None,"") else None,
               "COMPLETE" if int(r["actual_n"] or 0)==4 else "OPEN"))
        cur.execute("""SELECT COUNT(*) slots,SUM(actual_recorded_at IS NOT NULL) actual_n,
          SUM(forecast_pv_total_kwh) fpv,SUM(actual_pv_total_kwh) apv,
          SUM(planned_battery_discharge_kwh) pdis,SUM(actual_battery_discharge_kwh) adis,
          SUM(planned_buy_kwh) pbuy,SUM(actual_buy_kwh) abuy,
          SUM(forecast_load_kwh) fload,SUM(actual_load_kwh) aload,
          SUM(planned_battery_charge_kwh) pcharge,SUM(actual_battery_charge_kwh) acharge,
          SUM(COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0)) pexport,
          SUM(COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0)) aexport,
          SUM((COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(planned_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) pnet,
          SUM((COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(actual_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) anet
          FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s""",(day_start,day_start+timedelta(days=1)))
        d=cur.fetchone()
        cur.execute("""INSERT INTO ems_gpt_daily(day_date,closed_at,slot_count,expected_slot_count,
          forecast_pv_kwh,actual_pv_kwh,forecast_battery_discharge_kwh,actual_battery_discharge_kwh,
          forecast_import_kwh,actual_import_kwh,forecast_load_kwh,actual_load_kwh,
          forecast_battery_charge_kwh,actual_battery_charge_kwh,forecast_export_kwh,actual_export_kwh,
          forecast_pv_export_kwh,actual_pv_export_kwh,planned_net_pln,actual_net_pln)
          VALUES(%s,NOW(6),%s,96,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON DUPLICATE KEY UPDATE closed_at=NOW(6),slot_count=VALUES(slot_count),
          forecast_pv_kwh=VALUES(forecast_pv_kwh),actual_pv_kwh=VALUES(actual_pv_kwh),
          forecast_battery_discharge_kwh=VALUES(forecast_battery_discharge_kwh),
          actual_battery_discharge_kwh=VALUES(actual_battery_discharge_kwh),
          forecast_import_kwh=VALUES(forecast_import_kwh),actual_import_kwh=VALUES(actual_import_kwh),
          forecast_load_kwh=VALUES(forecast_load_kwh),actual_load_kwh=VALUES(actual_load_kwh),
          forecast_battery_charge_kwh=VALUES(forecast_battery_charge_kwh),
          actual_battery_charge_kwh=VALUES(actual_battery_charge_kwh),
          forecast_export_kwh=VALUES(forecast_export_kwh),actual_export_kwh=VALUES(actual_export_kwh),
          forecast_pv_export_kwh=VALUES(forecast_pv_export_kwh),
          actual_pv_export_kwh=VALUES(actual_pv_export_kwh),
          planned_net_pln=VALUES(planned_net_pln),actual_net_pln=VALUES(actual_net_pln)""",
          (day_start.date(),d["slots"],d["fpv"],d["apv"],d["pdis"],d["adis"],d["pbuy"],d["abuy"],
           d["fload"],d["aload"],d["pcharge"],d["acharge"],d["pexport"],d["aexport"],
           d["pexport"],d["aexport"],d["pnet"],d["anet"]))


def rebuild_recovery_materializations(days: int = 7) -> dict:
    """Rebuild affected HOUR/DAILY rows after an outage without inventing actual values."""
    now = local_now().replace(tzinfo=None)
    first_day = (now-timedelta(days=max(1, days)-1)).replace(hour=0,minute=0,second=0,microsecond=0)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DATE_FORMAT(s.slot_start,'%%Y-%%m-%%d %%H:00:00') hour_start,
          COUNT(*) expected_n,SUM(s.actual_recorded_at IS NOT NULL) terminal_n,
          SUM(s.actual_mode='MISSING_OUTAGE') missing_n,
          SUM(COALESCE(d.recovery_status,'')='RECOVERED') recovered_n,
          AVG(COALESCE(d.coverage_pct,0)) coverage,
          SUM(s.forecast_pv_total_kwh) fpv,SUM(s.actual_pv_total_kwh) apv,
          SUM(s.forecast_load_kwh) fload,SUM(s.actual_load_kwh) aload,
          SUM(s.planned_buy_kwh) pbuy,SUM(s.actual_buy_kwh) abuy,
          SUM(s.planned_pv_to_bat_kwh) pvbat,SUM(s.planned_pv_to_cwu_kwh) pvcwu,
          SUM(s.planned_pv_to_ev_kwh) pvev,SUM(s.planned_pv_export_kwh) pvexport,
          SUM(s.planned_pv_curtail_kwh) pvcurtail,
          SUM(COALESCE(s.planned_sell_kwh,0)+COALESCE(s.planned_pv_export_kwh,0)) pexport,
          SUM(COALESCE(s.actual_sell_kwh,0)+COALESCE(s.actual_pv_export_kwh,0)) aexport,
          SUM((COALESCE(s.planned_sell_kwh,0)+COALESCE(s.planned_pv_export_kwh,0))*COALESCE(s.price_sell_pln_kwh,0)-COALESCE(s.planned_buy_kwh,0)*COALESCE(s.price_buy_pln_kwh,0)) pnet,
          SUM((COALESCE(s.actual_sell_kwh,0)+COALESCE(s.actual_pv_export_kwh,0))*COALESCE(s.price_sell_pln_kwh,0)-COALESCE(s.actual_buy_kwh,0)*COALESCE(s.price_buy_pln_kwh,0)) anet,
          SUBSTRING_INDEX(GROUP_CONCAT(s.soc_end_pct ORDER BY s.slot_start DESC),',',1) soc,
          MAX(s.actual_recorded_at) watermark
          FROM ems_gpt_slots s LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
          WHERE s.slot_start>=%s AND s.slot_start<%s
          GROUP BY DATE_FORMAT(s.slot_start,'%%Y-%%m-%%d %%H:00:00')""", (first_day, now+timedelta(hours=1)))
        hour_rows = list(cur.fetchall())
        for r in hour_rows:
            hour_start_value = r["hour_start"]
            if isinstance(hour_start_value, str):
                hour_start_value = datetime.strptime(hour_start_value, "%Y-%m-%d %H:%M:%S")
            expected=int(r["expected_n"] or 0); terminal=int(r["terminal_n"] or 0)
            missing=int(r["missing_n"] or 0); recovered=int(r["recovered_n"] or 0)
            ended=hour_start_value+timedelta(hours=1)<=now
            completion="CLOSED" if ended and terminal==expected else "OPEN"
            quality="ACCEPTED" if completion=="CLOSED" and missing==0 and float(r["coverage"] or 0)>=float(OPTIONS.get("telemetry_learning_coverage_pct",80.0)) else \
                    "MISSING" if completion=="CLOSED" and missing==expected else \
                    "PARTIAL" if ended else "OPEN"
            cur.execute("""INSERT INTO ems_gpt_core_hourly
              (hour_start,slot_count,forecast_pv_kwh,actual_pv_kwh,forecast_load_kwh,actual_load_kwh,
               planned_import_kwh,actual_import_kwh,planned_export_kwh,actual_export_kwh,
               planned_net_pln,actual_net_pln,soc_end_pct,updated_at,quality_status,
               expected_slot_count,terminal_slot_count,recovered_slot_count,missing_slot_count,
               coverage_pct,completion_status,source_version,input_watermark,closed_at,learning_eligible)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6),%s,%s,%s,%s,%s,%s,%s,
               %s,%s,%s,%s) ON DUPLICATE KEY UPDATE
               slot_count=VALUES(slot_count),forecast_pv_kwh=VALUES(forecast_pv_kwh),actual_pv_kwh=VALUES(actual_pv_kwh),
               forecast_load_kwh=VALUES(forecast_load_kwh),actual_load_kwh=VALUES(actual_load_kwh),
               planned_import_kwh=VALUES(planned_import_kwh),actual_import_kwh=VALUES(actual_import_kwh),
               planned_export_kwh=VALUES(planned_export_kwh),actual_export_kwh=VALUES(actual_export_kwh),
               planned_net_pln=VALUES(planned_net_pln),actual_net_pln=VALUES(actual_net_pln),soc_end_pct=VALUES(soc_end_pct),
               updated_at=NOW(6),quality_status=VALUES(quality_status),expected_slot_count=VALUES(expected_slot_count),
               terminal_slot_count=VALUES(terminal_slot_count),recovered_slot_count=VALUES(recovered_slot_count),
               missing_slot_count=VALUES(missing_slot_count),coverage_pct=VALUES(coverage_pct),
               completion_status=VALUES(completion_status),source_version=VALUES(source_version),
               input_watermark=VALUES(input_watermark),closed_at=CASE WHEN VALUES(completion_status)='OPEN'
                 THEN NULL ELSE COALESCE(closed_at,VALUES(closed_at)) END,
               learning_eligible=VALUES(learning_eligible)""",
              (hour_start_value,expected,r["fpv"],r["apv"],r["fload"],r["aload"],r["pbuy"],r["abuy"],
               r["pexport"],r["aexport"],r["pnet"],r["anet"],float(r["soc"]) if r["soc"] not in (None,"") else None,
               quality,expected,terminal,recovered,missing,round(float(r["coverage"] or 0),2),completion,
               f"CORE_{APP_VERSION.replace('.', '_')}",
               r["watermark"],now if completion=="CLOSED" else None,quality=="ACCEPTED"))
            cur.execute("""UPDATE ems_gpt_core_hourly SET planned_pv_to_bat_kwh=%s,
              planned_pv_to_cwu_kwh=%s,planned_pv_to_ev_kwh=%s,planned_pv_export_kwh=%s,
              planned_pv_curtail_kwh=%s WHERE hour_start=%s""",
              (r["pvbat"] or 0,r["pvcwu"] or 0,r["pvev"] or 0,r["pvexport"] or 0,
               r["pvcurtail"] or 0,hour_start_value))
        for offset in range(max(1, days)):
            day_start=first_day+timedelta(days=offset); day_end=day_start+timedelta(days=1)
            cur.execute("""SELECT COUNT(*) slots,SUM(actual_recorded_at IS NOT NULL) terminal_n,
              SUM(actual_mode='MISSING_OUTAGE') missing_n,
              SUM(forecast_pv_total_kwh) fpv,SUM(actual_pv_total_kwh) apv,
              SUM(planned_battery_discharge_kwh) pdis,SUM(actual_battery_discharge_kwh) adis,
              SUM(planned_buy_kwh) pbuy,SUM(actual_buy_kwh) abuy,SUM(forecast_load_kwh) fload,SUM(actual_load_kwh) aload,
              SUM(planned_pv_to_bat_kwh) pvbat,SUM(planned_pv_to_cwu_kwh) pvcwu,
              SUM(planned_pv_to_ev_kwh) pvev,SUM(planned_pv_curtail_kwh) pvcurtail,
              SUM(planned_battery_charge_kwh) pcharge,SUM(actual_battery_charge_kwh) acharge,
              SUM(COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0)) pexport,
              SUM(COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0)) aexport,
              SUM((COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(planned_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) pnet,
              SUM((COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(actual_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) anet
              FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s""", (day_start,day_end))
            d=cur.fetchone(); expected=int(d["slots"] or 0); terminal=int(d["terminal_n"] or 0); missing=int(d["missing_n"] or 0)
            cur.execute("""SELECT COUNT(*) n FROM ems_gpt_core_execution_details
              WHERE slot_start>=%s AND slot_start<%s AND recovery_status='RECOVERED'""", (day_start,day_end))
            recovered=int(cur.fetchone()["n"] or 0)
            ended=day_end<=now; completion="CLOSED" if ended and expected in (92,96,100) and terminal==expected else "OPEN"
            quality="ACCEPTED" if completion=="CLOSED" and missing==0 and recovered==0 else "MISSING" if completion=="CLOSED" and missing==expected else "PARTIAL" if ended else "OPEN"
            cur.execute("""INSERT INTO ems_gpt_daily(day_date,closed_at,slot_count,expected_slot_count,
              forecast_pv_kwh,actual_pv_kwh,forecast_battery_discharge_kwh,actual_battery_discharge_kwh,
              forecast_import_kwh,actual_import_kwh,forecast_load_kwh,actual_load_kwh,
              forecast_battery_charge_kwh,actual_battery_charge_kwh,forecast_export_kwh,actual_export_kwh,
              forecast_pv_export_kwh,actual_pv_export_kwh,planned_net_pln,actual_net_pln,
              valid_actual_slot_count,missing_actual_slot_count,recovered_slot_count,data_quality_status,
              data_quality_reason,learning_eligible,completion_status,terminal_slot_count)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON DUPLICATE KEY UPDATE closed_at=CASE WHEN VALUES(completion_status)='OPEN'
                THEN NULL ELSE COALESCE(closed_at,VALUES(closed_at)) END,slot_count=VALUES(slot_count),
              expected_slot_count=VALUES(expected_slot_count),forecast_pv_kwh=VALUES(forecast_pv_kwh),
              actual_pv_kwh=VALUES(actual_pv_kwh),forecast_battery_discharge_kwh=VALUES(forecast_battery_discharge_kwh),
              actual_battery_discharge_kwh=VALUES(actual_battery_discharge_kwh),forecast_import_kwh=VALUES(forecast_import_kwh),
              actual_import_kwh=VALUES(actual_import_kwh),forecast_load_kwh=VALUES(forecast_load_kwh),actual_load_kwh=VALUES(actual_load_kwh),
              forecast_battery_charge_kwh=VALUES(forecast_battery_charge_kwh),actual_battery_charge_kwh=VALUES(actual_battery_charge_kwh),
              forecast_export_kwh=VALUES(forecast_export_kwh),actual_export_kwh=VALUES(actual_export_kwh),
              forecast_pv_export_kwh=VALUES(forecast_pv_export_kwh),actual_pv_export_kwh=VALUES(actual_pv_export_kwh),
              planned_net_pln=VALUES(planned_net_pln),actual_net_pln=VALUES(actual_net_pln),
              valid_actual_slot_count=VALUES(valid_actual_slot_count),missing_actual_slot_count=VALUES(missing_actual_slot_count),
              recovered_slot_count=VALUES(recovered_slot_count),data_quality_status=VALUES(data_quality_status),
              data_quality_reason=VALUES(data_quality_reason),learning_eligible=VALUES(learning_eligible),
              completion_status=VALUES(completion_status),terminal_slot_count=VALUES(terminal_slot_count)""",
              (day_start.date(),now if completion=="CLOSED" else None,expected,expected,d["fpv"],d["apv"],d["pdis"],d["adis"],
               d["pbuy"],d["abuy"],d["fload"],d["aload"],d["pcharge"],d["acharge"],d["pexport"],d["aexport"],
               d["pexport"],d["aexport"],d["pnet"],d["anet"],terminal-missing,missing,recovered,quality,
               json.dumps({"completion":completion,"expected":expected,"terminal":terminal,"missing":missing,"recovered":recovered}),
               quality=="ACCEPTED",completion,terminal))
            cur.execute("""UPDATE ems_gpt_daily SET planned_pv_to_bat_kwh=%s,
              planned_pv_to_cwu_kwh=%s,planned_pv_to_ev_kwh=%s,planned_pv_curtail_kwh=%s
              WHERE day_date=%s""", (d["pvbat"] or 0,d["pvcwu"] or 0,d["pvev"] or 0,
              d["pvcurtail"] or 0,day_start.date()))
    return {"hours":len(hour_rows),"days":max(1,days)}


def learn_missing_load() -> int:
    updated=0; cutoff=slot_start().replace(tzinfo=None)
    with db() as conn,conn.cursor() as cur:
        cur.execute("""SELECT slot_start FROM ems_gpt_slots WHERE slot_start>=%s
          AND actual_recorded_at IS NULL AND forecast_load_kwh IS NULL ORDER BY slot_start LIMIT 192""",(cutoff,))
        for row in cur.fetchall():
            s=row["slot_start"]
            cur.execute("""SELECT trimmed_mean_kwh,sample_count FROM ems_gpt_core_load_profiles
              WHERE weekday_no=%s AND hour_no=%s AND minute_no=%s""",(s.weekday(),s.hour,s.minute))
            profile=cur.fetchone()
            if profile and int(profile["sample_count"] or 0)>=3:
                value=float(profile["trimmed_mean_kwh"])
                cur.execute("UPDATE ems_gpt_slots SET forecast_load_kwh=%s,load_correction=1 WHERE slot_start=%s",(round(value,6),s))
                updated+=cur.rowcount
    return updated


def record_event(event_type: str, module: str, payload: dict, severity: str = "INFO") -> None:
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ems_gpt_core_events(created_at,severity,event_type,module_name,slot_start,payload_json) VALUES(NOW(6),%s,%s,%s,%s,%s)",
                        (severity, event_type, module, slot_start().replace(tzinfo=None),
                         json.dumps(payload, ensure_ascii=False, default=str)))
    except Exception as exc:
        LOG.error("event write failed: %s", exc)


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


def main() -> None:
    startup_executor = enable_production_on_startup()
    initialize()
    record_event("executor_startup_mode", "executor", startup_executor,
                 "INFO" if startup_executor["mode"] == "LIVE" else "WARNING")
    threading.Thread(target=engine_loop, daemon=True).start()
    LOG.info("%s %s started; executor=%s", APP_NAME, APP_VERSION, startup_executor["mode"])
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()


if __name__ == "__main__":
    main()
