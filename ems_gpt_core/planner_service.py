from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable

from config_service import deye_program_soc_baselines
from ingestion_service import derive_price_windows


def historical_terminal_soc(closing_rows: list[dict], terminal_day,
                            weights_pct: dict[int, float], fallback_pct: float) -> dict:
    """Forecast only the end-of-day SOC from completed historical closes.

    The 7/14/28-day windows intentionally overlap.  Missing windows are
    removed and the remaining configured weights are normalized.  This value
    is a terminal boundary only; slot targets are still derived by the
    backward energy-balance pass.
    """
    means: dict[int, float | None] = {}
    samples: dict[int, int] = {}
    for horizon in (7, 14, 28):
        values = []
        for row in closing_rows:
            day = row.get("local_day") or row.get("day_date")
            if isinstance(day, str):
                day = datetime.strptime(day[:10], "%Y-%m-%d").date()
            age = (terminal_day - day).days if day is not None else 0
            if 1 <= age <= horizon and row.get("soc_end_pct") is not None:
                values.append(float(row["soc_end_pct"]))
        samples[horizon] = len(values)
        means[horizon] = sum(values) / len(values) if values else None
    available_weight = sum(
        max(0.0, float(weights_pct.get(horizon, 0.0)))
        for horizon in (7, 14, 28) if means[horizon] is not None
    )
    if available_weight <= 0.0:
        forecast = float(fallback_pct)
        source = "FALLBACK_RESERVE"
    else:
        forecast = sum(
            float(means[horizon]) * max(0.0, float(weights_pct.get(horizon, 0.0)))
            for horizon in (7, 14, 28) if means[horizon] is not None
        ) / available_weight
        source = "WEIGHTED_ACTUAL_CLOSE_7_14_28D"
    return {"soc_pct": forecast, "means": means, "samples": samples,
            "available_weight_pct": available_weight, "source": source}


def historical_hp_power_kw(heating_rows: list[dict], planning_day,
                           weights_pct: dict[int, float], fallback_kw: float) -> dict:
    """Forecast electric heating power from active historical HP slots."""
    means: dict[int, float | None] = {}
    samples: dict[int, int] = {}
    for horizon in (7, 14, 28):
        values = []
        for row in heating_rows:
            day = row.get("local_day")
            if isinstance(day, str):
                day = datetime.strptime(day[:10], "%Y-%m-%d").date()
            age = (planning_day - day).days if day is not None else 0
            energy = row.get("actual_heating_consumed_kwh")
            if 1 <= age <= horizon and energy is not None and float(energy) > 0.02:
                values.append(float(energy) * 4.0)
        samples[horizon] = len(values)
        means[horizon] = sum(values) / len(values) if values else None
    available_weight = sum(
        max(0.0, float(weights_pct.get(horizon, 0.0)))
        for horizon in (7, 14, 28) if means[horizon] is not None
    )
    if available_weight <= 0.0:
        power_kw = max(0.0, float(fallback_kw))
        source = "FALLBACK_CONFIG"
    else:
        power_kw = sum(
            float(means[horizon]) * max(0.0, float(weights_pct.get(horizon, 0.0)))
            for horizon in (7, 14, 28) if means[horizon] is not None
        ) / available_weight
        source = "WEIGHTED_ACTUAL_HEATING_7_14_28D"
    return {"power_kw": power_kw, "means": means, "samples": samples,
            "available_weight_pct": available_weight, "source": source}


def strict_database_bool(value, field: str) -> bool:
    """Normalize MariaDB BOOLEAN/TINYINT once and reject ambiguous values."""
    if isinstance(value, bool):
        return value
    if value == 0 or str(value).strip().lower() in {"0", "false"}:
        return False
    if value == 1 or str(value).strip().lower() in {"1", "true"}:
        return True
    raise RuntimeError(f"INVALID_BOOLEAN:{field}:{value!r}")


def next_replenishment_prices(rows: list[dict]) -> list[float | None]:
    """Cheapest price in the nearest later contiguous battery BUY window."""
    result: list[float | None] = [None] * len(rows)
    nearest: float | None = None
    for index in range(len(rows) - 1, -1, -1):
        result[index] = nearest
        is_buy = strict_database_bool(rows[index].get("buy_window"), "buy_window")
        if is_buy:
            price = max(0.0, float(rows[index].get("price_buy_pln_kwh") or 0.0))
            starts_closer_window = (
                index + 1 == len(rows)
                or not strict_database_bool(rows[index + 1].get("buy_window"), "buy_window"))
            nearest = price if starts_closer_window or nearest is None else min(nearest, price)
    return result


def pv_first_target_caps(rows: list[dict], flows: list[dict],
                         selected_buy_indices: set[int], capacity_kwh: float,
                         reserve_pct: float, eta_c: float,
                         minimum_margin_pln: float = 0.0) -> dict[int, float]:
    """Cap a BUY deadline when later cheap-to-use PV is being exported.

    Only energy which can be removed without crossing the reserve before the
    next PV-surplus slot is shifted.  The future PV export proves that the
    terminal contract remains funded.  A cap is returned only when buying the
    shifted kWh costs more than exporting that PV earns, including the
    configured minimum margin.
    """
    if not rows or not selected_buy_indices:
        return {}
    capacity=max(0.001,float(capacity_kwh)); reserve=float(reserve_pct)
    efficiency=max(0.01,float(eta_c)); threshold=1e-6
    caps: dict[int,float]={}
    ends=sorted(i for i in selected_buy_indices if i+1 not in selected_buy_indices)
    for end in ends:
        start=end
        while start-1 in selected_buy_indices:
            start-=1
        next_buy=min((i for i in selected_buy_indices if i>end),default=len(rows))
        pv_indices=[]
        for i in range(end+1,next_buy):
            row=rows[i]
            load=(max(0.0,float(row.get("forecast_load_kwh") or 0.0))+
                  max(0.0,float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
            if (max(0.0,float(row.get("forecast_pv_total_kwh") or 0.0))>load+threshold
                    and float(flows[i].get("pv_export_kwh") or 0.0)>threshold):
                pv_indices.append(i)
        if not pv_indices:
            continue
        first_pv=min(pv_indices)
        minimum_soc=min(float(flows[i].get("soc_end_pct") or reserve)
                        for i in range(end,first_pv))
        bridge_margin_internal=max(0.0,(minimum_soc-reserve)*capacity/100.0)
        grid_input=sum(max(0.0,float(flows[i].get("grid_charge_kwh") or 0.0))
                       for i in range(start,end+1))
        pv_export=sum(max(0.0,float(flows[i].get("pv_export_kwh") or 0.0))
                      for i in pv_indices)
        shift_internal=min(grid_input*efficiency,pv_export*efficiency,bridge_margin_internal)
        if shift_internal<=threshold:
            continue
        buy_cost=sum(max(0.0,float(flows[i].get("grid_charge_kwh") or 0.0))*
                     float(rows[i].get("price_buy_pln_kwh") or 0.0)
                     for i in range(start,end+1))/max(grid_input,threshold)
        export_value=sum(max(0.0,float(flows[i].get("pv_export_kwh") or 0.0))*
                         float(rows[i].get("price_sell_pln_kwh") or 0.0)
                         for i in pv_indices)/max(pv_export,threshold)
        if buy_cost<=export_value+max(0.0,float(minimum_margin_pln)):
            continue
        caps[end]=max(reserve,float(flows[end].get("soc_end_pct") or reserve)-
                      shift_internal/capacity*100.0)
    return caps


def planning_tou_programs(live_programs: list[dict], baselines: dict[str, float]) -> list[dict]:
    """Use live TOU times but immutable configured SOC baselines for planning."""
    result = []
    for live in live_programs:
        program = int(live["program"])
        if str(program) not in baselines:
            raise RuntimeError(f"DEYE_PROGRAM_SOC_BASELINE_MISSING:{program}")
        normalized = dict(live)
        normalized["soc"] = max(0.0, min(100.0, float(baselines[str(program)])))
        result.append(normalized)
    return result


def tou_sale_safety_floors(rows: list[dict], tou_by_index: list[dict | None],
                           flows: list[dict], capacity_kwh: float,
                           reserve_pct: float, eta_d: float,
                           uncertainty_weight: float,
                           max_override_minutes: int,
                           slot_minutes: int,
                           daily_required_soc: list[float]) -> list[float]:
    """Protect TOU safety SOC from deliberate export.

    Normally a sale must leave the active TOU baseline plus the battery energy
    needed by native loads until a later program deliberately lowers that
    baseline.  Programs 5 and 6 may use a lower floor only when the economic
    plan contains a real, near-term grid charge on the same local day and the
    accepted trajectory still closes that day at its required SOC.
    """
    if not (len(rows) == len(tou_by_index) == len(flows) == len(daily_required_soc)):
        raise ValueError("TOU_SALE_FLOOR_LENGTH_MISMATCH")
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    max_slots = max(1, int(max_override_minutes) // max(1, int(slot_minutes)))
    selected_buys = {
        i for i, flow in enumerate(flows)
        if float(flow.get("grid_charge_kwh") or 0.0) > 1e-9
    }
    floors = [100.0] * len(rows)

    for i, row in enumerate(rows):
        if not strict_database_bool(row.get("sale_window", False), "sale_window"):
            continue
        program = tou_by_index[i]
        if not program:
            continue
        baseline = max(reserve, min(100.0, float(program["soc"])))
        program_number = int(program["program"])
        row_day = row.get("local_day") or row["slot_start"].date()

        day_end = i
        while (day_end + 1 < len(rows)
               and (rows[day_end + 1].get("local_day")
                    or rows[day_end + 1]["slot_start"].date()) == row_day):
            day_end += 1
        close_safe = (
            float(flows[day_end].get("soc_end_pct") or reserve) + 0.01
            >= float(daily_required_soc[day_end])
        )
        near_buy = next((j for j in range(i + 1, min(day_end, i + max_slots) + 1)
                         if j in selected_buys), None)
        if program_number in {5, 6} and near_buy is not None and close_safe:
            # The constrained pass still enforces the continuous SOC contract;
            # this only permits the executor to follow it below the TOU baseline.
            floors[i] = reserve
            continue

        stop = day_end + 1
        for j in range(i + 1, day_end + 1):
            future_program = tou_by_index[j]
            if future_program and float(future_program["soc"]) < baseline - 1e-9:
                stop = j
                break
        bridge_internal = 0.0
        for j in range(i + 1, stop):
            future = rows[j]
            load = (max(0.0, float(future.get("forecast_load_kwh") or 0.0))
                    + max(0.0, float(future.get("forecast_heat_pump_load_kwh") or 0.0)))
            pv = max(0.0, float(future.get("forecast_pv_total_kwh") or 0.0))
            bridge_internal += max(0.0, load - pv) / efficiency * (1.0 + uncertainty)
        floors[i] = min(100.0, baseline + bridge_internal / capacity * 100.0)
    return floors


def battery_sale_economics(rows: list[dict], index: int, eta_c: float, eta_d: float,
                           degradation: float, min_margin: float) -> dict:
    """Describe whether a slot pays for restoring the exported battery energy."""
    future_buys = [
        float(row["price_buy_pln_kwh"])
        for row in rows[index + 1:]
        if row.get("price_buy_pln_kwh") is not None
    ]
    replacement = min(future_buys) if future_buys else None
    sell_now = float(rows[index].get("price_sell_pln_kwh") or 0.0)
    required_sell = (
        replacement / (eta_c * eta_d) + degradation + min_margin
        if replacement is not None else None
    )
    return {
        "replacement_buy_price": replacement,
        "required_sell_price": required_sell,
        "expected_margin": (
            sell_now - replacement / (eta_c * eta_d) - degradation
            if replacement is not None else None
        ),
        "eligible": required_sell is not None and sell_now >= required_sell,
    }


def morning_sale_soc_requirements(rows: list[dict], required_soc_pcts: list[float],
                                  daily_terminal_soc: dict) -> tuple[list[float], list[int]]:
    """Require the historical daily SOC target immediately before a morning sale.

    The midnight boundary alone is insufficient: native overnight load can
    consume that reserve before the first high-price sale slot.  Placing the
    same quantitative requirement on the slot immediately preceding the
    morning SELL transition makes the optimizer preserve (or economically
    rebuild in an earlier BUY window) enough energy for the morning peak.
    """
    if len(rows) != len(required_soc_pcts):
        raise ValueError("rows and required SOC lengths differ")
    result = list(required_soc_pcts)
    protected_indices = []
    for index in range(1, len(rows)):
        row = rows[index]
        previous = rows[index - 1]
        slot_time = row.get("slot_start_local") or row["slot_start"]
        sale_starts = (strict_database_bool(row.get("sale_window"), "sale_window")
                       and not strict_database_bool(
                           previous.get("sale_window"), "sale_window"))
        if not sale_starts or slot_time.hour >= 12:
            continue
        sale_day = row.get("local_day") or slot_time.date()
        target = daily_terminal_soc.get(sale_day)
        if target is None:
            continue
        protected_index = index - 1
        result[protected_index] = max(result[protected_index], float(target))
        protected_indices.append(protected_index)
    return result, protected_indices


def economic_sell_indices(rows: list[dict], eta_c: float, eta_d: float,
                          degradation: float, min_margin: float) -> set[int]:
    """Select profitable price peaks over the complete available PPD horizon."""
    suffix_max_sell = [None] * len(rows)
    max_sell = None
    for index in range(len(rows) - 1, -1, -1):
        suffix_max_sell[index] = max_sell
        sell_price = rows[index].get("price_sell_pln_kwh")
        if sell_price is not None:
            price = float(sell_price)
            max_sell = price if max_sell is None else max(max_sell, price)
    selected = set()
    for index, row in enumerate(rows):
        future_peak = suffix_max_sell[index]
        sell_now = float(row.get("price_sell_pln_kwh") or 0.0)
        economics = battery_sale_economics(
            rows, index, eta_c, eta_d, degradation, min_margin)
        economically_ready = economics["eligible"]
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
                            max_soc_pct: float = 100.0,
                            minimum_soc_targets: list[float] | None = None,
                            hard_target_indices: set[int] | None = None,
                            target_due_indices: set[int] | None = None,
                            required_soc_pcts: list[float] | None = None,
                            battery_sales_enabled: bool = True,
                            allow_terminal_shortfall: bool = False) -> dict:
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
    costs = {start_unit: 0.0}
    buy_permissions = [
        strict_database_bool(row.get("buy_window", False), "buy_window")
        for row in rows
    ]
    buy_window_ends = {
        index for index, value in enumerate(buy_permissions)
        if value and (index + 1 == len(buy_permissions) or not buy_permissions[index + 1])
    }
    target_due_indices = buy_window_ends if target_due_indices is None else set(target_due_indices)
    predecessors: list[dict[int, tuple[int, dict]]] = []
    effective_target_pcts: list[float] = []
    for index, row in enumerate(rows):
        buy_price = float(row.get("price_buy_pln_kwh") or 0.0)
        sell_price = float(row.get("price_sell_pln_kwh") or 0.0)
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        load = max(0.0, float(row.get("forecast_load_kwh") or 0.0))
        load += max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0))
        surplus, deficit = max(0.0, pv-load), max(0.0, load-pv)
        floor_pct = max(reserve, min(100.0, float(sale_floor_pct[index])))
        target_pct = (float(max_soc_pct) if minimum_soc_targets is None else
                      max(reserve, min(float(max_soc_pct),
                          float(minimum_soc_targets[index]))))
        requested_target_unit = int(math.ceil(target_pct / step - 1e-9))
        required_pct = (reserve if required_soc_pcts is None else
                        max(reserve, min(float(max_soc_pct),
                            float(required_soc_pcts[index]))))
        required_unit = int(math.ceil(required_pct / step - 1e-9))
        # MariaDB returns BOOL/TINYINT as 0/1 (and some drivers as Decimal),
        # so identity checks against False would incorrectly allow 0.
        grid_charge_allowed = strict_database_bool(row.get("buy_window", True), "buy_window")
        battery_sale_allowed = (battery_sales_enabled
                                and strict_database_bool(
                                    row.get("sale_window", False), "sale_window"))
        available_charge_internal = min(
            max_internal_charge,
            surplus * eta_c + (max_internal_charge if grid_charge_allowed else 0.0),
        )
        reachable_up = int(math.floor(available_charge_internal / unit_kwh + 1e-9))
        # A due target is an execution deadline, not permission to invent
        # charging power.  A rolling replan can enter the final slot of a BUY
        # or PV replenishment window with less SOC than the backward contract
        # assumed.  Preserve the requested target as the grid-charge ceiling,
        # but enforce and publish no more than the highest state reachable from
        # the currently feasible frontier in this slot.
        enforced_target_unit = requested_target_unit
        if index in target_due_indices:
            enforced_target_unit = min(
                requested_target_unit,
                min(last_unit, max(costs) + reachable_up),
            )
        effective_target_pcts.append(enforced_target_unit * step)
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
                    if grid_charge > 1e-9 and not grid_charge_allowed:
                        continue
                    # SOC target is the ceiling of deliberate GRID
                    # replenishment only. Free PV remains allowed to fill
                    # the battery up to the physical max SOC.
                    if (minimum_soc_targets is not None
                            and grid_charge > 1e-9
                            and next_unit > max(current_unit, requested_target_unit)):
                        continue
                    grid_load = deficit
                    pv_export = max(0.0, surplus-pv_to_bat) if sell_price > 0 else 0.0
                    pv_curtail = max(0.0, surplus-pv_to_bat-pv_export)
                    battery_discharge = 0.0
                    battery_charge_internal = max(0.0, delta)
                    # SOC is intentionally quantized to 0.25%, while physical
                    # PV energy is continuous. Below target the sub-step
                    # remainder still belongs to the battery; it must never be
                    # reclassified as export merely because it cannot advance
                    # the displayed SOC state by a complete step.
                    if (minimum_soc_targets is not None
                            and next_unit < last_unit
                            and pv_export > 1e-9):
                        fractional_pv = pv_export
                        pv_to_bat += fractional_pv
                        battery_charge_internal += fractional_pv * eta_c
                        pv_export = 0.0
                        pv_curtail = 0.0
                else:
                    battery_discharge = -delta
                    delivered = battery_discharge * eta_d
                    battery_to_load = min(deficit, delivered)
                    battery_sell = max(0.0, delivered-battery_to_load)
                    if battery_sell > 1e-9 and not battery_sale_allowed:
                        continue
                    if battery_sell > 1e-9 and current_energy-battery_sell/eta_d < capacity*floor_pct/100-1e-9:
                        continue
                    grid_load = max(0.0, deficit-battery_to_load)
                    pv_export = surplus if sell_price > 0 else 0.0
                    pv_curtail = max(0.0, surplus-pv_export)
                    battery_charge_internal = 0.0
                # Grid energy that is physically unavoidable after PV and all
                # battery energy available above the technical reserve have
                # been exhausted is a residual flow, not an EMS BUY decision.
                # Reject only *voluntary* grid supply that preserves usable
                # battery energy.  This distinction keeps the horizon feasible
                # when a replan starts at minimum SOC while preserving the
                # contract that planned BUY exists only to charge the battery.
                usable_internal = min(
                    max_internal_discharge,
                    max(0.0, current_energy - first_unit * unit_kwh),
                )
                unavoidable_grid_load = max(
                    0.0, deficit - usable_internal * eta_d,
                )
                voluntary_grid_load = max(0.0, grid_load - unavoidable_grid_load)
                # A single SOC quantum can leave a few Wh of numerical
                # residual load: discharging one more step would become an
                # illegal battery export. A token SOC step must never unlock
                # a whole slot of grid-supplied native load. During a real
                # battery charge the planned battery input must be at least as
                # large as the voluntary part of grid supply to the house.
                if grid_charge > 1e-9:
                    if voluntary_grid_load > grid_charge + 1e-9:
                        continue
                elif voluntary_grid_load > unit_kwh * eta_d + 1e-9:
                    continue
                # During deliberate export the complete slot, including the
                # native load, must close at or above both independent
                # contracts. ``soc_floor`` remains the inverter's sale-only
                # stop threshold. ``soc_target`` is not rewritten as floor;
                # it separately reserves the energy needed until the selected
                # PV/BUY replenishment. Only energy above max(floor, target)
                # is therefore available for deliberate battery export.
                if battery_sell > 1e-9:
                    sale_stop_pct = max(floor_pct, required_pct)
                    if next_unit * step < sale_stop_pct - 1e-9:
                        continue
                # SOC required is the continuous per-slot energy bridge.  It
                # is independent of the BUY charge ceiling and is enforced at
                # every slot, not only at replenishment-window boundaries.
                if required_soc_pcts is not None and next_unit < required_unit:
                    continue
                # Outside a BUY window the backward energy contract is a hard
                # feasibility boundary. It protects future native load while
                # still allowing ordinary consumption below the unrelated
                # sale floor. Inside BUY it becomes due at the window end.
                if (minimum_soc_targets is not None and hard_target_indices is not None
                        and index in hard_target_indices and next_unit < enforced_target_unit):
                    continue
                # Usable PV fills all physically reachable battery capacity
                # before any remaining PV surplus is exported. Grid power is
                # deliberately excluded from this reachability calculation.
                pv_only_up = int(math.floor(
                    min(max_internal_charge, surplus * eta_c) / unit_kwh + 1e-9))
                pv_reachable_target = min(last_unit, current_unit + pv_only_up)
                if (minimum_soc_targets is not None and pv_export > 1e-9
                        and next_unit < pv_reachable_target):
                    continue
                # At the end of a replenishment window the calculated energy
                # requirement must be present. Earlier slots in the same window
                # may share the charge according to price and power limits.
                if (minimum_soc_targets is not None and index in target_due_indices
                        and next_unit < enforced_target_unit):
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
                        "battery_charge_internal_kwh": battery_charge_internal,
                        "pv_export_kwh": pv_export, "pv_curtail_kwh": pv_curtail,
                        "slot_cost_pln": slot_cost})
        if not next_costs:
            raise RuntimeError(f"No feasible SOC state at horizon slot {index}")
        costs, predecessors = next_costs, predecessors+[next_predecessors]
    candidates = [(cost, unit) for unit, cost in costs.items() if unit >= terminal_unit]
    if not candidates:
        if not allow_terminal_shortfall:
            raise RuntimeError("No feasible terminal SOC state for complete horizon")
        # A shortfall may be published only when the caller has already
        # exhausted the safer retry without deliberate battery sales and has
        # verified that the horizon ends in the current local day.  Preserve
        # as much energy as physics permits; price is only a tie-breaker for
        # the highest reachable SOC state.
        best_reachable_unit = max(costs)
        candidates = [(costs[best_reachable_unit], best_reachable_unit)]
    objective, unit = min(candidates, key=lambda value: (value[0], -value[1]))
    flows = [None]*len(rows)
    for index in range(len(rows)-1, -1, -1):
        unit, flows[index] = predecessors[index][unit]
    return {"flows": flows, "objective_pln": round(objective, 6),
            "soc_step_pct": step, "terminal_soc_pct": terminal_unit*step,
            "achieved_terminal_soc_pct": unit*step,
            "terminal_shortfall_pct": max(0.0, (terminal_unit-unit)*step),
            "effective_target_pcts": effective_target_pcts}


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
                           target_cap_pct: float,
                           sale_floor_pct: list[float] | None = None,
                           terminal_soc_pct: float | None = None,
                           soc_step_pct: float = 0.25) -> tuple[list[float], list[float]]:
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
    soc_step = max(0.001, float(soc_step_pct))
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    floors = [reserve] * len(rows)
    targets = [reserve] * len(rows)

    replenishment = []
    for row in rows:
        load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
        pv_surplus = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0) - load)
        replenishment.append(
            strict_database_bool(row.get("buy_window", False), "buy_window")
            or pv_surplus > 1e-9)

    cluster_end = [None] * len(rows)
    index = 0
    while index < len(rows):
        if not replenishment[index]:
            index += 1
            continue
        end = index
        while end + 1 < len(rows) and replenishment[end + 1]:
            end += 1
        for member in range(index, end + 1):
            cluster_end[member] = end
        index = end + 1

    for index in range(len(rows)):
        floors[index] = min(floor_cap, max(
            reserve,
            float(sale_floor_pct[index]) if sale_floor_pct is not None else reserve,
        ))
        start = cluster_end[index] if cluster_end[index] is not None else index
        need_kwh = 0.0
        has_next_replenishment = False
        for future in range(start + 1, len(rows)):
            if replenishment[future]:
                has_next_replenishment = True
                break
            row = rows[future]
            load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                    + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
            pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
            deficit = max(0.0, load - pv) / discharge_efficiency
            surplus = max(0.0, pv - load) * charge_efficiency
            sale_internal = (max(0.0, float(flows[future].get("battery_sell_kwh") or 0.0))
                             / discharge_efficiency)
            need_kwh = max(
                0.0,
                need_kwh + deficit * (1.0 + uncertainty) + sale_internal
                - surplus * max(0.0, 1.0 - uncertainty),
            )
        base_pct = (reserve if has_next_replenishment or terminal_soc_pct is None
                    else max(reserve, min(target_cap, float(terminal_soc_pct))))
        raw_target = min(target_cap, base_pct + need_kwh / capacity * 100.0)
        # Target is an executable SOC ceiling.  The optimizer and inverter use
        # discrete SOC steps, therefore round the required energy upward to the
        # same step before validating or publishing it.
        targets[index] = min(
            target_cap,
            math.ceil(raw_target / soc_step - 1e-9) * soc_step,
        )
    return floors, targets


def backward_target_commitments(rows: list[dict], capacity_kwh: float,
                                reserve_pct: float, eta_c: float, eta_d: float,
                                uncertainty_weight: float, target_cap_pct: float,
                                terminal_soc_pct: float,
                                soc_step_pct: float = 0.25,
                                selected_buy_indices: set[int] | None = None,
                                max_charge_kw: float = 5.0,
                                slot_minutes: int = 15) -> dict:
    """Build the executable SOC contract before dispatch decisions.

    The pass walks the 15-minute table backwards.  Future PV surplus is
    reserved against future load before it may become flexible surplus.  A
    contiguous BUY window is a possible replenishment boundary, not an order
    to import.  The returned target is the minimum energy which must remain at
    the end of each slot; charging uses the same value as its upper ceiling.
    """
    if not rows:
        return {"targets": [], "due": [], "source": [], "reserved_pv_kwh": []}
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    reserve_kwh = capacity * reserve / 100.0
    cap = max(reserve, min(100.0, float(target_cap_pct)))
    terminal_kwh = capacity * max(reserve, min(cap, float(terminal_soc_pct))) / 100.0
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    step = max(0.001, float(soc_step_pct))
    buy = [strict_database_bool(row.get("buy_window", False), "buy_window") for row in rows]
    if selected_buy_indices is None:
        selected_buy_indices = {i for i, value in enumerate(buy) if value}
    selected_buy_indices = {i for i in selected_buy_indices if 0 <= i < len(rows) and buy[i]}
    # Only a BUY window actually selected by the economic dispatch is a
    # replenishment boundary. Merely being marked BUY is permission, not an
    # obligation to postpone energy until that (possibly expensive) window.
    selected_buy_starts = {
        i for i in selected_buy_indices
        if i == 0 or i - 1 not in selected_buy_indices
    }
    selected_buy_end = {}
    for start in selected_buy_starts:
        end = start
        while end + 1 in selected_buy_indices:
            end += 1
        selected_buy_end[start] = end

    targets = [reserve] * len(rows)
    due = [None] * len(rows)
    source = ["HORIZON"] * len(rows)
    reserved_pv = [0.0] * len(rows)
    required_after = terminal_kwh
    next_due = rows[-1].get("slot_end") or rows[-1].get("slot_start")
    next_source = "HORIZON"
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        raw_pct = required_after / capacity * 100.0
        targets[i] = min(cap, math.ceil(raw_pct / step - 1e-9) * step)
        due[i] = next_due
        source[i] = next_source
        load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        deficit_internal = max(0.0, load - pv) / discharge_efficiency * (1.0 + uncertainty)
        surplus_internal = max(0.0, pv - load) * charge_efficiency * max(0.0, 1.0 - uncertainty)
        before_surplus = required_after + deficit_internal
        used_surplus = min(max(0.0, before_surplus - reserve_kwh), surplus_internal)
        reserved_pv[i] = used_surplus / charge_efficiency
        required_before = max(reserve_kwh, before_surplus - used_surplus)
        if i in selected_buy_starts:
            # BUY sets the due point for the remaining requirement, but must
            # not lower the ceiling in earlier slots. Earlier PV has priority
            # and may economically displace grid energy bought in this window.
            end = selected_buy_end[i]
            next_due = rows[end].get("slot_end") or rows[end].get("slot_start")
            next_source = "BUY"
            # A selected BUY is an executable replenishment boundary.  The
            # slots before it only need enough energy to reach that window;
            # they must not inherit the requirement for the rest of the
            # horizon.  The BUY slot itself keeps the full post-window target
            # calculated above and the optimizer validates it at window end.
            buy_capacity_internal = ((end - i + 1) * max(0.0, float(max_charge_kw))
                                     * max(1, int(slot_minutes)) / 60.0
                                     * charge_efficiency)
            required_before = max(reserve_kwh, required_before - buy_capacity_internal)
        elif (surplus_internal > 1e-9 and used_surplus > 1e-9
              and before_surplus - used_surplus <= reserve_kwh + 1e-9):
            # PV closes the bridge only when its conservative usable surplus
            # covers the complete remaining requirement.  Partial PV reduces
            # the target but cannot erase an unfunded remainder.
            next_due = row.get("slot_end") or row.get("slot_start")
            next_source = "PV"
            required_before = reserve_kwh
        required_after = required_before
    # ``target`` has two deliberately separate executor meanings: it is the
    # charge ceiling in every slot and becomes a hard minimum only at the end
    # of a selected replenishment window.  Before such a BUY, usable PV must
    # be allowed to displace the future grid charge.  Give all preceding rows
    # assigned to that BUY the window's final ceiling; do not turn it into an
    # earlier minimum (``target_due_indices`` controls that independently).
    for start in selected_buy_starts:
        buy_target = targets[start]
        buy_due = rows[selected_buy_end[start]].get("slot_end") or rows[selected_buy_end[start]].get("slot_start")
        for i in range(start - 1, -1, -1):
            if source[i] != "BUY" or due[i] != buy_due:
                break
            targets[i] = max(targets[i], buy_target)
    # PV is a replenishment *window*, not a single late slot.  The largest SOC
    # requirement reached within one continuous daylight block is its charge
    # ceiling from the first forecast PV.  Split at an actually selected BUY,
    # because grid replenishment starts a new energy bridge.  Without this
    # pass, early morning surplus is exported while a later slot in the same
    # PV window still carries the energy requirement for the evening/night.
    window_start = None
    for boundary in range(len(rows) + 1):
        pv_active = (boundary < len(rows)
                     and float(rows[boundary].get("forecast_pv_total_kwh") or 0.0) > 1e-9
                     and boundary not in selected_buy_starts)
        if pv_active and window_start is None:
            window_start = boundary
        if window_start is not None and not pv_active:
            window_end = boundary - 1
            window_target = max(targets[window_start:window_end + 1])
            for index in range(window_start, window_end + 1):
                targets[index] = max(targets[index], window_target)
            window_start = None

    # When a later PV slot closes the backward bridge, preceding non-PV rows
    # assigned to that due point inherit the already propagated window ceiling.
    due_index = {
        row.get("slot_end") or row.get("slot_start"): i
        for i, row in enumerate(rows)
    }
    for i in range(len(rows)):
        if source[i] != "PV" or due[i] not in due_index:
            continue
        closing_index = due_index[due[i]]
        targets[i] = max(targets[i], targets[closing_index])
    return {"targets": targets, "due": due, "source": source,
            "reserved_pv_kwh": reserved_pv}


def build_soc_contracts(rows: list[dict], economic_flows: list[dict],
                        capacity_kwh: float, reserve_pct: float,
                        eta_c: float, eta_d: float,
                        uncertainty_weight: float, terminal_soc_pct: float,
                        target_cap_pct: float,
                        soc_step_pct: float = 0.25,
                        daily_terminal_soc_pcts: list[float] | None = None) -> dict:
    """Build independent required, charge-target and sale contracts.

    The economic pass supplies an exact grid-energy allocation.  The backward
    pass subtracts only that allocated energy, never the theoretical capacity
    of a BUY window.  Therefore a replenishment boundary cannot reset an
    unfunded requirement to the technical reserve.
    """
    if len(rows) != len(economic_flows):
        raise ValueError("SOC_CONTRACT_FLOW_LENGTH_MISMATCH")
    if not rows:
        return {"required": [], "charge_targets": [], "due": [],
                "source": [], "reserved_pv_kwh": [], "buy_due_indices": set()}
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    cap = max(reserve, min(100.0, float(target_cap_pct)))
    reserve_kwh = capacity * reserve / 100.0
    required_after = capacity * max(
        reserve, min(cap, float(terminal_soc_pct))) / 100.0
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    step = max(0.001, float(soc_step_pct))
    required = [reserve] * len(rows)
    charge_targets = [reserve] * len(rows)
    due = [None] * len(rows)
    source = ["HORIZON"] * len(rows)
    reserved_pv = [0.0] * len(rows)

    selected_buy = {
        i for i, flow in enumerate(economic_flows)
        if float(flow.get("grid_charge_kwh") or 0.0) > 1e-9
    }
    buy_due_indices = {
        i for i in selected_buy
        if i + 1 == len(rows) or i + 1 not in selected_buy
    }
    next_due = rows[-1].get("slot_end") or rows[-1].get("slot_start")
    next_source = "HORIZON"
    for i in range(len(rows) - 1, -1, -1):
        if daily_terminal_soc_pcts is not None:
            required_after = max(
                required_after,
                capacity * max(reserve, min(
                    cap, float(daily_terminal_soc_pcts[i]))) / 100.0,
            )
        raw_required_pct = required_after / capacity * 100.0
        flow = economic_flows[i]
        # The economic trajectory is a proven reachable path.  Add back only
        # its optional battery sale to obtain the highest no-sale SOC that is
        # physically reachable at this point.  A backward contract may not
        # demand more than that state; doing so would fail the constrained
        # pass before the next BUY/PV opportunity could provide the energy.
        feasible_no_sale_pct = min(
            cap,
            float(flow.get("soc_end_pct") or reserve)
            + (max(0.0, float(flow.get("battery_sell_kwh") or 0.0))
               / discharge_efficiency / capacity * 100.0),
        )
        rounded_required = math.ceil(raw_required_pct / step - 1e-9) * step
        reachable_required = math.floor(
            feasible_no_sale_pct / step + 1e-9) * step
        required[i] = max(reserve, min(cap, rounded_required,
                                       reachable_required))
        due[i] = next_due
        source[i] = next_source
        row = rows[i]
        # The economic pass has already closed the physical balance.  Build
        # the mandatory bridge from its exact battery flows, not from the raw
        # load/PV deficit.  Recounting the complete deficit here also counted
        # unavoidable grid supply after the battery reached reserve and could
        # demand an SOC that was impossible to reach in the current slot.
        # Deliberate battery sale is intentionally excluded: it is optional
        # energy above the contract, never a reason to raise required SOC.
        native_discharge_internal = (
            max(0.0, float(flow.get("battery_to_load_kwh") or 0.0))
            / discharge_efficiency * (1.0 + uncertainty)
        )
        pv_internal = (
            max(0.0, float(flow.get("pv_to_bat_kwh") or 0.0))
            * charge_efficiency * max(0.0, 1.0 - uncertainty)
        )
        grid_internal = (max(0.0, float(flow.get("grid_charge_kwh") or 0.0))
                         * charge_efficiency)
        before_supply = required_after + native_discharge_internal
        used_pv = min(max(0.0, before_supply - reserve_kwh), pv_internal)
        reserved_pv[i] = used_pv / charge_efficiency
        required_before = max(
            reserve_kwh, before_supply - used_pv - grid_internal)
        required_after = required_before
        if i in buy_due_indices:
            next_due = row.get("slot_end") or row.get("slot_start")
            next_source = "BUY"
        elif used_pv > 1e-9:
            next_due = row.get("slot_end") or row.get("slot_start")
            next_source = "PV"

    for i, flow in enumerate(economic_flows):
        economic_end = float(flow.get("soc_end_pct") or reserve)
        if i in selected_buy:
            charge_targets[i] = min(cap, max(required[i], economic_end))
        else:
            charge_targets[i] = required[i]
    return {"required": required, "charge_targets": charge_targets,
            "due": due, "source": source, "reserved_pv_kwh": reserved_pv,
            "buy_due_indices": buy_due_indices,
            "selected_buy_indices": selected_buy}


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


def hp_heating_window_indices(rows: list[dict], day_value) -> set[int]:
    """Return slots allowed for automatic Heat+DHW on one local day."""
    day_start = datetime.combine(day_value, datetime.min.time())
    earliest_start = day_start + timedelta(hours=7)
    midday = day_start + timedelta(hours=12)
    latest_end = day_start + timedelta(hours=19)
    morning_sell_ends = [
        row.get("slot_end") or (row["slot_start"] + timedelta(minutes=15))
        for row in rows
        if row["slot_start"] < midday
        and strict_database_bool(row.get("sale_window", False), "sale_window")
    ]
    evening_sell_starts = [
        row["slot_start"] for row in rows
        if midday <= row["slot_start"] < latest_end
        and strict_database_bool(row.get("sale_window", False), "sale_window")
    ]
    window_start = max(earliest_start, max(morning_sell_ends)) \
        if morning_sell_ends else earliest_start
    window_end = min(latest_end, min(evening_sell_starts)) \
        if evening_sell_starts else latest_end
    return {
        index for index, row in enumerate(rows)
        if window_start <= row["slot_start"] < window_end
        and not strict_database_bool(row.get("sale_window", False), "sale_window")
    }


def hp_temperature_eligible(night_min: float | None, sample_count: int,
                            threshold: float, minimum_samples: int = 3) -> bool:
    """Qualify HP heating from actual 00:00-06:00 garden readings."""
    return (night_min is not None
            and int(sample_count) >= int(minimum_samples)
            and float(night_min) < float(threshold))


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
        started_monotonic = time.monotonic()
        planning_budget = max(10.0, min(600.0, float(
            OPTIONS.get("planner_deadline_seconds", 120.0))))

        def ensure_deadline(stage: str) -> None:
            if time.monotonic() - started_monotonic > planning_budget:
                raise RuntimeError(f"PLANNER_DEADLINE_EXCEEDED:{stage}:{planning_budget:.0f}s")

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
        fixed_forecast_correction = uncertainty_weight if bool(
            OPTIONS.get("forecast_fixed_corrections_enabled", False)) else 0.0
        floor_cap = max(reserve, min(100.0, float(OPTIONS.get("soc_floor_max_pct", 90.0))))
        target_cap = max(floor_cap, min(100.0, float(OPTIONS.get("soc_target_max_pct", 100.0))))
        flow_threshold = max(0.0, float(OPTIONS.get("planned_flow_threshold_kwh", 0.02)))
        technical_threshold = max(
            flow_threshold, float(OPTIONS.get("technical_flow_threshold_kwh", 0.05)))
        max_kw = max(0.25, float(OPTIONS.get("battery_max_power_kw", 5.0)))
        run_id = str(uuid.uuid4())
        hp_shortfalls = []
        tou_programs = planning_tou_programs(
            tou_program_snapshot(), deye_program_soc_baselines(OPTIONS))
        with db() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM ems_gpt_slots
              WHERE slot_start>=%s AND actual_recorded_at IS NULL
                AND price_source='PSE_API' AND price_buy_pln_kwh IS NOT NULL AND price_sell_pln_kwh IS NOT NULL
              ORDER BY slot_start""", (cutoff,))
            source = list(cur.fetchall())
            if not source:
                raise RuntimeError("No open forecast rows for planner horizon")
            missing_load = [str(row["slot_start"]) for row in source
                            if row.get("forecast_load_kwh") is None]
            if missing_load:
                raise RuntimeError(
                    f"MISSING_LOAD_FORECAST:{len(missing_load)}:{missing_load[0]}")
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
            # Re-evaluate durable window flags over the exact continuous
            # horizon used by this run. This also repairs flags produced before
            # the complete next-day RCE horizon was available.
            terminal_baseline = max(
                [float(program["soc"]) for program in tou_programs] or [reserve]
            )
            minimum_buy_slots = max(1, math.ceil(
                capacity * max(0.0, terminal_baseline - reserve) / 100.0
                / max(0.001, max_kw * int(OPTIONS["slot_minutes"]) / 60.0 * eta_c)
            ))
            price_windows = derive_price_windows(
                [{"sell": row["price_sell_pln_kwh"],
                  "buy": row["price_buy_pln_kwh"]} for row in source],
                eta_c, eta_d, degradation, min_margin,
                max(0.0, float(OPTIONS.get("buy_window_tolerance_pln_kwh", 0.05))),
                minimum_buy_slots)
            normalized_source = []
            for row, (sale_window, buy_window) in zip(source, price_windows):
                if bool(sale_window) and bool(buy_window):
                    raise RuntimeError(f"WINDOW_OVERLAP:{row['slot_start']}")
                work = dict(row)
                work["sale_window"], work["buy_window"] = sale_window, buy_window
                work["market_window"] = "SELL" if sale_window else ("BUY" if buy_window else "NEUTRAL")
                normalized_source.append(work)
                cur.execute("""UPDATE ems_gpt_slots SET sale_window=%s,buy_window=%s,market_window=%s
                  WHERE slot_start=%s AND actual_recorded_at IS NULL""",
                  (sale_window, buy_window, work["market_window"], row["slot_start"]))
            source = normalized_source
            cur.execute("""INSERT INTO ems_gpt_plan_runs
              (run_id,plan_day,run_type,stage_version,expected_slots,status,current_stage,created_at,updated_at)
              VALUES(%s,%s,%s,%s,%s,'RUNNING','RCE_RAW',NOW(6),NOW(6))""",
              (run_id, cutoff.date(), run_type, "CORE_0_34_0", len(source)))
            stage_columns = [
                "slot_start","slot_end","slot_id","slot_start_utc","slot_start_local","utc_offset_minutes",
                "local_fold","local_day","slot_index_local","price_sell_pln_kwh","price_buy_pln_kwh","price_source",
                "price_fetched_at","sale_window","buy_window","market_window","forecast_pv1_kwh","forecast_pv2_kwh",
                "forecast_pv_total_kwh","forecast_load_kwh","forecast_temperature_c",
                "forecast_cloud_coverage_pct","forecast_precipitation_mm","pv_correction",
                "load_correction","forecast_pv_source","planned_sell_kwh",
                "heat_pump_window",
            ]
            insert_cols = ["run_id"] + stage_columns
            placeholders = ",".join(["%s"] * len(insert_cols))
            for row in source:
                # Every run starts fail-closed. Never inherit an HP window from
                # the previously published plan before re-evaluating temperature.
                values = [run_id] + [0 if c == "heat_pump_window" else row.get(c) for c in stage_columns]
                cur.execute(f"INSERT INTO ems_gpt_plan_stage_rows ({','.join(insert_cols)}) VALUES({placeholders})", values)
            audit_stage(cur, run_id, "RCE_RAW", "OK", len(source), "durable prices copied")
            ensure_deadline("RCE_RAW")
            audit_stage(cur, run_id, "FORECAST", "OK", len(source), "PV/LOAD forecast copied from durable inputs")
            cur.execute("""UPDATE ems_gpt_plan_stage_rows SET
              grid_window=CASE WHEN COALESCE(sale_window,0)=1 THEN 'NO_BUY'
                WHEN COALESCE(buy_window,0)=1 THEN 'BUY_ALLOWED' ELSE 'NEUTRAL' END,
              windows_updated_at=NOW(6) WHERE run_id=%s""", (run_id,))
            audit_stage(cur, run_id, "WINDOWS", "OK", len(source))
            ensure_deadline("WINDOWS")
            cur.execute("SELECT * FROM ems_gpt_plan_stage_rows WHERE run_id=%s ORDER BY slot_start", (run_id,))
            rows = list(cur.fetchall())
            # Learn recurring DHW demand independently from native household
            # load. Weekdays and weekends have different schedules, while the
            # exact quarter-hour profile lets the 06:00 cycle raise the bridge
            # target before it starts instead of reacting after SOC falls.
            history_cutoff = now - timedelta(days=30)
            cur.execute("""SELECT CASE WHEN WEEKDAY(slot_start)<5 THEN 0 ELSE 1 END day_type,
              HOUR(slot_start) hour_no,MINUTE(slot_start) minute_no,
              AVG(COALESCE(actual_dhw_consumed_kwh,0)) mean_dhw_kwh,COUNT(*) sample_count
              FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s
                AND actual_recorded_at IS NOT NULL AND plan_published=1
                AND execution_reason LIKE 'CORE_TELEMETRY_%%'
              GROUP BY day_type,hour_no,minute_no""", (history_cutoff, now))
            dhw_profile = {
                (int(value["day_type"]), int(value["hour_no"]), int(value["minute_no"])):
                    float(value["mean_dhw_kwh"] or 0.0)
                for value in cur.fetchall() if int(value["sample_count"] or 0) >= 2
            }
            # Use the actual garden temperature recorded by HA between 00:00 and
            # 06:00. Three samples tolerate a partial HA outage without allowing
            # Open-Meteo forecasts to decide whether space heating is required.
            # The operator setting from the EMS panel is authoritative.
            night_threshold = float(OPTIONS.get("night_heating_threshold_c", 10.0))
            day_values = sorted({row.get("local_day") or row["slot_start"].date() for row in rows})
            night_min_by_day = {}
            if day_values:
                markers = ",".join(["%s"] * len(day_values))
                cur.execute(f"""SELECT local_day,MIN(actual_temperature_c) night_min,
                  COUNT(actual_temperature_c) sample_count
                  FROM ems_gpt_slots WHERE local_day IN ({markers})
                    AND TIME(slot_start)>='00:00:00' AND TIME(slot_start)<'06:00:00'
                    AND actual_temperature_c IS NOT NULL GROUP BY local_day""", tuple(day_values))
                night_min_by_day = {
                    str(value["local_day"]): {
                        "night_min": float(value["night_min"]),
                        "sample_count": int(value["sample_count"] or 0),
                    }
                    for value in cur.fetchall()
                }

            required_slots = max(1, int(float(OPTIONS.get("hp_min_heating_hours", 10.0)) * 4 + 0.999999))
            min_cycle_slots = max(1, int(float(OPTIONS.get("hp_min_cycle_hours", 2.0)) * 4 + 0.999999))
            min_gap_slots = max(1, int(float(OPTIONS.get("hp_min_cycle_break_hours", 1.0)) * 4 + 0.999999))
            max_gap_slots = max(min_gap_slots, int(float(OPTIONS.get("hp_max_cycle_break_hours", 3.0)) * 4 + 0.999999))
            hp_fallback_kw = max(0.0, float(OPTIONS.get("hp_planned_power_kw", 1.5)))
            hp_history_start = min(day_values) - timedelta(days=28) if day_values else now.date()
            cur.execute("""SELECT local_day,actual_heating_consumed_kwh
              FROM ems_gpt_slots WHERE local_day>=%s AND local_day<%s
                AND actual_recorded_at IS NOT NULL AND plan_published=1
                AND execution_reason LIKE 'CORE_TELEMETRY_%%'
                AND actual_heating_consumed_kwh>0.02""",
                (hp_history_start, min(day_values) if day_values else now.date()))
            hp_heating_history = list(cur.fetchall())
            hp_history_weights = {
                7: float(OPTIONS.get("soc_target_history_weight_7d_pct", 50.0)),
                14: float(OPTIONS.get("soc_target_history_weight_14d_pct", 25.0)),
                28: float(OPTIONS.get("soc_target_history_weight_28d_pct", 25.0)),
            }
            planned_hp_kw_by_day = {}
            for day_value in day_values:
                hp_power = historical_hp_power_kw(
                    hp_heating_history, day_value, hp_history_weights, hp_fallback_kw)
                planned_hp_kw_by_day[day_value] = max(0.0, float(hp_power["power_kw"]))
                record_event("historical_hp_power_forecast", "planner", {
                    "planning_day": str(day_value),
                    "planned_power_kw": round(planned_hp_kw_by_day[day_value], 3),
                    **hp_power,
                })
            cycle_penalty = max(0.0, float(OPTIONS.get("hp_cycle_start_penalty_pln", 0.25)))
            hp_selected_indices = set()
            for day_value in day_values:
                planned_hp_kw = planned_hp_kw_by_day[day_value]
                day_key = str(day_value)
                night_forecast = night_min_by_day.get(day_key) or {}
                if not hp_temperature_eligible(
                        night_forecast.get("night_min"),
                        int(night_forecast.get("sample_count") or 0),
                        night_threshold):
                    continue
                indexed_rows = [(index, row) for index, row in enumerate(rows)
                                if (row.get("local_day") or row["slot_start"].date()) == day_value]
                day_start = datetime.combine(day_value, datetime.min.time())
                # Planning remains independent from PPD and operator overrides.
                # For elapsed slots the planner assumes its own published plan
                # was executed.  Differences belong to execution analytics and
                # must never feed a PPD decision back into the next plan.
                cur.execute("""SELECT heat_pump_window FROM ems_gpt_slots
                  WHERE slot_start>=%s AND slot_start<%s
                  ORDER BY slot_start""",
                  (day_start, min(cutoff, day_start + timedelta(days=1))))
                past_states = [strict_database_bool(value.get("heat_pump_window"),
                                                    "heat_pump_window")
                               for value in cur.fetchall()]
                day_rows = [row for _, row in indexed_rows]
                allowed_local = hp_heating_window_indices(day_rows, day_value)
                allowed_rows = [row for index, row in enumerate(day_rows)
                                if index in allowed_local]
                selected_allowed = optimize_hp_heating_slots(
                    allowed_rows, past_states, required_slots,
                    min_cycle_slots, min_gap_slots, max_gap_slots, planned_hp_kw, cycle_penalty)
                allowed_positions = sorted(allowed_local)
                selected_local = {allowed_positions[index] for index in selected_allowed}
                hp_selected_indices.update(indexed_rows[index][0] for index in selected_local)
                delivered_slots = sum(past_states) + len(selected_local)
                if delivered_slots < required_slots:
                    hp_shortfalls.append({"day": day_key, "required_slots": required_slots,
                                          "scheduled_slots": delivered_slots,
                                          "missing_hours": round((required_slots-delivered_slots)/4, 2)})

            horizon_rows, sale_constraints, tou_by_index = [], [], []
            for index, row in enumerate(rows):
                work = dict(row)
                # From this boundary onward the planner sees only real bools,
                # regardless of how current or historical MariaDB rows encode
                # BOOLEAN/TINYINT values.
                work["buy_window"] = strict_database_bool(row.get("buy_window"), "buy_window")
                work["sale_window"] = strict_database_bool(row.get("sale_window"), "sale_window")
                work["forecast_load_kwh"] = max(0.0, float(row.get("forecast_load_kwh") or 0.0)*(1+0.10*fixed_forecast_correction))
                work["forecast_pv_total_kwh"] = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0)*(1-0.10*fixed_forecast_correction))
                slot_time = row.get("slot_start_local") or row["slot_start"]
                key = (0 if slot_time.weekday() < 5 else 1, slot_time.hour, slot_time.minute)
                historical_dhw_kwh = max(0.0, dhw_profile.get(key, 0.0))
                row_day = row.get("local_day") or row["slot_start"].date()
                planned_heating_kwh = (planned_hp_kw_by_day.get(row_day, hp_fallback_kw) * 0.25
                                       if index in hp_selected_indices else 0.0)
                work["forecast_heat_pump_load_kwh"] = max(
                    planned_heating_kwh, historical_dhw_kwh)
                work["forecast_heat_pump_dhw_load_kwh"] = historical_dhw_kwh
                cur.execute("""UPDATE ems_gpt_plan_stage_rows SET
                  forecast_heat_pump_load_kwh=%s,forecast_heat_pump_dhw_load_kwh=%s
                  WHERE run_id=%s AND slot_start=%s""",
                  (round(work["forecast_heat_pump_load_kwh"], 6),
                   round(historical_dhw_kwh, 6), run_id, row["slot_start"]))
                horizon_rows.append(work)
                program = active_tou_program(row["slot_start"], tou_programs)
                tou_by_index.append(program)
                # The TOU program SOC is an executor baseline, not a planning
                # floor.  Deliberate export is allowed only in SELL windows;
                # its exact stop SOC is derived from the accepted flow below.
                sale_constraints.append(reserve if work["sale_window"] else 100.0)
            planning_days = sorted({row.get("local_day") or row["slot_start"].date()
                                    for row in horizon_rows})
            terminal_day = planning_days[-1]
            history_start = planning_days[0] - timedelta(days=28)
            cur.execute("""SELECT local_day,
              SUBSTRING_INDEX(GROUP_CONCAT(soc_end_pct ORDER BY slot_start DESC),',',1) soc_end_pct,
              COUNT(*) slot_count,SUM(actual_recorded_at IS NOT NULL) terminal_count,
              SUM(actual_mode='MISSING_OUTAGE') missing_count
              FROM ems_gpt_slots
              WHERE local_day>=%s AND local_day<%s
              GROUP BY local_day
              HAVING slot_count IN (92,96,100) AND terminal_count=slot_count
                AND missing_count=0 AND soc_end_pct IS NOT NULL
              ORDER BY local_day""", (history_start, terminal_day))
            closing_history = list(cur.fetchall())
            history_weights = {
                    7: float(OPTIONS.get("soc_target_history_weight_7d_pct", 50.0)),
                    14: float(OPTIONS.get("soc_target_history_weight_14d_pct", 25.0)),
                    28: float(OPTIONS.get("soc_target_history_weight_28d_pct", 25.0)),
                }
            daily_terminal_soc = {}
            for planning_day in planning_days:
                terminal_history = historical_terminal_soc(
                    closing_history, planning_day, history_weights, reserve)
                daily_terminal_soc[planning_day] = max(
                    reserve, min(target_cap, float(terminal_history["soc_pct"])))
                record_event("historical_terminal_soc_forecast", "planner", {
                    "terminal_day": str(planning_day),
                    "terminal_soc_pct": round(daily_terminal_soc[planning_day], 3),
                    **terminal_history,
                })
            terminal_soc = daily_terminal_soc[terminal_day]
            daily_required_soc = [reserve] * len(horizon_rows)
            for index, row in enumerate(horizon_rows):
                row_day = row.get("local_day") or row["slot_start"].date()
                next_day = ((horizon_rows[index + 1].get("local_day")
                             or horizon_rows[index + 1]["slot_start"].date())
                            if index + 1 < len(horizon_rows) else None)
                if next_day != row_day:
                    daily_required_soc[index] = daily_terminal_soc[row_day]
            daily_required_soc, morning_protected_indices = morning_sale_soc_requirements(
                horizon_rows, daily_required_soc, daily_terminal_soc)
            for index in morning_protected_indices:
                record_event("morning_sale_soc_protected", "planner", {
                    "slot_start": str(horizon_rows[index]["slot_start"]),
                    "required_soc_pct": round(daily_required_soc[index], 3),
                    "sale_slot_start": str(horizon_rows[index + 1]["slot_start"]),
                })
            audit_stage(cur,run_id,"WINDOW_CANDIDATES","OK",len(rows),"pass 1: full horizon")
            audit_stage(cur,run_id,"LOAD","OK",len(rows),"pass 2: native and controllable load")
            audit_stage(cur,run_id,"PV","OK",len(rows),"pass 3: corrected PV balance")
            ensure_deadline("PV")
            # Pass 4: economics chooses exact grid-energy allocations.  Pass
            # 5 converts those kWh into independent continuous SOC contracts
            # and validates one constrained dispatch.  No BUY-path feedback
            # loop is allowed to redefine or reset the energy bridge.
            optimized_floors = list(sale_constraints)
            internal_soc_step=0.10
            daily_close_indices = {
                index for index, value in enumerate(daily_required_soc)
                if value > reserve + 0.01
            }
            enforced_daily_required = list(daily_required_soc)
            waived_daily_closes = set()
            battery_sales_enabled = True
            terminal_shortfall_allowed = False
            while True:
                try:
                    economic_optimization = optimize_energy_horizon(
                        horizon_rows,soc_now,capacity,reserve,eta_c,eta_d,
                        degradation,min_margin,max_kw,int(OPTIONS["slot_minutes"]),
                        optimized_floors,terminal_soc,internal_soc_step,target_cap,
                        required_soc_pcts=enforced_daily_required,
                        battery_sales_enabled=battery_sales_enabled,
                        allow_terminal_shortfall=terminal_shortfall_allowed)
                    break
                except RuntimeError as exc:
                    terminal_failure = (
                        str(exc) == "No feasible terminal SOC state for complete horizon"
                    )
                    if terminal_failure:
                        if battery_sales_enabled:
                            battery_sales_enabled = False
                            record_event("battery_sales_disabled_for_terminal_soc", "planner", {
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "reason": str(exc),
                            }, "WARNING")
                            continue
                        if terminal_day != local_now().date():
                            raise
                        terminal_shortfall_allowed = True
                        record_event("current_day_terminal_soc_best_effort", "planner", {
                            "terminal_day": str(terminal_day),
                            "requested_soc_pct": terminal_soc,
                            "reason": str(exc),
                        }, "WARNING")
                        continue
                    prefix = "No feasible SOC state at horizon slot "
                    if not str(exc).startswith(prefix):
                        raise
                    failed_index = int(str(exc)[len(prefix):])
                    if failed_index not in daily_close_indices:
                        raise
                    if battery_sales_enabled:
                        battery_sales_enabled = False
                        record_event("battery_sales_disabled_for_daily_soc", "planner", {
                            "slot_start": str(horizon_rows[failed_index]["slot_start"]),
                            "requested_soc_pct": daily_required_soc[failed_index],
                            "reason": str(exc),
                        }, "WARNING")
                        continue
                    enforced_daily_required[failed_index] = reserve
                    daily_close_indices.remove(failed_index)
                    waived_daily_closes.add(failed_index)
                    record_event("daily_terminal_soc_unreachable", "planner", {
                        "slot_start": str(horizon_rows[failed_index]["slot_start"]),
                        "requested_soc_pct": daily_required_soc[failed_index],
                        "reason": str(exc),
                    }, "WARNING")
            if economic_optimization.get("terminal_shortfall_pct", 0.0) > 1e-9:
                requested_terminal_soc = terminal_soc
                terminal_soc = float(economic_optimization["achieved_terminal_soc_pct"])
                record_event("current_day_terminal_soc_unreachable", "planner", {
                    "terminal_day": str(terminal_day),
                    "requested_soc_pct": requested_terminal_soc,
                    "achieved_soc_pct": terminal_soc,
                    "shortfall_pct": economic_optimization["terminal_shortfall_pct"],
                    "battery_sales_enabled": battery_sales_enabled,
                }, "WARNING")

            def optimize_remaining_pass(
                    pass_name: str, floors: list[float],
                    minimum_targets: list[float] | None = None,
                    hard_indices: set[int] | None = None,
                    due_indices: set[int] | None = None,
                    required_pcts: list[float] | None = None) -> dict:
                """Apply the terminal-SOC recovery contract to every later pass."""
                nonlocal battery_sales_enabled, terminal_shortfall_allowed, terminal_soc
                requested_terminal_soc = terminal_soc
                while True:
                    try:
                        result = optimize_energy_horizon(
                            horizon_rows,soc_now,capacity,reserve,eta_c,eta_d,
                            degradation,min_margin,max_kw,int(OPTIONS["slot_minutes"]),
                            floors,terminal_soc,internal_soc_step,target_cap,
                            minimum_targets,hard_indices,due_indices,required_pcts,
                            battery_sales_enabled=battery_sales_enabled,
                            allow_terminal_shortfall=terminal_shortfall_allowed)
                        break
                    except RuntimeError as exc:
                        if str(exc) != "No feasible terminal SOC state for complete horizon":
                            raise
                        if battery_sales_enabled:
                            battery_sales_enabled = False
                            record_event("battery_sales_disabled_for_terminal_soc", "planner", {
                                "stage": pass_name,
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "reason": str(exc),
                            }, "WARNING")
                            continue
                        if terminal_day != local_now().date():
                            raise
                        terminal_shortfall_allowed = True
                        record_event("current_day_terminal_soc_best_effort", "planner", {
                            "stage": pass_name,
                            "terminal_day": str(terminal_day),
                            "requested_soc_pct": terminal_soc,
                            "reason": str(exc),
                        }, "WARNING")
                if result.get("terminal_shortfall_pct", 0.0) > 1e-9:
                    terminal_soc = float(result["achieved_terminal_soc_pct"])
                    record_event("current_day_terminal_soc_unreachable", "planner", {
                        "stage": pass_name,
                        "terminal_day": str(terminal_day),
                        "requested_soc_pct": requested_terminal_soc,
                        "achieved_soc_pct": terminal_soc,
                        "shortfall_pct": result["terminal_shortfall_pct"],
                        "battery_sales_enabled": battery_sales_enabled,
                    }, "WARNING")
                return result
            # Convert the immutable TOU baselines into sale-only safety floors.
            # Iterate once after applying them because a permitted 5/6 bridge
            # override is valid only while the resulting plan retains its real
            # near-term battery BUY.
            for _ in range(2):
                guarded_floors = tou_sale_safety_floors(
                    horizon_rows, tou_by_index, economic_optimization["flows"],
                    capacity, reserve, eta_d, uncertainty_weight,
                    int(OPTIONS.get("tou_bridge_override_max_minutes", 180)),
                    int(OPTIONS["slot_minutes"]), enforced_daily_required)
                if all(abs(a - b) < 1e-9
                       for a, b in zip(guarded_floors, optimized_floors)):
                    break
                optimized_floors = guarded_floors
                economic_optimization = optimize_remaining_pass(
                    "TOU_GUARD_ITERATION", optimized_floors,
                    required_pcts=enforced_daily_required)
            final_guarded = tou_sale_safety_floors(
                horizon_rows, tou_by_index, economic_optimization["flows"],
                capacity, reserve, eta_d, uncertainty_weight,
                int(OPTIONS.get("tou_bridge_override_max_minutes", 180)),
                int(OPTIONS["slot_minutes"]), enforced_daily_required)
            if any(abs(a - b) >= 1e-9
                   for a, b in zip(final_guarded, optimized_floors)):
                # A BUY must never validate its own relaxation circularly. If
                # the two-pass result is not stable, retain the stricter floor.
                optimized_floors = [max(a, b)
                                    for a, b in zip(final_guarded, optimized_floors)]
                economic_optimization = optimize_remaining_pass(
                    "TOU_GUARD_FINAL", optimized_floors,
                    required_pcts=enforced_daily_required)
            commitment=build_soc_contracts(
                horizon_rows,economic_optimization["flows"],capacity,reserve,
                eta_c,eta_d,uncertainty_weight,terminal_soc,target_cap,0.25,
                enforced_daily_required)
            required_soc=list(commitment["required"])
            charge_targets=list(commitment["charge_targets"])
            for index in waived_daily_closes:
                charge_targets[index] = max(
                    charge_targets[index], daily_required_soc[index])
            target_due_indices=set(commitment["buy_due_indices"])
            selected_buy_indices=set(commitment["selected_buy_indices"])
            optimization=optimize_remaining_pass(
                "TARGET_COMMITMENT", optimized_floors, charge_targets, set(),
                target_due_indices, required_soc)
            targets=list(optimization.get("effective_target_pcts",charge_targets))
            audit_stage(cur,run_id,"TARGET_COMMITMENT","OK",len(rows),
                        f"deterministic SOC contracts; selected_buy_slots={len(selected_buy_indices)}")
            record_event("daily_plan_variant_selected","planner",{
                "run_id":run_id,"terminal_soc_pct":round(terminal_soc,3),
                "selected":"DETERMINISTIC_SOC_CONTRACT_V4",
                "economic_net_pln":-float(economic_optimization["objective_pln"]),
                "constrained_net_pln":-float(optimization["objective_pln"]),
            })
            ensure_deadline("TARGET_COMMITMENT")
            ensure_deadline("DISPATCH")
            bridge_floors = [reserve] * len(horizon_rows)
            for index, flow in enumerate(optimization["flows"]):
                row = horizon_rows[index]
                battery_sell = float(flow.get("battery_sell_kwh") or 0.0)
                grid_charge = float(flow.get("grid_charge_kwh") or 0.0)
                soc_end = float(flow.get("soc_end_pct") or 0.0)
                if (battery_sell > flow_threshold
                        and not strict_database_bool(row.get("sale_window"), "sale_window")):
                    raise RuntimeError(f"SALE_OUTSIDE_WINDOW:{index}")
                if (float(flow.get("battery_sell_kwh") or 0.0) > flow_threshold
                        and float(flow.get("soc_end_pct") or 0.0) + 1e-9
                        < optimized_floors[index]):
                    raise RuntimeError(
                        f"SALE_FLOOR_VIOLATION:{index}:"
                        f"{flow.get('soc_end_pct')}<{optimized_floors[index]}")
                if (battery_sell > flow_threshold
                        and soc_end + 0.01 < required_soc[index]):
                    raise RuntimeError(
                        f"SALE_REQUIRED_SOC_VIOLATION:{index}:"
                        f"{soc_end}<{required_soc[index]}")
                if grid_charge > flow_threshold and soc_end > targets[index] + 0.01:
                    raise RuntimeError(
                        f"BUY_TARGET_EXCEEDED:{index}:{soc_end}>{targets[index]}")
                if index in target_due_indices and soc_end + 0.01 < targets[index]:
                    raise RuntimeError(
                        f"SOC_TARGET_NOT_REACHED:{index}:{soc_end}<{targets[index]}")
                if soc_end + 0.01 < required_soc[index]:
                    raise RuntimeError(
                        f"SOC_REQUIRED_VIOLATION:{index}:"
                        f"{soc_end}<{required_soc[index]}")
                load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                        + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
                pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
                delivered_battery = (
                    max(0.0, float(flow.get("battery_to_load_kwh") or 0.0))
                    + battery_sell)
                # Core battery/load balance excludes flexible PV surplus.  PV
                # export/CWU/EV/curtailment are allocated and validated in the
                # later FLEX_SURPLUS pass.
                pv_core = min(pv, load + max(0.0, float(flow.get("pv_to_bat_kwh") or 0.0)))
                lhs = (pv_core + max(0.0, float(flow.get("grid_load_kwh") or 0.0))
                       + grid_charge + delivered_battery)
                rhs = (load + max(0.0, float(flow.get("pv_to_bat_kwh") or 0.0))
                       + grid_charge + battery_sell)
                if abs(lhs - rhs) > 1e-6:
                    raise RuntimeError(
                        f"ENERGY_BALANCE_VIOLATION:{index}:{lhs}!={rhs}")
            ensure_deadline("SLOT_BALANCE")
            audit_stage(cur,run_id,"SLOT_BALANCE","OK",len(rows),
                        "core balance excludes flexible PV surplus")
            audit_stage(cur,run_id,"DISPATCH","OK",len(rows),f"objective={optimization['objective_pln']:.3f}")
            purchase_slots=sum(float(flow["grid_charge_kwh"])>flow_threshold for flow in optimization["flows"])
            audit_stage(cur,run_id,"REPLENISHMENT","OK",purchase_slots,
                        "pass 5: load, falling PV, sale preparation and recovery")
            # ``soc_floor`` is the stop level of a concrete, accepted battery
            # sale.  It is not copied from TOU and it never limits native-load
            # discharge.  Non-sale slots publish only the technical reserve.
            floors = [
                max(reserve, min(100.0, float(flow.get("soc_end_pct") or reserve)))
                if float(flow.get("battery_sell_kwh") or 0.0) > flow_threshold
                else reserve
                for flow in optimization["flows"]
            ]
            base=[{"row":row,**flow} for row,flow in zip(rows,optimization["flows"])]
            sale_economics = [
                battery_sale_economics(
                    horizon_rows, index, eta_c, eta_d, degradation, min_margin)
                for index in range(len(horizon_rows))
            ]
            # The planner publishes only the physical flexible-PV remainder.
            # A separate PPD run reads the frozen plan and allocates that
            # remainder to CWU, EV, export or curtailment without writing SOC.
            for i,item in enumerate(base):
                row=item["row"]
                tou_program=tou_by_index[i]
                tou_block_reason=None if tou_program else "TOU_FLOOR_UNAVAILABLE"
                target=targets[i]
                required=required_soc[i]
                item["start"],item["end"]=item["soc_start_pct"],item["soc_end_pct"]
                pv=float(row.get("forecast_pv_total_kwh") or 0)
                native_load=float(row.get("forecast_load_kwh") or 0)
                row_day = row.get("local_day") or row["slot_start"].date()
                hp_load = (planned_hp_kw_by_day.get(row_day, hp_fallback_kw) * 0.25
                           if i in hp_selected_indices else 0.0)
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
                economics = sale_economics[i]
                if sell_bat and not economics["eligible"]:
                    raise RuntimeError(
                        f"UNECONOMIC_BATTERY_EXPORT:{row['slot_start']}:"
                        f"sell={sell_price:.6f}:required={economics['required_sell_price']}")
                floor=floors[i]
                paired_buy=buy>flow_threshold and any(float(previous["battery_sell_kwh"])>flow_threshold for previous in optimization["flows"][:i])
                buy_purpose="POST_SALE_RECOVERY" if paired_buy else "FUTURE_LOAD_OR_SALE_PREPARATION" if buy>flow_threshold else "NONE"
                no_sell_pv=sell_price<=0
                raw_flexible=max(0.0,pv_flex+float(item.get("pv_curtail_kwh") or 0.0))
                cwu_kwh=ev_kwh=0.0
                pv_export_kwh=raw_flexible if sell_price>0 else 0.0
                pv_curtail_kwh=raw_flexible-pv_export_kwh
                item["pv_export"]=pv_export_kwh
                grid_policy="BUY_ALLOWED" if buy>flow_threshold else ("NO_BUY" if sell_bat else "NEUTRAL")
                export_policy="SELL_BAT" if sell_bat else ("NO_SELL_PV" if no_sell_pv else ("SELL_PV" if item["pv_export"]>flow_threshold else "NEUTRAL"))
                recommendation=("Zakup ładowanie" if buy>flow_threshold else "Sprzedaż z baterii" if sell_bat else
                    "Sprzedaż PV" if item["pv_export"]>flow_threshold else "Ładowanie PV" if item["charge"]>flow_threshold else
                    "Autokonsumpcja PV" if pv>flow_threshold else "Autokonsumpcja z baterii" if item["battery_to_load_kwh"]>flow_threshold else
                    "Zasilanie z sieci" if item["grid_load_kwh"]>technical_threshold else "Neutralny")
                reason=(f"optimizer=FULL_HORIZON; horizon_slots={len(base)}; objective_pln={optimization['objective_pln']:.3f}; "
                        f"slot_cost_pln={item['slot_cost_pln']:.3f}; grid={grid_policy}; export={export_policy}; "
                        f"soc={item['end']:.2f}; required={required:.2f}; floor={floor:.2f}; charge_target={target:.2f}; hp_load_kwh={hp_load:.3f}; "
                        f"buy_purpose={buy_purpose}; ppd=PENDING_SEPARATE_RUN")
                if tou_block_reason:
                    reason += f"; {tou_block_reason}"
                cur.execute("""UPDATE ems_gpt_plan_stage_rows SET soc_start_plan_pct=%s,soc_end_plan_pct=%s,
                  soc_reserve_pct=%s,soc_required_pct=%s,soc_charge_target_pct=%s,
                  soc_sale_floor_pct=%s,soc_floor_pct=%s,soc_target_pct=%s,
                  soc_target_due=%s,soc_target_source=%s,
                  soc_target_reserved_pv_kwh=%s,planned_buy_kwh=%s,planned_battery_charge_kwh=%s,
                  planned_battery_discharge_kwh=%s,planned_sell_kwh=%s,planned_pv_export_kwh=%s,
                  recommendation=%s,grid_policy_planned=%s,export_policy_planned=%s,
                  planned_pv_to_bat_kwh=%s,planned_pv_to_cwu_kwh=%s,planned_pv_to_ev_kwh=%s,
                  planned_pv_curtail_kwh=%s,ppd_reason=%s,soc_updated_at=NOW(6),ppd_updated_at=NOW(6)
                  WHERE run_id=%s AND slot_start=%s""",
                  (round(item["start"],2),round(item["end"],2),round(reserve,2),
                   round(required,2),round(target,2),round(floor,2),round(floor,2),round(target,2),
                   commitment["due"][i],commitment["source"][i],round(commitment["reserved_pv_kwh"][i],6),round(buy,6),
                   round(item["charge"],6),round(item["discharge"],6),round(item["sell"],6),round(item["pv_export"],6),
                   recommendation,grid_policy,export_policy,
                   round(pv_to_bat,6),round(cwu_kwh,6),round(ev_kwh,6),
                   round(pv_curtail_kwh,6),reason[:255],run_id,row["slot_start"]))
                hp_window = i in hp_selected_indices
                cur.execute("""UPDATE ems_gpt_plan_stage_rows SET heat_pump_window=%s
                  WHERE run_id=%s AND slot_start=%s""", (hp_window,run_id,row["slot_start"]))
                ensure_deadline("FLEX_SURPLUS")
            audit_stage(cur,run_id,"FLEX_SURPLUS","OK",len(base),
                        "physical flexible PV remainder published for separate PPD")
            audit_stage(cur,run_id,"SOC","OK",len(base),f"pass 6: result SOC; step={optimization['soc_step_pct']:.2f}")
            cur.execute("""SELECT COUNT(*) n,
              SUM(price_buy_pln_kwh IS NULL OR price_sell_pln_kwh IS NULL) bad_price,
              SUM(soc_floor_pct IS NULL OR soc_target_pct IS NULL
                OR soc_required_pct IS NULL OR soc_charge_target_pct IS NULL
                OR soc_reserve_pct IS NULL OR soc_sale_floor_pct IS NULL
                OR soc_end_plan_pct+0.01<soc_required_pct) bad_soc,
              SUM(market_window NOT IN ('BUY','SELL','NEUTRAL')
                OR grid_policy_planned IS NULL OR export_policy_planned IS NULL
                OR planned_buy_kwh<0 OR planned_sell_kwh<0
                OR planned_battery_charge_kwh<0 OR planned_battery_discharge_kwh<0
                OR (planned_battery_charge_kwh>0.000001 AND planned_battery_discharge_kwh>0.000001)
                OR heat_pump_window NOT IN (0,1)) bad_ppd
              FROM ems_gpt_plan_stage_rows WHERE run_id=%s""",(run_id,))
            checks=cur.fetchone()
            accepted=checks["n"]==len(source) and not any(int(checks[k] or 0) for k in ("bad_price","bad_soc","bad_ppd"))
            if not accepted:
                cur.execute("UPDATE ems_gpt_plan_runs SET status='REJECTED',current_stage='VALIDATE',validated_at=NOW(6),updated_at=NOW(6),validation_status='REJECTED',validation_reason=%s WHERE run_id=%s",(json.dumps(checks,default=str),run_id))
                audit_stage(cur,run_id,"VALIDATE","REJECTED",0,json.dumps(checks,default=str))
                raise RuntimeError(f"planner validation rejected: {checks}")
            ensure_deadline("VALIDATE")
            cur.execute("""UPDATE ems_gpt_slots p JOIN ems_gpt_plan_stage_rows s
              ON s.slot_start=p.slot_start AND s.run_id=%s SET
              p.forecast_pv1_kwh=s.forecast_pv1_kwh,p.forecast_pv2_kwh=s.forecast_pv2_kwh,
              p.forecast_pv_total_kwh=s.forecast_pv_total_kwh,p.forecast_load_kwh=s.forecast_load_kwh,
              p.forecast_heat_pump_load_kwh=s.forecast_heat_pump_load_kwh,
              p.forecast_heat_pump_dhw_load_kwh=s.forecast_heat_pump_dhw_load_kwh,
              p.soc_start_plan_pct=s.soc_start_plan_pct,p.soc_end_plan_pct=s.soc_end_plan_pct,
              p.soc_floor_pct=s.soc_floor_pct,p.soc_target_pct=s.soc_target_pct,
              p.soc_reserve_pct=s.soc_reserve_pct,p.soc_required_pct=s.soc_required_pct,
              p.soc_charge_target_pct=s.soc_charge_target_pct,
              p.soc_sale_floor_pct=s.soc_sale_floor_pct,
              p.soc_target_due=s.soc_target_due,p.soc_target_source=s.soc_target_source,
              p.soc_target_reserved_pv_kwh=s.soc_target_reserved_pv_kwh,
              p.market_window=s.market_window,
              p.planned_buy_kwh=s.planned_buy_kwh,
              p.planned_sell_kwh=s.planned_sell_kwh,p.planned_pv_export_kwh=s.planned_pv_export_kwh,
              p.planned_battery_charge_kwh=s.planned_battery_charge_kwh,
              p.planned_battery_discharge_kwh=s.planned_battery_discharge_kwh,p.recommendation=s.recommendation,
              p.grid_policy_planned=s.grid_policy_planned,p.export_policy_planned=s.export_policy_planned,
              p.planned_pv_to_bat_kwh=s.planned_pv_to_bat_kwh,
              p.planned_pv_to_cwu_kwh=s.planned_pv_to_cwu_kwh,
              p.planned_pv_to_ev_kwh=s.planned_pv_to_ev_kwh,
              p.planned_pv_curtail_kwh=s.planned_pv_curtail_kwh,
              p.heat_pump_window=s.heat_pump_window,
              p.ppd_reason=s.ppd_reason,p.ppd_run_type=%s,
              p.ppd_version='CORE_0_34_0',p.ppd_locked_at=NOW(6),p.plan_run_id=%s,p.plan_stage='PUBLISHED',
              p.plan_stage_version='CORE_0_34_0',p.plan_stage_updated_at=NOW(6),
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
