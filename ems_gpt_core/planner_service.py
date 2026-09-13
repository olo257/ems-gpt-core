from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable


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


@dataclass(frozen=True)
class PlannerAdapters:
    options: dict
    db: Callable
    local_now: Callable
    slot_start: Callable
    setting: Callable
    audit_stage: Callable
    active_tou_program: Callable
    tou_program_snapshot: Callable
    record_event: Callable


def build_planner(a: PlannerAdapters):
    OPTIONS = a.options
    db = a.db
    local_now = a.local_now
    slot_start = a.slot_start
    setting = a.setting
    audit_stage = a.audit_stage
    active_tou_program = a.active_tou_program
    tou_program_snapshot = a.tou_program_snapshot
    record_event = a.record_event

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

    return SimpleNamespace(
        optimize_hp_heating_slots=optimize_hp_heating_slots,
        run_planner=run_planner,
    )
