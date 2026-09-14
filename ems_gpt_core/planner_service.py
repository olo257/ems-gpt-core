from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable


def planning_tou_programs(live_programs: list[dict], baseline_json: str) -> list[dict]:
    """Use live TOU times but immutable configured SOC baselines for planning."""
    try:
        baselines = json.loads(str(baseline_json or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("DEYE_PROGRAM_SOC_BASELINE_INVALID") from exc
    result = []
    for live in live_programs:
        program = int(live["program"])
        if str(program) not in baselines:
            raise RuntimeError(f"DEYE_PROGRAM_SOC_BASELINE_MISSING:{program}")
        normalized = dict(live)
        normalized["soc"] = max(0.0, min(100.0, float(baselines[str(program)])))
        result.append(normalized)
    return result


def economic_sell_indices(rows: list[dict], eta_c: float, eta_d: float,
                          degradation: float, min_margin: float) -> set[int]:
    """Select profitable price peaks over the complete available PPD horizon."""
    suffix_min_buy = [None] * len(rows)
    suffix_max_sell = [None] * len(rows)
    min_buy = max_sell = None
    for index in range(len(rows) - 1, -1, -1):
        suffix_min_buy[index], suffix_max_sell[index] = min_buy, max_sell
        buy_price = rows[index].get("price_buy_pln_kwh")
        sell_price = rows[index].get("price_sell_pln_kwh")
        if buy_price is not None:
            price = float(buy_price)
            min_buy = price if min_buy is None else min(min_buy, price)
        if sell_price is not None:
            price = float(sell_price)
            max_sell = price if max_sell is None else max(max_sell, price)
    selected = set()
    for index, row in enumerate(rows):
        replacement = suffix_min_buy[index]
        future_peak = suffix_max_sell[index]
        sell_now = float(row.get("price_sell_pln_kwh") or 0.0)
        required_sell = (replacement / (eta_c * eta_d) + degradation + min_margin
                         if replacement is not None else None)
        economically_ready = required_sell is not None and sell_now >= required_sell
        peak_ready = future_peak is None or sell_now >= future_peak - min_margin
        if economically_ready and peak_ready:
            selected.add(index)
    return selected


def paired_arbitrage_buy_indices(rows: list[dict], eta_c: float, eta_d: float,
                                 degradation: float, min_margin: float,
                                 sell_indices: set[int] | None = None) -> set[int]:
    """Return post-sale slots that can economically restore exported battery energy.

    This is deliberately independent from clock-based sessions. A slot is
    eligible only after a real price-defined sale window and only while
    buying back the energy still clears efficiency, degradation and margin.
    The sequential allocator decides how many of these slots are actually used.
    """
    sell_indices = (economic_sell_indices(rows, eta_c, eta_d, degradation, min_margin)
                    if sell_indices is None else sell_indices)
    eligible = set()
    best_prior_sale = None
    for index, row in enumerate(rows):
        sell_price = row.get("price_sell_pln_kwh")
        if index in sell_indices and sell_price is not None:
            price = float(sell_price)
            best_prior_sale = price if best_prior_sale is None else max(best_prior_sale, price)
            continue
        buy_price = row.get("price_buy_pln_kwh")
        if best_prior_sale is None or buy_price is None:
            continue
        cycle_margin = best_prior_sale * eta_d - float(buy_price) / eta_c - degradation
        if cycle_margin >= min_margin:
            eligible.add(index)
    return eligible


def cheapest_recovery_indices(rows: list[dict], eligible: set[int], start_index: int,
                              recovery_kwh: float, max_grid_slot_kwh: float,
                              eta_c: float) -> set[int]:
    """Select only as many cheapest future slots as the SOC recovery requires."""
    internal_per_slot = max(0.000001, max_grid_slot_kwh * eta_c)
    slots_needed = max(0, math.ceil(max(0.0, recovery_kwh) / internal_per_slot))
    ranked = sorted(
        (float(rows[index]["price_buy_pln_kwh"]), index)
        for index in eligible if index >= start_index
    )
    return {index for _, index in ranked[:slots_needed]}


def soc_bridge_envelopes(rows: list[dict], grid_replenishment_indices: set[int],
                         reserve_pct: float, capacity_kwh: float, eta_c: float,
                         eta_d: float, max_grid_slot_kwh: float,
                         uncertainty_weight: float,
                         floor_cap_pct: float,
                         target_cap_pct: float) -> tuple[list[float], list[float]]:
    """Derive sale floors and charge targets from the next energy bridge.

    Future load raises the required SOC, forecast PV lowers it and every grid
    replenishment contributes only its physically available slot capacity.
    There is no clock-based evening target or invented terminal SOC.
    """
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    floor_cap = max(reserve, min(100.0, float(floor_cap_pct)))
    target_cap = max(floor_cap, min(100.0, float(target_cap_pct)))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    grid_internal = max(0.0, float(max_grid_slot_kwh)) * max(0.01, float(eta_c))
    nominal_after_kwh = guarded_after_kwh = 0.0
    floors = [reserve] * len(rows)
    targets = [reserve] * len(rows)

    for index in range(len(rows) - 1, -1, -1):
        nominal_pct = nominal_after_kwh / capacity * 100.0
        guarded_pct = guarded_after_kwh / capacity * 100.0
        floors[index] = min(floor_cap, max(reserve, reserve + nominal_pct))
        targets[index] = min(target_cap, max(floors[index], reserve + guarded_pct))

        row = rows[index]
        native_load = max(0.0, float(row.get("forecast_load_kwh") or 0.0))
        hp_load = max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0))
        load = native_load + hp_load
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        deficit = max(0.0, load - pv) / max(0.01, float(eta_d))
        surplus = max(0.0, pv - load) * max(0.01, float(eta_c))
        nominal_before = max(0.0, nominal_after_kwh + deficit - surplus)
        guarded_before = max(0.0, guarded_after_kwh
                             + deficit * (1.0 + uncertainty)
                             - surplus * max(0.0, 1.0 - uncertainty))
        if index in grid_replenishment_indices:
            nominal_before = max(0.0, nominal_before - grid_internal)
            guarded_before = max(0.0, guarded_before - grid_internal)
        nominal_after_kwh, guarded_after_kwh = nominal_before, guarded_before

    return floors, targets


def allocate_slot_discharge(energy_kwh: float, capacity_kwh: float,
                            reserve_pct: float, sale_floor_pct: float,
                            native_deficit_kwh: float, sale_request_kwh: float,
                            max_slot_output_kwh: float,
                            eta_d: float) -> tuple[float, float]:
    """Allocate deliberate export above sale floor, then native load to reserve."""
    efficiency = max(0.01, float(eta_d))
    output_limit_internal = max(0.0, float(max_slot_output_kwh)) / efficiency
    sale_internal = min(max(0.0, float(sale_request_kwh)) / efficiency,
                        output_limit_internal,
                        max(0.0, float(energy_kwh)
                            - float(capacity_kwh) * float(sale_floor_pct) / 100.0))
    after_sale = float(energy_kwh) - sale_internal
    native_internal = min(max(0.0, float(native_deficit_kwh)) / efficiency,
                          max(0.0, output_limit_internal - sale_internal),
                          max(0.0, after_sale
                              - float(capacity_kwh) * float(reserve_pct) / 100.0))
    return sale_internal, native_internal


def optimize_energy_horizon(rows: list[dict], initial_soc_pct: float,
                            capacity_kwh: float, reserve_pct: float,
                            eta_c: float, eta_d: float, degradation: float,
                            min_margin: float, max_power_kw: float,
                            slot_minutes: int, sale_floor_pct: list[float],
                            terminal_soc_pct: float,
                            soc_step_pct: float = 0.25,
                            max_soc_pct: float = 100.0) -> dict:
    """Minimize total energy cost across every available slot and SOC state."""
    if not rows:
        return {"flows": [], "objective_pln": 0.0, "soc_step_pct": soc_step_pct}
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    step = max(0.05, float(soc_step_pct))
    first_unit = int(math.ceil(reserve / step - 1e-9))
    last_unit = int(math.floor(min(100.0, max(reserve, float(max_soc_pct))) / step + 1e-9))
    start_unit = max(first_unit, min(last_unit, int(round(float(initial_soc_pct) / step))))
    terminal_unit = max(first_unit, min(last_unit, int(math.ceil(float(terminal_soc_pct) / step - 1e-9))))
    unit_kwh = capacity * step / 100.0
    max_internal_charge = max_power_kw * slot_minutes / 60.0 * eta_c
    max_internal_discharge = max_power_kw * slot_minutes / 60.0 / eta_d
    max_up = int(math.floor(max_internal_charge / unit_kwh + 1e-9))
    max_down = int(math.floor(max_internal_discharge / unit_kwh + 1e-9))
    # Grid supply for native loads is not an independent EMS purchase mode.
    # It is allowed above the technical reserve only when preserving the same
    # battery energy for a later, materially more valuable export is economic.
    future_sell = [None] * len(rows)
    best_future_sell = None
    for index in range(len(rows) - 1, -1, -1):
        future_sell[index] = best_future_sell
        price = rows[index].get("price_sell_pln_kwh")
        if price is not None and float(price) > 0.0:
            best_future_sell = (float(price) if best_future_sell is None
                                else max(best_future_sell, float(price)))

    costs = {start_unit: 0.0}
    predecessors: list[dict[int, tuple[int, dict]]] = []
    for index, row in enumerate(rows):
        buy_price = float(row.get("price_buy_pln_kwh") or 0.0)
        sell_price = float(row.get("price_sell_pln_kwh") or 0.0)
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        load = max(0.0, float(row.get("forecast_load_kwh") or 0.0))
        load += max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0))
        surplus, deficit = max(0.0, pv-load), max(0.0, load-pv)
        floor_pct = max(reserve, min(100.0, float(sale_floor_pct[index])))
        next_costs, next_predecessors = {}, {}
        for current_unit, accumulated in costs.items():
            current_energy = current_unit * unit_kwh
            for next_unit in range(max(first_unit, current_unit-max_down),
                                   min(last_unit, current_unit+max_up)+1):
                delta = (next_unit-current_unit) * unit_kwh
                pv_to_bat = grid_charge = battery_to_load = battery_sell = 0.0
                if delta >= 0.0:
                    charge_input = delta / eta_c
                    pv_to_bat = min(surplus, charge_input)
                    grid_charge = max(0.0, charge_input-pv_to_bat)
                    grid_load = deficit
                    pv_export = max(0.0, surplus-pv_to_bat) if sell_price > 0 else 0.0
                    pv_curtail = max(0.0, surplus-pv_to_bat-pv_export)
                    battery_discharge = 0.0
                else:
                    battery_discharge = -delta
                    delivered = battery_discharge * eta_d
                    battery_to_load = min(deficit, delivered)
                    battery_sell = max(0.0, delivered-battery_to_load)
                    if battery_sell > 1e-9 and current_energy-battery_sell/eta_d < capacity*floor_pct/100-1e-9:
                        continue
                    grid_load = max(0.0, deficit-battery_to_load)
                    pv_export = surplus if sell_price > 0 else 0.0
                    pv_curtail = max(0.0, surplus-pv_export)
                # Do not choose grid-only supply for the house while usable
                # battery energy exists. The sole exception is an economic
                # hold for a later sale; normal BUY always charges the battery.
                if (grid_load > unit_kwh * eta_d + 1e-9 and grid_charge <= 1e-9
                        and current_energy > capacity * reserve / 100.0 + 1e-9):
                    later_sell = future_sell[index]
                    hold_value = (later_sell * eta_d - degradation
                                  if later_sell is not None else float("-inf"))
                    if hold_value < buy_price + min_margin:
                        continue
                # During deliberate export the complete slot, including the
                # native load, must close at or above the protected sale floor.
                if battery_sell > 1e-9 and next_unit * step < floor_pct - 1e-9:
                    continue
                slot_cost = ((grid_load+grid_charge)*buy_price
                             -(pv_export+battery_sell)*sell_price
                             +battery_to_load*degradation
                             +battery_sell*(degradation+min_margin))
                total = accumulated+slot_cost
                if next_unit not in next_costs or total < next_costs[next_unit]-1e-10:
                    next_costs[next_unit] = total
                    next_predecessors[next_unit] = (current_unit, {
                        "soc_start_pct": current_unit*step, "soc_end_pct": next_unit*step,
                        "pv_kwh": pv, "load_kwh": load, "pv_to_bat_kwh": pv_to_bat,
                        "grid_charge_kwh": grid_charge, "grid_load_kwh": grid_load,
                        "battery_to_load_kwh": battery_to_load, "battery_sell_kwh": battery_sell,
                        "battery_discharge_internal_kwh": battery_discharge,
                        "battery_charge_internal_kwh": max(0.0, delta),
                        "pv_export_kwh": pv_export, "pv_curtail_kwh": pv_curtail,
                        "slot_cost_pln": slot_cost})
        if not next_costs:
            raise RuntimeError(f"No feasible SOC state at horizon slot {index}")
        costs, predecessors = next_costs, predecessors+[next_predecessors]
    candidates = [(cost, unit) for unit, cost in costs.items() if unit >= terminal_unit]
    if not candidates:
        raise RuntimeError("No feasible terminal SOC state for complete horizon")
    objective, unit = min(candidates, key=lambda value: (value[0], -value[1]))
    flows = [None]*len(rows)
    for index in range(len(rows)-1, -1, -1):
        unit, flows[index] = predecessors[index][unit]
    return {"flows": flows, "objective_pln": round(objective, 6),
            "soc_step_pct": step, "terminal_soc_pct": terminal_unit*step}


def derive_soc_commitments(flows: list[dict], capacity_kwh: float,
                           reserve_pct: float, terminal_soc_pct: float,
                           floor_cap_pct: float,
                           target_cap_pct: float) -> tuple[list[float], list[float]]:
    """Keep the sale floor and purchase target as separate control contracts."""
    capacity = max(0.001, float(capacity_kwh))
    reserve_kwh = capacity * reserve_pct / 100.0
    required_after = max(reserve_kwh, capacity * terminal_soc_pct / 100.0)
    floors, targets = [reserve_pct]*len(flows), [reserve_pct]*len(flows)
    for index in range(len(flows) - 1, -1, -1):
        flow = flows[index]
        soc_end = max(reserve_pct, float(flow.get("soc_end_pct") or reserve_pct))
        battery_sell = max(0.0, float(flow.get("battery_sell_kwh") or 0.0))
        # Target is the energy required after this slot to execute every
        # accepted future flow until the next replenishment represented by the
        # optimizer. It is deliberately calculated backwards, not copied from
        # the current floor or from the current SOC trajectory.
        required_pct = required_after / capacity * 100.0
        targets[index] = min(target_cap_pct, max(reserve_pct, required_pct))
        # Floor limits deliberate battery export only. Native load may still
        # discharge to the technical reserve.
        if battery_sell > 1e-9:
            floors[index] = min(floor_cap_pct, soc_end)
        charge = max(0.0, float(flow.get("battery_charge_internal_kwh") or 0.0))
        discharge = max(0.0, float(flow.get("battery_discharge_internal_kwh") or 0.0))
        required_after = max(reserve_kwh, required_after + discharge - charge)
    return floors, targets


def bridge_soc_commitments(rows: list[dict], flows: list[dict], capacity_kwh: float,
                           reserve_pct: float, eta_c: float, eta_d: float,
                           max_grid_slot_kwh: float, uncertainty_weight: float,
                           floor_cap_pct: float,
                           target_cap_pct: float) -> tuple[list[float], list[float]]:
    """SOC needed until the next forecast PV or planned battery BUY.

    A sale does not create a literal buy-back debt.  Its energy is recovered
    by forecast PV first; grid BUY covers only the remaining bridge deficit.
    The target in a BUY slot is never lower than the optimizer's planned SOC
    after that charge, otherwise the executor would stop before the plan is met.
    """
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    floor_cap = max(reserve, min(100.0, float(floor_cap_pct)))
    target_cap = max(floor_cap, min(100.0, float(target_cap_pct)))
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    nominal_after_kwh = guarded_after_kwh = 0.0
    floors = [reserve] * len(rows)
    targets = [reserve] * len(rows)

    for index in range(len(rows) - 1, -1, -1):
        floors[index] = min(
            floor_cap, max(reserve, reserve + nominal_after_kwh / capacity * 100.0))
        targets[index] = min(
            target_cap, max(floors[index],
                            reserve + guarded_after_kwh / capacity * 100.0))
        row = rows[index]
        load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        deficit = max(0.0, load - pv) / discharge_efficiency
        surplus = max(0.0, pv - load) * charge_efficiency
        # Use the accepted charge, not the theoretical 5 kW slot maximum.
        # This keeps the bridge conservative when a BUY slot is partial.
        grid_recovery = (max(0.0, float(flows[index].get("grid_charge_kwh") or 0.0))
                         * charge_efficiency)
        nominal_after_kwh = max(
            0.0, nominal_after_kwh + deficit - surplus - grid_recovery)
        guarded_after_kwh = max(
            0.0, guarded_after_kwh + deficit * (1.0 + uncertainty)
            - surplus * max(0.0, 1.0 - uncertainty) - grid_recovery)

    buy_indices = {
        index for index, flow in enumerate(flows)
        if float(flow.get("grid_charge_kwh") or 0.0) > 1e-9
    }
    for index in buy_indices:
        targets[index] = min(
            target_cap_pct,
            max(targets[index], float(flows[index].get("soc_end_pct") or reserve_pct)),
        )
    return floors, targets


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
        uncertainty_weight = max(0.0, min(2.0, float(OPTIONS.get("forecast_uncertainty_weight", 1.0))))
        floor_cap = max(reserve, min(100.0, float(OPTIONS.get("soc_floor_max_pct", 90.0))))
        target_cap = max(floor_cap, min(100.0, float(OPTIONS.get("soc_target_max_pct", 95.0))))
        flow_threshold = max(0.0, float(OPTIONS.get("planned_flow_threshold_kwh", 0.02)))
        technical_threshold = max(
            flow_threshold, float(OPTIONS.get("technical_flow_threshold_kwh", 0.05)))
        max_kw = max(0.25, float(OPTIONS.get("battery_max_power_kw", 5.0)))
        cwu_threshold = max(0.0, float(OPTIONS.get("pv_cwu_min_surplus_kw", 2.0))) * .25
        ev_threshold = max(0.0, float(OPTIONS.get("pv_ev_min_surplus_kw", 1.5))) * .25
        run_id = str(uuid.uuid4())
        hp_shortfalls = []
        tou_programs = planning_tou_programs(
            tou_program_snapshot(), OPTIONS.get("deye_program_soc_baseline_json", "{}"))
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
              (run_id, cutoff.date(), run_type, "CORE_0_32_3", len(source)))
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

            horizon_rows, sale_constraints, tou_by_index = [], [], []
            for index, row in enumerate(rows):
                work = dict(row)
                work["forecast_load_kwh"] = max(0.0, float(row.get("forecast_load_kwh") or 0.0)*(1+0.10*uncertainty_weight))
                work["forecast_pv_total_kwh"] = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0)*(1-0.10*uncertainty_weight))
                work["forecast_heat_pump_load_kwh"] = planned_hp_kw*0.25 if index in hp_selected_indices else 0.0
                horizon_rows.append(work)
                program = active_tou_program(row["slot_start"], tou_programs)
                tou_by_index.append(program)
                sale_constraints.append(max(reserve, float(program["soc"])) if program else 100.0)
            terminal_soc = sale_constraints[-1] if sale_constraints[-1] < 100 else reserve
            audit_stage(cur,run_id,"WINDOW_CANDIDATES","OK",len(rows),"pass 1: full horizon")
            audit_stage(cur,run_id,"LOAD","OK",len(rows),"pass 2: native and controllable load")
            audit_stage(cur,run_id,"PV","OK",len(rows),"pass 3: corrected PV balance")
            # Resolve the SOC bridge and the optimized flows together.  PV may
            # recover energy after a sale; BUY is required only for the net
            # deficit that remains before the next replenishment opportunity.
            optimized_floors = list(sale_constraints)
            for _ in range(3):
                optimization = optimize_energy_horizon(
                    horizon_rows,soc_now,capacity,reserve,eta_c,eta_d,degradation,min_margin,
                    max_kw,int(OPTIONS["slot_minutes"]),optimized_floors,
                    terminal_soc,0.25,target_cap)
                bridge_floors, targets = bridge_soc_commitments(
                    horizon_rows, optimization["flows"], capacity, reserve,
                    eta_c, eta_d, max_kw*int(OPTIONS["slot_minutes"])/60.0,
                    uncertainty_weight, floor_cap, target_cap)
                next_floors = [max(sale_constraints[index], bridge_floors[index])
                               for index in range(len(rows))]
                if all(abs(a-b) < 1e-9 for a,b in zip(next_floors, optimized_floors)):
                    break
                optimized_floors = next_floors
            else:
                optimization = optimize_energy_horizon(
                    horizon_rows,soc_now,capacity,reserve,eta_c,eta_d,degradation,min_margin,
                    max_kw,int(OPTIONS["slot_minutes"]),optimized_floors,
                    terminal_soc,0.25,target_cap)
            bridge_floors, targets = bridge_soc_commitments(
                horizon_rows, optimization["flows"], capacity, reserve,
                eta_c, eta_d, max_kw*int(OPTIONS["slot_minutes"])/60.0,
                uncertainty_weight, floor_cap, target_cap)
            for index, flow in enumerate(optimization["flows"]):
                if (float(flow.get("battery_sell_kwh") or 0.0) > flow_threshold
                        and float(flow.get("soc_end_pct") or 0.0) + 1e-9
                        < optimized_floors[index]):
                    raise RuntimeError(
                        f"SALE_FLOOR_VIOLATION:{index}:"
                        f"{flow.get('soc_end_pct')}<{optimized_floors[index]}")
            audit_stage(cur,run_id,"ECONOMY","OK",len(rows),f"pass 4: objective={optimization['objective_pln']:.3f}")
            purchase_slots=sum(float(flow["grid_charge_kwh"])>flow_threshold for flow in optimization["flows"])
            audit_stage(cur,run_id,"REPLENISHMENT","OK",purchase_slots,
                        "pass 5: load, falling PV, sale preparation and recovery")
            floors = [max(sale_constraints[index], bridge_floors[index])
                      for index in range(len(rows))]
            base=[{"row":row,**flow} for row,flow in zip(rows,optimization["flows"])]
            for i,item in enumerate(base):
                row=item["row"]
                tou_program=tou_by_index[i]
                tou_floor=float(tou_program["soc"]) if tou_program else None
                effective_floor=max(reserve,tou_floor) if tou_floor is not None else reserve
                tou_block_reason=None if tou_program else "TOU_FLOOR_UNAVAILABLE"
                target=targets[i]
                item["start"],item["end"]=item["soc_start_pct"],item["soc_end_pct"]
                pv=float(row.get("forecast_pv_total_kwh") or 0)
                native_load=float(row.get("forecast_load_kwh") or 0)
                hp_load=planned_hp_kw * 0.25 if i in hp_selected_indices else 0.0
                load=native_load+hp_load
                pv_surplus=max(0.0,pv-load)
                pv_to_bat=item["pv_to_bat_kwh"]
                pv_flex=item["pv_export_kwh"]
                buy=item["grid_charge_kwh"]
                item["charge"]=item["battery_charge_internal_kwh"]
                item["discharge"]=item["battery_discharge_internal_kwh"]
                item["sell"]=item["battery_sell_kwh"]
                sell_price=float(row.get("price_sell_pln_kwh") or 0)
                sell_bat=item["sell"]>flow_threshold
                floor=min(floor_cap,max(reserve,floors[i],effective_floor if sell_bat else reserve))
                paired_buy=buy>flow_threshold and any(float(previous["battery_sell_kwh"])>flow_threshold for previous in optimization["flows"][:i])
                buy_purpose="POST_SALE_RECOVERY" if paired_buy else "FUTURE_LOAD_OR_SALE_PREPARATION" if buy>flow_threshold else "NONE"
                grid_hold=(item["grid_load_kwh"]>technical_threshold and buy<=flow_threshold
                           and any(float(future["battery_sell_kwh"])>flow_threshold
                                   for future in optimization["flows"][i+1:]))
                grid_purpose="SOC_HOLD_FOR_FUTURE_SALE" if grid_hold else "NONE"
                no_sell_pv=sell_price<=0
                pv_cwu=(not sell_bat and pv_flex>=cwu_threshold and item["end"]>=target)
                cwu_kwh=min(pv_flex,0.625) if pv_cwu else 0.0
                after_cwu=max(0.0,pv_flex-cwu_kwh)
                pv_ev=(not sell_bat and after_cwu>=ev_threshold and item["end"]>=min(100,target+20))
                ev_kwh=after_cwu if pv_ev else 0.0
                after_flex=max(0.0,after_cwu-ev_kwh)
                pv_export_kwh=after_flex if sell_price>0 else 0.0
                pv_curtail_kwh=after_flex-pv_export_kwh
                item["pv_export"]=pv_export_kwh
                grid_policy="BUY_ALLOWED" if buy>flow_threshold else ("NO_BUY" if sell_bat else "NEUTRAL")
                export_policy="SELL_BAT" if sell_bat else ("NO_SELL_PV" if no_sell_pv else ("SELL_PV" if item["pv_export"]>flow_threshold else "NEUTRAL"))
                recommendation=("Zakup ładowanie" if buy>flow_threshold else "Sprzedaż z baterii" if sell_bat else
                    "Sprzedaż PV" if item["pv_export"]>flow_threshold else "Ładowanie PV" if item["charge"]>flow_threshold else
                    "Autokonsumpcja PV" if pv>flow_threshold else "Autokonsumpcja z baterii" if item["battery_to_load_kwh"]>flow_threshold else
                    "Ochrona SOC przed sprzedażą" if grid_hold else "Zasilanie z sieci")
                reason=(f"optimizer=FULL_HORIZON; horizon_slots={len(base)}; objective_pln={optimization['objective_pln']:.3f}; "
                        f"slot_cost_pln={item['slot_cost_pln']:.3f}; grid={grid_policy}; export={export_policy}; "
                        f"soc={item['end']:.2f}; floor={floor:.2f}; target={target:.2f}; hp_load_kwh={hp_load:.3f}; "
                        f"buy_purpose={buy_purpose}; grid_purpose={grid_purpose}")
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
                  (round(item["start"],2),round(item["end"],2),round(floor,2),round(target,2),round(buy,6),
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
                    ("BATTERY_EXPORT", export_policy == "SELL_BAT", export_policy, f"optimizer=FULL_HORIZON; sell={sell_price:.3f}"),
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
            audit_stage(cur,run_id,"SOC","OK",len(base),f"pass 6: result SOC; step={optimization['soc_step_pct']:.2f}")
            audit_stage(cur,run_id,"PPD","OK",len(base),"pass 7: derived only from completed optimized plan")
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
              p.ppd_version='CORE_0_32_3',p.ppd_locked_at=NOW(6),p.plan_run_id=%s,p.plan_stage='PUBLISHED',
              p.plan_stage_version='CORE_0_32_3',p.plan_stage_updated_at=NOW(6),
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
