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
APP_VERSION = "0.27.1"
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
RUNTIME_SETTINGS_PATH = DATA_DIR / "runtime-settings.json"
HA_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("ems-gpt-core")


OPTIONS = load_options(OPTIONS_PATH, RUNTIME_SETTINGS_PATH)
OPERATIONAL_SETTINGS = {
    "purchase_margin_pln_kwh": (0.0, 5.0, "Mar≈ºa zakupu [PLN/kWh]", "Ceny i ekonomia"),
    "minimum_arbitrage_margin_pln_kwh": (0.0, 5.0, "Minimalna mar≈ºa arbitra≈ºu [PLN/kWh]", "Ceny i ekonomia"),
    "battery_degradation_cost_pln_kwh": (0.0, 5.0, "Degradacja baterii [PLN/kWh]", "Ceny i ekonomia"),
    "battery_charge_efficiency": (0.01, 1.0, "Sprawno≈õƒá ≈Çadowania", "Bateria"),
    "battery_discharge_efficiency": (0.01, 1.0, "Sprawno≈õƒá roz≈Çadowania", "Bateria"),
    "battery_capacity_kwh": (1.0, 100.0, "Pojemno≈õƒá baterii [kWh]", "Bateria"),
    "battery_min_soc_pct": (0.0, 90.0, "Minimalny SOC [%]", "Bateria"),
    "battery_max_power_kw": (0.25, 30.0, "Maksymalna moc baterii [kW]", "Bateria"),
    "historical_soc_drop_p80_pct": (0.0, 100.0, "Historyczny spadek SOC P80 [%]", "Prognozy i procesy"),
    "pv_cwu_min_surplus_kw": (0.0, 20.0, "Pr√≥g PV‚ÜíCWU [kW]", "Prognozy i procesy"),
    "pv_ev_min_surplus_kw": (0.0, 20.0, "Pr√≥g PV‚ÜíEV [kW]", "Prognozy i procesy"),
    "evening_soc_target_pct": (15.0, 95.0, "Bazowy cel SOC o 20:00 [%]", "Prognozy i procesy"),
    "forecast_uncertainty_weight": (0.0, 2.0, "Waga niepewno≈õci prognozy", "Prognozy i procesy"),
    "terminal_soc_value_weight": (0.0, 2.0, "Waga warto≈õci ko≈Ñcowego SOC", "Prognozy i procesy"),
    "buy_window_tolerance_pln_kwh": (0.0, 2.0, "Tolerancja okna zakupu [PLN/kWh]", "Ceny i ekonomia"),
    "planned_flow_threshold_kwh": (0.0, 1.0, "Pr√≥g istotnego przep≈Çywu planu [kWh/slot]", "Ceny i ekonomia"),
    "technical_flow_threshold_kwh": (0.0, 1.0, "Pr√≥g przep≈Çywu technicznego [kWh/slot]", "Ceny i ekonomia"),
    "soc_floor_max_pct": (15.0, 100.0, "Maksymalny SOC floor [%]", "Bateria"),
    "soc_target_max_pct": (15.0, 100.0, "Maksymalny SOC target [%]", "Bateria"),
    "sale_morning_start_hour": (0.0, 24.0, "Sprzeda≈º rano ‚Äî poczƒÖtek [h]", "Okna czasowe"),
    "sale_morning_end_hour": (0.0, 24.0, "Sprzeda≈º rano ‚Äî koniec [h]", "Okna czasowe"),
    "sale_evening_start_hour": (0.0, 24.0, "Sprzeda≈º wiecz√≥r ‚Äî poczƒÖtek [h]", "Okna czasowe"),
    "sale_evening_end_hour": (0.0, 24.0, "Sprzeda≈º wiecz√≥r ‚Äî koniec [h]", "Okna czasowe"),
    "night_heating_threshold_c": (-20.0, 25.0, "Nocny pr√≥g ogrzewania [¬∞C]", "Pompa ciep≈Ça"),
    "hp_min_heating_hours": (1.0, 24.0, "Minimalne grzanie domu [h/dobƒô]", "Pompa ciep≈Ça"),
    "hp_min_cycle_hours": (0.25, 6.0, "Minimalna d≈Çugo≈õƒá cyklu HP [h]", "Pompa ciep≈Ça"),
    "hp_min_cycle_break_hours": (0.25, 6.0, "Minimalna przerwa miƒôdzy cyklami [h]", "Pompa ciep≈Ça"),
    "hp_max_cycle_break_hours": (0.25, 12.0, "Maksymalna przerwa miƒôdzy cyklami [h]", "Pompa ciep≈Ça"),
    "hp_planned_power_kw": (0.25, 15.0, "Planowana moc elektryczna HP [kW]", "Pompa ciep≈Ça"),
    "hp_cycle_start_penalty_pln": (0.0, 20.0, "Koszt uruchomienia kolejnego cyklu HP [PLN]", "Pompa ciep≈Ça"),
    "telemetry_min_samples_per_slot": (1.0, 15.0, "Minimalna liczba pr√≥bek slotu", "Jako≈õƒá danych"),
    "telemetry_learning_coverage_pct": (0.0, 100.0, "Minimalne pokrycie do uczenia [%]", "Jako≈õƒá danych"),
    "recovery_lookback_days": (1.0, 31.0, "Zakres odtwarzania po awarii [dni]", "Jako≈õƒá danych"),
    "observer_pv_wape_warn_pct": (0.0, 500.0, "Observer: pr√≥g PV WAPE [%]", "AI Observer"),
    "observer_load_wape_warn_pct": (0.0, 500.0, "Observer: pr√≥g Load WAPE [%]", "AI Observer"),
    "observer_soc_mae_warn_pct": (0.0, 100.0, "Observer: pr√≥g SOC MAE [%]", "AI Observer"),
    "observer_cost_variance_warn_pln": (0.0, 10000.0, "Observer: pr√≥g odchylenia kosztu [PLN]", "AI Observer"),
    "observer_min_quality_score_pct": (0.0, 100.0, "Observer: minimalna jako≈õƒá [%]", "AI Observer"),
}
TZ = ZoneInfo(OPTIONS["timezone"])
_RUNTIME = build_runtime(APP_NAME, APP_VERSION, LOG)
LOCK = _RUNTIME.lock
STATE = _RUNTIME.state
run_serialized = _RUNTIME.run_serialized


_DATABASE = build_database(OPTIONS, TZ)
db = _DATABASE.db
qname = _DATABASE.qname


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
       -x€Om¢Gß≤⁄Óù∆≠y–ÄÄÄÄÄÅëïç•Õ•Ω∏ıY1UL°ëïç•Õ•Ω∏§±ï±•ù•â±îıY1UL°ï±•ù•â±î§±…ïÖÕΩ∏ıY1UL°…ïÖÕΩ∏§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕ±Ω—}•êı=1M°Õ±Ω—}•ê±Y1UL°Õ±Ω—}•ê§§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ¡±Öπ}…’π}•êıY1UL°¡±Öπ}…’π}•ê§±ŸÖ±•ë}’π—•∞ıY1UL°ŸÖ±•ë}’π—•∞§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅçΩππïç—Ω…}…ï≈’•…ïêÙƒ±¡’â±•Õ°ïë}Ö–ı9=\†ÿ§ààà∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°…Ω›lâÕ±Ω—}Õ—Ö…–ât∞Å…Ω‹πùï–†âÕ±Ω—}•êà§∞Å¡…ΩçïÕÕ}πÖµî∞Åëïç•Õ•Ω∏∞Åï±•ù•â±î∞Å¡…ΩçïÕÕ}…ïÖÕΩ∏∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ…’π}•ê∞Å…Ω›lâÕ±Ω—}Õ—Ö…–ât≠—•µïëï±—Ñ°µ•π’—ïÃÙƒÿ§§§(ÄÄÄÄÄÄÄÅÖ’ë•—}Õ—Öùî°ç’»±…’π}•ê∞âM=à∞â=,à±±ï∏°âÖÕî§∞âçΩπ—•π’Ω’ÃÅ…ïÕï…ŸîÄ¨Å¡°ÂÕ•çÖ∞ÅïπŸï±Ω¡îà§(ÄÄÄÄÄÄÄÅÖ’ë•—}Õ—Öùî°ç’»±…’π}•ê∞âAAà∞â=,à±±ï∏°âÖÕî§∞â<°∏§Åô’—’…îÅÕçÖ∏ÏÅ…ï¡±Öçïµïπ–ÅçΩÕ–Ä¨Åïôô•ç•ïπç‰Ä¨Åëïù…ÖëÖ—•Ω∏Ä¨ÅM=Å…ïÕï…Ÿîà§(ÄÄÄÄÄÄÄÅç’»πï·ïç’—î†ààâM1PÅ=U9P†®§Å∏∞(ÄÄÄÄÄÄÄÄÄÅMU4°¡…•çï}â’Â}¡±π}≠›†Å%LÅ9U10Å=HÅ¡…•çï}Õï±±}¡±π}≠›†Å%LÅ9U10§ÅâÖë}¡…•çî∞(ÄÄÄÄÄÄÄÄÄÅMU4°ÕΩç}ô±ΩΩ…}¡ç–Å%LÅ9U10Å=HÅÕΩç}—Ö…ùï—}¡ç–Å%LÅ9U10§ÅâÖë}ÕΩå∞(ÄÄÄÄÄÄÄÄÄÅMU4°ù…•ë}¡Ω±•çÂ}¡±ÖππïêÅ%LÅ9U10Å=HÅï·¡Ω…—}¡Ω±•çÂ}¡±ÖππïêÅ%LÅ9U10(ÄÄÄÄÄÄÄÄÄÄÄÅ=HÅù…•ë}â’Â}Ö±±Ω›ïê≠ù…•ë}πΩ}â’‰≠ù…•ë}πï’—…Ö∞¯ƒ(ÄÄÄÄÄÄÄÄÄÄÄÅ=HÅÕï±±}âÖ—}Ö±±Ω›ïê≠πΩ}Õï±±}âÖ–¯ƒ(ÄÄÄÄÄÄÄÄÄÄÄÅ=HÅÕï±±}¡Ÿ}Ö±±Ω›ïê≠πΩ}Õï±±}¡ÿ¯ƒ(ÄÄÄÄÄÄÄÄÄÄÄÅ=HÅ°ïÖ—}¡’µ¡}›•πëΩ‹Å9=PÅ%8Ä†¿∞ƒ§§ÅâÖë}¡¡ê(ÄÄÄÄÄÄÄÄÄÅI=4ÅïµÕ}ù¡—}¡±Öπ}Õ—Öùï}…Ω›ÃÅ]!IÅ…’π}•êÙïÃààà∞°…’π}•ê∞§§(ÄÄÄÄÄÄÄÅç°ïç≠Ãıç’»πôï—ç°Ωπî†§(ÄÄÄÄÄÄÄÅÖççï¡—ïêıç°ïç≠Õlâ∏âtÙı±ï∏°ÕΩ’…çî§ÅÖπêÅπΩ–ÅÖπ‰°•π–°ç°ïç≠Õm≠tÅΩ»Ä¿§ÅôΩ»Å¨Å•∏Ä†ââÖë}¡…•çîà∞ââÖë}ÕΩåà∞ââÖë}¡¡êà§§(ÄÄÄÄÄÄÄÅ•òÅπΩ–ÅÖççï¡—ïêË(ÄÄÄÄÄÄÄÄÄÄÄÅç’»πï·ïç’—î†âUAQÅïµÕ}ù¡—}¡±Öπ}…’πÃÅMPÅÕ—Ö—’ÃÙùI)Qú±ç’……ïπ—}Õ—ÖùîÙùY1%Qú±ŸÖ±•ëÖ—ïë}Ö–ı9=\†ÿ§±’¡ëÖ—ïë}Ö–ı9=\†ÿ§±ŸÖ±•ëÖ—•Ωπ}Õ—Ö—’ÃÙùI)Qú±ŸÖ±•ëÖ—•Ωπ}…ïÖÕΩ∏ÙïÃÅ]!IÅ…’π}•êÙïÃà∞°©ÕΩ∏πë’µ¡Ã°ç°ïç≠Ã±ëïôÖ’±–ıÕ—»§±…’π}•ê§§(ÄÄÄÄÄÄÄÄÄÄÄÅÖ’ë•—}Õ—Öùî°ç’»±…’π}•ê∞âY1%Qà∞âI)Qà∞¿±©ÕΩ∏πë’µ¡Ã°ç°ïç≠Ã±ëïôÖ’±–ıÕ—»§§(ÄÄÄÄÄÄÄÄÄÄÄÅ…Ö•ÕîÅI’π—•µï……Ω»°òâ¡±Öππï»ÅŸÖ±•ëÖ—•Ω∏Å…ï©ïç—ïêËÅÌç°ïç≠ÕÙà§(ÄÄÄÄÄÄÄÅç’»πï·ïç’—î†ààâUAQÅïµÕ}ù¡—}Õ±Ω—ÃÅ¿Å)=%8ÅïµÕ}ù¡—}¡±Öπ}Õ—Öùï}…Ω›ÃÅÃ(ÄÄÄÄÄÄÄÄÄÅ=8ÅÃπÕ±Ω—}Õ—Ö…–ı¿πÕ±Ω—}Õ—Ö…–Å9ÅÃπ…’π}•êÙïÃÅMP(ÄÄÄÄÄÄÄÄÄÅ¿πôΩ…ïçÖÕ—}¡ÿ≈}≠›†ıÃπôΩ…ïçÖÕ—}¡ÿ≈}≠›†±¿πôΩ…ïçÖÕ—}¡ÿ…}≠›†ıÃπôΩ…ïçÖÕ—}¡ÿ…}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿πôΩ…ïçÖÕ—}¡Ÿ}—Ω—Ö±}≠›†ıÃπôΩ…ïçÖÕ—}¡Ÿ}—Ω—Ö±}≠›†±¿πôΩ…ïçÖÕ—}±ΩÖë}≠›†ıÃπôΩ…ïçÖÕ—}±ΩÖë}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿πÕΩç}Õ—Ö…—}¡±Öπ}¡ç–ıÃπÕΩç}Õ—Ö…—}¡±Öπ}¡ç–±¿πÕΩç}ïπë}¡±Öπ}¡ç–ıÃπÕΩç}ïπë}¡±Öπ}¡ç–∞(ÄÄÄÄÄÄÄÄÄÅ¿πÕΩç}ô±ΩΩ…}¡ç–ıÃπÕΩç}ô±ΩΩ…}¡ç–±¿πÕΩç}—Ö…ùï—}¡ç–ıÃπÕΩç}—Ö…ùï—}¡ç–±¿π¡±Öππïë}â’Â}≠›†ıÃπ¡±Öππïë}â’Â}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öππïë}Õï±±}≠›†ıÃπ¡±Öππïë}Õï±±}≠›†±¿π¡±Öππïë}¡Ÿ}ï·¡Ω…—}≠›†ıÃπ¡±Öππïë}¡Ÿ}ï·¡Ω…—}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öππïë}âÖ——ï…Â}ç°Ö…ùï}≠›†ıÃπ¡±Öππïë}âÖ——ï…Â}ç°Ö…ùï}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öππïë}âÖ——ï…Â}ë•Õç°Ö…ùï}≠›†ıÃπ¡±Öππïë}âÖ——ï…Â}ë•Õç°Ö…ùï}≠›†±¿π…ïçΩµµïπëÖ—•Ω∏ıÃπ…ïçΩµµïπëÖ—•Ω∏∞(ÄÄÄÄÄÄÄÄÄÅ¿πù…•ë}¡Ω±•çÂ}¡±ÖππïêıÃπù…•ë}¡Ω±•çÂ}¡±Öππïê±¿πï·¡Ω…—}¡Ω±•çÂ}¡±ÖππïêıÃπï·¡Ω…—}¡Ω±•çÂ}¡±Öππïê∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡Ÿ}—Ω}âÖ—}¡±ÖππïêıÃπ¡Ÿ}—Ω}âÖ—}¡±Öππïê±¿π¡Ÿ}—Ω}ç›’}¡±ÖππïêıÃπ¡Ÿ}—Ω}ç›’}¡±Öππïê∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡Ÿ}—Ω}ïŸ}¡±ÖππïêıÃπ¡Ÿ}—Ω}ïŸ}¡±Öππïê±¿π¡Ÿ}ï·¡Ω…—}¡±ÖππïêıÃπ¡Ÿ}ï·¡Ω…—}¡±Öππïê∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡Ÿ}ç’…—Ö•±}¡±ÖππïêıÃπ¡Ÿ}ç’…—Ö•±}¡±Öππïê∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öππïë}¡Ÿ}—Ω}âÖ—}≠›†ıÃπ¡±Öππïë}¡Ÿ}—Ω}âÖ—}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öππïë}¡Ÿ}—Ω}ç›’}≠›†ıÃπ¡±Öππïë}¡Ÿ}—Ω}ç›’}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öππïë}¡Ÿ}—Ω}ïŸ}≠›†ıÃπ¡±Öππïë}¡Ÿ}—Ω}ïŸ}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öππïë}¡Ÿ}ç’…—Ö•±}≠›†ıÃπ¡±Öππïë}¡Ÿ}ç’…—Ö•±}≠›†∞(ÄÄÄÄÄÄÄÄÄÅ¿πù…•ë}â’Â}Ö±±Ω›ïêıÃπù…•ë}â’Â}Ö±±Ω›ïê±¿πù…•ë}πΩ}â’‰ıÃπù…•ë}πΩ}â’‰±¿πù…•ë}πï’—…Ö∞ıÃπù…•ë}πï’—…Ö∞∞(ÄÄÄÄÄÄÄÄÄÅ¿πÕï±±}âÖ—}Ö±±Ω›ïêıÃπÕï±±}âÖ—}Ö±±Ω›ïê±¿ππΩ}Õï±±}âÖ–ıÃππΩ}Õï±±}âÖ–∞(ÄÄÄÄÄÄÄÄÄÅ¿πÕï±±}¡Ÿ}Ö±±Ω›ïêıÃπÕï±±}¡Ÿ}Ö±±Ω›ïê±¿ππΩ}Õï±±}¡ÿıÃππΩ}Õï±±}¡ÿ∞(ÄÄÄÄÄÄÄÄÄÅ¿π°ïÖ—}¡’µ¡}›•πëΩ‹ıÃπ°ïÖ—}¡’µ¡}›•πëΩ‹∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡¡ë}…ïÖÕΩ∏ıÃπ¡¡ë}…ïÖÕΩ∏±¿π¡¡ë}…’π}—Â¡îÙïÃ∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡¡ë}Ÿï…Õ•Ω∏Ùù=I|¡|—|ƒú±¿π¡¡ë}±Ωç≠ïë}Ö–ı9=\†ÿ§±¿π¡±Öπ}…’π}•êÙïÃ±¿π¡±Öπ}Õ—ÖùîÙùAU	1%M!ú∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öπ}Õ—Öùï}Ÿï…Õ•Ω∏Ùù=I|¡|—|ƒú±¿π¡±Öπ}Õ—Öùï}’¡ëÖ—ïë}Ö–ı9=\†ÿ§∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öπ}ŸÖ±•ëÖ—•Ωπ}Õ—Ö—’ÃÙùAQú±¿π¡±Öπ}ŸÖ±•ëÖ—•Ωπ}…ïÖÕΩ∏Ùù=,ú∞(ÄÄÄÄÄÄÄÄÄÅ¿π¡±Öπ}¡’â±•Õ°ïë}Ö–ı9=\†ÿ§±¿π¡±Öπ}¡’â±•Õ°ïêÙƒÅ]!IÅ¿πÖç—’Ö±}…ïçΩ…ëïë}Ö–Å%LÅ9U10Å9Å¿πÕ±Ω—}Õ—Ö…–¯ÙïÃààà∞(ÄÄÄÄÄÄÄÄÄÄ°…’π}•ê±…’π}—Â¡î±…’π}•ê±ç’—Ωôò§§(ÄÄÄÄÄÄÄÅ¡’â±•Õ°ïêıç’»π…Ω›çΩ’π–(ÄÄÄÄÄÄÄÅç’»πï·ïç’—î†ààâUAQÅïµÕ}ù¡—}¡±Öπ}…’πÃÅMPÅÕ—Ö—’ÃÙùAU	1%M!ú±ç’……ïπ—}Õ—ÖùîÙùY1%Qú∞(ÄÄÄÄÄÄÄÄÄÅŸÖ±•ëÖ—ïë}Ö–ı9=\†ÿ§±¡’â±•Õ°ïë}Ö–ı9=\†ÿ§±ŸÖ±•ëÖ—•Ωπ}Õ—Ö—’ÃÙùAQú∞(ÄÄÄÄÄÄÄÄÄÅŸÖ±•ëÖ—•Ωπ}…ïÖÕΩ∏Ùù=,ú±’¡ëÖ—ïë}Ö–ı9=\†ÿ§Å]!IÅ…’π}•êÙïÃààà∞°…’π}•ê∞§§(ÄÄÄÄÄÄÄÅÖ’ë•—}Õ—Öùî°ç’»±…’π}•ê∞âY1%Qà∞âAQà±¡’â±•Õ°ïê∞âÖ—Ωµ•åÅ¡’â±•Õ†à§(ÄÄÄÅ…ïçΩ…ë}ïŸïπ–†â¡±Öπ}¡’â±•Õ°ïêà∞â¡±Öππï»à±Ïâ…’π}•êàÈ…’π}•ê∞â…’π}—Â¡îàÈ…’π}—Â¡î∞â…Ω›ÃàÈ¡’â±•Õ°ïëÙ§(ÄÄÄÅôΩ»ÅÕ°Ω…—ôÖ±∞Å•∏Å°¡}Õ°Ω…—ôÖ±±ÃË(ÄÄÄÄÄÄÄÅ…ïçΩ…ë}ïŸïπ–†â°¡}µ•π•µ’µ}°ïÖ—•πù}Õ°Ω…—ôÖ±∞à∞Äâ¡±Öππï»à∞ÅÕ°Ω…—ôÖ±∞∞Äâ]I9%9à§(ÄÄÄÅ…ï—’…∏ÅÏâ…’π}•êàÈ…’π}•ê∞â…Ω›ÃàÈ¡’â±•Õ°ïëÙ(()ëïòÅ…’π}ÖπÖ±Â—•çÃ†§Ä¥¯Åë•ç–Ë(ÄÄÄÅ…ï—’…∏Å…’π}ÖπÖ±Â—•çÕ}Õï…Ÿ•çî°Ω¡—•ΩπÃı=AQ%=9L∞Åëàıëà∞Å±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞Å…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–§(()ëïòÅ…’π}Ö•}ΩâÕï…Ÿï»°ÕΩ’…çï}…ïòËÅÕ—»ÅÅ9ΩπîÄÙÅ9Ωπî§Ä¥¯Åë•ç–Ë(ÄÄÄÅ…ï—’…∏Å…’π}ΩâÕï…Ÿï…}Õï…Ÿ•çî†(ÄÄÄÄÄÄÄÅÕΩ’…çï}…ïò∞ÅΩ¡—•ΩπÃı=AQ%=9L∞Åëàıëà∞Åç…ïÖ—ï}—Ωëºıç…ïÖ—ï}—Ωëº∞(ÄÄÄÄÄÄÄÅ…ïçΩπç•±ï}ΩâÕï…Ÿï…}—ΩëΩÃı…ïçΩπç•±ï}ΩâÕï…Ÿï…}—ΩëΩÃ∞Å…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–∞(ÄÄÄÄ§(()ëïòÅùïπï…Ö—ï}ë•ÖùπΩÕ—•ç}…ï¡Ω…–°—…•ùùï…}πÖµîËÅÕ—»ÄÙÄâÕç°ïë’±ïêà§Ä¥¯Åë•ç–Ë(ÄÄÄÅ…ï—’…∏Å…’π}ë•ÖùπΩÕ—•çÕ}Õï…Ÿ•çî†(ÄÄÄÄÄÄÄÅ—…•ùùï…}πÖµî∞ÅΩ¡—•ΩπÃı=AQ%=9L∞Åëàıëà∞Å±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞ÅÕ±Ω—}Õ—Ö…–ıÕ±Ω—}Õ—Ö…–∞(ÄÄÄÄÄÄÄÅçÖπΩπ•çÖ±}Õ±Ω—Õ}ôΩ…}ëÖ‰ıçÖπΩπ•çÖ±}Õ±Ω—Õ}ôΩ…}ëÖ‰∞Åç…ïÖ—ï}—Ωëºıç…ïÖ—ï}—Ωëº∞(ÄÄÄÄÄÄÄÅ…ïçΩπç•±ï}ë•ÖùπΩÕ—•ç}—ΩëΩÃı…ïçΩπç•±ï}ë•ÖùπΩÕ—•ç}—ΩëΩÃ∞Å…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–∞(ÄÄÄÄ§(()ëïòÅ}—ΩëΩ}Õï…Ÿ•çî†§Ä¥¯ÅQΩëΩMï…Ÿ•çîË(ÄÄÄÅ…ï—’…∏ÅQΩëΩMï…Ÿ•çî°ëàıëà∞Å±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞Å…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–§(()ëïòÅç…ïÖ—ï}—Ωëº°µΩë’±îËÅÕ—»∞Å—•—±îËÅÕ—»∞Åëï—Ö•±ÃËÅÕ—»∞ÅÕïŸï…•—‰ËÅÕ—»ÄÙÄâ%9<à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅÕΩ’…çï}…ïòËÅÕ—»ÅÅ9ΩπîÄÙÅ9Ωπî∞Å…ï≈’•…ï}çΩπÕïç’—•Ÿï}ëÖÂÃËÅâΩΩ∞ÄÙÅÖ±Õî§Ä¥¯ÅÕ—»Ë(ÄÄÄÅ…ï—’…∏Å}—ΩëΩ}Õï…Ÿ•çî†§πç…ïÖ—î°µΩë’±î∞Å—•—±î∞Åëï—Ö•±Ã∞ÅÕïŸï…•—‰∞ÅÕΩ’…çï}…ïò∞Å…ï≈’•…ï}çΩπÕïç’—•Ÿï}ëÖÂÃ§(()ëïòÅ…ïçΩπç•±ï}ΩâÕï…Ÿï…}—ΩëΩÃ°Öç—•Ÿï}—•—±ïÃËÅ±•Õ—mÕ—…t§Ä¥¯Å•π–Ë(ÄÄÄÅ…ï—’…∏Å}—ΩëΩ}Õï…Ÿ•çî†§π…ïçΩπç•±ï}ΩâÕï…Ÿï»°Öç—•Ÿï}—•—±ïÃ§(()ëïòÅ…ïçΩπç•±ï}ë•ÖùπΩÕ—•ç}—ΩëΩÃ°Öç—•Ÿï}—•—±ïÃËÅ±•Õ—mÕ—…t§Ä¥¯Å•π–Ë(ÄÄÄÅ…ï—’…∏Å}—ΩëΩ}Õï…Ÿ•çî†§π…ïçΩπç•±ï}ë•ÖùπΩÕ—•çÃ°Öç—•Ÿï}—•—±ïÃ§(()ëïòÅµÖ•π—Ö•π}—ΩëΩ}Ö…ç°•Ÿî†§Ä¥¯Åë•ç–Ë(ÄÄÄÅ…ï—’…∏Å}—ΩëΩ}Õï…Ÿ•çî†§πµÖ•π—Ö•π}Ö…ç°•Ÿî†§(()ëïòÅ…ïŸ•ï›}—Ωëº°¡ÖÂ±ΩÖêËÅë•ç–∞ÅÖç—Ω»ËÅÕ—»§Ä¥¯Åë•ç–Ë(ÄÄÄÅ…ï—’…∏Å}—ΩëΩ}Õï…Ÿ•çî†§π…ïŸ•ï‹°¡ÖÂ±ΩÖê∞ÅÖç—Ω»§(((åÅM±Ω–ÅµÖ—ï…•Ö±•ÈÖ—•ΩπÃÅÖ…îÅ›•…ïêÅÖô—ï»Å…ïçΩ…ë}ïŸïπ–Å•ÃÅëïô•πïê∏(()ëïòÅ…ïçΩ…ë}ïŸïπ–°ïŸïπ—}—Â¡îËÅÕ—»∞ÅµΩë’±îËÅÕ—»∞Å¡ÖÂ±ΩÖêËÅë•ç–∞ÅÕïŸï…•—‰ËÅÕ—»ÄÙÄâ%9<à§Ä¥¯Å9ΩπîË(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ›•—†Åëà†§ÅÖÃÅçΩπ∏∞ÅçΩπ∏πç’…ÕΩ»†§ÅÖÃÅç’»Ë(ÄÄÄÄÄÄÄÄÄÄÄÅç’»πï·ïç’—î†â%9MIPÅ%9Q<ÅïµÕ}ù¡—}çΩ…ï}ïŸïπ—Ã°ç…ïÖ—ïë}Ö–±ÕïŸï…•—‰±ïŸïπ—}—Â¡î±µΩë’±ï}πÖµî±Õ±Ω—}Õ—Ö…–±¡ÖÂ±ΩÖë}©ÕΩ∏§ÅY1UL°9=\†ÿ§∞ïÃ∞ïÃ∞ïÃ∞ïÃ∞ïÃ§à∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ°ÕïŸï…•—‰∞ÅïŸïπ—}—Â¡î∞ÅµΩë’±î∞ÅÕ±Ω—}Õ—Ö…–†§π…ï¡±Öçî°—È•πôºı9Ωπî§∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÅ©ÕΩ∏πë’µ¡Ã°¡ÖÂ±ΩÖê∞ÅïπÕ’…ï}ÖÕç•§ıÖ±Õî∞ÅëïôÖ’±–ıÕ—»§§§(ÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÅ1=πï……Ω»†âïŸïπ–Å›…•—îÅôÖ•±ïêËÄïÃà∞Åï·å§(()}I=YIdÄÙÅâ’•±ë}…ïçΩŸï…‰°IïçΩŸï…ÂëÖ¡—ï…Ã†(ÄÄÄÅΩ¡—•ΩπÃı=AQ%=9L∞Åëàıëà∞Å≈πÖµîı≈πÖµî∞Å…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–∞(§§)âΩΩ—Õ—…Ö¡}±ïùÖçÂ}—Öâ±ïÃÄÙÅ}I=YIdπâΩΩ—Õ—…Ö¡}±ïùÖçÂ}—Öâ±ïÃ)…ïçΩŸï…}•π—ï……’¡—ïë}…’πÃÄÙÅ}I=YIdπ…ïçΩŸï…}•π—ï……’¡—ïë}…’πÃ(()}5QI%1%iQ%=9LÄÙÅâ’•±ë}µÖ—ï…•Ö±•ÈÖ—•ΩπÃ°5Ö—ï…•Ö±•ÈÖ—•ΩπëÖ¡—ï…Ã†(ÄÄÄÅΩ¡—•ΩπÃı=AQ%=9L∞ÅÖ¡¡}Ÿï…Õ•Ω∏ıAA}YIM%=8∞Åëàıëà∞Å±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞(ÄÄÄÅÕ±Ω—}Õ—Ö…–ıÕ±Ω—}Õ—Ö…–∞Å…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–∞(§§)ç±ΩÕï}ô•π•Õ°ïë}Õ±Ω—ÃÄÙÅ}5QI%1%iQ%=9Lπç±ΩÕï}ô•π•Õ°ïë}Õ±Ω—Ã)âÖç≠ô•±±}ï·ïç’—•Ωπ}ëï—Ö•±ÃÄÙÅ}5QI%1%iQ%=9LπâÖç≠ô•±±}ï·ïç’—•Ωπ}ëï—Ö•±Ã)Öùù…ïùÖ—ï}…ïÕ’±—ÃÄÙÅ}5QI%1%iQ%=9LπÖùù…ïùÖ—ï}…ïÕ’±—Ã)…ïâ’•±ë}…ïçΩŸï…Â}µÖ—ï…•Ö±•ÈÖ—•ΩπÃÄÙÅ}5QI%1%iQ%=9Lπ…ïâ’•±ë}…ïçΩŸï…Â}µÖ—ï…•Ö±•ÈÖ—•ΩπÃ)±ïÖ…π}µ•ÕÕ•πù}±ΩÖêÄÙÅ}5QI%1%iQ%=9Lπ±ïÖ…π}µ•ÕÕ•πù}±ΩÖê(()}%9MQ%=8ÄÙÅâ’•±ë}•πùïÕ—•Ω∏°%πùïÕ—•ΩπëÖ¡—ï…Ã†(ÄÄÄÅΩ¡—•ΩπÃı=AQ%=9L∞(ÄÄÄÅ—•µïÈΩπîıQh∞(ÄÄÄÅ¡Ÿ}ôΩ…ïçÖÕ—}ïπ—•—•ïÃıAY}=IMQ}9Q%Q%L∞(ÄÄÄÅëàıëà∞(ÄÄÄÅ±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞(ÄÄÄÅÕ±Ω—}Õ—Ö…–ıÕ±Ω—}Õ—Ö…–∞(ÄÄÄÅçÖπΩπ•çÖ±}Õ±Ω—Õ}ôΩ…}ëÖ‰ıçÖπΩπ•çÖ±}Õ±Ω—Õ}ôΩ…}ëÖ‰∞(ÄÄÄÅπ’µâï»ıπ’µâï»∞(ÄÄÄÅ°Ö}Õ—Ö—îı°Ö}Õ—Ö—î∞(ÄÄÄÅ°Ö}Õï…Ÿ•çï}…ïÕ¡ΩπÕîı°Ö}Õï…Ÿ•çï}…ïÕ¡ΩπÕî∞(ÄÄÄÅ…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–∞(§§)…ïô…ïÕ°}¡Ÿ}ôΩ…ïçÖÕ–ÄÙÅ}%9MQ%=8π…ïô…ïÕ°}¡Ÿ}ôΩ…ïçÖÕ–)…ïô…ïÕ°}›ïÖ—°ï…}ôΩ…ïçÖÕ–ÄÙÅ}%9MQ%=8π…ïô…ïÕ°}›ïÖ—°ï…}ôΩ…ïçÖÕ–)…ïô…ïÕ°}…çîÄÙÅ}%9MQ%=8π…ïô…ïÕ°}…çî(()}aUQ=HÄÙÅâ’•±ë}ï·ïç’—Ω»°·ïç’—Ω…ëÖ¡—ï…Ã†(ÄÄÄÅΩ¡—•ΩπÃı=AQ%=9L∞(ÄÄÄÅΩ¡ï…Ö—•ΩπÖ±}Õï——•πùÃı=AIQ%=91}MQQ%9L∞(ÄÄÄÅ…’π—•µï}Õï——•πùÕ}¡Ö—†ıIU9Q%5}MQQ%9M}AQ ∞(ÄÄÄÅ±Ωç¨ı1=,∞(ÄÄÄÅÕ—Ö—îıMQQ∞(ÄÄÄÅ…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–∞(ÄÄÄÅ±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞(ÄÄÄÅëàıëà∞(ÄÄÄÅÕ±Ω—}Õ—Ö…–ıÕ±Ω—}Õ—Ö…–∞(ÄÄÄÅ—Ω’}¡…Ωù…Öµ}ÕπÖ¡Õ°Ω–ı—Ω’}¡…Ωù…Öµ}ÕπÖ¡Õ°Ω–∞(ÄÄÄÅÖç—•Ÿï}—Ω’}¡…Ωù…Ö¥ıÖç—•Ÿï}—Ω’}¡…Ωù…Ö¥∞(ÄÄÄÅπ’µâï»ıπ’µâï»∞(ÄÄÄÅ°Ö}Õ—Ö—îı°Ö}Õ—Ö—î∞(ÄÄÄÅ°Ö}Õï…Ÿ•çï}…ïÕ¡ΩπÕîı°Ö}Õï…Ÿ•çï}…ïÕ¡ΩπÕî∞(§§)Õï——•πùÕ}¡ÖÂ±ΩÖêÄÙÅ}aUQ=HπÕï——•πùÕ}¡ÖÂ±ΩÖê)’¡ëÖ—ï}Ω¡ï…Ö—•ΩπÖ±}Õï——•πùÃÄÙÅ}aUQ=Hπ’¡ëÖ—ï}Ω¡ï…Ö—•ΩπÖ±}Õï——•πùÃ)’¡ëÖ—ï}ï·ïç’—Ω…}µΩëîÄÙÅ}aUQ=Hπ’¡ëÖ—ï}ï·ïç’—Ω…}µΩëî)ïπÖâ±ï}¡…Ωë’ç—•Ωπ}Ωπ}Õ—Ö…—’¿ÄÙÅ}aUQ=HπïπÖâ±ï}¡…Ωë’ç—•Ωπ}Ωπ}Õ—Ö…—’¿)’¡ëÖ—ï}¡…ΩçïÕÕ}ΩŸï……•ëîÄÙÅ}aUQ=Hπ’¡ëÖ—ï}¡…ΩçïÕÕ}ΩŸï……•ëî)ï·¡•…ï}¡…ΩçïÕÕ}ΩŸï……•ëïÃÄÙÅ}aUQ=Hπï·¡•…ï}¡…ΩçïÕÕ}ΩŸï……•ëïÃ)ï·¡•…ï}Õ—Ö±ï}çΩµµÖπëÃÄÙÅ}aUQ=Hπï·¡•…ï}Õ—Ö±ï}çΩµµÖπëÃ)ï·—ï…πÖ±±Â}Õ—Ö…—ïë}°¡}•Õ}…’ππ•πúÄÙÅ}aUQ=Hπï·—ï…πÖ±±Â}Õ—Ö…—ïë}°¡}•Õ}…’ππ•πú)Õ—Öùï}ï·ïç’—Ω…}çΩµµÖπëÃÄÙÅ}aUQ=HπÕ—Öùï}ï·ïç’—Ω…}çΩµµÖπëÃ)ë•Õ¡Ö—ç°}…ïÖëÂ}çΩµµÖπëÃÄÙÅ}aUQ=Hπë•Õ¡Ö—ç°}…ïÖëÂ}çΩµµÖπëÃ)Öç≠πΩ›±ïëùï}çΩµµÖπêÄÙÅ}aUQ=HπÖç≠πΩ›±ïëùï}çΩµµÖπê(()ëïòÅçΩµ¡±ï—ï}…çï}çÂç±î°…ïÕ’±–ËÅë•ç–∞Å…’π}—Â¡îËÅÕ—»§Ä¥¯Åë•ç–Ë(ÄÄÄÄààâIïô…ïÕ†Åëï¡ïπëïπ–Å•π¡’—ÃÅÖπêÅÖ—Ωµ•çÖ±±‰Å¡’â±•Õ†ÅAAÅÖô—ï»ÅÑÅçΩµ¡±ï—îÅIÅëÖ‰∏ààà(ÄÄÄÅ•òÅ…ïÕ’±–πùï–†âÕ—Ö—’Ãà§ÄÑÙÄâ=,àÅΩ»Å•π–°…ïÕ’±–πùï–†â…Ω›Ãà§ÅΩ»Ä¿§ÄÑÙÅ•π–°…ïÕ’±–πùï–†âï·¡ïç—ïêà§ÅΩ»Ä¿§Ë(ÄÄÄÄÄÄÄÅ…ï—’…∏ÅÏ®©…ïÕ’±–∞Äâ¡±Öππï»àËÅÏâÕ—Ö—’ÃàËÄâ]%Q%9}=I}=5A1Q}I}dâıÙ(ÄÄÄÅ¡ÿÄÙÅ…ïô…ïÕ°}¡Ÿ}ôΩ…ïçÖÕ–†§(ÄÄÄÅ›ïÖ—°ï»ÄÙÅ…ïô…ïÕ°}›ïÖ—°ï…}ôΩ…ïçÖÕ–†§(ÄÄÄÅ±ïÖ…πïêÄÙÅ±ïÖ…π}µ•ÕÕ•πù}±ΩÖê†§(ÄÄÄÅ¡±Ö∏ÄÙÅ…’π}¡±Öππï»°…’π}—Â¡î§(ÄÄÄÅçΩµ¡±ï—ïêÄÙÅÏ®©…ïÕ’±–∞Äâ¡ÿàËÅ¡ÿ∞Äâ›ïÖ—°ï»àËÅ›ïÖ—°ï»∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ±ΩÖë}Õ±Ω—Õ}ô•±±ïêàËÅ±ïÖ…πïê∞Äâ¡±Öππï»àËÅÏâÕ—Ö—’ÃàËÄâAQà∞Ä®©¡±ÖπıÙ(ÄÄÄÅ…ïçΩ…ë}ïŸïπ–†â…çï}ëï¡ïπëïπ—}çÂç±ï}çΩµ¡±ï—ïêà∞ÄâçΩ…îà∞ÅçΩµ¡±ï—ïê§(ÄÄÄÅ…ï—’…∏ÅçΩµ¡±ï—ïê(()ëïòÅïπù•πï}±ΩΩ¿†§Ä¥¯Å9ΩπîË(ÄÄÄÅ…’π}Õç°ïë’±ï»°Mç°ïë’±ï…ëÖ¡—ï…Ã†(ÄÄÄÄÄÄÄÅΩ¡—•ΩπÃı=AQ%=9L∞ÅÕ—Ö—îıMQQ∞Å±Ωç¨ı1=,∞Å±Ωúı1=∞Åëàıëà∞Å±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞(ÄÄÄÄÄÄÄÅÕ±Ω—}Õ—Ö…–ıÕ±Ω—}Õ—Ö…–∞ÅçÖ¡—’…ï}—ï±ïµï—…‰ıçÖ¡—’…ï}—ï±ïµï—…‰∞(ÄÄÄÄÄÄÄÅç±ΩÕï}ô•π•Õ°ïë}Õ±Ω—Ãıç±ΩÕï}ô•π•Õ°ïë}Õ±Ω—Ã∞ÅâÖç≠ô•±±}ï·ïç’—•Ωπ}ëï—Ö•±ÃıâÖç≠ô•±±}ï·ïç’—•Ωπ}ëï—Ö•±Ã∞(ÄÄÄÄÄÄÄÅ…’π}Õï…•Ö±•Èïêı…’π}Õï…•Ö±•Èïê∞Å…ïâ’•±ë}…ïçΩŸï…Â}µÖ—ï…•Ö±•ÈÖ—•ΩπÃı…ïâ’•±ë}…ïçΩŸï…Â}µÖ—ï…•Ö±•ÈÖ—•ΩπÃ∞(ÄÄÄÄÄÄÄÅ±ïÖ…π}µ•ÕÕ•πù}±ΩÖêı±ïÖ…π}µ•ÕÕ•πù}±ΩÖê∞Åï·¡•…ï}¡…ΩçïÕÕ}ΩŸï……•ëïÃıï·¡•…ï}¡…ΩçïÕÕ}ΩŸï……•ëïÃ∞(ÄÄÄÄÄÄÄÅï·¡•…ï}Õ—Ö±ï}çΩµµÖπëÃıï·¡•…ï}Õ—Ö±ï}çΩµµÖπëÃ∞ÅµÖ•π—Ö•π}—ΩëΩ}Ö…ç°•ŸîıµÖ•π—Ö•π}—ΩëΩ}Ö…ç°•Ÿî∞(ÄÄÄÄÄÄÄÅïπÕ’…ï}Õ±Ω—}çÖ±ïπëÖ»ıïπÕ’…ï}Õ±Ω—}çÖ±ïπëÖ»∞Å…ïô…ïÕ°}¡Ÿ}ôΩ…ïçÖÕ–ı…ïô…ïÕ°}¡Ÿ}ôΩ…ïçÖÕ–∞(ÄÄÄÄÄÄÄÅ…ïô…ïÕ°}›ïÖ—°ï…}ôΩ…ïçÖÕ–ı…ïô…ïÕ°}›ïÖ—°ï…}ôΩ…ïçÖÕ–∞Å…ïçΩ…ë}ïŸïπ–ı…ïçΩ…ë}ïŸïπ–∞(ÄÄÄÄÄÄÄÅ…ïô…ïÕ°}…çîı…ïô…ïÕ°}…çî∞ÅçΩµ¡±ï—ï}…çï}çÂç±îıçΩµ¡±ï—ï}…çï}çÂç±î∞Å…’π}¡±Öππï»ı…’π}¡±Öππï»∞(ÄÄÄÄÄÄÄÅÕ—Öùï}ï·ïç’—Ω…}çΩµµÖπëÃıÕ—Öùï}ï·ïç’—Ω…}çΩµµÖπëÃ∞Åë•Õ¡Ö—ç°}…ïÖëÂ}çΩµµÖπëÃıë•Õ¡Ö—ç°}…ïÖëÂ}çΩµµÖπëÃ∞(ÄÄÄÄÄÄÄÅ…’π}ÖπÖ±Â—•çÃı…’π}ÖπÖ±Â—•çÃ∞Å…’π}Ö•}ΩâÕï…Ÿï»ı…’π}Ö•}ΩâÕï…Ÿï»∞(ÄÄÄÄÄÄÄÅùïπï…Ö—ï}ë•ÖùπΩÕ—•ç}…ï¡Ω…–ıùïπï…Ö—ï}ë•ÖùπΩÕ—•ç}…ï¡Ω…–∞(ÄÄÄÄ§§()!Q50ÄÙÅAÖ—†°}}ô•±ï}|§π›•—°}πÖµî†â›ïâ’§π°—µ∞à§π…ïÖë}—ï·–°ïπçΩë•πúÙâ’—ò¥‡à§(()!Öπë±ï»ÄÙÅâ’•±ë}°Öπë±ï»°¡•ëÖ¡—ï…Ã†(ÄÄÄÅÖ¡¡}πÖµîıAA}95∞ÅÖ¡¡}Ÿï…Õ•Ω∏ıAA}YIM%=8∞ÅÕ—Ö—îıMQQ∞Å±Ωç¨ı1=,∞Å±Ωúı1=∞(ÄÄÄÅëàıëà∞Å±ΩçÖ±}πΩ‹ı±ΩçÖ±}πΩ‹∞ÅÕ±Ω—}Õ—Ö…–ıÕ±Ω—}Õ—Ö…–∞ÅÕï——•πùÕ}¡ÖÂ±ΩÖêıÕï——•πùÕ}¡ÖÂ±ΩÖê∞(ÄÄÄÅ’¡ëÖ—ï}Ω¡ï…Ö—•ΩπÖ±}Õï——•πùÃı’¡ëÖ—ï}Ω¡ï…Ö—•ΩπÖ±}Õï——•πùÃ∞Å’¡ëÖ—ï}ï·ïç’—Ω…}µΩëîı’¡ëÖ—ï}ï·ïç’—Ω…}µΩëî∞(ÄÄÄÅ’¡ëÖ—ï}¡…ΩçïÕÕ}ΩŸï……•ëîı’¡ëÖ—ï}¡…ΩçïÕÕ}ΩŸï……•ëî∞ÅÕ—Öùï}ï·ïç’—Ω…}çΩµµÖπëÃıÕ—Öùï}ï·ïç’—Ω…}çΩµµÖπëÃ∞(ÄÄÄÅÖç≠πΩ›±ïëùï}çΩµµÖπêıÖç≠πΩ›±ïëùï}çΩµµÖπê∞Å…’π}Õï…•Ö±•Èïêı…’π}Õï…•Ö±•Èïê∞Å…’π}¡±Öππï»ı…’π}¡±Öππï»∞(ÄÄÄÅ…ïô…ïÕ°}…çîı…ïô…ïÕ°}…çî∞ÅçΩµ¡±ï—ï}…çï}çÂç±îıçΩµ¡±ï—ï}…çï}çÂç±î∞(ÄÄÄÅ…ïô…ïÕ°}¡Ÿ}ôΩ…ïçÖÕ–ı…ïô…ïÕ°}¡Ÿ}ôΩ…ïçÖÕ–∞Å…ïô…ïÕ°}›ïÖ—°ï…}ôΩ…ïçÖÕ–ı…ïô…ïÕ°}›ïÖ—°ï…}ôΩ…ïçÖÕ–∞(ÄÄÄÅ…’π}ÖπÖ±Â—•çÃı…’π}ÖπÖ±Â—•çÃ∞Å…’π}Ö•}ΩâÕï…Ÿï»ı…’π}Ö•}ΩâÕï…Ÿï»∞(ÄÄÄÅùïπï…Ö—ï}ë•ÖùπΩÕ—•ç}…ï¡Ω…–ıùïπï…Ö—ï}ë•ÖùπΩÕ—•ç}…ï¡Ω…–∞Å…ïŸ•ï›}—Ωëºı…ïŸ•ï›}—Ωëº∞Å°—µ∞ı!Q50∞(§§(()ëïòÅ•π•—•Ö±•Èî†§Ä¥¯Å9ΩπîË(ÄÄÄÅQ}%Hπµ≠ë•»°¡Ö…ïπ—ÃıQ…’î∞Åï·•Õ—}Ω¨ıQ…’î§(ÄÄÄÅëïÖë±•πîÄÙÅ—•µîπ—•µî†§Ä¨Äƒ»¿(ÄÄÄÅ›°•±îÅQ…’îË(ÄÄÄÄÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÄÄÄÄÅïπÕ’…ï}…’π—•µï}Õç°ïµÑ†§(ÄÄÄÄÄÄÄÄÄÄÄÅµ•ù…Ö—ïêÄÙÅâΩΩ—Õ—…Ö¡}±ïùÖçÂ}—Öâ±ïÃ†§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïçΩŸï…ïêÄÙÅ…ïçΩŸï…}•π—ï……’¡—ïë}…’πÃ†§(ÄÄÄÄÄÄÄÄÄÄÄÅçÖ±ïπëÖ»ÄÙÅïπÕ’…ï}Õ±Ω—}çÖ±ïπëÖ»°±ΩçÖ±}πΩ‹†§πëÖ—î†§µ—•µïëï±—Ñ°ëÖÂÃÙƒ§∞Å±ΩçÖ±}πΩ‹†§πëÖ—î†§∞Å±ΩçÖ±}πΩ‹†§πëÖ—î†§≠—•µïëï±—Ñ°ëÖÂÃÙƒ§§(ÄÄÄÄÄÄÄÄÄÄÄÅÕ±Ω—}…ï±Ö—•ΩπÃÄÙÅâÖç≠ô•±±}Õ±Ω—}…ï±Ö—•ΩπÃ†§(ÄÄÄÄÄÄÄÄÄÄÄÅ›•—†Å1=,ËÅMQQlâµ•ù…Ö—ïë}—Öâ±ïÃâtÄÙÅµ•ù…Ö—ïêÏÅMQQlâëÖ—ÖâÖÕîâtÄÙÄâ=99Qà(ÄÄÄÄÄÄÄÄÄÄÄÅ…ïçΩ…ë}ïŸïπ–†âÖ¡¡±•çÖ—•Ωπ}Õ—Ö…—ïêà∞ÄâçΩ…îà∞ÅÏâŸï…Õ•Ω∏àËÅAA}YIM%=8∞Äâµ•ù…Ö—ïë}—Öâ±ïÃàËÅµ•ù…Ö—ïê∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ…ïçΩŸï…ïêàËÅ…ïçΩŸï…ïê∞ÄâÕ±Ω—}çÖ±ïπëÖ»àËÅçÖ±ïπëÖ»∞ÄâÕ±Ω—}…ï±Ö—•ΩπÃàËÅÕ±Ω—}…ï±Ö—•ΩπÕÙ§(ÄÄÄÄÄÄÄÄÄÄÄÅ…ï—’…∏(ÄÄÄÄÄÄÄÅï·çï¡–Å·çï¡—•Ω∏ÅÖÃÅï·åË(ÄÄÄÄÄÄÄÄÄÄÄÅ•òÅ—•µîπ—•µî†§Ä¯ÙÅëïÖë±•πîËÅ…Ö•Õî(ÄÄÄÄÄÄÄÄÄÄÄÅ1=π›Ö…π•πú†â›Ö•—•πúÅôΩ»Å5Ö…•ÖËÄïÃà∞Åï·å§ÏÅ—•µîπÕ±ïï¿†‘§(()ëïòÅµÖ•∏†§Ä¥¯Å9ΩπîË(ÄÄÄÅÕ—Ö…—’¡}ï·ïç’—Ω»ÄÙÅïπÖâ±ï}¡…Ωë’ç—•Ωπ}Ωπ}Õ—Ö…—’¿†§(ÄÄÄÅÕï…Ÿï»ÄÙÅQ°…ïÖë•πù!QQAMï…Ÿï»††à¿∏¿∏¿∏¿à∞Ä‡¿‰‰§∞Å!Öπë±ï»§(ÄÄÄÅÕï…Ÿï…}—°…ïÖêÄÙÅ—°…ïÖë•πúπQ°…ïÖê°—Ö…ùï–ıÕï…Ÿï»πÕï…Ÿï}ôΩ…ïŸï»∞ÅëÖïµΩ∏ıQ…’î§(ÄÄÄÅÕï…Ÿï…}—°…ïÖêπÕ—Ö…–†§(ÄÄÄÅ—…‰Ë(ÄÄÄÄÄÄÄÅ•π•—•Ö±•Èî†§(ÄÄÄÄÄÄÄÅ…ïçΩ…ë}ïŸïπ–†âï·ïç’—Ω…}Õ—Ö…—’¡}µΩëîà∞Äâï·ïç’—Ω»à∞ÅÕ—Ö…—’¡}ï·ïç’—Ω»∞(ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄâ%9<àÅ•òÅÕ—Ö…—’¡}ï·ïç’—Ω…lâµΩëîâtÄÙÙÄâ1%YàÅï±ÕîÄâ]I9%9à§(ÄÄÄÄÄÄÄÅ—°…ïÖë•πúπQ°…ïÖê°—Ö…ùï–ıïπù•πï}±ΩΩ¿∞ÅëÖïµΩ∏ıQ…’î§πÕ—Ö…–†§(ÄÄÄÄÄÄÄÅ1=π•πôº†àïÃÄïÃÅÕ—Ö…—ïêÏÅï·ïç’—Ω»ÙïÃà∞ÅAA}95∞ÅAA}YIM%=8∞ÅÕ—Ö…—’¡}ï·ïç’—Ω…lâµΩëîât§(ÄÄÄÄÄÄÄÅÕï…Ÿï…}—°…ïÖêπ©Ω•∏†§(ÄÄÄÅô•πÖ±±‰Ë(ÄÄÄÄÄÄÄÅÕï…Ÿï»πÕ°’—ëΩ›∏†§(ÄÄÄÄÄÄÄÅÕï…Ÿï»πÕï…Ÿï…}ç±ΩÕî†§(()•òÅ}}πÖµï}|ÄÙÙÄâ}}µÖ•π}|àË(ÄÄÄÅµÖ•∏†§(