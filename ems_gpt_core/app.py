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

APP_NAME = "EMS-GPT Core"
APP_VERSION = "0.25.16"
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
STATE = {
    "app": APP_NAME,
    "version": APP_VERSION,
    "status": "STARTING",
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


def refresh_pv_forecast() -> dict:
    """Disaggregate Open-Meteo daily PV energy using an actual-production profile."""
    today = local_now().date()
    results = {}
    with db() as conn, conn.cursor() as cur:
        for label, target in (("today", today), ("tomorrow", today+timedelta(days=1))):
            pv1 = number(ha_state(PV_FORECAST_ENTITIES[label][0]))
            pv2 = number(ha_state(PV_FORECAST_ENTITIES[label][1]))
            if pv1 is None or pv2 is None:
                results[label] = {"status": "WAITING_SOURCE"}
                continue
            lower = max(slot_start().replace(tzinfo=None), datetime.combine(target, datetime.min.time())) if label == "today" else datetime.combine(target, datetime.min.time())
            upper = datetime.combine(target+timedelta(days=1), datetime.min.time())
            cur.execute("""SELECT s.slot_start,p.mean_share FROM ems_gpt_slots s
              LEFT JOIN ems_gpt_core_pv_profiles p ON p.month_no=MONTH(s.slot_start)
               AND p.hour_no=HOUR(s.slot_start) AND p.minute_no=MINUTE(s.slot_start)
              WHERE s.slot_start>=%s AND s.slot_start<%s AND s.actual_recorded_at IS NULL
              ORDER BY s.slot_start""", (lower, upper))
            slots = list(cur.fetchall())
            weight_sum = sum(float(r.get("mean_share") or 0) for r in slots)
            if not slots or weight_sum <= 0:
                results[label] = {"status": "WAITING_PROFILE", "slots": len(slots)}
                continue
            for row in slots:
                share = float(row.get("mean_share") or 0)/weight_sum
                a, b = pv1*share, pv2*share
                cur.execute("""UPDATE ems_gpt_slots SET forecast_pv1_kwh=%s,forecast_pv2_kwh=%s,
                  forecast_pv_total_kwh=%s,forecast_pv_source='OPEN_METEO_PROFILE_V1',
                  pv_correction=1 WHERE slot_start=%s AND actual_recorded_at IS NULL""",
                  (round(a, 6), round(b, 6), round(a+b, 6), row["slot_start"]))
            results[label] = {"status": "OK", "slots": len(slots), "pv1_kwh": pv1, "pv2_kwh": pv2}
    record_event("pv_forecast_refreshed", "analytics", results)
    return results


def refresh_weather_forecast() -> dict:
    response = ha_service_response("weather", "get_forecasts", {"entity_id": "weather.dom", "type": "hourly"}, return_response=True) or {}
    service_response = response.get("service_response", response)
    forecast = (service_response.get("weather.dom") or {}).get("forecast", [])
    updated = 0
    with db() as conn, conn.cursor() as cur:
        for item in forecast:
            try:
                hour = datetime.fromisoformat(str(item["datetime"]).replace("Z", "+00:00")).astimezone(TZ).replace(tzinfo=None)
                cur.execute("""UPDATE ems_gpt_slots SET forecast_temperature_c=%s,
                  forecast_cloud_coverage_pct=%s,forecast_precipitation_mm=%s
                  WHERE slot_start>=%s AND slot_start<%s AND actual_recorded_at IS NULL""",
                  (item.get("temperature"), item.get("cloud_coverage"), item.get("precipitation"),
                   hour, hour+timedelta(hours=1)))
                updated += cur.rowcount
            except (KeyError, TypeError, ValueError):
                continue
    result = {"status": "OK" if forecast else "WAITING_SOURCE", "hours": len(forecast), "slots_updated": updated, "source": "OPEN_METEO"}
    record_event("weather_forecast_refreshed", "analytics", result, "INFO" if forecast else "WARNING")
    return result


def refresh_rce(day=None) -> dict:
    target = day or (local_now().date()+timedelta(days=1))
    calendar = canonical_slots_for_day(target)
    calendar_by_local = {r["slot_start_local"]: r for r in calendar if not r["local_fold"]}
    expected = len(calendar)
    margin=max(0.0,float(OPTIONS.get("purchase_margin_pln_kwh", 0.59)))
    params={"$select":"dtime,period,rce_pln,business_date,publication_ts",
            "$filter":f"business_date eq '{target:%Y-%m-%d}'","$first":200}
    req=urllib.request.Request("https://api.raporty.pse.pl/api/rce-pln?"+urlencode(params),
                               headers={"Accept":"application/json","User-Agent":"EMS-GPT-Core/0.4"})
    with urllib.request.urlopen(req,timeout=30) as response: payload=json.load(response)
    unique={}
    for item in payload.get("value",[]):
        try:
            end=datetime.fromisoformat(str(item["dtime"]).replace("Z","+00:00"))
            if end.tzinfo is None: end=end.replace(tzinfo=TZ)
            start=end.astimezone(TZ)-timedelta(minutes=15)
            if start.date()!=target: continue
            raw=float(item["rce_pln"])/1000
            pub=item.get("publication_ts")
            unique[start.replace(tzinfo=None)]={"sell":raw,"buy":raw+margin,"publication":pub}
        except Exception:
            continue
    ordered=sorted(unique)
    eta_c=max(.01,min(1.0,float(OPTIONS.get("battery_charge_efficiency",.90))))
    eta_d=max(.01,min(1.0,float(OPTIONS.get("battery_discharge_efficiency",.95))))
    degradation=max(0,float(OPTIONS.get("battery_degradation_cost_pln_kwh",.08)))
    min_margin=max(0,float(OPTIONS.get("minimum_arbitrage_margin_pln_kwh",.05)))
    buy_tolerance=max(0.0,float(OPTIONS.get("buy_window_tolerance_pln_kwh",.05)))
    morning_start=float(OPTIONS.get("sale_morning_start_hour",6.0)); morning_end=float(OPTIONS.get("sale_morning_end_hour",10.0))
    evening_start=float(OPTIONS.get("sale_evening_start_hour",17.0)); evening_end=float(OPTIONS.get("sale_evening_end_hour",23.0))
    def decimal_hour(s): return s.hour+s.minute/60.0
    def sale_session(s):
        hour=decimal_hour(s)
        return morning_start<=hour<morning_end or evening_start<=hour<evening_end
    for s in ordered:
        peers=[q for q in ordered if sale_session(q) and ((q.hour<12)==(s.hour<12))]
        peak=max(peers,key=lambda q:unique[q]["sell"]) if peers else None
        threshold=unique[peak]["sell"]*.8 if peak else float("inf")
        unique[s]["sale_window"]=bool(sale_session(s) and unique[s]["sell"]>=threshold and unique[s]["sell"]>degradation+min_margin)
        later=[unique[q]["sell"] for q in ordered if q>s]
        unique[s]["buy_window"]=bool(not sale_session(s) and later and unique[s]["sell"]<=min(later)+buy_tolerance and max(later)*eta_d-unique[s]["buy"]/eta_c-degradation>=min_margin)
    with db() as conn,conn.cursor() as cur:
        for s,v in unique.items():
            canonical=calendar_by_local.get(s)
            cur.execute("""INSERT INTO ems_gpt_slots(slot_start,slot_end,price_sell_pln_kwh,price_buy_pln_kwh,
              sale_window,buy_window,price_source,price_fetched_at,price_publication_at,
              slot_id,slot_start_utc,slot_start_local,utc_offset_minutes,local_fold,local_day,slot_index_local)
              VALUES(%s,%s,%s,%s,%s,%s,'PSE_API',NOW(6),%s,%s,%s,%s,%s,%s,%s,%s)
              ON DUPLICATE KEY UPDATE price_sell_pln_kwh=IF(actual_recorded_at IS NULL,VALUES(price_sell_pln_kwh),price_sell_pln_kwh),
              price_buy_pln_kwh=IF(actual_recorded_at IS NULL,VALUES(price_buy_pln_kwh),price_buy_pln_kwh),
              sale_window=IF(actual_recorded_at IS NULL,VALUES(sale_window),sale_window),
              buy_window=IF(actual_recorded_at IS NULL,VALUES(buy_window),buy_window),
              price_fetched_at=IF(actual_recorded_at IS NULL,NOW(6),price_fetched_at),
              price_publication_at=IF(actual_recorded_at IS NULL,VALUES(price_publication_at),price_publication_at),
              price_source=IF(actual_recorded_at IS NULL,'PSE_API',price_source),
              slot_id=COALESCE(slot_id,VALUES(slot_id)),slot_start_utc=COALESCE(slot_start_utc,VALUES(slot_start_utc)),
              slot_start_local=COALESCE(slot_start_local,VALUES(slot_start_local)),
              utc_offset_minutes=COALESCE(utc_offset_minutes,VALUES(utc_offset_minutes)),
              local_day=COALESCE(local_day,VALUES(local_day)),slot_index_local=COALESCE(slot_index_local,VALUES(slot_index_local))""",
              (s,s+timedelta(minutes=15),v["sell"],v["buy"],v["sale_window"],v["buy_window"],v["publication"],
               canonical["slot_id"] if canonical else None,canonical["slot_start_utc"] if canonical else None,s,
               canonical["utc_offset_minutes"] if canonical else None,canonical["local_fold"] if canonical else 0,
               target,canonical["slot_index_local"] if canonical else None))
    result={"day":str(target),"rows":len(unique),"expected":expected,
            "status":"OK" if len(unique)==expected else "PARTIAL","margin":margin}
    record_event("rce_refreshed","core",result)
    return result


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
            load = float(row.get("forecast_load_kwh") or 0)
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
            load=float(row.get("forecast_load_kwh") or 0)
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
            reason=f"grid={grid_policy}; export={export_policy}; soc={item['end']:.2f}; floor={effective_floor:.2f}; target={target:.2f}"
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
                 f"window={hp_window}; night_min={night_min}; threshold={night_threshold}; minimum_hours={OPTIONS.get('hp_min_heating_hours',10.0)}"),
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


def _wape(rows: list[dict], forecast_key: str, actual_key: str) -> float | None:
    pairs = [(float(r[forecast_key]), float(r[actual_key])) for r in rows
             if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    denominator = sum(abs(actual) for _, actual in pairs)
    return None if not pairs or denominator <= 1e-9 else round(100 * sum(abs(forecast-actual) for forecast, actual in pairs) / denominator, 3)


def _bias(rows: list[dict], forecast_key: str, actual_key: str) -> float | None:
    pairs = [(float(r[forecast_key]), float(r[actual_key])) for r in rows
             if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    return None if not pairs else round(sum(forecast-actual for forecast, actual in pairs), 6)


def _mae(rows: list[dict], forecast_key: str, actual_key: str) -> float | None:
    pairs = [(float(r[forecast_key]), float(r[actual_key])) for r in rows
             if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    return None if not pairs else round(sum(abs(forecast-actual) for forecast, actual in pairs)/len(pairs), 3)


def run_analytics() -> dict:
    """Persist slot quality, rolling WAPE and a reproducible analysis watermark."""
    run_id = str(uuid.uuid4())
    cutoff = local_now().replace(tzinfo=None) - timedelta(days=30)
    minimum_samples = max(1, int(OPTIONS.get("telemetry_min_samples_per_slot", 10)))
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO ems_gpt_core_analytics_runs(run_id,started_at,status) VALUES(%s,NOW(6),'RUNNING')", (run_id,))
        cur.execute("""SELECT s.*,
          (SELECT COUNT(*) FROM ems_gpt_telemetry_snapshots t
           WHERE t.slot_start=s.slot_start) sample_count
          FROM ems_gpt_slots s WHERE s.slot_start>=%s AND s.actual_recorded_at IS NOT NULL
          ORDER BY s.slot_start DESC LIMIT 2880""", (cutoff,))
        rows = list(cur.fetchall())
        complete = 0
        for row in rows:
            forecast_ok = all(row.get(k) is not None for k in ("forecast_pv_total_kwh", "forecast_load_kwh"))
            actual_ok = all(row.get(k) is not None for k in ("actual_pv_total_kwh", "actual_load_kwh"))
            price_ok = all(row.get(k) is not None for k in ("price_buy_pln_kwh", "price_sell_pln_kwh")) and row.get("price_source") == "PSE_API"
            samples = int(row.get("sample_count") or 0)
            present = sum((forecast_ok, actual_ok, price_ok, samples >= minimum_samples))
            completeness = round(present * 25.0, 2)
            reasons = []
            if not forecast_ok: reasons.append("MISSING_FORECAST")
            if not actual_ok: reasons.append("MISSING_ACTUAL")
            if not price_ok: reasons.append("MISSING_OR_NON_PSE_PRICE")
            if samples < minimum_samples: reasons.append("LOW_SAMPLE_COUNT")
            status = "COMPLETE" if not reasons else ("PARTIAL" if present >= 2 else "INVALID")
            complete += status == "COMPLETE"
            cur.execute("""INSERT INTO ems_gpt_core_slot_quality
              (slot_start,sample_count,completeness_pct,forecast_complete,actual_complete,
               price_complete,pv_abs_error_kwh,load_abs_error_kwh,import_abs_error_kwh,
               export_abs_error_kwh,status,checked_at,reasons_json)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6),%s)
              ON DUPLICATE KEY UPDATE sample_count=VALUES(sample_count),completeness_pct=VALUES(completeness_pct),
               forecast_complete=VALUES(forecast_complete),actual_complete=VALUES(actual_complete),
               price_complete=VALUES(price_complete),pv_abs_error_kwh=VALUES(pv_abs_error_kwh),
               load_abs_error_kwh=VALUES(load_abs_error_kwh),import_abs_error_kwh=VALUES(import_abs_error_kwh),
               export_abs_error_kwh=VALUES(export_abs_error_kwh),status=VALUES(status),
               checked_at=NOW(6),reasons_json=VALUES(reasons_json)""",
              (row["slot_start"], samples, completeness, forecast_ok, actual_ok, price_ok,
               abs(float(row.get("forecast_pv_total_kwh") or 0)-float(row.get("actual_pv_total_kwh") or 0)) if forecast_ok and actual_ok else None,
               abs(float(row.get("forecast_load_kwh") or 0)-float(row.get("actual_load_kwh") or 0)) if forecast_ok and actual_ok else None,
               abs(float(row.get("planned_buy_kwh") or 0)-float(row.get("actual_buy_kwh") or 0)),
               abs(float(row.get("planned_pv_export_kwh") or 0)-float(row.get("actual_pv_export_kwh") or 0)),
               status, json.dumps(reasons)))
        metrics = {
            "pv1_wape_pct": _wape(rows, "forecast_pv1_kwh", "actual_pv1_kwh"),
            "pv2_wape_pct": _wape(rows, "forecast_pv2_kwh", "actual_pv2_kwh"),
            "pv_wape_pct": _wape(rows, "forecast_pv_total_kwh", "actual_pv_total_kwh"),
            "load_wape_pct": _wape(rows, "forecast_load_kwh", "actual_load_kwh"),
            "import_wape_pct": _wape(rows, "planned_buy_kwh", "actual_buy_kwh"),
            "export_wape_pct": _wape(rows, "planned_pv_export_kwh", "actual_pv_export_kwh"),
            "pv_bias_kwh": _bias(rows, "forecast_pv_total_kwh", "actual_pv_total_kwh"),
            "load_bias_kwh": _bias(rows, "forecast_load_kwh", "actual_load_kwh"),
            "import_bias_kwh": _bias(rows, "planned_buy_kwh", "actual_buy_kwh"),
            "export_bias_kwh": _bias(rows, "planned_pv_export_kwh", "actual_pv_export_kwh"),
            "soc_mae_pct": _mae(rows, "soc_end_plan_pct", "soc_end_pct"),
        }
        planned_net = sum(float(r.get("planned_sell_kwh") or 0)*float(r.get("price_sell_pln_kwh") or 0)
                          - float(r.get("planned_buy_kwh") or 0)*float(r.get("price_buy_pln_kwh") or 0) for r in rows)
        actual_net = sum((float(r.get("actual_sell_kwh") or 0)+float(r.get("actual_pv_export_kwh") or 0))*float(r.get("price_sell_pln_kwh") or 0)
                         - float(r.get("actual_buy_kwh") or 0)*float(r.get("price_buy_pln_kwh") or 0) for r in rows)
        metrics["net_cost_variance_pln"] = round(actual_net-planned_net, 3)
        score = round(100 * complete / len(rows), 2) if rows else 0.0
        profiles = {}
        pv_days = {}
        for row in rows:
            if row.get("actual_load_kwh") is None:
                continue
            if int(row.get("sample_count") or 0) < minimum_samples or row.get("actual_mode") == "MISSING_OUTAGE":
                continue
            key = (row["slot_start"].weekday(), row["slot_start"].hour, row["slot_start"].minute)
            profiles.setdefault(key, []).append(float(row["actual_load_kwh"]))
            if row.get("actual_pv_total_kwh") is not None:
                day_key = row["slot_start"].date()
                pv_days.setdefault(day_key, []).append(row)
        for (weekday, hour, minute), values in profiles.items():
            ordered = sorted(values)
            trim = max(0, int(len(ordered)*0.1))
            trimmed = ordered[trim:len(ordered)-trim] if trim and len(ordered)-2*trim else ordered
            mean = sum(ordered)/len(ordered)
            trimmed_mean = sum(trimmed)/len(trimmed)
            p80 = ordered[min(len(ordered)-1, int((len(ordered)-1)*0.8))]
            denominator = sum(abs(v) for v in ordered)
            profile_wape = None if denominator <= 1e-9 else 100*sum(abs(trimmed_mean-v) for v in ordered)/denominator
            cur.execute("""INSERT INTO ems_gpt_core_load_profiles
              (weekday_no,hour_no,minute_no,sample_count,mean_kwh,trimmed_mean_kwh,p80_kwh,wape_pct,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW(6)) ON DUPLICATE KEY UPDATE
              sample_count=VALUES(sample_count),mean_kwh=VALUES(mean_kwh),
              trimmed_mean_kwh=VALUES(trimmed_mean_kwh),p80_kwh=VALUES(p80_kwh),
              wape_pct=VALUES(wape_pct),updated_at=NOW(6)""",
              (weekday, hour, minute, len(ordered), mean, trimmed_mean, p80, profile_wape))
        pv_profiles = {}
        for day_rows in pv_days.values():
            total = sum(float(r.get("actual_pv_total_kwh") or 0) for r in day_rows)
            if total <= 0.1:
                continue
            for row in day_rows:
                key = (row["slot_start"].month, row["slot_start"].hour, row["slot_start"].minute)
                pv_profiles.setdefault(key, []).append(float(row.get("actual_pv_total_kwh") or 0)/total)
        for (month, hour, minute), shares in pv_profiles.items():
            cur.execute("""INSERT INTO ems_gpt_core_pv_profiles
              (month_no,hour_no,minute_no,sample_days,mean_share,updated_at)
              VALUES(%s,%s,%s,%s,%s,NOW(6)) ON DUPLICATE KEY UPDATE
              sample_days=VALUES(sample_days),mean_share=VALUES(mean_share),updated_at=NOW(6)""",
              (month, hour, minute, len(shares), sum(shares)/len(shares)))
        cur.execute("""UPDATE ems_gpt_core_analytics_runs SET completed_at=NOW(6),status='COMPLETED',
          slots_scanned=%s,complete_slots=%s,pv1_wape_pct=%s,pv2_wape_pct=%s,pv_wape_pct=%s,load_wape_pct=%s,
          import_wape_pct=%s,export_wape_pct=%s,quality_score=%s,pv_bias_kwh=%s,load_bias_kwh=%s,
          import_bias_kwh=%s,export_bias_kwh=%s,soc_mae_pct=%s,net_cost_variance_pln=%s,
          details_json=%s WHERE run_id=%s""",
          (len(rows), complete, metrics["pv1_wape_pct"], metrics["pv2_wape_pct"],
           metrics["pv_wape_pct"], metrics["load_wape_pct"],
           metrics["import_wape_pct"], metrics["export_wape_pct"], score,
           metrics["pv_bias_kwh"], metrics["load_bias_kwh"], metrics["import_bias_kwh"],
           metrics["export_bias_kwh"], metrics["soc_mae_pct"], metrics["net_cost_variance_pln"],
           json.dumps({"cutoff": str(cutoff), "metrics": metrics, "load_profiles": len(profiles), "pv_profiles": len(pv_profiles)}), run_id))
    result = {"run_id": run_id, "slots": len(rows), "complete": complete, "quality_score": score,
              "load_profiles": len(profiles), "pv_profiles": len(pv_profiles), **metrics}
    record_event("analytics_completed", "analytics", result)
    return result


def run_ai_observer(source_ref: str | None = None) -> dict:
    """Read-only shadow observer: persist evidence and suggestions, never mutate plan or controls."""
    if not bool(OPTIONS.get("ai_observer_enabled", False)):
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
                      "soc_mae_pct", "net_cost_variance_pln")}}
        suggestions = []
        def flag(metric, value, threshold, message, severity="WARNING"):
            if value is not None and abs(float(value)) > threshold:
                suggestions.append({"metric": metric, "value": float(value), "threshold": threshold,
                                    "severity": severity, "suggestion": message})
        flag("pv_wape_pct", analytics.get("pv_wape_pct"), float(OPTIONS.get("observer_pv_wape_warn_pct", 30.0)), "Sprawdź profil rozdziału prognozy PV i różnice PV1/PV2.")
        flag("load_wape_pct", analytics.get("load_wape_pct"), float(OPTIONS.get("observer_load_wape_warn_pct", 35.0)), "Zwiększ liczbę próbek profilu zużycia przed zmianą planera.")
        flag("soc_mae_pct", analytics.get("soc_mae_pct"), float(OPTIONS.get("observer_soc_mae_warn_pct", 8.0)), "Zweryfikuj sprawności baterii oraz znak i źródło mocy baterii.")
        flag("net_cost_variance_pln", analytics.get("net_cost_variance_pln"), float(OPTIONS.get("observer_cost_variance_warn_pln", 10.0)), "Przeanalizuj koszt planowany względem wykonania bez automatycznej korekty PPD.")
        quality_threshold = float(OPTIONS.get("observer_min_quality_score_pct", 80.0))
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
    for item in suggestions:
        create_todo("ai_observer", f"Obserwator: {item['metric']}",
                    json.dumps(item, ensure_ascii=False), item["severity"], run_id)
    record_event("ai_observer_completed", "ai_observer",
                 {"run_id": run_id, "source_ref": source_ref, "decision": decision, "suggestions": len(suggestions)})
    return {"status": "COMPLETED", "run_id": run_id, "source_ref": source_ref,
            "decision": decision, "auto_score": auto_score, "suggestions": suggestions}


def generate_diagnostic_report(trigger_name: str = "scheduled") -> dict:
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
        cur.execute("SELECT COUNT(*) n FROM ems_gpt_core_commands WHERE status IN ('READY_FOR_CONNECTOR','DISPATCHED') AND expires_at<=NOW(6)")
        expired_commands = int(cur.fetchone()["n"] or 0)
        add("no_expired_active_commands", expired_commands == 0, expired_commands, 0)
        cur.execute("""SELECT process_name,COUNT(*) n FROM ems_gpt_core_process_overrides
          WHERE status='ACTIVE' AND valid_from<=NOW(6) AND valid_until>NOW(6)
          GROUP BY process_name HAVING COUNT(*)>1""")
        add("single_active_override_per_process", cur.fetchone() is None, "checked", 1)
        executor_guard = (not bool(OPTIONS.get('executor_enabled', False))) or bool(OPTIONS.get('executor_dry_run', True)) or \
            OPTIONS.get('executor_activation_ack') == 'EMS_CONNECTOR_ACCEPTED'
        add("executor_guard", executor_guard,
            {"enabled": bool(OPTIONS.get('executor_enabled', False)), "dry_run": bool(OPTIONS.get('executor_dry_run', True)),
             "activation_ack": OPTIONS.get('executor_activation_ack') == 'EMS_CONNECTOR_ACCEPTED'},
            "disabled, dry-run, or explicitly accepted")
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
    return result


def create_todo(module: str, title: str, details: str, severity: str = "INFO", source_ref: str | None = None) -> str:
    todo_id = str(uuid.uuid4())
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT todo_id FROM ems_gpt_core_todo WHERE local_day=%s AND module_name=%s
          AND title=%s AND status='OPEN' LIMIT 1""", (local_now().date(), module[:32], title[:300]))
        existing = cur.fetchone()
        if existing:
            return existing["todo_id"]
        cur.execute("""INSERT INTO ems_gpt_core_todo
          (todo_id,created_at,local_day,severity,module_name,title,details,status,source_ref)
          VALUES(%s,NOW(6),%s,%s,%s,%s,%s,'OPEN',%s)""",
          (todo_id, local_now().date(), severity[:12], module[:32], title[:300], details, source_ref))
    return todo_id


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


def settings_payload() -> dict:
    return {key: {"value": float(OPTIONS[key]), "min": spec[0], "max": spec[1], "label": spec[2], "group": spec[3]}
            for key, spec in OPERATIONAL_SETTINGS.items()}


def update_operational_settings(payload: dict) -> dict:
    changed = {}
    for key, raw in payload.items():
        if key not in OPERATIONAL_SETTINGS:
            raise ValueError(f"unknown setting: {key}")
        low, high, _, _ = OPERATIONAL_SETTINGS[key]
        value = float(raw)
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        changed[key] = value
    if not changed:
        raise ValueError("no settings supplied")
    current = {}
    try:
        current = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    current.update(changed)
    temporary = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, RUNTIME_SETTINGS_PATH)
    with LOCK:
        OPTIONS.update(changed)
    record_event("operational_settings_updated", "core", {"changed": changed})
    return {"changed": changed, "settings": settings_payload()}


def update_executor_mode(payload: dict, requested_by: str = "operator") -> dict:
    requested = str(payload.get("mode", "")).upper()
    if requested not in {"LIVE", "OFF"}:
        raise ValueError("mode must be LIVE or OFF")
    if requested == "LIVE":
        if payload.get("confirmation") != "EMS_CONNECTOR_ACCEPTED":
            raise ValueError("explicit executor confirmation required")
        try:
            service_map = json.loads(str(OPTIONS.get("connector_service_map_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("connector_service_map_json is invalid") from exc
        missing = [p for p in PROCESS_NAMES if not all(
            isinstance(service_map.get(p, {}).get(state), str)
            and service_map[p][state].startswith("script.")
            for state in ("ON", "OFF")
        )]
        if missing:
            raise ValueError("missing safe script mapping: " + ", ".join(missing))
        changed = {
            "executor_enabled": True,
            "executor_dry_run": False,
            "executor_activation_ack": "EMS_CONNECTOR_ACCEPTED",
        }
        state = "LIVE"
    else:
        changed = {
            "executor_enabled": False,
            "executor_dry_run": True,
            "executor_activation_ack": "",
        }
        state = "OFF"
    try:
        current = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    current.update(changed)
    temporary = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, RUNTIME_SETTINGS_PATH)
    with LOCK:
        OPTIONS.update(changed)
        STATE["executor"] = state
        STATE["modules"]["executor"] = state
    record_event("executor_mode_updated", "executor", {
        "mode": state, "requested_by": requested_by
    }, "WARNING" if state == "LIVE" else "INFO")
    return {"mode": state}


PROCESS_NAMES = ("BATTERY_IMPORT", "BATTERY_EXPORT", "PV_CWU", "PV_EV", "HP_HEAT_DHW")
OVERRIDE_STATES = ("AUTO", "FORCE_ON", "FORCE_OFF")


def enable_production_on_startup() -> dict:
    """Start LIVE when the complete guarded script connector is available."""
    try:
        service_map = json.loads(str(OPTIONS.get("connector_service_map_json") or "{}"))
    except json.JSONDecodeError:
        service_map = {}
    missing = [p for p in PROCESS_NAMES if not all(
        isinstance(service_map.get(p, {}).get(state), str)
        and service_map[p][state].startswith("script.")
        for state in ("ON", "OFF")
    )]
    changed = {
        "executor_enabled": not missing,
        "executor_dry_run": bool(missing),
        "executor_activation_ack": "" if missing else "EMS_CONNECTOR_ACCEPTED",
    }
    state = "OFF" if missing else "LIVE"
    try:
        current = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    current.update(changed)
    temporary = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, RUNTIME_SETTINGS_PATH)
    with LOCK:
        OPTIONS.update(changed)
        STATE["executor"] = state
        STATE["modules"]["executor"] = state
    return {"mode": state, "missing_safe_script_mappings": missing}


def update_process_override(payload: dict, requested_by: str = "operator") -> dict:
    process = str(payload.get("process") or "").upper()
    requested = str(payload.get("state") or "").upper()
    if process not in PROCESS_NAMES:
        raise ValueError("unknown process")
    if requested not in OVERRIDE_STATES:
        raise ValueError("state must be AUTO, FORCE_ON or FORCE_OFF")
    reason = str(payload.get("reason") or "operator panel")[:1000]
    minutes = int(payload.get("minutes") or 60)
    if not 1 <= minutes <= 1440:
        raise ValueError("minutes must be between 1 and 1440")
    if process == "HP_HEAT_DHW" and requested == "FORCE_ON":
        minimum_hp_minutes = max(1, int(float(OPTIONS.get("hp_min_cycle_hours", 2.0)) * 60 + 0.999999))
        if minutes < minimum_hp_minutes:
            raise ValueError(f"HP_HEAT_DHW FORCE_ON must last at least {minimum_hp_minutes} minutes")
    now = local_now().replace(tzinfo=None)
    indefinite_block = process == "HP_HEAT_DHW" and requested == "FORCE_OFF"
    override_until = datetime(9999, 12, 31, 23, 59, 59) if indefinite_block else now + timedelta(minutes=minutes)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE ems_gpt_core_process_overrides SET status='CANCELLED',cancelled_at=NOW(6)
          WHERE process_name=%s AND status='ACTIVE'""", (process,))
        if requested == "AUTO":
            override_id = None
        else:
            override_id = str(uuid.uuid4())
            cur.execute("""INSERT INTO ems_gpt_core_process_overrides
              (override_id,process_name,requested_state,requested_at,valid_from,valid_until,
               requested_by,reason,status) VALUES(%s,%s,%s,NOW(6),%s,%s,%s,%s,'ACTIVE')""",
              (override_id, process, requested, now, override_until, requested_by[:100], reason))
    result = {"process": process, "state": requested, "override_id": override_id,
              "valid_until": override_until if override_id and not indefinite_block else None,
              "indefinite": bool(override_id and indefinite_block)}
    record_event("process_override_updated", "operator", result)
    return result


def expire_process_overrides() -> int:
    with db() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE ems_gpt_core_process_overrides SET status='EXPIRED'
          WHERE status='ACTIVE' AND valid_until<=NOW(6)""")
        expired = cur.rowcount
    if expired:
        record_event("process_overrides_expired", "operator", {"count": expired})
    return expired


def externally_started_hp_is_running(cur, now: datetime) -> bool:
    """Return true only for a fresh compressor run not initiated by EMS.

    A FORCE_OFF override always remains authoritative and is handled by the
    caller.  Requiring compressor frequency avoids treating standby power as
    an external heating cycle.  A recent EMS ON command keeps ownership with
    EMS, so the planned end of an automatic cycle can still issue OFF.
    """
    cur.execute("""SELECT hp_compressor_frequency_hz,captured_at
      FROM ems_gpt_telemetry_snapshots
      WHERE captured_at>=%s ORDER BY captured_at DESC LIMIT 1""",
      (now - timedelta(minutes=3),))
    sample = cur.fetchone()
    if not sample or float(sample.get("hp_compressor_frequency_hz") or 0) <= 0:
        return False
    cur.execute("""SELECT decision,created_at FROM ems_gpt_core_commands
      WHERE process_name='HP_HEAT_DHW' AND status<>'DRY_RUN'
      ORDER BY created_at DESC LIMIT 1""")
    command = cur.fetchone()
    return not (command and command.get("decision") == "ON"
                and command.get("created_at") >= now - timedelta(minutes=30))


def stage_executor_commands() -> dict:
    """Build auditable commands; never call HA unless the guarded executor is explicitly enabled."""
    if not bool(OPTIONS.get("executor_enabled", False)):
        with LOCK:
            STATE["executor"] = "OFF"
            STATE["modules"]["executor"] = "OFF"
        return {"status": "OFF", "staged": 0}
    dry_run = bool(OPTIONS.get("executor_dry_run", True))
    live_accepted = OPTIONS.get("executor_activation_ack") == "EMS_CONNECTOR_ACCEPTED"
    if not dry_run and not live_accepted:
        with LOCK:
            STATE["executor"] = "ACTIVATION_ACK_REQUIRED"
            STATE["modules"]["executor"] = "ACTIVATION_ACK_REQUIRED"
        return {"status": "ACTIVATION_ACK_REQUIRED", "staged": 0}
    now = local_now().replace(tzinfo=None)
    start = slot_start().replace(tzinfo=None)
    end = start + timedelta(minutes=int(OPTIONS.get("slot_minutes", 15)) + 1)
    staged = 0
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT d.*,o.requested_state,o.override_id FROM ems_gpt_core_process_decisions d
          LEFT JOIN ems_gpt_core_process_overrides o ON o.process_name=d.process_name
           AND o.status='ACTIVE' AND o.valid_from<=%s AND o.valid_until>%s
          WHERE d.slot_start=%s ORDER BY d.process_name""", (now, now, start))
        for row in cur.fetchall():
            planned_on = bool(row["eligible"])
            requested = row.get("requested_state")
            external_hp_on = (row["process_name"] == "HP_HEAT_DHW"
                              and requested != "FORCE_OFF"
                              and not planned_on
                              and externally_started_hp_is_running(cur, now))
            effective_on = (True if requested == "FORCE_ON" else
                            False if requested == "FORCE_OFF" else
                            True if external_hp_on else planned_on)
            decision = "ON" if effective_on else "OFF"
            source = "OVERRIDE" if requested else "EXTERNAL_MANUAL" if external_hp_on else "PLAN"
            if external_hp_on:
                record_event("external_hp_control_preserved", "executor", {
                    "process": "HP_HEAT_DHW", "decision": "HOLD_ON",
                    "reason": "fresh compressor run without recent EMS ON command"
                })
                continue
            command_id = str(uuid.uuid4())
            safety = {"executor_enabled": True, "dry_run": dry_run, "connector_required": True,
                      "soc_programs_1_6_write_allowed": False, "override_id": row.get("override_id")}
            cur.execute("""INSERT IGNORE INTO ems_gpt_core_commands
              (command_id,slot_start,slot_id,process_name,decision,plan_version,created_at,expires_at,
               source,status,safety_json) VALUES(%s,%s,%s,%s,%s,%s,NOW(6),%s,%s,%s,%s)""",
              (command_id, start, row.get("slot_id"), row["process_name"], decision, row["plan_run_id"], end, source,
               "DRY_RUN" if dry_run else "READY_FOR_CONNECTOR", json.dumps(safety)))
            staged += cur.rowcount
    state = "DRY_RUN" if dry_run else "LIVE"
    with LOCK:
        STATE["executor"] = state
        STATE["modules"]["executor"] = state
    return {"status": state, "staged": staged}


def dispatch_ready_commands() -> dict:
    """Fail-closed HA adapter. It can only invoke explicitly mapped script entities."""
    if not bool(OPTIONS.get("executor_enabled", False)) or bool(OPTIONS.get("executor_dry_run", True)):
        return {"status": "DISABLED_OR_DRY_RUN", "dispatched": 0}
    if OPTIONS.get("executor_activation_ack") != "EMS_CONNECTOR_ACCEPTED":
        return {"status": "ACTIVATION_ACK_REQUIRED", "dispatched": 0}
    try:
        service_map = json.loads(str(OPTIONS.get("connector_service_map_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("connector_service_map_json is invalid") from exc
    now = local_now().replace(tzinfo=None)
    current_slot = slot_start().replace(tzinfo=None)
    dispatched = 0
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ems_gpt_core_commands SET status='EXPIRED' WHERE status='READY_FOR_CONNECTOR' AND expires_at<=%s", (now,))
        cur.execute("""SELECT * FROM ems_gpt_core_commands WHERE status='READY_FOR_CONNECTOR'
          AND slot_start=%s AND expires_at>%s ORDER BY created_at""", (current_slot, now))
        for command in cur.fetchall():
            process_map = service_map.get(command["process_name"], {}) if isinstance(service_map, dict) else {}
            entity_id = process_map.get(command["decision"])
            if not isinstance(entity_id, str) or not entity_id.startswith("script."):
                cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                            (json.dumps({"reason": "MISSING_OR_INVALID_ALLOWLIST_MAPPING"}), command["command_id"]))
                continue
            if "inverter_program_" in entity_id or "soc" in entity_id.lower():
                cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                            (json.dumps({"reason": "PROTECTED_SOC_PROGRAM_MAPPING"}), command["command_id"]))
                continue
            if command["process_name"] == "BATTERY_EXPORT" and command["decision"] == "ON":
                live_programs = tou_program_snapshot()
                live_program = active_tou_program(now, live_programs)
                live_soc = number(ha_state("sensor.inverter_battery"))
                if live_program is None or live_soc is None or live_soc <= float(live_program["soc"]) + 0.01:
                    reason = ("TOU_FLOOR_UNAVAILABLE" if live_program is None or live_soc is None else
                              f"TOU_FLOOR_BLOCK: program={live_program['program']}, soc={live_program['soc']:.0f}%")
                    off_entity = process_map.get("OFF")
                    safe_response = ha_service_response("script", "turn_on", {"entity_id": off_entity}) \
                        if isinstance(off_entity, str) and off_entity.startswith("script.") else None
                    cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                                (json.dumps({"reason": reason, "live_soc": live_soc,
                                             "safe_off_dispatched": safe_response is not None}), command["command_id"]))
                    record_event("battery_export_blocked_by_tou_floor", "executor",
                                 {"reason": reason, "live_soc": live_soc}, "WARNING")
                    continue
            response = ha_service_response("script", "turn_on", {"entity_id": entity_id})
            if response is None:
                cur.execute("UPDATE ems_gpt_core_commands SET status='FAILED',acknowledgement_json=%s WHERE command_id=%s",
                            (json.dumps({"reason": "HA_SERVICE_FAILED", "entity_id": entity_id}), command["command_id"]))
                continue
            cur.execute("""UPDATE ems_gpt_core_commands SET status='DISPATCHED',dispatched_at=NOW(6),
              acknowledgement_json=%s WHERE command_id=%s AND status='READY_FOR_CONNECTOR'""",
              (json.dumps({"entity_id": entity_id, "ha_response": response}, ensure_ascii=False, default=str), command["command_id"]))
            dispatched += cur.rowcount
    return {"status": "DISPATCHED", "dispatched": dispatched}


def acknowledge_command(payload: dict) -> dict:
    command_id = str(payload.get("command_id") or "")
    status = str(payload.get("status") or "").upper()
    if not command_id:
        raise ValueError("command_id is required")
    if status not in {"ACCEPTED", "EXECUTED", "REJECTED", "FAILED"}:
        raise ValueError("invalid acknowledgement status")
    now = local_now().replace(tzinfo=None)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM ems_gpt_core_commands WHERE command_id=%s FOR UPDATE", (command_id,))
        command = cur.fetchone()
        if not command:
            raise ValueError("unknown command_id")
        if command["expires_at"] <= now and status in {"ACCEPTED", "EXECUTED"}:
            cur.execute("UPDATE ems_gpt_core_commands SET status='EXPIRED',acknowledgement_json=%s WHERE command_id=%s",
                        (json.dumps(payload, ensure_ascii=False), command_id))
            raise ValueError("expired command cannot be accepted or executed")
        terminal = command["status"] in {"EXECUTED", "REJECTED", "FAILED", "EXPIRED"}
        if terminal and command["status"] != status:
            raise ValueError(f"command already terminal: {command['status']}")
        cur.execute("""UPDATE ems_gpt_core_commands SET status=%s,
          acknowledged_at=CASE WHEN %s='ACCEPTED' THEN COALESCE(acknowledged_at,NOW(6)) ELSE acknowledged_at END,
          executed_at=CASE WHEN %s='EXECUTED' THEN COALESCE(executed_at,NOW(6)) ELSE executed_at END,
          acknowledgement_json=%s WHERE command_id=%s""",
          (status, status, status, json.dumps(payload, ensure_ascii=False), command_id))
    result = {"command_id": command_id, "status": status}
    record_event("connector_acknowledgement", "executor", result, "INFO" if status in {"ACCEPTED", "EXECUTED"} else "WARNING")
    return result


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
    previous = None
    while True:
        clock = local_now()
        start = slot_start(clock)
        key = start.isoformat()
        error = None
        try:
            telemetry_ok = capture_telemetry()
            closed = close_finished_slots()
            backfill_execution_details()
            rebuild_recovery_materializations(int(OPTIONS.get("recovery_lookback_days", 7)))
            learn_missing_load()
            expire_process_overrides()
            if key != previous:
                ensure_slot_calendar(clock.date(), clock.date()+timedelta(days=1))
                refresh_pv_forecast()
                refresh_weather_forecast()
                record_event("slot_opened", "core", {"slot_start": key, "recovered_after_restart": previous is None})
                previous = key
            minute = clock.minute
            hour = clock.hour
            blackout = hour in (0, 14)
            with db() as conn, conn.cursor() as cur:
                cur.execute("SELECT MAX(published_at) last_run FROM ems_gpt_plan_runs WHERE status='PUBLISHED'")
                last_run = cur.fetchone()["last_run"]
                rce_key=f"RCE_{clock.date()}_{'NEXT' if hour>=14 else 'TODAY'}"
                cur.execute("SELECT COUNT(*) n FROM ems_gpt_core_events WHERE event_type=%s",(rce_key,))
                rce_done=int(cur.fetchone()["n"] or 0)>0
            rce_due=14<=hour<=16 and minute%10==0
            if rce_due and not rce_done:
                target=clock.date()+timedelta(days=1)
                result=refresh_rce(target)
                record_event("rce_import_attempt", "core", result, "INFO" if result["status"]=="OK" else "WARNING")
                if result["status"]=="OK":
                    record_event(rce_key,"core",result)
                    complete_rce_cycle(result, "rce_import")
            due = minute in (7,22,37,52) and not blackout and (last_run is None or (now := local_now().replace(tzinfo=None))-last_run >= timedelta(minutes=55))
            if due:
                run_planner("hourly_replan")
            stage_executor_commands()
            dispatch_ready_commands()
            if start.minute == 8:
                with db() as conn, conn.cursor() as cur:
                    cur.execute("SELECT MAX(completed_at) v FROM ems_gpt_core_analytics_runs WHERE status='COMPLETED'")
                    last_analytics = cur.fetchone()["v"]
                if last_analytics is None or local_now().replace(tzinfo=None)-last_analytics >= timedelta(minutes=50):
                    analytics_result = run_analytics()
                    run_ai_observer(analytics_result.get("run_id"))
            if (hour, minute) in ((2, 8), (8, 8), (14, 23), (20, 8)):
                with db() as conn, conn.cursor() as cur:
                    cur.execute("SELECT MAX(created_at) v FROM ems_gpt_core_diagnostic_reports")
                    last_diag = cur.fetchone()["v"]
                if last_diag is None or local_now().replace(tzinfo=None)-last_diag >= timedelta(minutes=10):
                    generate_diagnostic_report("scheduled")
            with LOCK:
                STATE["database"] = "CONNECTED"
                STATE["ha_input"] = "CONNECTED" if telemetry_ok else "PARTIAL"
                STATE["status"] = "RUNNING"
                STATE["modules"].update(core="RUNNING", planner="RUNNING", ppd="RUNNING", analytics="RUNNING", diagnostics="RUNNING",
                                        ai_observer="DISABLED" if not OPTIONS.get("ai_observer_enabled") else "SHADOW_READ_ONLY")
        except Exception as exc:
            error = str(exc)
            LOG.exception("engine cycle failed")
            with LOCK:
                STATE["database"] = "ERROR"
                STATE["status"] = "DEGRADED"
        with LOCK:
            STATE["active_slot"] = key
            STATE["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            STATE["last_error"] = error
        time.sleep(60)


HTML = """<!doctype html><html lang='pl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>EMS-GPT Core</title><style>body{font-family:system-ui;margin:0;background:#071426;color:#eaf2ff}.wrap{max-width:1400px;margin:auto;padding:24px}header{display:flex;gap:16px;align-items:center}header img{width:72px;height:72px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.card{background:#10243d;border:1px solid #173657;border-radius:16px;padding:16px}.clickable{cursor:pointer}.clickable:hover{border-color:#15c987}.executor-actions{display:none;margin-top:12px}.executor-actions.open{display:block}.danger{background:#d94a64;color:white;border:0;padding:10px 14px;border-radius:10px;cursor:pointer}.label{color:#8ea7c4;font-size:12px;text-transform:uppercase}.value{font-size:20px;font-weight:750;margin-top:6px}.ok{color:#23e88f}.warn{color:#ffbd3f}pre{white-space:pre-wrap}.tag{display:inline-block;padding:5px 9px;border-radius:20px;background:#163454;margin:3px}.tabs{display:flex;gap:8px;flex-wrap:wrap}.tabs button,.apply{background:#163454;color:#dcecff;border:0;padding:10px 14px;border-radius:10px;cursor:pointer}.tabs button.active,.apply{background:#15c987;color:#03150e}.settings,.column-settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}.setting-section{border:1px solid #1d3b5b;border-radius:12px;padding:14px;margin:12px 0}.setting-section h3{margin:0 0 12px}.setting label{display:block;color:#8ea7c4;font-size:12px;margin-bottom:5px}.setting input{box-sizing:border-box;width:100%;padding:9px;border:1px solid #275277;border-radius:9px;background:#071426;color:#eaf2ff}.column-option{display:flex;gap:8px;align-items:center;padding:7px;border-radius:8px;background:#0b1c31}.column-option input{width:auto}.hint{color:#8ea7c4;font-size:13px}.tablebox{overflow-x:scroll;overflow-y:auto;scrollbar-gutter:stable;max-height:58vh;border-radius:12px}table{width:100%;min-width:max-content;border-collapse:separate;border-spacing:0;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #1d3b5b;white-space:nowrap;text-align:right}th{position:sticky;top:0;background:#10243d;color:#8ea7c4;z-index:2}th:first-child,td:first-child{position:sticky;left:0;background:#10243d;text-align:left;z-index:1}th:first-child{z-index:3}.yes{color:#23e88f}.no{color:#ff647c}@media(max-width:600px){.wrap{padding:12px}header img{width:56px;height:56px}h1{font-size:22px}}</style></head>
<body><div class='wrap'><header><img src='icon.png'><div><h1>EMS-GPT Core</h1><div style='color:#8ea7c4'>Niezależny silnik planowania i analityki energii</div></div></header><br><div class='grid'>
<div class='card'><div class='label'>Stan aplikacji</div><div id='status' class='value'>…</div><div id='applicationStatus' class='hint'>Wersja: … · heartbeat: …</div></div><div class='card'><div class='label'>MariaDB</div><div id='db' class='value'>…</div></div><div class='card'><div class='label'>Dane z HA</div><div id='ha' class='value'>…</div></div><div id='executorCard' class='card clickable'><div class='label'>Wykonawca · kliknij, aby sterować</div><div id='executor' class='value warn'>…</div><div id='executorActions' class='executor-actions'><button id='executorLive' class='apply'>Włącz LIVE</button> <button id='executorOff' class='danger'>Wyłącz</button><div id='executorMessage' class='hint'></div></div></div></div><br>
<div class='card'><div class='label'>Moduły</div><div id='modules'></div></div><br><div class='card'><div class='label'>Aktywny slot</div><div id='slot' class='value'>…</div><pre id='error'>Brak błędów</pre></div><br>
<div class='card'><div class='tabs'><button data-view='plan' class='active'>Planer</button><button data-view='execution'>Wykonanie</button><button data-view='hourly'>Godzinowe</button><button data-view='daily'>Dobowe</button><button data-view='processes'>Procesy</button><button data-view='process-execution'>Przebiegi</button><button data-view='analytics'>Analityka</button><button data-view='ai-runs'>AI Observer</button><button data-view='diagnostics'>Diagnostyka</button><button data-view='configuration'>Konfiguracja</button></div><br><div id='tablePanel'><div id='processControls' style='display:none'></div><div id='tableTitle' class='label'>PLANER</div><div class='tablebox'><table><thead id='thead'></thead><tbody id='tbody'></tbody></table></div></div><div id='configPanel' style='display:none'><div class='label'>Konfiguracja aplikacji</div><div id='settings'></div><br><button id='applySettings' class='apply'>Zastosuj parametry</button> <span id='settingsStatus'></span><section class='setting-section'><h3>Kolumny tabel</h3><p class='hint'>Wybierz widok i zaznacz dane, które mają być widoczne. Ustawienie jest zapisywane lokalnie w tej przeglądarce.</p><select id='columnView'></select> <button id='resetColumns'>Przywróć domyślne</button><div id='columnSettings' class='column-settings'></div></section></div></div></div>
<script>
const layouts={
plan:[['slot_start','Slot'],['recommendation','Rekomendacja'],['soc_start_plan_pct','SOC przed'],['soc_end_plan_pct','SOC po'],['soc_floor_pct','SOC floor'],['soc_target_pct','SOC target'],['forecast_pv1_kwh','PV1 plan'],['forecast_pv2_kwh','PV2 plan'],['forecast_pv_total_kwh','PV razem'],['forecast_load_kwh','Zużycie'],['price_buy_pln_kwh','Zakup PLN/kWh'],['price_sell_pln_kwh','Sprzedaż PLN/kWh'],['buy_window','Okno BUY'],['sale_window','Okno SELL'],['grid_policy_planned','Polityka sieci'],['grid_buy_allowed','BUY_ALLOWED'],['grid_no_buy','NO_BUY'],['grid_neutral','GRID_NEUTRAL'],['export_policy_planned','Polityka eksportu'],['sell_bat_allowed','SELL_BAT'],['no_sell_bat','NO_SELL_BAT'],['sell_pv_allowed','SELL_PV'],['no_sell_pv','NO_SELL_PV'],['planned_battery_charge_kwh','Ład. BAT'],['planned_battery_discharge_kwh','Rozł. BAT'],['planned_buy_kwh','Import'],['planned_sell_kwh','BAT→sieć'],['planned_pv_to_bat_kwh','PV→BAT kWh'],['planned_pv_to_cwu_kwh','PV→CWU kWh'],['planned_pv_to_ev_kwh','PV→EV kWh'],['planned_pv_export_kwh','PV→sieć kWh'],['planned_pv_curtail_kwh','PV ogranicz. kWh'],['pv_export_planned','Eksport PV plan'],['pv_curtail_planned','Redukcja PV'],['pv_to_bat_planned','PV→BAT'],['pv_to_cwu_planned','PV→CWU'],['pv_to_ev_planned','PV→EV'],['heat_pump_window','Okno HP'],['ppd_reason','Powód PPD']],
execution:[['slot_start','Slot'],['recommendation','Rekomendacja planu'],['actual_pv1_kwh','PV1'],['actual_pv2_kwh','PV2'],['actual_pv_total_kwh','PV razem'],['actual_load_kwh','Zużycie'],['actual_buy_kwh','Import'],['actual_grid_export_kwh','Eksport sieć'],['actual_battery_charge_kwh','Ład. BAT'],['actual_battery_discharge_kwh','Rozł. BAT'],['actual_ev_kwh','EV'],['soc_start_pct','SOC start'],['soc_min_pct','SOC min'],['soc_end_pct','SOC koniec'],['soc_delta_pct','ΔSOC'],['actual_temperature_c','Temp. ogród'],['actual_dhw_temperature_c','CWU °C'],['actual_heat_pump_mode','Tryb HP'],['actual_heat_pump_electric_kwh','HP pobrana'],['actual_heat_pump_thermal_kwh','HP użytkowa'],['actual_heat_pump_cop','COP'],['actual_heat_pump_outlet_temperature_c','HP zasilanie'],['actual_heat_pump_inlet_temperature_c','HP powrót'],['actual_heat_pump_delta_t_c','HP ΔT'],['actual_heat_pump_compressor_frequency_hz','Sprężarka Hz'],['actual_heat_pump_flow_l_min','Przepływ'],['actual_heat_pump_is_running','HP pracuje'],['sample_count','Próbki'],['coverage_pct','Pokrycie %'],['export_attribution','Źródło eksportu']],
hourly:[['hour_start','Godzina'],['completion_status','Zamknięcie'],['quality_status','Jakość'],['expected_slot_count','Sloty oczek.'],['terminal_slot_count','Sloty terminalne'],['recovered_slot_count','Odtworzone'],['missing_slot_count','Brak danych'],['coverage_pct','Pokrycie %'],['learning_eligible','Do uczenia'],['forecast_pv_kwh','PV plan'],['actual_pv_kwh','PV wykon.'],['planned_pv_to_bat_kwh','PV→BAT'],['planned_pv_to_cwu_kwh','PV→CWU'],['planned_pv_to_ev_kwh','PV→EV'],['planned_pv_export_kwh','PV→sieć'],['planned_pv_curtail_kwh','PV ogranicz.'],['forecast_load_kwh','Zuż. plan'],['actual_load_kwh','Zuż. wykon.'],['planned_import_kwh','Import plan'],['actual_import_kwh','Import wykon.'],['planned_export_kwh','Eksport plan'],['actual_export_kwh','Eksport wykon.'],['soc_end_pct','SOC koniec'],['planned_net_pln','PLN plan'],['actual_net_pln','PLN wykon.'],['slot_count','Sloty'],['closed_at','Zamknięto'],['updated_at','Aktualizacja']],
daily:[['day_date','Dzień'],['completion_status','Zamknięcie'],['data_quality_status','Jakość'],['expected_slot_count','Sloty oczek.'],['terminal_slot_count','Sloty terminalne'],['valid_actual_slot_count','Sloty ważne'],['missing_actual_slot_count','Sloty brak'],['recovered_slot_count','Sloty odtw.'],['learning_eligible','Do uczenia'],['forecast_pv_kwh','PV plan'],['actual_pv_kwh','PV wykon.'],['planned_pv_to_bat_kwh','PV→BAT'],['planned_pv_to_cwu_kwh','PV→CWU'],['planned_pv_to_ev_kwh','PV→EV'],['planned_pv_curtail_kwh','PV ogranicz.'],['forecast_load_kwh','Zuż. plan'],['actual_load_kwh','Zuż. wykon.'],['forecast_import_kwh','Import plan'],['actual_import_kwh','Import wykon.'],['forecast_export_kwh','Eksport plan'],['actual_export_kwh','Eksport wykon.'],['forecast_battery_charge_kwh','Ład. BAT plan'],['actual_battery_charge_kwh','Ład. BAT wykon.'],['forecast_battery_discharge_kwh','Rozł. BAT plan'],['actual_battery_discharge_kwh','Rozł. BAT wykon.'],['forecast_pv_export_kwh','PV eksport plan'],['actual_pv_export_kwh','PV eksport wykon.'],['actual_heat_pump_electric_kwh','HP pobrana'],['actual_heat_pump_thermal_kwh','HP użytkowa'],['actual_heat_pump_cop','HP COP'],['actual_dhw_generated_kwh','CWU wytw.'],['actual_heating_generated_kwh','CO wytw.'],['planned_net_pln','PLN plan'],['actual_net_pln','PLN wykon.'],['pv_production_start_time','PV start'],['pv_production_end_time','PV koniec'],['heating_production_start_time','CO start'],['heating_production_end_time','CO koniec'],['dhw_production_start_time','CWU start'],['dhw_production_end_time','CWU koniec'],['closed_at','Zamknięto'],['data_quality_reason','Powód jakości']],
analytics:[['completed_at','Zakończono'],['status','Status'],['slots_scanned','Sloty'],['complete_slots','Kompletne'],['quality_score','Jakość %'],['pv1_wape_pct','PV1 WAPE'],['pv2_wape_pct','PV2 WAPE'],['pv_wape_pct','PV razem WAPE'],['load_wape_pct','Load WAPE'],['import_wape_pct','Import WAPE'],['export_wape_pct','Eksport WAPE'],['pv_bias_kwh','PV bias kWh'],['load_bias_kwh','Load bias kWh'],['import_bias_kwh','Import bias kWh'],['export_bias_kwh','Eksport bias kWh'],['soc_mae_pct','SOC MAE %'],['net_cost_variance_pln','Odchylenie PLN']],
'ai-runs':[['completed_at','Zakończono'],['status','Status'],['role_name','Rola'],['source_ref','Analiza źródłowa'],['auto_score','Autoocena'],['decision','Decyzja'],['result_json','Sugestie i wynik']],
diagnostics:[['created_at','Utworzono'],['status','Status'],['alert_count','Alarmy'],['trigger_name','Wyzwalacz'],['summary','Podsumowanie']],
processes:[['slot_start','Slot'],['process_name','Proces'],['decision','Decyzja'],['eligible','Zgoda'],['valid_until','Ważna do'],['connector_required','Konektor'],['reason','Przyczyna']],
'process-execution':[['slot_start','Slot'],['process_name','Proces'],['planned_state','Plan'],['effective_state','Efektywnie'],['observed_state','Obserwacja'],['consistency','Zgodność'],['control_origin','Źródło sterowania'],['observed_energy_kwh','Energia kWh'],['recorded_at','Zapisano'],['reason','Przyczyna']],
};
const customizableViews=['plan','execution','hourly','daily','processes','process-execution','analytics','ai-runs','diagnostics'];
const viewNames={plan:'Planer',execution:'Wykonanie',hourly:'Godzinowe',daily:'Dobowe',processes:'Procesy','process-execution':'Przebiegi',analytics:'Analityka','ai-runs':'AI Observer',diagnostics:'Diagnostyka'};
const defaultColumns=Object.fromEntries(customizableViews.map(v=>[v,layouts[v].map(c=>c[0])]));
const booleanFields=new Set(['eligible','connector_required','learning_eligible','buy_window','sale_window','heat_pump_window','pv_to_cwu_planned','pv_to_ev_planned','pv_to_bat_planned','pv_export_planned','pv_curtail_planned','grid_buy_allowed','grid_no_buy','grid_neutral','sell_bat_allowed','no_sell_bat','sell_pv_allowed','no_sell_pv','actual_heat_pump_is_running']);
function columnPrefs(){try{return JSON.parse(localStorage.getItem('ems-gpt-columns-v1')||'{}')}catch(e){return {}}}
function activeLayout(view){const selected=columnPrefs()[view];if(!Array.isArray(selected)||!selected.length)return layouts[view];return selected.map(k=>layouts[view].find(c=>c[0]===k)).filter(Boolean)}
function saveColumns(view,keys){const prefs=columnPrefs();prefs[view]=keys;localStorage.setItem('ems-gpt-columns-v1',JSON.stringify(prefs))}
function renderColumnSettings(){const view=columnView.value;const selected=new Set(activeLayout(view).map(c=>c[0]));columnSettings.innerHTML=layouts[view].map(c=>`<label class='column-option'><input type='checkbox' data-column='${c[0]}' ${selected.has(c[0])?'checked':''}>${c[1]}</label>`).join('');columnSettings.querySelectorAll('input').forEach(i=>i.onchange=()=>{const keys=[...columnSettings.querySelectorAll('input:checked')].map(x=>x.dataset.column);if(!keys.length){i.checked=true;return}saveColumns(view,keys)})}
function fmt(v,key){if(v===null||v===undefined)return '—';if(booleanFields.has(key)&&(v===0||v===1||v==='0'||v==='1'))return Number(v)?'TAK':'NIE';if(typeof v==='number')return v.toFixed(3);const text=String(v).replace(/^NEU\\s*RAL$/i,'NEUTRAL');return text.replace(/(\\d)T(?=\\d)/,'$1 ').slice(0,32)}
function cellValue(row,key){if(key==='consistency'){if(!row.observed_state)return 'BRAK DANYCH';const plannedOn=row.planned_state==='ON';const observedOn=row.observed_state==='RUNNING';return plannedOn===observedOn?'ZGODNE':'ROZBIEŻNOŚĆ'}if(key==='control_origin'){return row.control_origin||(row.command_id?'AUTO':row.observed_state==='RUNNING'&&row.planned_state!=='ON'?'EXTERNAL_MANUAL':row.decision_source||'OBSERWACJA')}if(key==='data_quality_status'&&!row[key]&&row.day_date===new Date().toLocaleDateString('sv-SE'))return 'OPEN';return row[key]}
const processNames=['BATTERY_IMPORT','BATTERY_EXPORT','PV_CWU','PV_EV','HP_HEAT_DHW'];
async function loadProcessControls(){const r=await fetch('api/overrides?limit=30');const j=await r.json();const active={};j.rows.forEach(x=>{if(x.status==='ACTIVE')active[x.process_name]=x});processControls.innerHTML=`<div class='grid'>${processNames.map(p=>{const a=active[p];const hpBlocked=p==='HP_HEAT_DHW'&&a?.requested_state==='FORCE_OFF';const status=hpBlocked?'BLOKADA BEZTERMINOWA':a?a.requested_state+' do '+fmt(a.valid_until,'valid_until'):'AUTO';return `<div class='card'><b>${p}</b><div class='label ${hpBlocked?'no':''}'>${status}</div>${p==='HP_HEAT_DHW'?`<label class='label' for='hpManualHours'>Czas ręcznego włączenia [h]</label><input id='hpManualHours' type='number' min='2' max='24' step='.25' value='2'>`:''}<p><button class='apply' onclick="setOverride('${p}','FORCE_ON')">Włącz</button> <button class='${hpBlocked?'danger':''}' onclick="setOverride('${p}','FORCE_OFF')">Blokuj</button> <button onclick="setOverride('${p}','AUTO')">Auto</button></p></div>`}).join('')}</div><br>`}
async function setOverride(process,state){let minutes=60;if(process==='HP_HEAT_DHW'&&state==='FORCE_ON'){const hours=Number(document.getElementById('hpManualHours')?.value||2);if(!Number.isFinite(hours)||hours<2||hours>24){alert('Czas HP musi wynosić od 2 do 24 godzin');return}minutes=Math.round(hours*60)}const r=await fetch('api/process/override',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({process,state,minutes,reason:'panel operatora'})});const j=await r.json();if(!r.ok)alert(j.error||'Błąd');await loadProcessControls()}
async function loadView(view){document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));const config=view==='configuration';tablePanel.style.display=config?'none':'block';configPanel.style.display=config?'block':'none';if(config){await loadSettings();return}processControls.style.display=view==='processes'?'block':'none';if(view==='processes')await loadProcessControls();tableTitle.textContent=(viewNames[view]||view).toUpperCase();const r=await fetch('api/'+view+'?limit=96');const j=await r.json();const cols=activeLayout(view);thead.innerHTML='<tr>'+cols.map(c=>'<th>'+c[1]+'</th>').join('')+'</tr>';tbody.innerHTML=j.rows.map(row=>'<tr>'+cols.map(c=>'<td>'+fmt(cellValue(row,c[0]),c[0])+'</td>').join('')+'</tr>').join('')}
async function loadSettings(){const r=await fetch('api/settings');const j=await r.json();const groups={};Object.entries(j.settings).forEach(([k,s])=>(groups[s.group]??=[]).push([k,s]));settings.innerHTML=Object.entries(groups).map(([group,items])=>`<section class='setting-section'><h3>${group}</h3><div class='settings'>${items.map(([k,s])=>`<div class='setting'><label>${s.label}</label><input data-key='${k}' type='number' min='${s.min}' max='${s.max}' step='0.01' value='${s.value}'></div>`).join('')}</div></section>`).join('');if(!columnView.options.length)columnView.innerHTML=customizableViews.map(v=>`<option value='${v}'>${viewNames[v]}</option>`).join('');renderColumnSettings()}
columnView.onchange=renderColumnSettings;
resetColumns.onclick=()=>{saveColumns(columnView.value,defaultColumns[columnView.value]);renderColumnSettings()};
applySettings.onclick=async()=>{const values={};settings.querySelectorAll('input').forEach(i=>values[i.dataset.key]=Number(i.value));settingsStatus.textContent='Zapisywanie…';const r=await fetch('api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});const j=await r.json();settingsStatus.textContent=r.ok?'Zapisano — obowiązuje od następnego przeliczenia':(j.error||'Błąd')}
executorCard.onclick=e=>{if(e.target.tagName!=='BUTTON')executorActions.classList.toggle('open')};
async function setExecutorMode(mode){if(mode==='LIVE'&&!confirm('Włączyć wykonawcę LIVE? Polecenia będą wysyłane do urządzeń.'))return;executorMessage.textContent='Zapisywanie…';const body={mode};if(mode==='LIVE')body.confirmation='EMS_CONNECTOR_ACCEPTED';const r=await fetch('api/executor/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();executorMessage.textContent=r.ok?'Tryb: '+j.mode:(j.error||'Błąd');await tick()}
executorLive.onclick=()=>setExecutorMode('LIVE');
executorOff.onclick=()=>setExecutorMode('OFF');
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>loadView(b.dataset.view));
async function tick(){const r=await fetch('api/status');const s=await r.json();status.textContent=s.status;status.className='value '+(s.status==='RUNNING'?'ok':'warn');const heartbeat=s.last_heartbeat?new Date(s.last_heartbeat).toLocaleString('pl-PL'):'brak';applicationStatus.textContent=`Wersja: ${s.version||'—'} · heartbeat: ${heartbeat}`;applicationStatus.className='hint '+(s.last_heartbeat?'ok':'warn');db.textContent=s.database;ha.textContent=s.ha_input;executor.textContent=s.executor;slot.textContent=s.active_slot||'—';error.textContent=s.last_error||'Brak błędów';modules.innerHTML=Object.entries(s.modules).map(([k,v])=>`<span class='tag'>${k}: <b>${v}</b></span>`).join('')}tick();loadView('plan');setInterval(tick,5000)
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def json(self, payload: dict, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/health") or path == "/health":
            with LOCK: healthy = STATE["database"] == "CONNECTED" and STATE["status"] in {"RUNNING", "DEGRADED"}
            return self.json({"ok": healthy, "app": APP_NAME, "version": APP_VERSION}, HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE)
        if path.endswith("/api/status") or path == "/api/status":
            with LOCK: return self.json(dict(STATE))
        if path.endswith("/api/settings") or path == "/api/settings":
            return self.json({"settings": settings_payload()})
        if path.endswith("/api/process-status") or path == "/api/process-status":
            return self.json({})
        if any(path.endswith(f"/api/{name}") or path == f"/api/{name}" for name in ("plan","execution","hourly","daily","runs","analytics","diagnostics","processes","overrides","commands","process-execution","todo","ai-runs")):
            name=path.rsplit("/",1)[-1]; params=parse_qs(urlparse(self.path).query); limit=min(500,max(1,int(params.get("limit",["96"])[0])))
            queries={
              "plan":("SELECT * FROM ems_gpt_slots WHERE actual_recorded_at IS NULL AND slot_start>=%s ORDER BY slot_start LIMIT %s",(slot_start().replace(tzinfo=None),limit)),
              "execution":("""SELECT s.*,d.actual_grid_export_kwh,d.actual_ev_kwh,d.actual_dhw_kwh,
                d.soc_start_pct,d.soc_min_pct,d.soc_delta_pct,
                d.sample_count,d.coverage_pct,d.export_attribution FROM ems_gpt_slots s
                LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
                WHERE s.actual_recorded_at IS NOT NULL ORDER BY s.slot_start DESC LIMIT %s""",(limit,)),
              "hourly":("SELECT * FROM ems_gpt_core_hourly WHERE hour_start<=%s ORDER BY hour_start DESC LIMIT %s",(local_now().replace(tzinfo=None),limit)),
              "daily":("SELECT * FROM ems_gpt_daily ORDER BY day_date DESC LIMIT %s",(limit,)),
              "runs":("SELECT * FROM ems_gpt_plan_runs ORDER BY created_at DESC LIMIT %s",(limit,)),
              "analytics":("SELECT * FROM ems_gpt_core_analytics_runs ORDER BY started_at DESC LIMIT %s",(limit,)),
              "diagnostics":("SELECT * FROM ems_gpt_core_diagnostic_reports ORDER BY created_at DESC LIMIT %s",(limit,)),
              "processes":("SELECT * FROM ems_gpt_core_process_decisions WHERE slot_start>=%s ORDER BY slot_start,process_name LIMIT %s",(slot_start().replace(tzinfo=None),limit)),
              "overrides":("SELECT * FROM ems_gpt_core_process_overrides ORDER BY requested_at DESC LIMIT %s",(limit,)),
              "commands":("SELECT * FROM ems_gpt_core_commands ORDER BY created_at DESC LIMIT %s",(limit,)),
              "process-execution":("SELECT * FROM ems_gpt_core_process_execution ORDER BY recorded_at DESC LIMIT %s",(limit,)),
              "todo":("SELECT * FROM ems_gpt_core_todo ORDER BY created_at DESC LIMIT %s",(limit,)),
              "ai-runs":("SELECT * FROM ems_gpt_core_ai_runs ORDER BY started_at DESC LIMIT %s",(limit,)),
            }
            with db() as conn,conn.cursor() as cur: cur.execute(*queries[name]); rows=cur.fetchall()
            return self.json({"view":name,"count":len(rows),"rows":rows})
        if path.endswith("/icon.png") or path == "/icon.png":
            data = Path("/app/icon.png").read_bytes(); self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(len(data))); self.end_headers(); return self.wfile.write(data)
        data = HTML.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

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
            try:
                return self.json({"status":"ACCEPTED",**run_planner("manual_api")})
            except Exception as exc:
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
                result=run_analytics()
                return self.json({"status":"ACCEPTED", **result, "observer":run_ai_observer(result.get("run_id"))})
            except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.INTERNAL_SERVER_ERROR)
        if path.endswith("/api/ai-observer/run") or path=="/api/ai-observer/run":
            try: return self.json(run_ai_observer())
            except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.INTERNAL_SERVER_ERROR)
        if path.endswith("/api/diagnostics/run") or path=="/api/diagnostics/run":
            try: return self.json(generate_diagnostic_report("manual_api"))
            except Exception as exc: return self.json({"status":"ERROR","error":str(exc)},HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.json({"error":"not_found"},HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args):
        LOG.info("http " + fmt, *args)


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
