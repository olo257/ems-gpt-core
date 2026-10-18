from __future__ import annotations

from datetime import datetime, timedelta

from planner_validation_service import strict_database_bool


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
            # A cycle already running at the replan boundary is a
            # commitment: keep it on until its minimum duration is met,
            # even when the daily heating requirement was already counted.
            if current_phase == "ON" and current_run < min_cycle_slots:
                choices = [True]
            else:
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


def hp_temperature_eligible(below_threshold_sample_count: int,
                            minimum_samples: int = 3) -> bool:
    """Qualify HP heating after enough actual below-threshold readings."""
    return int(below_threshold_sample_count) >= int(minimum_samples)


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

