Warning: truncated output (original token count: 44631)
Total output lines: 3177

from __future__ import annotations

import json
import math
import time as monotonic_clock
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Callable

from config_service import deye_program_soc_baselines

PLANNER_VERSION = "CORE_0_40_39"


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


def terminal_soc_recovery(requested_soc_pct: float, safety_soc_pct: float,
                          battery_sales_enabled: bool,
                          current_day: bool = False) -> tuple[str, float]:
    """Preserve today's historical close and allow an explicit best-effort shortfall.

    The independent replenishment/safety SOC remains a hard floor. For the
    current day, an infeasible historical close must not be silently relaxed
    to the technical reserve: the optimizer should select the highest
    physically reachable terminal SOC and report the shortfall.
    """
    if battery_sales_enabled:
        return "DISABLE_BATTERY_SALES", float(requested_soc_pct)
    requested = float(requested_soc_pct)
    safety = float(safety_soc_pct)
    if requested > safety + 1e-9:
        if current_day:
            return "ALLOW_TERMINAL_SHORTFALL", requested
        return "RELAX_HISTORICAL_TARGET", safety
    return "FAIL", requested


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


def validate_rce_market_window(row: dict) -> dict:
    """Validate and consume the window assignment persisted by the RCE importer."""
    if row.get("sale_window") is None or row.get("buy_window") is None:
        raise RuntimeError(f"RCE_PRICE_WINDOW_MISSING:{row.get('slot_start')}")
    sale = strict_database_bool(row.get("sale_window"), "sale_window")
    buy = strict_database_bool(row.get("buy_window"), "buy_window")
    if sale and buy:
        raise RuntimeError(f"WINDOW_OVERLAP:{row.get('slot_start')}")
    expected = "SELL" if sale else "BUY" if buy else "NEUTRAL"
    persisted = row.get("market_window")
    if persisted is not None and str(persisted).upper() != expected:
        raise RuntimeError(
            f"RCE_MARKET_WINDOW_MISMATCH:{row.get('slot_start')}:"
            f"{persisted!r}!={expected}")
    normalized = dict(row)
    normalized["sale_window"] = sale
    normalized["buy_window"] = buy
    normalized["market_window"] = expected
    return normalized


def validate_pv_forecasts(rows: list[dict]) -> None:
    """Unknown PV is never a zero-production scenario for dispatch or SOC."""
    for row in rows:
        values = [row.get(key) for key in (
            "forecast_pv1_kwh", "forecast_pv2_kwh", "forecast_pv_total_kwh")]
        if any(value is None for value in values):
            raise RuntimeError(f"MISSING_PV_FORECAST:{row['slot_start']}")
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in values):
            raise RuntimeError(f"INVALID_PV_FORECAST:{row['slot_start']}")
        if abs(float(values[0]) + float(values[1]) - float(values[2])) > 0.000002:
            raise RuntimeError(f"INCONSISTENT_PV_FORECAST:{row['slot_start']}")


def elapsed_hp_plan_states(cur, day_start: datetime, cutoff: datetime) -> tuple[list[bool], list[dict]]:
    """Count only known published HP windows; unknown history grants no heat credit."""
    cur.execute("""SELECT slot_start,plan_published,heat_pump_window FROM ems_gpt_slots
      WHERE slot_start>=%s AND slot_start<%s ORDER BY slot_start""",
      (day_start, cutoff))
    states, unknown = [], []
    for row in cur.fetchall():
        # Missing publication metadata is also legacy/unknown history. Never
        # use an unpublished window (even a stored 1) as completed heating.
        published = row["plan_published"]
        value = row["heat_pump_window"]
        if published is None or not strict_database_bool(published, "plan_published"):
            states.append(False)
            unknown.append({"slot_start": str(row["slot_start"]), "reason": "UNPUBLISHED"})
        elif value is None:
            states.append(False)
            unknown.append({"slot_start": str(row["slot_start"]), "reason": "MISSING_HP_WINDOW"})
        else:
            states.append(strict_database_bool(value, "heat_pump_window"))
    return states, unknown


def recoverable_soc_requirements(rows: list[dict], required_soc_pcts: list[float],
                                  initial_soc_pct: float, reserve_pct: float) -> dict:
    """Temporarily relax an already-missed SOC bridge until the next BUY slot.

    A rolling plan cannot meet a safety buffer that was already breached before
    the horizon began. Keep the technical reserve as a hard floor, then restore
    the original bridge at the first permitted battery BUY opportunity so the
    optimizer can schedule recovery there.
    """
    if len(rows) != len(required_soc_pcts):
        raise ValueError("RECOVERY_SOC_REQUIREMENT_LENGTH_MISMATCH")
    required = [float(value) for value in required_soc_pcts]
    initial = float(initial_soc_pct)
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    if not required or initial + 1e-9 >= required[0]:
        return {"required_soc_pcts": required, "relaxed_indices": [],
                "recovery_buy_index": None}
    recovery_buy = next((i for i, row in enumerate(rows)
                         if strict_database_bool(row.get("buy_window", False),
                                                 "buy_window")
                         and not strict_database_bool(
                             row.get("sale_window", False), "sale_window")), None)
    if recovery_buy is None:
        return {"required_soc_pcts": required, "relaxed_indices": [],
                "recovery_buy_index": None}
    recovery_floor = max(reserve, min(required[0], initial))
    relaxed = []
    for index in range(recovery_buy):
        if required[index] > recovery_floor + 1e-9:
            required[index] = recovery_floor
            relaxed.append(index)
    return {"required_soc_pcts": required, "relaxed_indices": relaxed,
            "recovery_buy_index": recovery_buy}


def _unavoidable_grid_load_allowed(
        grid_load_kwh: float, current_unit: int, next_unit: int,
        reserve_unit: int, max_discharge_units: int,
        unit_kwh: float, eta_discharge: float) -> bool:
    """Allow only physical residual load after reserve/power limits bind."""
    tolerance = unit_kwh * eta_discharge + 1e-9
    if grid_load_kwh <= tolerance:
        return False
    discharged_units = current_unit - next_unit
    at_reserve = (
        current_unit >= reserve_unit and next_unit == reserve_unit
        and discharged_units >= 0
    ) or (
        current_unit < reserve_unit and next_unit == current_unit
    )
    at_power_limit = (
        discharged_units > 0 and discharged_units >= max_discharge_units
    )
    return at_reserve or at_power_limit


def _soc_reachability_profile(
        rows: list[dict], initial_soc_pct: float, capacity_kwh: float,
        reserve_pct: float, eta_c: float, eta_d: float,
        max_power_kw: float, slot_minutes: int, soc_step_pct: float = 0.10,
        required_soc_pcts: list[float] | None = None,
        max_grid_import_kw: float = 12.0,
) -> dict:
    """Return prefix reachability and a feasible path with minimum floor shortfall.

    Transitions mirror the optimizer's physical limits and voluntary-grid-load
    guard, with battery sales disabled. A per-slot maximum alone is not enough:
    a high floor in one slot can make a later BUY floor unreachable. The DP
    therefore scores complete paths against the whole required-floor sequence.
    """
    if required_soc_pcts is not None and len(rows) != len(required_soc_pcts):
        raise ValueError("RECOVERY_SOC_REQUIREMENT_LENGTH_MISMATCH")
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    step = max(0.05, float(soc_step_pct))
    unit_kwh = capacity * step / 100.0
    first_unit = int(math.ceil(reserve / step - 1e-9))
    last_unit = int(math.floor(100.0 / step + 1e-9))
    initial_pct = float(initial_soc_pct)
    if not math.isfinite(initial_pct) or not 0.0 <= initial_pct <= 100.0:
        raise RuntimeError(f"INVALID_INITIAL_SOC:{initial_pct!r}")
    current_unit = max(0, min(last_unit, int(math.floor(initial_pct / step + 1e-9))))
    max_grid_slot_kwh = max(0.0, float(max_grid_import_kw)) * slot_minutes / 60.0
    eta_charge = max(0.01, min(1.0, float(eta_c)))
    eta_discharge = max(0.01, min(1.0, float(eta_d)))
    max_internal_charge = max(0.0, float(max_power_kw)) * slot_minutes / 60.0 * eta_charge
    max_internal_discharge = max(0.0, float(max_power_kw)) * slot_minutes / 60.0 / eta_discharge
    max_up = int(math.floor(max_internal_charge / unit_kwh + 1e-9))
    max_down = int(math.floor(max_internal_discharge / unit_kwh + 1e-9))
    # Keep the minimum total squared floor shortfall for each reachable state;
    # state alone determines future transitions, so dominated paths can drop.
    scores = {current_unit: (0.0, -current_unit * step)}
    predecessor_layers = []
    ceilings = []
    for index, row in enumerate(rows):
        pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
        load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
        deficit, surplus = max(0.0, load - pv), max(0.0, pv - load)
        buy_allowed = (
            strict_database_bool(row.get("buy_window", False), "buy_window")
            and not strict_database_bool(row.get("sale_window", False), "sale_window")
        )
        next_scores = {}
        next_predecessors = {}
        for current, score in scores.items():
            lower_state = current if current < first_unit else first_unit
            for following in range(max(lower_state, current-max_down),
                                    min(last_unit, current+max_up)+1):
                delta = (following-current) * unit_kwh
                if delta >= 0.0:
                    charge_input = delta / eta_charge
                    pv_to_bat = min(surplus, charge_input)
                    grid_charge = max(0.0, charge_input-pv_to_bat)
                    if grid_charge > 1e-9 and not buy_allowed:
                        continue
                    battery_to_load = 0.0
                    grid_load = deficit
                else:
                    battery_discharge = -delta
                    delivered = battery_discharge * eta_discharge
                    battery_to_load = min(deficit, delivered)
                    # Reachability deliberately excludes battery export.
                    if delivered-battery_to_load > 1e-9:
                        continue
                    grid_charge = 0.0
                    grid_load = max(0.0, deficit-battery_to_load)
                unavoidable_grid_load = _unavoidable_grid_load_allowed(
                    grid_load, current, following, first_unit, max_down,
                    unit_kwh, eta_discharge)
                if (grid_load > unit_kwh * eta_discharge + 1e-9
                        and not unavoidable_grid_load):
                    continue
                if grid_load + grid_charge > max_grid_slot_kwh + 1e-9:
                    continue
                floor_shortfall = (0.0 if required_soc_pcts is None else
                    max(0.0, float(required_soc_pcts[index]) - following * step))
                candidate = (score[0] + floor_shortfall * floor_shortfall,
                             score[1] - following * step)
                previous = next_scores.get(following)
                if previous is None or candidate < previous:
                    next_scores[following] = candidate
                    next_predecessors[following] = current
        if not next_scores:
            raise RuntimeError(f"SOC_NO_PHYSICALLY_REACHABLE_STATE:{index}")
        scores = next_scores
        predecessor_layers.append(next_predecessors)
        ceilings.append(max(scores) * step)
    terminal_unit = min(scores, key=lambda unit: (scores[unit][0], scores[unit][1], -unit))
    path_units = [terminal_unit]
    for layer in reversed(predecessor_layers[1:]):
        path_units.append(layer[path_units[-1]])
    path_units.reverse()
    path = [unit * step for unit in path_units]
    return {"reachable_soc_ceiling_pcts": ceilings, "feasible_soc_path_pcts": path}


def reachable_soc_ceiling_pcts(
        rows: list[dict], initial_soc_pct: float, capacity_kwh: float,
        reserve_pct: float, eta_c: float, eta_d: float,
        max_power_kw: float, slot_minutes: int, soc_step_pct: float = 0.10,
        max_grid_import_kw: float = 12.0,
) -> list[float]:
    """Compute each slot's physical upper SOC bound with exact transitions."""
    return _soc_reachability_profile(
        rows, initial_soc_pct, capacity_kwh, reserve_pct, eta_c, eta_d,
        max_power_kw, slot_minutes, soc_step_pct,
        max_grid_import_kw=max_grid_import_kw)["reachable_soc_ceiling_pcts"]


def validate_recoverable_soc_requirements(
        rows: list[dict], required_soc_pcts: list[float], initial_soc_pct: float,
        reserve_pct: float, target_cap_pct: float, *,
        capacity_kwh: float | None = None, eta_c: float = 0.9,
        eta_d: float = 0.95, max_power_kw: float = 5.0,
        slot_minutes: int = 15, soc_step_pct: float = 0.10,
        max_grid_import_kw: float = 12.0) -> dict:
    """Recover missed bridges and bound every hard SOC floor to reachability.

    A requirement above 100% or above the highest SOC physically reachable at
    that slot is not an executable target. Preserve the technical reserve,
    disable battery sales, and route unavoidable household demand to grid.
    """
    recovery = recoverable_soc_requirements(
        rows, required_soc_pcts, initial_soc_pct, reserve_pct)
    required = list(recovery["required_soc_pcts"])
    physical_cap = 100.0
    reachable = None
    if capacity_kwh is not None:
        profile = _soc_reachability_profile(
            rows, initial_soc_pct, capacity_kwh, reserve_pct,
            eta_c, eta_d, max_power_kw, slot_minutes, soc_step_pct, required,
            max_grid_import_kw=max_grid_import_kw)
        reachable = profile["reachable_soc_ceiling_pcts"]
        feasible_path = profile["feasible_soc_path_pcts"]
        if len(reachable) != len(required):
            raise ValueError("RECOVERY_SOC_REACHABILITY_LENGTH_MISMATCH")
    limited_indices = []
    unreachable_indices = []
    bounded = list(required)
    for index, requested in enumerate(required):
        ceiling = physical_cap if reachable is None else min(physical_cap, reachable[index])
        if requested > physical_cap + 1e-9:
            # An over-cap bridge is impossible, even when a BUY window could
            # physically fill the battery to 100%. Do not turn an impossible
            # safety requirement into a command to buy to full: relax it to
            # the technical reserve and disable battery sales for this run.
            limited_indices.append(index)
            unreachable_indices.append(index)
            bounded[index] = max(0.0, min(100.0, float(reserve_pct)))
        elif requested > ceiling + 1e-9 or (
                reachable is not None and requested > feasible_path[index] + 1e-9):
            unreachable_indices.append(index)
            limited_indices.append(index)
            if reachable is None:
                bounded[index] = max(0.0, ceiling)
            else:
                # A per-slot ceiling ignores how earlier floors constrain the
                # trajectory. Clamp to the jointly feasible no-sales path even
                # when the requested value also exceeds the local ceiling.
                bounded[index] = max(0.0, min(ceiling, feasible_path[index]))
    if limited_indices:
        recovery["required_soc_pcts"] = bounded
        recovery["capacity_limited_indices"] = sorted(set(limited_indices))
        recovery["unreachable_indices"] = sorted(set(unreachable_indices))
        recovery["disable_battery_sales"] = True
        recovery["reachable_soc_ceiling_pcts"] = reachable
    else:
        recovery["required_soc_pcts"] = bounded
        recovery["capacity_limited_indices"] = []
        recovery["unreachable_indices"] = []
        recovery["disable_battery_sales"] = False
        recovery["reachable_soc_ceiling_pcts"] = reachable
    return recovery

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


def planning_tou_programs(live_programs: list[dict] | None, baselines: dict[str, float]) -> list[dict]:
    """Use live TOU times but immutable configured SOC baselines for planning."""
    if not isinstance(live_programs, list) or not live_programs:
        raise RuntimeError("TOU_PROGRAMS_UNAVAILABLE")
    if any(not isinstance(program, dict) for program in live_programs):
        raise RuntimeError("TOU_PROGRAMS_INVALID")
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
        "eligible": (sell_now > 0.0 and required_sell is not None
                     and sell_now >= required_sell),
    }


def assess_sale_plan_against_no_sale(sale_plan: dict, no_sale_plan: dict,
                                     rows: list[dict], min_margin: float,
                                     capacity_kwh: float, eta_c: float,
                                     tolerance_kwh: float = 0.05,
                                     eta_d: float = 0.95,
                                     degradation: float = 0.08) -> dict:
    """Check the full cost of export against keeping the energy for native load.

    The counterfactual has the same forecast and required terminal SOC. Both
    objectives include the grid purchases actually selected by each plan.
    Any remaining inventory difference is valued separately; grid supply to
    the house may not increase as a consequence of battery sale.
    """
    sales = sale_plan["flows"]
    baseline = no_sale_plan["flows"]
    if len(sales) != len(rows) or len(baseline) != len(rows):
        raise ValueError("SALE_COUNTERFACTUAL_LENGTH_MISMATCH")
    sold = sum(max(0.0, float(f.get("battery_sell_kwh") or 0.0)) for f in sales)
    extra_native_import = sum(max(0.0, float(a.get("grid_load_kwh") or 0.0)
                                  - float(b.get("grid_load_kwh") or 0.0))
                              for a, b in zip(sales, baseline))
    extra_grid_charge = sum(max(0.0, float(a.get("grid_charge_kwh") or 0.0)
                                - float(b.get("grid_charge_kwh") or 0.0))
                            for a, b in zip(sales, baseline))
    extra_grid_charge_cost = sum(
        max(0.0, float(a.get("grid_charge_kwh") or 0.0)
            - float(b.get("grid_charge_kwh") or 0.0))
        * float(row.get("price_buy_pln_kwh") or 0.0)
        for a, b, row in zip(sales, baseline, rows))

    # Keep a FIFO cost basis for battery energy charged from the grid. Without
    # this, SOC above the terminal target can look free: a plan can buy energy,
    # later export it below cost, and still meet the terminal SOC contract.
    grid_inventory: list[list[float]] = []  # internal kWh, cost PLN/internal kWh
    grid_load_inventory: list[list[float]] = []  # native-load kWh, avoided import PLN/kWh
    grid_load_sale_loss = 0.0
    grid_origin_sale_kwh = 0.0
    grid_origin_sale_revenue = 0.0
    grid_origin_sale_cost = 0.0
    grid_origin_sale_loss = 0.0
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    for flow, row in zip(sales, rows):
        grid_charge = max(0.0, float(flow.get("grid_charge_kwh") or 0.0))
        if grid_charge > 1e-9:
            grid_inventory.append([
                grid_charge * charge_efficiency,
                float(row.get("price_buy_pln_kwh") or 0.0) / charge_efficiency,
            ])
        native_discharge = (max(0.0, float(flow.get("battery_to_load_kwh") or 0.0))
                            / discharge_efficiency)
        while native_discharge > 1e-9 and grid_inventory:
            used = min(native_discharge, grid_inventory[0][0])
            grid_inventory[0][0] -= used
            native_discharge -= used
            if grid_inventory[0][0] <= 1e-9:
                grid_inventory.pop(0)
        grid_load = max(0.0, float(flow.get("grid_load_kwh") or 0.0))
        if grid_load > tolerance_kwh + 1e-9:
            grid_load_inventory.append([
                grid_load, float(row.get("price_buy_pln_kwh") or 0.0),
            ])
        battery_sale = max(0.0, float(flow.get("battery_sell_kwh") or 0.0))
        sale_internal = battery_sale / discharge_efficiency
        sale_output_remaining = battery_sale
        sale_price = float(row.get("price_sell_pln_kwh") or 0.0)
        while sale_output_remaining > 1e-9 and grid_load_inventory:
            used = min(sale_output_remaining, grid_load_inventory[0][0])
            required_price = (grid_load_inventory[0][1]
                              + float(degradation) + float(min_margin))
            grid_load_sale_loss += used * max(0.0, required_price - sale_price)
            grid_load_inventory[0][0] -= used
            sale_output_remaining -= used
            if grid_load_inventory[0][0] <= 1e-9:
                grid_load_inventory.pop(0)
        while sale_internal > 1e-9 and grid_inventory:
            used = min(sale_internal, grid_inventory[0][0])
            revenue = used * sale_price * discharge_efficiency
            cost = used * grid_inventory[0][1]
            grid_origin_sale_kwh += used
            grid_origin_sale_revenue += revenue
            grid_origin_sale_cost += cost
            grid_origin_sale_loss += max(
                0.0, cost + used * discharge_efficiency
                * (float(degradation) + float(min_margin)) - revenue)
            grid_inventory[0][0] -= used
            sale_internal -= used
            if grid_inventory[0][0] <= 1e-9:
                grid_inventory.pop(0)
    gross_gain = float(no_sale_plan["objective_pln"]) - float(sale_plan["objective_pln"])
    ending_energy_shortfall = max(0.0,
        (float(baseline[-1].get("soc_end_pct") or 0.0)
         - float(sales[-1].get("soc_end_pct") or 0.0))
        * capacity_kwh / 100.0)
    last_sale = max((i for i, flow in enumerate(sales)
                     if float(flow.get("battery_sell_kwh") or 0.0) > 1e-9),
                    default=-1)
    future_rows = rows[last_sale + 1:]
    feasible_buy_prices = [float(row["price_buy_pln_kwh"])
                           for row in future_rows
                           if row.get("price_buy_pln_kwh") is not None
                           and strict_database_bool(row.get("buy_window", False), "buy_window")]
    future_prices = [float(row["price_buy_pln_kwh"])
                     for row in future_rows if row.get("price_buy_pln_kwh") is not None]
    # Without a permitted BUY, energy kept in the battery can avoid the
    # expensive future native-load tariff. A theoretical cheap price outside
    # BUY is not a feasible replacement for depleted terminal inventory.
    replacement_price = (min(feasible_buy_prices) if feasible_buy_prices else
                         max(future_prices) if future_prices else None)
    ending_energy_cost = (
        0.0 if ending_energy_shortfall <= 1e-9 else
        ending_energy_shortfall * replacement_price / max(0.01, eta_c)
        if replacement_price is not None else None)
    gain = (gross_gain - ending_energy_cost
            if ending_energy_cost is not None else None)
    # The optimizer already charges degradation and minimum margin per kWh of
    # export. The positive counterfactual gain is an independent final check.
    eligible = (sold <= 1e-9 or
                (gain is not None and gain > 1e-6
                 and extra_native_import <= tolerance_kwh + 1e-9
                 and grid_origin_sale_loss <= 1e-6
                 and grid_load_sale_loss <= 1e-6))
    return {"eligible": eligible, "sold_kwh": round(sold, 6),
            "gross_gain_pln": round(gross_gain, 6),
            "net_gain_pln": round(gain, 6) if gain is not None else None,
            "ending_energy_shortfall_kwh": round(ending_energy_shortfall, 6),
            "ending_energy_replacement_cost_pln": (
                round(ending_energy_cost, 6) if ending_energy_cost is not None else None),
            "extra_native_import_kwh": round(extra_native_import, 6),
            "extra_grid_charge_kwh": round(extra_grid_charge, 6),
            "extra_grid_charge_cost_pln": round(extra_grid_charge_cost, 6),
            "grid_load_sale_loss_pln": round(grid_load_sale_loss, 6),
            "grid_origin_sale_kwh": round(grid_origin_sale_kwh, 6),
            "grid_origin_sale_revenue_pln": round(grid_origin_sale_revenue, 6),
            "grid_origin_sale_cost_pln": round(grid_origin_sale_cost, 6),
            "grid_origin_sale_loss_pln": round(grid_origin_sale_loss, 6),
            "minimum_margin_pln_kwh": min_margin}


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
                        an…24631 tokens truncated…          float(details["reachable"]), 3),
                            "action": "KEEP_LAST_ACCEPTED_PLAN_REVIEW_BUY_WINDOWS",
                            "battery_sales_disabled": True,
                            "rce_windows_unchanged": True,
                        }, "ERROR")
                        raise RuntimeError(
                            "BUY_TARGET_UNREACHABLE_PRESERVED_PLAN:"
                            f"slot={horizon_rows[failed_index]['slot_start']}:"
                            f"requested={details['requested']}:"
                            f"reachable={details['reachable']}"
                        ) from exc
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
                        recovery_action, recovered_terminal = terminal_soc_recovery(
                            terminal_soc, safety_required_soc[-1], battery_sales_enabled,
                            current_day=terminal_day == local_now().date())
                        if recovery_action == "RELAX_HISTORICAL_TARGET":
                            record_event("historical_terminal_soc_relaxed_for_feasibility", "planner", {
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "safety_soc_pct": safety_required_soc[-1],
                                "reason": str(exc),
                            }, "WARNING")
                            terminal_soc = recovered_terminal
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
                    if battery_sales_enabled:
                        battery_sales_enabled = False
                        record_event("battery_sales_disabled_for_daily_soc", "planner", {
                            "slot_start": str(horizon_rows[failed_index]["slot_start"]),
                            "requested_soc_pct": daily_required_soc[failed_index],
                            "reason": str(exc),
                        }, "WARNING")
                        continue
                    requested_required = enforced_daily_required[failed_index]
                    reachable_required = target_contract["reachable_required"][failed_index]
                    recoverable_required = relax_soc_requirement_to_reachable(
                        requested_required, reachable_required, reserve)
                    if recoverable_required >= requested_required - 1e-9:
                        raise RuntimeError(f"SOC_SAFETY_BRIDGE_UNREACHABLE:{failed_index}") from exc
                    enforced_daily_required[failed_index] = recoverable_required
                    is_daily_close = failed_index in daily_close_indices
                    daily_close_indices.discard(failed_index)
                    waived_daily_closes.add(failed_index)
                    record_event("soc_bridge_requirement_unreachable", "planner", {
                        "slot_start": str(horizon_rows[failed_index]["slot_start"]),
                        "requested_soc_pct": round(requested_required, 3),
                        "safety_soc_pct": round(safety_required_soc[failed_index], 3),
                        "reachable_soc_pct": round(recoverable_required, 3),
                        "shortfall_pct": round(max(
                            0.0, requested_required - recoverable_required), 3),
                        "daily_close": is_daily_close,
                        "reason": str(exc),
                    }, "WARNING")
            if target_seed.get("terminal_shortfall_pct", 0.0) > 1e-9:
                requested_terminal_soc = terminal_soc
                terminal_soc = float(target_seed["achieved_terminal_soc_pct"])
                record_event("current_day_terminal_soc_unreachable", "planner", {
                    "terminal_day": str(terminal_day),
                    "requested_soc_pct": requested_terminal_soc,
                    "achieved_soc_pct": terminal_soc,
                    "shortfall_pct": target_seed["terminal_shortfall_pct"],
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
                target_commitment_fallback_level = 0
                # Never downgrade a failed BUY commitment into a lower target.
                # Keep the previously accepted plan if no physically feasible
                # allocation exists under the immutable RCE permissions.
                original_import_caps = (None if minimum_targets is None
                                        else list(minimum_targets))
                original_required_pcts = (None if required_pcts is None
                                          else list(required_pcts))
                while True:
                    try:
                        result = optimize_energy_horizon(
                            horizon_rows,soc_now,capacity,reserve,eta_c,eta_d,
                            degradation,min_margin,max_kw,int(OPTIONS["slot_minutes"]),
                            floors,terminal_soc,internal_soc_step,target_cap,
                            minimum_targets,hard_indices,due_indices,required_pcts,
                            battery_sales_enabled=battery_sales_enabled,
                            allow_terminal_shortfall=terminal_shortfall_allowed,
                            max_grid_import_kw=max_grid_import_kw,
                            grid_charge_caps=grid_charge_caps,
                            minimum_grid_charge_kwh=flow_threshold)
                        break
                    except RuntimeError as exc:
                        slot_error_prefix = "No feasible SOC state at horizon slot "
                        # A failed SOC bridge must not be repaired by removing
                        # BUY deadlines or lowering the required energy.
                        # This preserves the last accepted plan rather than
                        # publishing a horizon that buys too little.
                        if (pass_name == "TARGET_COMMITMENT"
                                and battery_sales_enabled
                                and str(exc).startswith(slot_error_prefix)):
                            battery_sales_enabled = False
                            record_event("battery_sales_disabled_for_target_commitment", "planner", {
                                "stage": pass_name,
                                "reason": str(exc),
                            }, "WARNING")
                            continue
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
                        recovery_action, recovered_terminal = terminal_soc_recovery(
                            terminal_soc, safety_required_soc[-1], battery_sales_enabled,
                            current_day=terminal_day == local_now().date())
                        if recovery_action == "RELAX_HISTORICAL_TARGET":
                            record_event("historical_terminal_soc_relaxed_for_feasibility", "planner", {
                                "stage": pass_name,
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "safety_soc_pct": safety_required_soc[-1],
                                "reason": str(exc),
                            }, "WARNING")
                            terminal_soc = recovered_terminal
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
                # Keep the post-dispatch assertions aligned with recovery.
                # A retry can remove optional deadlines locally while keeping
                # the import cap; publishing against the original set would
                # turn that deliberate recovery into SOC_TARGET_NOT_REACHED.
                result["effective_target_due_indices"] = set(due_indices or ())
                result["effective_required_soc_pcts"] = list(
                    required_pcts if required_pcts is not None
                    else [reserve] * len(horizon_rows))
                return result
            # Convert the immutable TOU baselines into sale-only safety floors.
            # Iterate once after applying them because a permitted 5/6 bridge
            # override is valid only while the resulting plan retains its real
            # near-term battery BUY.
            for _ in range(2):
                guarded_floors = tou_sale_safety_floors(
                    horizon_rows, tou_by_index, target_seed["flows"],
                    capacity, reserve, eta_d, uncertainty_weight,
                    int(OPTIONS.get("tou_bridge_override_max_minutes", 180)),
                    int(OPTIONS["slot_minutes"]), enforced_daily_required)
                if all(abs(a - b) < 1e-9
                       for a, b in zip(guarded_floors, optimized_floors)):
                    break
                optimized_floors = guarded_floors
                target_seed = optimize_remaining_pass(
                    "TOU_GUARD_ITERATION", optimized_floors,
                    required_pcts=enforced_daily_required)
            final_guarded = tou_sale_safety_floors(
                horizon_rows, tou_by_index, target_seed["flows"],
                capacity, reserve, eta_d, uncertainty_weight,
                int(OPTIONS.get("tou_bridge_override_max_minutes", 180)),
                int(OPTIONS["slot_minutes"]), enforced_daily_required)
            if any(abs(a - b) >= 1e-9
                   for a, b in zip(final_guarded, optimized_floors)):
                # A BUY must never validate its own relaxation circularly. If
                # the two-pass result is not stable, retain the stricter floor.
                optimized_floors = [max(a, b)
                                    for a, b in zip(final_guarded, optimized_floors)]
                target_seed = optimize_remaining_pass(
                    "TOU_GUARD_FINAL", optimized_floors,
                    required_pcts=enforced_daily_required)
            for sale_guard_attempt in range(2):
                commitment = target_contract
                required_soc = list(enforced_daily_required)
                charge_targets = list(target_contract["charge_targets"])
                for index in waived_daily_closes:
                    charge_targets[index] = max(
                        charge_targets[index], daily_required_soc[index])
                target_due_indices = set(target_contract["buy_due_indices"])
                selected_buy_indices = set(target_contract["selected_buy_indices"])
                optimization=optimize_remaining_pass(
                    "TARGET_COMMITMENT", optimized_floors, charge_targets, set(),
                    target_due_indices, required_soc)
                target_due_indices = active_soc_target_deadlines(
                    optimization, target_due_indices)
                sell_indices = {
                    index for index, flow in enumerate(optimization["flows"])
                    if float(flow.get("battery_sell_kwh") or 0.0) > flow_threshold
                }
                unbacked_load_indices = voluntary_load_without_charging_indices(
                    horizon_rows, optimization["flows"],
                    technical_threshold, flow_threshold)
                if not sell_indices:
                    voluntary_import = bool(unbacked_load_indices)
                    if voluntary_import and battery_sales_enabled:
                        # A grid supplied native load is an allowed exception
                        # only when it protects energy for a later sale. If the
                        # selected route has no sale, replan with that exception
                        # disabled instead of publishing a hidden house import.
                        battery_sales_enabled = False
                        target_seed = optimize_remaining_pass(
                            "GRID_LOAD_WITHOUT_SALE_GUARD", optimized_floors,
                            required_pcts=enforced_daily_required)
                        ensure_deadline("GRID_LOAD_WITHOUT_SALE_GUARD")
                        continue
                    if voluntary_import:
                        record_event("voluntary_native_import_rejected", "planner", {
                            "slots": [
                                {"slot_start": str(horizon_rows[index]["slot_start"]),
                                 "buy_window": horizon_rows[index].get("buy_window"),
                                 "sale_window": horizon_rows[index].get("sale_window"),
                                 "grid_charge_kwh": optimization["flows"][index].get("grid_charge_kwh"),
                                 "voluntary_grid_load_kwh": optimization["flows"][index].get("voluntary_grid_load_kwh")}
                                for index in sorted(unbacked_load_indices)],
                            "charge_threshold_kwh": flow_threshold,
                            "technical_threshold_kwh": technical_threshold,
                        }, "ERROR")
                        raise RuntimeError("VOLUNTARY_GRID_LOAD_WITHOUT_SALE")
                    break
                voluntary_without_later_sale = any(
                    index in unbacked_load_indices
                    and not any(sale_index > index for sale_index in sell_indices)
                    for index, flow in enumerate(optimization["flows"])
                )
                if voluntary_without_later_sale:
                    battery_sales_enabled = False
                    target_seed = optimize_remaining_pass(
                        "GRID_LOAD_AFTER_SALE_GUARD", optimized_floors,
                        required_pcts=enforced_daily_required)
                    ensure_deadline("GRID_LOAD_AFTER_SALE_GUARD")
                    continue
                try:
                    no_sale_optimization = optimize_energy_horizon(
                        horizon_rows, soc_now, capacity, reserve, eta_c, eta_d,
                        degradation, min_margin, max_kw, int(OPTIONS["slot_minutes"]),
                        optimized_floors, terminal_soc, internal_soc_step, target_cap,
                        charge_targets, set(), target_due_indices, required_soc,
                        battery_sales_enabled=False,
                        allow_terminal_shortfall=terminal_shortfall_allowed,
                        max_grid_import_kw=max_grid_import_kw,
                        grid_charge_caps=grid_charge_caps,
                        minimum_grid_charge_kwh=flow_threshold)
                    sale_assessment = assess_sale_plan_against_no_sale(
                        optimization, no_sale_optimization, horizon_rows,
                        min_margin, capacity, eta_c, technical_threshold,
                        eta_d=eta_d, degradation=degradation)
                except RuntimeError as exc:
                    sale_assessment = {"eligible": False, "reason": str(exc)}
                record_event("battery_sale_counterfactual", "planner", {
                    "run_id": run_id, **sale_assessment,
                    "comparison": "same_forecast_and_terminal_target",
                }, "INFO" if sale_assessment["eligible"] else "WARNING")
                if sale_assessment["eligible"]:
                    break
                battery_sales_enabled = False
                target_seed = optimize_remaining_pass(
                    "SALE_GUARD_REPLAN", optimized_floors,
                    required_pcts=enforced_daily_required)
                ensure_deadline("SALE_GUARD_REPLAN")
            else:
                raise RuntimeError("SALE_GUARD_REPLAN_EXHAUSTED")
            effective_required = optimization.get("effective_required_soc_pcts")
            if effective_required is not None:
                required_soc = effective_required_soc_for_dispatch(
                    enforced_daily_required, effective_required)
            targets=list(optimization.get("effective_target_pcts",charge_targets))
            unreachable_buy_targets = [
                {
                    "slot_start": str(horizon_rows[index]["slot_start"]),
                    "requested_soc_pct": round(float(charge_targets[index]), 3),
                    "achievable_soc_pct": round(float(targets[index]), 3),
                    "shortfall_pct": round(max(
                        0.0, float(charge_targets[index]) - float(targets[index])), 3),
                    "planned_import_kwh": round(float(
                        optimization["flows"][index].get("grid_charge_kwh") or 0.0), 3),
                }
                for index in sorted(target_due_indices)
                if float(targets[index]) + 0.01 < float(charge_targets[index])
            ]
            if unreachable_buy_targets:
                record_event("buy_soc_target_shortfall", "planner", {
                    "slots": unreachable_buy_targets,
                    "action": "PRESERVE_SLOT_BALANCE_AND_IMPORT_CAP",
                }, "WARNING")
            target_due_indices = validate_active_soc_target_deadlines(
                optimization, target_due_indices, targets)
            physical_buy_audit = validate_physical_buy_commitments(
                horizon_rows, optimization["flows"], target_due_indices,
                targets, max_kw, int(OPTIONS["slot_minutes"]),
                capacity, eta_c)
            record_event("buy_physical_commitment_validated", "planner", {
                "run_id": run_id, **physical_buy_audit,
            })
            deadline_targets = list(targets)
            targets = planned_buy_execution_targets(
                horizon_rows, optimization["flows"], targets, flow_threshold)
            record_event("economic_buy_targets", "planner", {
                "run_id": run_id,
                "optional_caps_pct": grid_charge_caps,
                "execution_targets_pct": targets,
                "required_deadline_targets_pct": deadline_targets,
                "policy": "CHEAPER_BUY_REPLACES_DEARER_BUY_SHARED_RUN_TARGET",
            })
            audit_stage(cur,run_id,"TARGET_COMMITMENT","OK",len(rows),
                        f"deterministic SOC contracts; selected_buy_slots={len(selected_buy_indices)}")
            record_event("daily_plan_variant_selected","planner",{
                "run_id":run_id,"terminal_soc_pct":round(terminal_soc,3),
                "selected":"DETERMINISTIC_SOC_CONTRACT_V4",
                "target_seed_objective_pln":float(target_seed["objective_pln"]),
                "final_dispatch_objective_pln":float(optimization["objective_pln"]),
                "target_source":"FORECAST_LOAD_PV_AND_RCE_WINDOWS",
            })
            ensure_deadline("TARGET_COMMITMENT")
            ensure_deadline("DISPATCH")
            attempt["candidate_rows"] = [
                {
                    "slot_start": horizon_rows[index].get("slot_start"),
                    "market_window": horizon_rows[index].get("market_window"),
                    "buy_window": horizon_rows[index].get("buy_window"),
                    "sale_window": horizon_rows[index].get("sale_window"),
                    "price_buy_pln_kwh": horizon_rows[index].get("price_buy_pln_kwh"),
                    "price_sell_pln_kwh": horizon_rows[index].get("price_sell_pln_kwh"),
                    "forecast_pv_total_kwh": horizon_rows[index].get("forecast_pv_total_kwh"),
                    "forecast_load_kwh": horizon_rows[index].get("forecast_load_kwh"),
                    "soc_start_plan_pct": flow.get("soc_start_pct"),
                    "soc_end_plan_pct": flow.get("soc_end_pct"),
                    "soc_required_pct": required_soc[index],
                    "soc_charge_target_pct": targets[index],
                    "planned_buy_kwh": flow.get("grid_charge_kwh"),
                    "planned_sell_kwh": flow.get("battery_sell_kwh"),
                    "planned_battery_discharge_kwh": flow.get("battery_discharge_internal_kwh"),
                    "planned_grid_load_kwh": flow.get("grid_load_kwh"),
                    "planned_pv_to_bat_kwh": flow.get("pv_to_bat_kwh"),
                }
                for index, flow in enumerate(optimization["flows"])
            ]
            for index, flow in enumerate(optimization["flows"]):
                row = horizon_rows[index]
                battery_sell = float(flow.get("battery_sell_kwh") or 0.0)
                grid_charge = float(flow.get("grid_charge_kwh") or 0.0)
                soc_end = float(flow.get("soc_end_pct") or 0.0)
                if (battery_sell > flow_threshold
                        and not strict_database_bool(row.get("sale_window"), "sale_window")):
                    raise RuntimeError(f"SALE_OUTSIDE_WINDOW:{index}")
                if battery_sell > flow_threshold and float(
                        row.get("price_sell_pln_kwh") or 0.0) <= 0.0:
                    raise RuntimeError(f"SALE_AT_NONPOSITIVE_PRICE:{index}")
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
                if index in target_due_indices and soc_end + 0.01 < deadline_targets[index]:
                    raise RuntimeError(
                        f"SOC_TARGET_NOT_REACHED:{index}:{soc_end}<{deadline_targets[index]}")
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
                grid_policy="BUY_ALLOWED" if buy>flow_threshold else ("NO_BUY" if sell_bat else "NEUTRAL")
                export_policy="SELL_BAT" if sell_bat else ("NO_SELL_PV" if no_sell_pv else "NEUTRAL")
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
                  planned_battery_discharge_kwh=%s,planned_sell_kwh=%s,
                  grid_policy_planned=%s,export_policy_planned=%s,
                  sell_bat_policy_allowed=%s,sell_pv_policy_allowed=%s,
                  planned_pv_to_bat_kwh=%s,soc_updated_at=NOW(6)
                  WHERE run_id=%s AND slot_start=%s""",
                  (round(item["start"],2),round(item["end"],2),round(reserve,2),
                   round(required,2),round(target,2),round(floor,2),round(floor,2),round(target,2),
                   commitment["due"][i],commitment["source"][i],round(commitment["reserved_pv_kwh"][i],6),round(buy,6),
                   round(item["charge"],6),round(item["discharge"],6),round(item["sell"],6),
                   grid_policy,export_policy,
                   int(bool(economics["eligible"])),int(sell_price>0.0),
                   round(pv_to_bat,6),run_id,row["slot_start"]))
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
              p.planned_buy_kwh=s.planned_buy_kwh,
              p.planned_sell_kwh=s.planned_sell_kwh,
              p.planned_battery_charge_kwh=s.planned_battery_charge_kwh,
              p.planned_battery_discharge_kwh=s.planned_battery_discharge_kwh,
              p.grid_policy_planned=s.grid_policy_planned,p.export_policy_planned=s.export_policy_planned,
              p.sell_bat_policy_allowed=s.sell_bat_policy_allowed,
              p.sell_pv_policy_allowed=s.sell_pv_policy_allowed,
              p.planned_pv_to_bat_kwh=s.planned_pv_to_bat_kwh,
              p.heat_pump_window=s.heat_pump_window,
              p.plan_run_id=%s,p.plan_stage='PUBLISHED',
              p.plan_stage_version=%s,p.plan_stage_updated_at=NOW(6),
              p.plan_validation_status='ACCEPTED',p.plan_validation_reason='OK',
              p.plan_published_at=NOW(6),p.plan_published=1 WHERE p.actual_recorded_at IS NULL AND p.slot_start>=%s""",
              (run_id,run_id,PLANNER_VERSION,cutoff))
            published=cur.rowcount
            cur.execute("""UPDATE ems_gpt_plan_runs SET status='PUBLISHED',current_stage='VALIDATE',
              validated_at=NOW(6),published_at=NOW(6),validation_status='ACCEPTED',
              validation_reason='OK',updated_at=NOW(6) WHERE run_id=%s""",(run_id,))
            audit_stage(cur,run_id,"VALIDATE","ACCEPTED",published,"atomic publish")
        record_event("plan_published","planner",{"run_id":run_id,"run_type":run_type,"rows":published})
        for shortfall in hp_shortfalls:
            record_event("hp_minimum_heating_shortfall", "planner", shortfall, "WARNING")
        return {"run_id":run_id,"rows":published}

    def run_planner(run_type: str = "scheduled") -> dict:
        attempt = {
            "attempt_id": str(uuid.uuid4()),
            "run_type": str(run_type)[:32],
            "started_at": local_now().replace(tzinfo=None).isoformat(),
            "failed_stage": "STARTUP",
            "candidate_rows": [],
        }
        try:
            return _run_planner(run_type, attempt)
        except Exception as exc:
            reason = str(exc)[:4000]
            attempt["failure_reason"] = reason
            attempt["status"] = "REJECTED"
            tokens = reason.split(":")
            failed_index = None
            if len(tokens) > 1 and tokens[1].isdigit():
                failed_index = int(tokens[1])
            candidates = attempt.get("candidate_rows") or []
            failed_slot = (
                candidates[failed_index].get("slot_start")
                if failed_index is not None and failed_index < len(candidates)
                else None
            )
            try:
                with db() as conn, conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO ems_gpt_plan_attempts
                          (attempt_id,run_id,run_type,status,attempted_at,failed_stage,
                           failure_index,failed_slot,initial_soc_pct,failure_reason,
                           candidate_rows_json)
                          VALUES(%s,%s,%s,'REJECTED',NOW(6),%s,%s,%s,%s,%s,%s)""",
                        (
                            attempt["attempt_id"], attempt.get("planner_run_id"),
                            attempt["run_type"], attempt.get("failed_stage"),
                            failed_index, failed_slot, attempt.get("initial_soc_pct"),
                            reason, json.dumps(candidates, ensure_ascii=False, default=str),
                        ),
                    )
                    cur.execute(
                        """DELETE FROM ems_gpt_plan_attempts
                          WHERE attempted_at < NOW(6) - INTERVAL 90 DAY"""
                    )
            except Exception as persist_error:
                record_event("planner_attempt_persist_failed", "planner", {
                    "attempt_id": attempt["attempt_id"],
                    "planner_run_id": attempt.get("planner_run_id"),
                    "reason": str(persist_error)[:1000],
                    "original_failure": reason[:1000],
                }, "ERROR")
            raise

    return SimpleNamespace(
        optimize_hp_heating_slots=optimize_hp_heating_slots,
        run_planner=run_planner,
    )

