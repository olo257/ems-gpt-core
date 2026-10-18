from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

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


def prepare_heat_pump_plan(cur, source: list[dict], rows: list[dict], now,
                           cutoff, options: dict, run_id: str,
                           record_event: Callable) -> dict:
    """Load HP/DHW histories and prepare per-slot heating inputs for dispatch."""
    hp_shortfalls = []
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
    # Qualify the heating day from actual garden readings beginning at
    # 19:00 the previous evening through 05:59 local time. The +5 hour
    # shift maps prior-evening readings to their following heating day.
    # Count only readings strictly below the operator threshold, and
    # exclude future telemetry so replans can qualify immediately.
    night_threshold = float(options.get("night_heating_threshold_c", 10.0))
    day_values = sorted({row.get("local_day") or row["slot_start"].date() for row in rows})
    night_min_by_day = {}
    if day_values:
        markers = ",".join(["%s"] * len(day_values))
        cur.execute(f"""SELECT DATE(slot_start + INTERVAL 5 HOUR) heating_day,
          SUM(CASE WHEN actual_temperature_c < %s THEN 1 ELSE 0 END) below_count
          FROM ems_gpt_slots
          WHERE DATE(slot_start + INTERVAL 5 HOUR) IN ({markers})
            AND (TIME(slot_start)>='19:00:00' OR TIME(slot_start)<'06:00:00')
            AND slot_start<=%s
            AND actual_temperature_c IS NOT NULL
          GROUP BY DATE(slot_start + INTERVAL 5 HOUR)""",
          (night_threshold, *day_values, now))
        night_min_by_day = {
            str(value["heating_day"]): {
                "sample_count": int(value["below_count"] or 0),
            }
            for value in cur.fetchall()
        }

    required_slots = max(1, int(float(options.get("hp_min_heating_hours", 10.0)) * 4 + 0.999999))
    min_cycle_slots = max(1, int(float(options.get("hp_min_cycle_hours", 2.0)) * 4 + 0.999999))
    min_gap_slots = max(1, int(float(options.get("hp_min_cycle_break_hours", 1.0)) * 4 + 0.999999))
    max_gap_slots = max(min_gap_slots, int(float(options.get("hp_max_cycle_break_hours", 3.0)) * 4 + 0.999999))
    hp_fallback_kw = max(0.0, float(options.get("hp_planned_power_kw", 1.5)))
    hp_history_start = min(day_values) - timedelta(days=28) if day_values else now.date()
    cur.execute("""SELECT local_day,actual_heating_consumed_kwh
      FROM ems_gpt_slots WHERE local_day>=%s AND local_day<%s
        AND actual_recorded_at IS NOT NULL AND plan_published=1
        AND execution_reason LIKE 'CORE_TELEMETRY_%%'
        AND actual_heating_consumed_kwh>0.02""",
        (hp_history_start, min(day_values) if day_values else now.date()))
    hp_heating_history = list(cur.fetchall())
    hp_history_weights = {
        7: float(options.get("soc_target_history_weight_7d_pct", 50.0)),
        14: float(options.get("soc_target_history_weight_14d_pct", 25.0)),
        28: float(options.get("soc_target_history_weight_28d_pct", 25.0)),
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
    cycle_penalty = max(0.0, float(options.get("hp_cycle_start_penalty_pln", 0.25)))
    hp_selected_indices = set()
    for day_value in day_values:
        planned_hp_kw = planned_hp_kw_by_day[day_value]
        day_key = str(day_value)
        night_forecast = night_min_by_day.get(day_key) or {}
        if not hp_temperature_eligible(
                int(night_forecast.get("sample_count") or 0)):
            continue
        indexed_rows = [(index, row) for index, row in enumerate(rows)
                        if (row.get("local_day") or row["slot_start"].date()) == day_value]
        day_start = datetime.combine(day_value, datetime.min.time())
        # Planning remains independent from PPD and operator overrides.
        # For elapsed slots the planner assumes its own published plan
        # was executed.  Differences belong to execution analytics and
        # must never feed a PPD decision back into the next plan.
        past_states, unknown_hp_history = elapsed_hp_plan_states(
            cur, day_start, min(cutoff, day_start + timedelta(days=1)))
        if unknown_hp_history:
            record_event("hp_elapsed_plan_history_missing", "planner", {
                "planning_day": day_key, "run_id": run_id,
                "unknown_slots": len(unknown_hp_history),
                "slots": unknown_hp_history,
                "action": "NO_HEATING_CREDIT_CONTINUE_PLANNING",
            }, "WARNING")
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

    # Consume the same versioned batch used for household + DHW.
    # A missing component preserves previous production input explicitly.
    from component_forecast import heating_profile_for_plan
    cur.execute("""SELECT * FROM ems_gpt_core_consumption_forecasts
      WHERE slot_start>=%s ORDER BY slot_start""", (cutoff,))
    component_rows = {item["slot_start"]: item for item in cur.fetchall()}
    previous_loads = {item["slot_start"]: item.get("forecast_heat_pump_load_kwh")
                      for item in source}
    component_heat = {}
    selected_times = [rows[i]["slot_start"] for i in sorted(hp_selected_indices)]
    if selected_times:
        evidence = component_rows.get(selected_times[0]) or {}
        profile = json.loads(evidence.get("heat_profile_json") or "null")
        if profile:
            cur.execute("""SELECT slot_start,actual_heating_consumed_kwh heat_kwh
              FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s
                AND actual_recorded_at IS NOT NULL
              ORDER BY slot_start""", (cutoff-timedelta(days=1),cutoff))
            component_heat = heating_profile_for_plan(selected_times,profile,list(cur.fetchall()))
    record_event("planner_consumption_batch", "planner", {
        "run_id": run_id,
        "batch_ids": sorted({item["batch_id"] for item in component_rows.values()}),
        "profiled_heat_slots": len(component_heat),
        "heat_fallback": "PRESERVE_PREVIOUS_OR_HISTORICAL_POWER" if not component_heat else None})
    return {
        "dhw_profile": dhw_profile,
        "hp_fallback_kw": hp_fallback_kw,
        "planned_hp_kw_by_day": planned_hp_kw_by_day,
        "hp_selected_indices": hp_selected_indices,
        "hp_shortfalls": hp_shortfalls,
        "component_rows": component_rows,
        "previous_loads": previous_loads,
        "component_heat": component_heat,
    }
