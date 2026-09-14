from __future__ import annotations

import json
from pathlib import Path


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

BACKUP_SETTINGS = {
    "backup_enabled": {"type": "boolean", "label": "Włącz codzienny backup", "group": "Backup bazy ems_gpt"},
    "backup_time": {"type": "time", "label": "Godzina backupu", "group": "Backup bazy ems_gpt"},
    "backup_local_directory": {"type": "text", "label": "Katalog lokalny", "group": "Backup bazy ems_gpt"},
    "backup_omv_directory": {"type": "text", "label": "Katalog OMV", "group": "Backup bazy ems_gpt"},
    "backup_daily_retention": {"type": "number", "min": 1, "max": 90, "step": 1, "label": "Kopie dzienne", "group": "Backup bazy ems_gpt"},
    "backup_weekly_retention": {"type": "number", "min": 0, "max": 52, "step": 1, "label": "Kopie tygodniowe", "group": "Backup bazy ems_gpt"},
    "backup_monthly_retention": {"type": "number", "min": 0, "max": 36, "step": 1, "label": "Kopie miesięczne", "group": "Backup bazy ems_gpt"},
    "backup_sha256_enabled": {"type": "boolean", "label": "Weryfikuj SHA-256", "group": "Backup bazy ems_gpt"},
    "backup_min_size_bytes": {"type": "number", "min": 1024, "max": 10737418240, "step": 1024, "label": "Minimalny rozmiar [B]", "group": "Backup bazy ems_gpt"},
}

APPLIANCE_DEFAULTS = {
    "dishwasher": ("Zmywarka", "sensor.zmywarka_energy", "sensor.zmywarka_power", "", "switch.zmywarka", "total", 5.0),
    "large_fridge": ("Duża lodówka", "", "", "", "", "total", 5.0),
    "small_fridge": ("Mała lodówka", "", "", "", "", "total", 5.0),
    "freezer": ("Zamrażarka", "", "", "", "", "total", 5.0),
    "washer": ("Pralka", "sensor.pralka_daily_energy_consumption", "", "sensor.pralnia_pralka_daily_water_consumption", "", "daily", 5.0),
    "dryer": ("Suszarka", "sensor.suszarka_do_ubran_daily_energy_consumption", "", "", "", "daily", 5.0),
}


def appliance_settings() -> dict:
    result = {}
    for key, (label, *_values) in APPLIANCE_DEFAULTS.items():
        group = f"Urządzenia — {label}"
        result.update({
            f"appliance_{key}_enabled": {"type": "boolean", "label": "Uwzględniaj w analityce", "group": group},
            f"appliance_{key}_energy_entity": {"type": "entity", "label": "Encja energii", "group": group},
            f"appliance_{key}_power_entity": {"type": "entity", "label": "Encja mocy", "group": group},
            f"appliance_{key}_water_entity": {"type": "entity", "label": "Encja wody", "group": group},
            f"appliance_{key}_state_entity": {"type": "entity", "label": "Encja stanu", "group": group},
            f"appliance_{key}_counter_type": {"type": "select", "options": ["daily", "total"], "label": "Rodzaj licznika", "group": group},
            f"appliance_{key}_active_power_threshold_w": {"type": "number", "min": 0, "max": 5000, "step": 1, "label": "Próg pracy [W]", "group": group},
        })
    return result


CONFIG_SETTINGS = {**BACKUP_SETTINGS, **appliance_settings()}


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
    "analytics_pv_daylight_threshold_kwh": 0.02,
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
    "backup_enabled": False,
    "backup_time": "02:20",
    "backup_local_directory": "/backup/ems-gpt",
    "backup_omv_directory": "/media/ems-gpt-backup",
    "backup_daily_retention": 14,
    "backup_weekly_retention": 8,
    "backup_monthly_retention": 12,
    "backup_sha256_enabled": True,
    "backup_min_size_bytes": 1024 * 1024,
}

for _key, (_label, _energy, _power, _water, _state, _counter, _threshold) in APPLIANCE_DEFAULTS.items():
    DEFAULT_OPTIONS.update({
        f"appliance_{_key}_enabled": bool(_energy),
        f"appliance_{_key}_energy_entity": _energy,
        f"appliance_{_key}_power_entity": _power,
        f"appliance_{_key}_water_entity": _water,
        f"appliance_{_key}_state_entity": _state,
        f"appliance_{_key}_counter_type": _counter,
        f"appliance_{_key}_active_power_threshold_w": _threshold,
    })


def load_options(options_path: Path, runtime_settings_path: Path) -> dict:
    result = dict(DEFAULT_OPTIONS)
    for path in (options_path, runtime_settings_path):
        try:
            result.update(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
    return result
