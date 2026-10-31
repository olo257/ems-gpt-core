"""SOC reachability and recovery algorithms used by the planner."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Callable

from planner_validation_service import strict_database_bool


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
        grid_charge_caps: list[float] | None = None,
        minimum_grid_charge_kwh: float = 0.0,
) -> dict:
    """Return prefix reachability and a feasible path with minimum floor shortfall.

    Transitions mirror the optimizer's physical limits and voluntary-grid-load
    guard, with battery sales disabled. A per-slot maximum alone is not enough:
    a high floor in one slot can make a later BUY floor unreachable. The DP
    therefore scores complete paths against the whole required-floor sequence.
    """
    if required_soc_pcts is not None and len(rows) != len(required_soc_pcts):
        raise ValueError("RECOVERY_SOC_REQUIREMENT_LENGTH_MISMATCH")
    if grid_charge_caps is not None and len(rows) != len(grid_charge_caps):
        raise ValueError("RECOVERY_SOC_GRID_CAP_LENGTH_MISMATCH")
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
        grid_cap_unit = (last_unit if grid_charge_caps is None else
                         int(math.ceil(max(reserve, min(100.0,
                             float(grid_charge_caps[index]))) / step - 1e-9)))
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
                    # Match the optimizer: a grid BUY is an all-available
                    # slot charge, bounded by the same SOC ceiling. Partial
                    # BUY transitions make reachability optimistic and can
                    # claim a bridge is feasible when dispatch rejects it.
                    if grid_charge > 1e-9:
                        if following > max(current, grid_cap_unit):
                            continue
                        site_charge_input = surplus + max(
                            0.0, max_grid_slot_kwh - deficit)
                        full_internal = min(
                            max_internal_charge,
                            site_charge_input * eta_charge)
                        full_up = int(math.floor(
                            full_internal / unit_kwh + 1e-9))
                        full_next = min(
                            last_unit, current + full_up,
                            max(current, grid_cap_unit))
                        if following != full_next:
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
                # Match dispatch: during a real BUY charge, the grid supplies
                # native load as well as the battery. Rejecting that load here
                # erased every charging transition whenever the house drew
                # more than one SOC quantum, and collapsed BUY ceilings.
                grid_load_during_buy_charge = (
                    buy_allowed and grid_charge > max(
                        1e-9, float(minimum_grid_charge_kwh)))
                if (grid_load > unit_kwh * eta_discharge + 1e-9
                        and not unavoidable_grid_load
                        and not grid_load_during_buy_charge):
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
        max_grid_import_kw: float = 12.0,
        grid_charge_caps: list[float] | None = None,
        minimum_grid_charge_kwh: float = 0.0) -> dict:
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
            max_grid_import_kw=max_grid_import_kw,
            grid_charge_caps=grid_charge_caps,
            minimum_grid_charge_kwh=minimum_grid_charge_kwh)
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

def current_soc_for_replan(setting: Callable) -> float:
    """Read fresh battery SOC from Home Assistant for this planner invocation."""
    soc = setting("sensor.inverter_battery", math.nan)
    if not math.isfinite(soc) or not 0.0 <= soc <= 100.0:
        raise RuntimeError(f"INVALID_INITIAL_SOC:sensor.inverter_battery={soc!r}")
    return float(soc)


def project_soc_to_horizon_start(
        measured_soc_pct: float, now: datetime, horizon_start: datetime,
        active_row: dict | None, telemetry: dict | None,
        capacity_kwh: float, max_power_kw: float, eta_c: float, eta_d: float,
        slot_minutes: int = 15, max_age_seconds: float = 120.0) -> dict:
    """Align fresh SOC with the next-slot horizon using only remaining time.

    Normalized telemetry power is DC battery power: do not apply efficiency
    twice. Missing/stale power never earns forecast charging credit; planned
    discharge remains a conservative fallback. The live slot is not rewritten.
    """
    measured = float(measured_soc_pct)
    if not math.isfinite(measured) or not 0.0 <= measured <= 100.0:
        raise RuntimeError(f"INVALID_INITIAL_SOC:{measured!r}")
    remaining = (horizon_start - now).total_seconds()
    if not 0.0 <= remaining <= slot_minutes * 60:
        raise ValueError("SOC_PROJECTION_INTERVAL_MISMATCH")
    row, sample = active_row or {}, telemetry or {}
    captured = sample.get("captured_at")
    age = (now - captured).total_seconds() if isinstance(captured, datetime) else math.inf
    powers = [sample.get("battery_charge_power_w"), sample.get("battery_discharge_power_w")]
    fresh = (0.0 <= age <= max_age_seconds and all(
        value is not None and math.isfinite(float(value)) and float(value) >= 0.0
        for value in powers))
    if fresh:
        charge_kw = min(float(powers[0])/1000.0, max_power_kw * eta_c)
        discharge_kw = min(float(powers[1])/1000.0, max_power_kw / max(.01, eta_d))
        delta_kwh = (charge_kw - discharge_kw) * remaining / 3600.0
        source = "FRESH_BATTERY_DC_POWER"
    else:
        delta_kwh = -max(0.0, float(row.get("planned_battery_discharge_kwh") or 0.0)) * remaining / (slot_minutes * 60)
        source = "NO_FRESH_POWER_NO_CHARGE_CREDIT"
    projected = max(0.0, min(100.0, measured + delta_kwh / max(.001, capacity_kwh) * 100.0))
    if (delta_kwh > 0.0 and float(row.get("planned_buy_kwh") or 0.0) > 0.0
            and sample.get("pv_power_w") is not None and sample.get("load_power_w") is not None
            and float(sample["pv_power_w"]) <= float(sample["load_power_w"])):
        target = row.get("soc_charge_target_pct")
        if target is not None and math.isfinite(float(target)):
            projected = min(projected, max(measured, float(target)))
    return {"measured_soc_pct": measured, "horizon_initial_soc_pct": projected,
            "remaining_seconds": remaining, "delta_kwh": (projected-measured)*capacity_kwh/100.0,
            "source": source}

def relax_soc_requirement_to_reachable(requested_soc_pct: float,
                                        reachable_soc_pct: float,
                                        reserve_pct: float) -> float:
    """Return the best feasible floor without crossing the technical reserve."""
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    requested = max(reserve, min(100.0, float(requested_soc_pct)))
    reachable = max(reserve, min(100.0, float(reachable_soc_pct)))
    return max(reserve, min(requested, reachable))

def effective_required_soc_for_dispatch(
        safety_required_soc_pcts: list[float],
        dispatch_required_soc_pcts: list[float]) -> list[float]:
    """Validate against the effective optimizer floors without waiving safety."""
    if len(safety_required_soc_pcts) != len(dispatch_required_soc_pcts):
        raise ValueError("DISPATCH_SOC_REQUIREMENT_LENGTH_MISMATCH")
    return [
        max(float(safety), float(dispatch))
        for safety, dispatch in zip(
            safety_required_soc_pcts, dispatch_required_soc_pcts)
    ]
