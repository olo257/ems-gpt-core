from __future__ import annotations

import json
from pathlib import Path


DEYE_PROGRAM_SOC_DEFAULTS = {
    "deye_program_1_soc_pct": 10,
    "deye_program_2_soc_pct": 10,
    "deye_program_3_soc_pct": 10,
    "deye_program_4_soc_pct": 10,
    "deye_program_5_soc_pct": 10,
    "deye_program_6_soc_pct": 30,
}

# These values belong exclusively to Supervisor add-on options. They must
# never be shadowed by runtime-settings.json written from the application UI.
ADDON_ONLY_SETTINGS = {
    *DEYE_PROGRAM_SOC_DEFAULTS,
    "garden_temperature_entity",
}


def deye_program_soc_baselines(options: dict) -> dict[str, float]:
    """Return the six validated TOU SOC values from add-on configuration."""
    result = {}
    for program in range(1, 7):
        key = f"deye_program_{program}_soc_pct"
        try:
            value = float(options[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"DEYE_PROGRAM_SOC_BASELINE_INVALID:{program}") from exc
        if not 0.0 <= value <= 100.0:
            raise RuntimeError(f"DEYE_PROGRAM_SOC_BASELINE_INVALID:{program}:{value}")
        result[str(program)] = value
    return result


OPERATIONAL_SETTINGS = {
    "purchase_margin_pln_kwh": (0.0, 5.0, "Marża zakupu [PLN/kWh]", "Ceny i ekonomia"),
    "minimum_arbitrage_margin_pln_kwh": (0.0, 5.0, "Minimalna marża arbitrażu [PLN/kWh]", "Ceny i ekonomia"),
    "battery_degradation_cost_pln_kwh": (0.0, 5.0, "Degradacja baterii [PLN/kWh]", "Ceny i ekonomia"),
    "battery_charge_efficiency": (0.01, 1.0, "Sprawność ładowania", "Bateria"),
    "battery_discharge_efficiency": (0.01, 1.0, "Sprawność rozładowania", "Bateria"),
    "battery_capacity_kwh": (1.0, 100.0, "Pojemność baterii [kWh]", "Bateria"),
    "battery_min_soc_pct": (0.0, 90.0, "Minimalny SOC [%]", "Bateria"),
    "battery_max_power_kw": (0.25, 30.0, "Maksymalna moc baterii [kW]", "Bateria"),
    "pv_cwu_min_surplus_kw": (0.0, 20.0, "Próg PV→CWU [kW]", "Prognozy i procesy"),
    "pv_ev_min_surplus_kw": (0.0, 20.0, "Próg PV→EV [kW]", "Prognozy i procesy"),
    "forecast_uncertainty_weight": (0.0, 2.0, "Waga niepewności prognozy", "Prognozy i procesy"),
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
    "target_history_days": (7.0, 90.0, "Historia targetu [dni]", "Analiza SOC target"),
    "target_history_min_samples": (1.0, 100.0, "Minimalna liczba próbek targetu", "Analiza SOC target"),
    "target_history_max_correction_pct": (0.0, 50.0, "Limit sugerowanej korekty [p.p.]", "Analiza SOC target"),
    "target_history_pv_relief_threshold_kwh": (0.0, 2.0, "Próg odciążenia PV [kWh/slot]", "Analiza SOC target"),
    "soc_target_history_weight_7d_pct": (0.0, 100.0, "Waga końcowego SOC — 7 dni [%]", "Analiza SOC target"),
    "soc_target_history_weight_14d_pct": (0.0, 100.0, "Waga końcowego SOC — 14 dni [%]", "Analiza SOC target"),
    "soc_target_history_weight_28d_pct": (0.0, 100.0, "Waga końcowego SOC — 28 dni [%]", "Analiza SOC target"),
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
CONFIG_SETTINGS.update({
    "forecast_fixed_corrections_enabled": {
        "type": "boolean", "label": "Stała korekta prognoz ±10%", "group": "Prognozy i procesy"
    },
    "forecast_history_corrections_enabled": {
        "type": "boolean", "label": "Korekta PV z historii", "group": "Prognozy i procesy"
    },
})
EXECUTOR_SCRIPT_DEFAULTS = {
    "executor_battery_import_on_script": "script.ems_gpt_core_battery_import_on",
    "executor_battery_import_off_script": "script.ems_gpt_core_battery_import_off",
    "executor_battery_export_on_script": "script.ems_gpt_core_battery_export_on",
    "executor_battery_export_off_script": "script.ems_gpt_core_battery_export_off",
    "executor_pv_cwu_on_script": "script.ems_gpt_core_pv_cwu_on",
    "executor_pv_cwu_off_script": "script.ems_gpt_core_pv_cwu_off",
    "executor_pv_ev_on_script": "script.ems_gpt_core_pv_ev_on",
    "executor_pv_ev_off_script": "script.ems_gpt_core_pv_ev_off",
    "executor_hp_heat_dhw_on_script": "script.ems_gpt_core_hp_heat_dhw_on",
    "executor_hp_heat_dhw_off_script": "script.ems_gpt_core_hp_heat_dhw_off",
}

for _key, _entity_id in EXECUTOR_SCRIPT_DEFAULTS.items():
    process = _key.removeprefix("executor_").removesuffix("_script")
    state = "ON" if process.endswith("_on") else "OFF"
    process = process.removesuffix("_on").removesuffix("_off").upper()
    CONFIG_SETTINGS[_key] = {
        "type": "entity",
        "label": f"{process} — skrypt {state}",
        "group": "Executor — skrypty Home Assistant",
    }


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
    "pv_cwu_min_surplus_kw": 2.0,
    "pv_ev_min_surplus_kw": 1.5,
    "forecast_uncertainty_weight": 1.0,
    "forecast_fixed_corrections_enabled": False,
    "forecast_history_corrections_enabled": True,
    "buy_window_tolerance_pln_kwh": 0.05,
    "planned_flow_threshold_kwh": 0.02,
    "technical_flow_threshold_kwh": 0.05,
    "soc_floor_max_pct": 90.0,
    "soc_target_max_pct": 95.0,
    **DEYE_PROGRAM_SOC_DEFAULTS,
    "garden_temperature_entity": "sensor.klimat_w_ogrodzie_temperature",
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
    "target_history_days": 30.0,
    "target_history_min_samples": 7.0,
    "target_history_max_correction_pct": 15.0,
    "target_history_pv_relief_threshold_kwh": 0.10,
    "soc_target_history_weight_7d_pct": 50.0,
    "soc_target_history_weight_14d_pct": 25.0,
    "soc_target_history_weight_28d_pct": 25.0,
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

DEFAULT_OPTIONS.update(EXECUTOR_SCRIPT_DEFAULTS)

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
    try:
        result.update(json.loads(options_path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    try:
        runtime = json.loads(runtime_settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        runtime = {}
    result.update({key: value for key, value in runtime.items()
                   if key not in ADDON_ONLY_SETTINGS
                   and key != "deye_program_soc_baseline_json"})
    return result
