"""SOC target construction and battery dispatch optimization."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from planner_economics_service import battery_sale_economics
from planner_soc_service import _unavoidable_grid_load_allowed
from planner_validation_service import strict_database_bool


def end_of_day_soc_target(historical_soc_pct: float, reserve_pct: float,
                          target_cap_pct: float) -> float:
    """Keep daily closes in the 40–45% operating band.

    Weighted history remains useful inside the band, but low or high closes
    caused by faulty plans/execution cannot pull the next terminal target
    outside it. Physical reserve and target-cap limits still apply.
    """
    history_pct = float(historical_soc_pct)
    bounded_history_pct = max(40.0, min(45.0, history_pct))
    return max(float(reserve_pct), min(float(target_cap_pct),
                                       bounded_history_pct))


def daily_close_soc_requirements(rows: list[dict], safety_pcts: list[float],
                                 daily_terminal_soc: dict) -> tuple[list[float], set[int]]:
    """Apply historical close SOC after an early next-day BUY opportunity.

    The independent safety bridge remains in force before replenishment. If a
    permitted BUY window occurs before the daily 06:00 DHW start, defer the
    previous day's historical target until the end of the last such window.
    This keeps the historical terminal target from forcing a charge before a
    cheaper night window; the independent bridge still covers load through the
    next PV/BUY opportunity, including the 06:00 DHW cycle.
    """
    if len(rows) != len(safety_pcts):
        raise ValueError("rows and safety SOC lengths differ")
    required = list(safety_pcts)
    closes = set()
    for index, row in enumerate(rows):
        row_day = row.get("local_day") or row["slot_start"].date()
        next_day = ((rows[index + 1].get("local_day")
                     or rows[index + 1]["slot_start"].date())
                    if index + 1 < len(rows) else None)
        if next_day == row_day:
            continue

        target_index = index
        first_next = index + 1
        if first_next < len(rows):
            first_row = rows[first_next]
            first_day = first_row.get("local_day") or first_row["slot_start"].date()
            if first_day != row_day:
                early_buy_ends = []
                cursor = first_next
                while cursor < len(rows):
                    candidate = rows[cursor]
                    candidate_day = candidate.get("local_day") or candidate["slot_start"].date()
                    if candidate_day != first_day:
                        break
                    local_start = candidate.get("slot_start_local") or candidate["slot_start"]
                    if isinstance(local_start, str):
                        local_start = datetime.fromisoformat(local_start)
                    if local_start.time() >= time(6, 0):
                        break
                    candidate_is_buy = (
                        strict_database_bool(candidate.get("buy_window", False), "buy_window")
                        and not strict_database_bool(candidate.get("sale_window", False), "sale_window")
                    )
                    if not candidate_is_buy:
                        cursor += 1
                        continue
                    window_end = cursor
                    while window_end + 1 < len(rows):
                        following = rows[window_end + 1]
                        following_day = following.get("local_day") or following["slot_start"].date()
                        following_start = following.get("slot_start_local") or following["slot_start"]
                        if isinstance(following_start, str):
                            following_start = datetime.fromisoformat(following_start)
                        if (following_day != first_day or following_start.time() >= time(6, 0)
                                or not strict_database_bool(following.get("buy_window", False), "buy_window")
                                or strict_database_bool(following.get("sale_window", False), "sale_window")):
                            break
                        window_end += 1
                    early_buy_ends.append(window_end)
                    cursor = window_end + 1
                if early_buy_ends:
                    target_index = early_buy_ends[-1]

        required[target_index] = max(
            required[target_index], daily_terminal_soc[row_day])
        closes.add(target_index)
    return required, closes


def deterministic_soc_target_contract(
        rows: list[dict], safety_required_soc_pcts: list[float],
        daily_required_soc_pcts: list[float], capacity_kwh: float,
        reserve_pct: float, eta_c: float, eta_d: float,
        max_power_kw: float, slot_minutes: int,
        uncertainty_weight: float, soc_step_pct: float = 0.10) -> dict:
    """Calculate required SOC from load/PV and fixed RCE permissions first.

    No optimizer flow or price objective participates in target calculation.
    BUY windows contribute only their physical slot capacity; PV contributes
    surplus after native and HP load. Grid charging remains limited by RCE BUY.
    """
    if not (len(rows) == len(safety_required_soc_pcts)
            == len(daily_required_soc_pcts)):
        raise ValueError("DIRECT_SOC_CONTRACT_LENGTH_MISMATCH")
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(15.0, float(reserve_pct))
    base_kwh = capacity * reserve / 100.0
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    max_charge = max(0.0, float(max_power_kw)) * slot_minutes / 60.0 * charge_efficiency
    step = max(0.001, float(soc_step_pct))
    quantum_kwh = capacity * step / 100.0
    selected_buy = {
        i for i, row in enumerate(rows)
        if strict_database_bool(row.get("buy_window", False), "buy_window")
        and not strict_database_bool(row.get("sale_window", False), "sale_window")
    }
    buy_due = {i for i in selected_buy
               if i + 1 == len(rows) or i + 1 not in selected_buy}
    required = [reserve] * len(rows)
    raw_required = [reserve] * len(rows)
    next_requirement_kwh = base_kwh
    for i in range(len(rows) - 1, -1, -1):
        requested = max(float(safety_required_soc_pcts[i]),
                        float(daily_required_soc_pcts[i]), reserve)
        after_kwh = max(next_requirement_kwh,
                        capacity * requested / 100.0)
        after_kwh = math.ceil(after_kwh / quantum_kwh - 1e-9) * quantum_kwh
        # Preserve over-cap demand for reachability validation. Clipping here
        # used to turn an impossible bridge into an apparently valid 100% goal.
        raw_required[i] = after_kwh / capacity * 100.0
        required[i] = min(100.0, after_kwh / capacity * 100.0)
        row = rows[i]
        load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        deficit = max(0.0, load - pv) / discharge_efficiency * (1.0 + uncertainty)
        surplus = max(0.0, pv - load) * charge_efficiency * (1.0 - uncertainty)
        buy_allowed = i in selected_buy
        supply = max_charge if buy_allowed else min(max_charge, surplus)
        supply = math.floor(supply / quantum_kwh + 1e-9) * quantum_kwh
        next_requirement_kwh = max(base_kwh, after_kwh + deficit - supply)
    due, source = [], []
    for i in range(len(rows)):
        future_buys = [j for j in buy_due if j >= i]
        future_pv = [j for j in range(i, len(rows))
                     if max(0.0, float(rows[j].get("forecast_pv_total_kwh") or 0.0))
                     > max(0.0, float(rows[j].get("forecast_load_kwh") or 0.0))
                     + max(0.0, float(rows[j].get("forecast_heat_pump_load_kwh") or 0.0))]
        candidates = [(j, "BUY") for j in future_buys] + [(j, "PV") for j in future_pv]
        if candidates:
            event_index, event_source = min(candidates, key=lambda item: item[0])
            due.append(rows[event_index].get("slot_end") or rows[event_index].get("slot_start"))
            source.append(event_source)
        else:
            due.append(rows[-1].get("slot_end") or rows[-1].get("slot_start"))
            source.append("HORIZON")

    # A continuous BUY window is one replenishment opportunity. Keep the
    # per-slot required SOC as the minimum bridge, but expose the highest
    # dynamic target reached within the window as its charge ceiling from the
    # first slot. Otherwise early BUY slots can be capped below live SOC and
    # charging is deferred until a later slot's target rises.
    charge_targets = list(required)
    buy_starts = [i for i in selected_buy
                  if i == 0 or i - 1 not in selected_buy]
    for start in buy_starts:
        end = start
        while end + 1 in selected_buy:
            end += 1
        window_target = max(required[start:end + 1])
        for index in range(start, end + 1):
            charge_targets[index] = window_target

    return {
        "required": required,
        "raw_required": raw_required,
        "charge_targets": charge_targets,
        "due": due,
        "source": source,
        "reserved_pv_kwh": [0.0] * len(rows),
        "buy_due_indices": buy_due,
        "selected_buy_indices": selected_buy,
    }



def economic_buy_charge_caps(rows: list[dict], required_targets: list[float],
                             max_soc_pct: float = 100.0,
                             capacity_kwh: float = 15.0, eta_c: float = 0.9,
                             eta_d: float = 0.95, max_power_kw: float = 5.0,
                             slot_minutes: int = 15) -> list[float]:
    """Allow the available battery charge in BUY only up to its SOC target.

    The target is the charge ceiling. A BUY slot uses the maximum physically
    feasible volume only while it can still move SOC toward that target; BUY
    must never raise a 40% target to 100%. Physical SOC, inverter power and
    the site's total import limit remain hard constraints.
    """
    if len(rows) != len(required_targets):
        raise ValueError("ECONOMIC_BUY_CAP_LENGTH_MISMATCH")
    cap = min(100.0, max(0.0, float(max_soc_pct)))
    result = [min(cap, float(value)) for value in required_targets]
    allowed = [strict_database_bool(row.get("buy_window", False), "buy_window")
               and not strict_database_bool(row.get("sale_window", False), "sale_window")
               for row in rows]
    for index, row in enumerate(rows):
        if allowed[index] and row.get("price_buy_pln_kwh") is not None:
            result[index] = min(cap, max(0.0, float(required_targets[index])))
    return result


def voluntary_load_without_charging_indices(
        rows: list[dict], flows: list[dict], technical_threshold: float,
        charge_threshold: float) -> set[int]:
    """Home supply during real battery BUY is part of the permitted import.

    Only native import without a battery charging decision requires the
    separate economic sale justification. Never exempt SELL or non-BUY.
    """
    if len(rows) != len(flows):
        raise ValueError("VOLUNTARY_LOAD_LENGTH_MISMATCH")
    forbidden = set()
    for index, (row, flow) in enumerate(zip(rows, flows)):
        if float(flow.get("voluntary_grid_load_kwh") or 0.0) <= technical_threshold:
            continue
        charging_buy = (
            strict_database_bool(row.get("buy_window", False), "buy_window")
            and not strict_database_bool(row.get("sale_window", False), "sale_window")
            and float(flow.get("grid_charge_kwh") or 0.0) > charge_threshold)
        if not charging_buy:
            forbidden.add(index)
    return forbidden


def planned_buy_execution_targets(rows: list[dict], flows: list[dict],
                                  required_targets: list[float],
                                  threshold_kwh: float = 1e-7) -> list[float]:
    """Publish one achieved peak target from the first slot of a BUY run."""
    if not len(rows) == len(flows) == len(required_targets):
        raise ValueError("BUY_EXECUTION_TARGET_LENGTH_MISMATCH")
    targets = list(required_targets)
    active = [float(flow.get("grid_charge_kwh") or 0.0) > threshold_kwh
              and strict_database_bool(row.get("buy_window", False), "buy_window")
              and not strict_database_bool(row.get("sale_window", False), "sale_window")
              for row, flow in zip(rows, flows)]
    start = 0
    while start < len(rows):
        if not active[start]:
            start += 1
            continue
        end = start + 1
        while end < len(rows) and active[end]:
            end += 1
        peak = min(100.0, max(
            max(float(flow["soc_end_pct"]) for flow in flows[start:end]),
            max(float(value) for value in targets[start:end])))
        targets[start:end] = [peak] * (end - start)
        start = end
    return targets


def preserve_target_contract_after_reachability(
        target_contract: dict, recovery: dict,
        daily_close_indices: set[int]) -> dict:
    """Keep requested goals separate from physically executable BUY ceilings.

    Reachability may relax intermediate hard floors so a rolling plan can still
    start from measured SOC. Keep valid daily close goals, but a BUY ceiling
    cannot exceed the physical end-of-window frontier. Preserve its requested
    value separately so a shortage never disappears from diagnostics.
    """
    requested = list(target_contract.get("required", []))
    reachable = list(recovery.get("required_soc_pcts", []))
    if len(requested) != len(reachable):
        raise ValueError("TARGET_RECOVERY_LENGTH_MISMATCH")
    required = list(reachable)
    raw_required = list(target_contract.get("raw_required", requested))
    if len(raw_required) != len(required):
        raise ValueError("TARGET_RECOVERY_LENGTH_MISMATCH")
    for index in daily_close_indices:
        if not 0 <= index < len(required):
            raise ValueError("TARGET_RECOVERY_CLOSE_INDEX_INVALID")
        if raw_required[index] <= 100.0 + 1e-9:
            required[index] = max(required[index], requested[index])
    result = dict(target_contract)
    result["requested_required"] = requested
    result["reachable_required"] = reachable
    result["required"] = required
    requested_targets = list(target_contract.get("charge_targets", requested))
    targets = list(requested_targets)
    ceilings = recovery.get("reachable_soc_ceiling_pcts")
    shortages = []
    if ceilings is not None:
        if len(ceilings) != len(required):
            raise ValueError("TARGET_RECOVERY_LENGTH_MISMATCH")
        for index, raw in enumerate(raw_required):
            if raw > 100.0 + 1e-9:
                targets[index] = min(targets[index], float(ceilings[index]))
        selected = set(target_contract.get("selected_buy_indices") or [])
        for start in sorted(i for i in selected if i - 1 not in selected):
            end = start
            while end + 1 in selected:
                end += 1
            requested_target = max(requested_targets[start:end + 1])
            executable_target = min(requested_target, float(ceilings[end]))
            targets[start:end + 1] = [executable_target] * (end - start + 1)
            if executable_target + 1e-9 < requested_target:
                shortages.append({"start_index": start, "end_index": end,
                                  "requested_soc_pct": requested_target,
                                  "executable_soc_pct": executable_target,
                                  "shortfall_pct": requested_target - executable_target})
    result["requested_charge_targets"] = requested_targets
    result["charge_targets"] = targets
    result["charge_target_shortfalls"] = shortages
    result["over_capacity_indices"] = [
        i for i, value in enumerate(raw_required) if value > 100.0 + 1e-9]
    result["unreachable_indices"] = list(recovery.get("unreachable_indices") or [])
    result["capacity_limited_indices"] = list(recovery.get("capacity_limited_indices") or [])
    return result

def replenishment_soc_requirements(rows: list[dict], capacity_kwh: float,
                                   reserve_pct: float, buffer_pct: float,
                                   eta_c: float, eta_d: float,
                                   max_power_kw: float, slot_minutes: int,
                                   uncertainty_weight: float,
                                   soc_step_pct: float = 0.10) -> list[float]:
    """Minimum end-of-slot SOC to bridge future load to feasible replenishment.

    Historical daily closes never enter this calculation. BUY contributes only
    its finite charge capacity outside SELL; PV contributes only surplus after
    house/HP load, limited by the same battery power. The economic pass chooses
    actual purchases under these requirements; build_soc_contracts subsequently
    subtracts only those committed kWh. An insufficient window cannot reset the
    bridge to reserve. Requirements are deliberately not clipped to capacity:
    an impossible safety bridge must be reported rather than silently waived.
    """
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(15.0, float(reserve_pct))
    buffer = max(0.0, float(buffer_pct))
    base_kwh = capacity * (reserve + buffer) / 100.0
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    max_charge = max(0.0, float(max_power_kw)) * slot_minutes / 60.0 * charge_efficiency
    quantum_kwh = capacity * max(0.001, float(soc_step_pct)) / 100.0
    required_after = base_kwh
    result = [0.0] * len(rows)
    for index in range(len(rows) - 1, -1, -1):
        result[index] = required_after / capacity * 100.0
        row = rows[index]
        load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        deficit = max(0.0, load - pv) / discharge_efficiency * (1.0 + uncertainty)
        surplus = max(0.0, pv - load) * charge_efficiency * (1.0 - uncertainty)
        buy_allowed = (strict_database_bool(row.get("buy_window", False), "buy_window")
                       and not strict_database_bool(row.get("sale_window", False), "sale_window"))
        supply = max_charge if buy_allowed else min(max_charge, surplus)
        # The optimizer quantizes SOC. Do not promise fractional supply that
        # its reachable frontier cannot deliver in this slot.
        supply = math.floor(supply / quantum_kwh + 1e-9) * quantum_kwh
        required_after = max(base_kwh, required_after + deficit - supply)
    return result


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
                            allow_terminal_shortfall: bool = False,
                            max_grid_import_kw: float = 12.0,
                            grid_charge_caps: list[float] | None = None,
                            minimum_grid_charge_kwh: float = 0.0,
                            allow_buy_target_shortfall: bool = False) -> dict:
    """Minimize total energy cost across every available slot and SOC state."""
    if not rows:
        return {"flows": [], "objective_pln": 0.0, "soc_step_pct": soc_step_pct}
    if grid_charge_caps is not None and len(grid_charge_caps) != len(rows):
        raise ValueError("GRID_CHARGE_CAP_LENGTH_MISMATCH")
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    step = max(0.05, float(soc_step_pct))
    first_unit = int(math.ceil(reserve / step - 1e-9))
    last_unit = int(math.floor(min(100.0, max(reserve, float(max_soc_pct))) / step + 1e-9))
    initial_soc = float(initial_soc_pct)
    if not math.isfinite(initial_soc) or not 0.0 <= initial_soc <= 100.0:
        raise RuntimeError(f"INVALID_INITIAL_SOC:{initial_soc!r}")
    start_unit = max(0, min(last_unit, int(math.floor(initial_soc / step + 1e-9))))
    terminal_unit = max(first_unit, min(last_unit, int(math.ceil(float(terminal_soc_pct) / step - 1e-9))))
    unit_kwh = capacity * step / 100.0
    max_grid_slot_kwh = max(0.0, float(max_grid_import_kw)) * slot_minutes / 60.0
    max_internal_charge = max_power_kw * slot_minutes / 60.0 * eta_c
    max_internal_discharge = max_power_kw * slot_minutes / 60.0 / eta_d
    max_up = int(math.floor(max_internal_charge / unit_kwh + 1e-9))
    max_down = int(math.floor(max_internal_discharge / unit_kwh + 1e-9))
    costs = {start_unit: 0.0}
    buy_permissions = [
        strict_database_bool(row.get("buy_window", False), "buy_window")
        and not strict_database_bool(row.get("sale_window", False), "sale_window")
        for row in rows
    ]
    buy_window_ends = {
        index for index, value in enumerate(buy_permissions)
        if value and (index + 1 == len(buy_permissions) or not buy_permissions[index + 1])
    }
    target_due_indices = buy_window_ends if target_due_indices is None else set(target_due_indices)
    # Pass 4 never commands grid purchases for household consumption or to
    # preserve energy for later sale. Unavoidable physical load residual at the
    # discharge-power ceiling or technical reserve is tracked separately from
    # grid charging and is never a planned BUY. Battery buy/sell economics use
    # FIFO cost basis and the sale-versus-no-sale counterfactual below.
    predecessors: list[dict[int, tuple[int, dict]]] = []
    effective_target_pcts: list[float] = []
    buy_target_shortfalls = []
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
        # The slot's planner target is always the hard ceiling on grid charge.
        # A separate grid cap may tighten that limit, but can never raise it.
        grid_cap_pct = (target_pct if grid_charge_caps is None else
                        min(target_pct, max(reserve, min(
                            float(max_soc_pct), float(grid_charge_caps[index])))))
        grid_cap_unit = (requested_target_unit if grid_charge_caps is None else
                         int(math.ceil(grid_cap_pct / step - 1e-9)))
        required_pct = (reserve if required_soc_pcts is None else
                        max(0.0, min(float(max_soc_pct),
                            float(required_soc_pcts[index]))))
        required_unit = int(math.ceil(required_pct / step - 1e-9))
        # MariaDB returns BOOL/TINYINT as 0/1 (and some drivers as Decimal),
        # so identity checks against False would incorrectly allow 0.
        grid_charge_allowed = (
            strict_database_bool(row.get("buy_window", False), "buy_window")
            and not strict_database_bool(row.get("sale_window", False), "sale_window")
        )
        battery_sale_allowed = (battery_sales_enabled and sell_price > 0.0
                                and strict_database_bool(
                                    row.get("sale_window", False), "sale_window"))
        available_charge_internal = min(
            max_internal_charge,
            surplus * eta_c + (max_internal_charge if grid_charge_allowed else 0.0),
        )
        next_costs, next_predecessors = {}, {}
        for current_unit, accumulated in costs.items():
            current_energy = current_unit * unit_kwh
            lower_state = current_unit if current_unit < first_unit else first_unit
            for next_unit in range(max(lower_state, current_unit-max_down),
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
                    if ((minimum_soc_targets is not None or grid_charge_caps is not None)
                            and grid_charge > 1e-9
                            and next_unit > max(current_unit, grid_cap_unit)):
                        continue
                    # A selected grid purchase must fill this slot's available
                    # charge volume. Retain OFF/discharge alternatives for future
                    # feasibility, rather than reserving headroom with token BUYs.
                    if grid_charge > 1e-9:
                        site_charge_input = surplus + max(0.0, max_grid_slot_kwh-deficit)
                        full_internal = min(max_internal_charge, site_charge_input*eta_c)
                        full_up = int(math.floor(full_internal/unit_kwh + 1e-9))
                        full_next = min(last_unit, current_unit+full_up,
                                        max(current_unit, grid_cap_unit))
                        if next_unit != full_next:
                            continue
                    grid_load = deficit
                    pv_export = max(0.0, surplus-pv_to_bat) if sell_price > 0 else 0.0
                    pv_curtail = max(0.0, surplus-pv_to_bat-pv_export)
                    battery_discharge = 0.0
                    battery_charge_internal = max(0.0, delta)
                    # The DP state is quantized. Do not report sub-step PV as
                    # battery charge unless the SOC transition records that
                    # energy; otherwise it disappears from the next slot's
                    # state and creates a nonphysical trajectory.
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
                unavoidable_grid_load = _unavoidable_grid_load_allowed(
                    grid_load, current_unit, next_unit, first_unit, max_down,
                    unit_kwh, eta_d)
                grid_load_during_buy_charge = (
                    grid_charge_allowed
                    and grid_charge > max(1e-9, float(minimum_grid_charge_kwh))
                )
                if (grid_load > unit_kwh * eta_d + 1e-9
                        and not unavoidable_grid_load
                        and not grid_load_during_buy_charge):
                    continue
                voluntary_grid_load = (
                    0.0 if unavoidable_grid_load else max(0.0, grid_load))
                # The physical site limit still applies to total imports.
                if grid_load + grid_charge > max_grid_slot_kwh + 1e-9:
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
                        and index in hard_target_indices and index not in target_due_indices
                        and next_unit < requested_target_unit):
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
                        "voluntary_grid_load_kwh": voluntary_grid_load,
                        "pv_export_kwh": pv_export, "pv_curtail_kwh": pv_curtail,
                        "slot_cost_pln": slot_cost})
        if not next_costs:
            raise RuntimeError(f"No feasible SOC state at horizon slot {index}")
        future_buy_load = False
        if grid_charge_allowed:
            window_end = index
            while (window_end + 1 < len(rows)
                   and buy_permissions[window_end + 1]):
                window_end += 1
            future_buy_load = any(
                max(0.0, float(rows[future].get("forecast_load_kwh") or 0.0))
                + max(0.0, float(rows[future].get("forecast_heat_pump_load_kwh") or 0.0))
                > 1e-9
                for future in range(index + 1, window_end + 1)
            )
        if grid_charge_allowed and not future_buy_load:
            # Without later household load retain the established maximal
            # BUY frontier. With later load, keep OFF/discharge alternatives
            # for feasibility. Every positive BUY transition was already
            # restricted to its full available volume above.
            highest_reachable_unit = max(next_costs)
            next_costs = {highest_reachable_unit: next_costs[highest_reachable_unit]}
            next_predecessors = {
                highest_reachable_unit: next_predecessors[highest_reachable_unit]}
        effective_target_unit = requested_target_unit
        if minimum_soc_targets is not None and index in target_due_indices:
            # Determine the due SOC from transitions that survived the full
            # slot balance, including the energy needed by native loads.  A
            # charge-only bound (max(frontier) + charge power) can overstate
            # what remains after serving the house in this same slot and make
            # every otherwise-feasible transition fail the due-target check.
            due_candidates = [unit for unit in next_costs
                              if unit >= requested_target_unit]
            if due_candidates:
                eligible = set(due_candidates)
                next_costs = {unit: cost for unit, cost in next_costs.items()
                              if unit in eligible}
                next_predecessors = {
                    unit: predecessor for unit, predecessor in next_predecessors.items()
                    if unit in eligible}
            else:
                # Do not publish a plan that silently misses the requested
                # battery replenishment commitment.
                best_reachable_unit = max(next_costs)
                if (allow_buy_target_shortfall and not battery_sales_enabled
                        and required_soc_pcts is not None
                        and best_reachable_unit >= required_unit):
                    # The charging ceiling stays unchanged. A no-sale plan may
                    # report a missed desired charge level only while every
                    # mandatory energy bridge remains satisfied. Never restore
                    # token BUYs or lower the target/required SOC to pass.
                    next_costs = {best_reachable_unit: next_costs[best_reachable_unit]}
                    next_predecessors = {
                        best_reachable_unit: next_predecessors[best_reachable_unit]}
                    buy_target_shortfalls.append({
                        "index": index, "target_soc_pct": requested_target_unit*step,
                        "achieved_soc_pct": best_reachable_unit*step,
                        "required_soc_pct": required_unit*step,
                        "shortfall_pct": (requested_target_unit-best_reachable_unit)*step})
                else:
                    raise RuntimeError(
                        f"BUY_TARGET_UNREACHABLE:slot={index}:"
                        f"requested={requested_target_unit * step:.3f}:"
                        f"reachable={best_reachable_unit * step:.3f}")
        effective_target_pcts.append(effective_target_unit * step)
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
    selected_terminal_unit = unit
    flows = [None]*len(rows)
    for index in range(len(rows)-1, -1, -1):
        unit, flows[index] = predecessors[index][unit]
    for index, flow in enumerate(flows):
        soc_delta_internal = (
            float(flow["soc_end_pct"]) - float(flow["soc_start_pct"])
        ) * capacity / 100.0
        flow_delta_internal = (
            float(flow["battery_charge_internal_kwh"])
            - float(flow["battery_discharge_internal_kwh"])
        )
        if abs(soc_delta_internal - flow_delta_internal) > 1e-7:
            raise RuntimeError(
                f"SOC_TRANSITION_VIOLATION:{index}:"
                f"{soc_delta_internal}!={flow_delta_internal}")
    return {"flows": flows, "objective_pln": round(objective, 6),
            "buy_target_shortfalls": buy_target_shortfalls,
            "soc_step_pct": step, "terminal_soc_pct": terminal_unit*step,
            "achieved_terminal_soc_pct": selected_terminal_unit*step,
            "terminal_shortfall_pct": max(
                0.0, (terminal_unit-selected_terminal_unit)*step),
            "effective_target_pcts": effective_target_pcts,
            "requested_target_pcts": [
                (float(max_soc_pct) if minimum_soc_targets is None else
                 max(reserve, min(float(max_soc_pct), float(target))))
                for target in (minimum_soc_targets or [max_soc_pct] * len(rows))
            ]}


def validated_buy_deadline_targets(result: dict, targets: list[float],
                                   required: list[float],
                                   battery_sales_enabled: bool) -> list[float]:
    """Validate explicit no-sale shortages without rewriting charging ceilings."""
    flows = result.get("flows") or []
    if not len(flows) == len(targets) == len(required):
        raise RuntimeError("BUY_SHORTFALL_VALIDATION_LENGTH_MISMATCH")
    deadlines = list(targets)
    for shortage in result.get("buy_target_shortfalls", []):
        index = int(shortage["index"])
        if not 0 <= index < len(flows):
            raise RuntimeError("INVALID_BUY_TARGET_SHORTFALL_INDEX")
        achieved = float(flows[index]["soc_end_pct"])
        if (battery_sales_enabled or achieved + 1e-9 < float(required[index])
                or abs(float(shortage["target_soc_pct"])-float(targets[index])) > .01
                or abs(float(shortage["achieved_soc_pct"])-achieved) > .01
                or any(float(flow.get("battery_sell_kwh") or 0) > 1e-9 for flow in flows)):
            raise RuntimeError("UNSAFE_BUY_TARGET_SHORTFALL")
        deadlines[index] = achieved
    return deadlines

def validate_physical_buy_commitments(
        rows: list[dict], flows: list[dict], due_indices: set[int],
        target_pcts: list[float], max_power_kw: float,
        slot_minutes: int, capacity_kwh: float,
        eta_c: float, tolerance_pct: float = 0.11) -> dict:
    """Verify actual scheduled energy, not hypothetical BUY capacity.

    BUY is a permission only. Its physical contribution is the sum of
    grid_charge_kwh in the selected dispatch. The due SOC is checked after
    native load and PV for the entire BUY window have been accounted for.
    """
    if len(rows) != len(flows) or len(rows) != len(target_pcts):
        raise RuntimeError("BUY_PHYSICAL_COMMITMENT_LENGTH_MISMATCH")
    max_charge_input = max(0.0, float(max_power_kw)) * slot_minutes / 60.0
    actual_buy_kwh = 0.0
    buy_slots = 0
    for index, (row, flow) in enumerate(zip(rows, flows)):
        buy = (strict_database_bool(row.get("buy_window", False), "buy_window")
               and not strict_database_bool(row.get("sale_window", False), "sale_window"))
        imported = max(0.0, float(flow.get("grid_charge_kwh") or 0.0))
        pv_charged = max(0.0, float(flow.get("pv_to_bat_kwh") or 0.0))
        if imported > 1e-7 and not buy:
            raise RuntimeError(f"GRID_CHARGE_OUTSIDE_BUY:{index}")
        if imported + pv_charged > max_charge_input + 1e-6:
            raise RuntimeError(f"CHARGE_POWER_LIMIT:{index}")
        if buy:
            buy_slots += 1
            actual_buy_kwh += imported
        if index in due_indices:
            actual_soc = float(flow.get("soc_end_pct") or 0.0)
            required_soc = float(target_pcts[index])
            if actual_soc + tolerance_pct < required_soc:
                raise RuntimeError(
                    f"BUY_PHYSICAL_DEFICIT:{index}:{actual_soc:.3f}"
                    f"<{required_soc:.3f}")
        if index:
            previous_soc = float(flows[index - 1].get("soc_end_pct") or 0.0)
            starting_soc = float(flow.get("soc_start_pct") or 0.0)
            if abs(previous_soc - starting_soc) > tolerance_pct:
                raise RuntimeError(f"SOC_DISCONTINUITY:{index}")
    return {
        "actual_buy_kwh": round(actual_buy_kwh, 6),
        "buy_slot_count": buy_slots,
        "buy_deadline_count": len(due_indices),
    }

def active_soc_target_deadlines(result: dict,
                                requested_due_indices: set[int]) -> set[int]:
    """Return only deadlines the successful dispatch pass actually enforced.

    TARGET_COMMITMENT may drop optional BUY deadlines after proving they are
    unreachable.  Its SOC target ceiling remains in force, but the caller must
    not validate a dropped deadline as if it were still a hard minimum.
    """
    effective = result.get("effective_target_due_indices")
    if effective is None:
        return set(requested_due_indices)
    return {int(index) for index in effective}

def validate_active_soc_target_deadlines(result: dict,
                                          requested_due_indices: set[int],
                                          target_pcts: list[float],
                                          tolerance_pct: float = 0.01
                                          ) -> set[int]:
    """Validate only BUY deadlines that survived the final planner fallback."""
    flows = result.get("flows") or []
    if len(flows) != len(target_pcts):
        raise RuntimeError("SOC_TARGET_VALIDATION_LENGTH_MISMATCH")
    effective_due_indices = active_soc_target_deadlines(
        result, requested_due_indices)
    for index in sorted(effective_due_indices):
        soc_end = float(flows[index].get("soc_end_pct") or 0.0)
        target = float(target_pcts[index])
        if soc_end + float(tolerance_pct) < target:
            raise RuntimeError(
                f"SOC_TARGET_NOT_REACHED:{index}:{soc_end}<{target}")
    return effective_due_indices


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
    required_after_slot = [reserve] * len(rows)
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
        required_after_slot[i] = max(reserve, min(cap, raw_required_pct))
        flow = economic_flows[i]
        # Keep the independently calculated balance contract intact.
        # Feasibility is handled by the constrained optimization/recovery pass;
        # an earlier economic trajectory must not lower the required SOC.
        rounded_required = math.ceil(raw_required_pct / step - 1e-9) * step
        required[i] = max(reserve, min(cap, rounded_required))
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

    for i in range(len(rows)):
        charge_targets[i] = required[i]
    # A selected BUY window receives only the energy needed to reach the next
    # replenishment/terminal requirement. Do not turn an unconstrained
    # economic-path state (which can be 100% at a negative price) into a
    # purchase target. The same ceiling applies to every slot in the selected
    # contiguous purchase run so the optimizer may choose its cheapest slot.
    for end in sorted(buy_due_indices):
        start = end
        while start - 1 in selected_buy:
            start -= 1
        window_target = max(
            required_after_slot[end], max(required[start:end + 1], default=reserve))
        window_target = min(cap, max(reserve, window_target))
        for i in range(start, end + 1):
            charge_targets[i] = window_target
        # Carry the same BUY ceiling backward across its assigned energy bridge
        # so an earlier PV surplus can replace part or all of the later import.
        # ``due`` on bridge slots points to the end of the complete selected
        # BUY run (the optimizer's deadline), not to the first slot in that
        # run.  Matching the latter silently skipped target propagation for
        # multi-slot BUY windows and left the whole preceding day on the lower
        # safety trajectory until the final purchase slots.
        group_due = rows[end].get("slot_end") or rows[end].get("slot_start")
        for i in range(start - 1, -1, -1):
            if source[i] != "BUY" or due[i] != group_due:
                break
            charge_targets[i] = max(charge_targets[i], window_target)
    return {"required": required, "charge_targets": charge_targets,
            "due": due, "source": source, "reserved_pv_kwh": reserved_pv,
            "buy_due_indices": buy_due_indices,
            "selected_buy_indices": selected_buy}
