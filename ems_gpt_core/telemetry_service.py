"""Home Assistant telemetry collection and normalization."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta, timezone
from types import SimpleNamespace
from typing import Callable


@dataclass(frozen=True)
class TelemetryAdapters:
    options: dict
    entities: dict
    db: Callable
    local_now: Callable
    slot_start: Callable
    canonical_slots_for_day: Callable
    number: Callable
    ha_state: Callable


def build_telemetry(a: TelemetryAdapters):
    OPTIONS, ENTITIES = a.options, a.entities
    db, local_now, slot_start = a.db, a.local_now, a.slot_start
    canonical_slots_for_day, number, ha_state = a.canonical_slots_for_day, a.number, a.ha_state

    
    
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

    return SimpleNamespace(capture_telemetry=capture_telemetry, power_w=power_w)
