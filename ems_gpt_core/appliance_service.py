"""Configurable appliance metering without changing control decisions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from config_service import APPLIANCE_DEFAULTS


@dataclass(frozen=True)
class ApplianceAdapters:
    options: dict
    db: Callable
    local_now: Callable
    ha_state: Callable
    record_event: Callable


def _number(state: dict | None) -> float | None:
    try:
        value = float((state or {}).get("state"))
        return value if value == value else None
    except (TypeError, ValueError):
        return None


def build_appliance_meter(a: ApplianceAdapters):
    def capture_appliances() -> dict:
        day = a.local_now().date()
        captured = 0
        degraded = 0
        with a.db() as conn, conn.cursor() as cur:
            for key, (label, *_defaults) in APPLIANCE_DEFAULTS.items():
                prefix = f"appliance_{key}_"
                if not a.options.get(prefix + "enabled"):
                    continue
                energy_entity = str(a.options.get(prefix + "energy_entity") or "").strip()
                power_entity = str(a.options.get(prefix + "power_entity") or "").strip()
                water_entity = str(a.options.get(prefix + "water_entity") or "").strip()
                state_entity = str(a.options.get(prefix + "state_entity") or "").strip()
                counter_type = str(a.options.get(prefix + "counter_type") or "total")
                threshold = float(a.options.get(prefix + "active_power_threshold_w") or 0)
                energy = _number(a.ha_state(energy_entity)) if energy_entity else None
                power = _number(a.ha_state(power_entity)) if power_entity else None
                water = _number(a.ha_state(water_entity)) if water_entity else None
                state_value = (a.ha_state(state_entity) or {}).get("state") if state_entity else None
                quality = "OK" if energy is not None else "MISSING_ENERGY"
                degraded += quality != "OK"
                cur.execute("""SELECT first_energy_kwh,last_energy_kwh,daily_energy_kwh,
                  first_water_l,last_water_l,daily_water_l,is_active
                  FROM ems_gpt_core_appliance_daily WHERE local_day=%s AND appliance_key=%s""",
                            (day, key))
                prior = cur.fetchone()
                first_energy = energy if prior is None else prior["first_energy_kwh"]
                first_water = water if prior is None else prior["first_water_l"]
                if counter_type == "daily":
                    daily_energy = energy
                    daily_water = water
                else:
                    daily_energy = max(0.0, energy - first_energy) if energy is not None and first_energy is not None else None
                    daily_water = max(0.0, water - first_water) if water is not None and first_water is not None else None
                active = bool(power is not None and power >= threshold)
                cycle_increment = int(active and not bool((prior or {}).get("is_active")))
                cur.execute("""INSERT INTO ems_gpt_core_appliance_daily
                  (local_day,appliance_key,appliance_name,counter_type,energy_entity,power_entity,
                   water_entity,state_entity,first_energy_kwh,last_energy_kwh,daily_energy_kwh,
                   first_water_l,last_water_l,daily_water_l,current_power_w,max_power_w,is_active,
                   cycle_count,state_value,quality_status,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6))
                  ON DUPLICATE KEY UPDATE appliance_name=VALUES(appliance_name),counter_type=VALUES(counter_type),
                   energy_entity=VALUES(energy_entity),power_entity=VALUES(power_entity),
                   water_entity=VALUES(water_entity),state_entity=VALUES(state_entity),
                   last_energy_kwh=VALUES(last_energy_kwh),daily_energy_kwh=VALUES(daily_energy_kwh),
                   last_water_l=VALUES(last_water_l),daily_water_l=VALUES(daily_water_l),
                   current_power_w=VALUES(current_power_w),max_power_w=GREATEST(COALESCE(max_power_w,0),COALESCE(VALUES(current_power_w),0)),
                   is_active=VALUES(is_active),cycle_count=cycle_count+%s,state_value=VALUES(state_value),
                   quality_status=VALUES(quality_status),updated_at=NOW(6)""",
                            (day, key, label, counter_type, energy_entity or None, power_entity or None,
                             water_entity or None, state_entity or None, first_energy, energy, daily_energy,
                             first_water, water, daily_water, power, power, int(active), cycle_increment,
                             state_value, quality, cycle_increment))
                captured += 1
        result = {"status": "OK" if degraded == 0 else "PARTIAL", "devices": captured, "degraded": degraded}
        return result

    return capture_appliances
