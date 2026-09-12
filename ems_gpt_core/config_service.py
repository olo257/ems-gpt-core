from __future__ import annotations

import json
from pathlib import Path


DEFAULT_OPTIONS = {
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


def load_options(options_path: Path, runtime_settings_path: Path) -> dict:
    result = dict(DEFAULT_OPTIONS)
    for path in (options_path, runtime_settings_path):
        try:
            result.update(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
    return result
