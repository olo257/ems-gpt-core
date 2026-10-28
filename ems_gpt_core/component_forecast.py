"""Versioned household/DHW/heating forecasts from complete measured slots.

No missing channel is interpreted as zero. Flexible consumers with no energy
meter invalidate a training slot rather than leaking into battery demand.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import mean, median

MODEL_VERSION = "COMPONENT_LOAD_V1"
SLOT = timedelta(minutes=15)


def energy(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def split_slot(row, minimum_samples=10):
    """Conserve metered energy and explicitly reject incomplete attribution."""
    if (row.get("actual_mode") == "MISSING_OUTAGE"
            or not str(row.get("execution_reason") or "").startswith("CORE_TELEMETRY_")
            or int(row.get("sample_count") or 0) < minimum_samples
            or int(row.get("component_samples") or 0) < minimum_samples
            or float(row.get("coverage_pct") or 0) < 100 * minimum_samples / 15):
        return None
    keys = ("actual_load_kwh", "actual_heating_consumed_kwh",
            "actual_dhw_consumed_kwh", "actual_cooling_consumed_kwh", "actual_ev_kwh")
    values = [energy(row.get(key)) for key in keys]
    if any(value is None for value in values):
        return None
    total, heat, dhw, cool, ev = values
    # PV_CWU has a state sensor but no dedicated energy channel in existing
    # telemetry. Never subtract the HP's DHW channel as if it metered a heater.
    flex = energy(row.get("pv_cwu_kwh"))
    if flex is None:
        if row.get("pv_cwu_on") is None or bool(row["pv_cwu_on"]):
            return None
        flex = 0.0
    # Circulation cools the tank and causes HP_DHW reheating. Those losses
    # already appear in measured DHW cycles; never add them a second time.
    # The circulation pump's own electricity remains in household demand.
    house = total - heat - dhw - ev - flex
    if house < 0:
        return None
    return {"slot_start": row["slot_start"], "house_kwh": max(0.0, house),
            "dhw_kwh": dhw, "heat_kwh": heat,
            "cooling_in_house_kwh": cool}


def complete_cycles(rows, channel, *, threshold=0.02, max_idle_slots=0):
    """Learn whole cycles bracketed by valid idle slots; discard gap fragments.

    Short heating/DHW interruptions preserve the active-slot age of CO.
    More than max_idle_slots idle slots confirms the end of a session.
    """
    cycles, active = [], []
    previous = None
    left_idle = False
    idle_count = 0
    for row in rows:
        contiguous = previous is not None and row["slot_start"] - previous["slot_start"] == SLOT
        if not contiguous:
            active = []
            left_idle = False
            idle_count = 0
        if row[channel] >= threshold:
            if left_idle:
                active.append(row)
            idle_count = 0
        else:
            idle_count += 1
            if active and idle_count > max_idle_slots:
                cycles.append({"start": active[0]["slot_start"],
                               "end": active[-1]["slot_start"] + SLOT,
                               "profile": [item[channel] for item in active]})
                active = []
            left_idle = True
        previous = row
    return cycles


def mean_profile(cycles):
    if len(cycles) < 2:
        return None
    # Include zero tails, so averaging preserves mean complete-cycle energy.
    length = max(len(cycle["profile"]) for cycle in cycles)
    return [mean(cycle["profile"][i] if i < len(cycle["profile"]) else 0.0
                 for cycle in cycles) for i in range(length)]


def build_component_forecast(raw_rows, horizon, *, now, minimum_samples=10):
    """One batch supplies all components; incomplete evidence preserves old input."""
    horizon = sorted(horizon)
    rows = [part for row in raw_rows
            if now - timedelta(days=7) <= row["slot_start"] < now
            and (part := split_slot(row, minimum_samples)) is not None]
    rows.sort(key=lambda item: item["slot_start"])
    dhw_cycles = complete_cycles(rows, "dhw_kwh")
    heat_cycles = complete_cycles(rows, "heat_kwh", max_idle_slots=12)
    dhw_profile = mean_profile(dhw_cycles)
    heat_profile = mean_profile(heat_cycles)
    quarters = {}
    for row in rows:
        key = (row["slot_start"].hour, row["slot_start"].minute)
        quarters.setdefault(key, []).append(row)
    reasons = []
    if not dhw_profile:
        reasons.append("INSUFFICIENT_COMPLETE_DHW_CYCLES")
    if not heat_profile:
        reasons.append("INSUFFICIENT_COMPLETE_HEAT_CYCLES")
    intervals = [(b["start"] - a["start"]).total_seconds() / 3600
                 for a, b in zip(dhw_cycles, dhw_cycles[1:])
                 if a["start"].date() == b["start"].date()
                 and 2 <= (b["start"] - a["start"]).total_seconds() / 3600 <= 3]
    interval_slots = max(8, min(12, round(median(intervals) * 4))) if intervals else 10
    dhw_by_slot = {when: 0.0 for when in horizon}
    if dhw_profile and horizon:
        days = sorted({when.date() for when in horizon})
        for day in days:
            start = datetime.combine(day, datetime.min.time()).replace(hour=6)
            # Continue the observed phase, including only remaining cycle slots.
            recent = [cycle for cycle in dhw_cycles if cycle["start"].date() == day]
            # An open measured cycle is not a training cycle but is valid phase evidence.
            latest = rows[-1] if rows else None
            if latest and latest["slot_start"] == now - SLOT and latest["dhw_kwh"] >= .02 and latest["slot_start"].date() == day:
                tail = [latest]
                for prior in reversed(rows[:-1]):
                    if tail[-1]["slot_start"] - prior["slot_start"] != SLOT or prior["dhw_kwh"] < .02:
                        break
                    tail.append(prior)
                start = tail[-1]["slot_start"]
            elif recent:
                start = recent[-1]["start"]
            while start.hour < 21:
                for i, value in enumerate(dhw_profile):
                    when = start + SLOT * i
                    if when in dhw_by_slot:
                        dhw_by_slot[when] += value
                start += SLOT * interval_slots
    result = {}
    for when in horizon:
        samples = quarters.get((when.hour, when.minute), [])
        house_source = "EXACT_QUARTER" if len(samples) >= 3 else None
        house_samples = samples
        if house_source is None:
            target_minute = when.hour * 60 + when.minute
            nearby = []
            for (hour, minute), candidates in quarters.items():
                distance = abs(((hour * 60 + minute - target_minute + 720) % 1440) - 720)
                if len(candidates) >= 3 and distance <= 30:
                    nearby.append((distance, hour * 60 + minute, candidates))
            if nearby:
                _, _, house_samples = min(nearby, key=lambda item: (item[0], item[1]))
                house_source = "NEAREST_QUALIFIED_QUARTER"
        valid = house_source is not None and dhw_profile is not None
        result[when] = {
            "house_kwh": round(mean(item["house_kwh"] for item in house_samples), 6) if valid else None,
            "dhw_kwh": round(dhw_by_slot[when], 6) if valid else None,
            "sample_count": len(samples),
            "house_source": house_source if valid else "NO_QUALIFIED_QUARTER",
        }
    return {"version": MODEL_VERSION, "slots": result, "heat_profile": heat_profile,
            "dhw_profile": dhw_profile, "dhw_cycle_count": len(dhw_cycles),
            "heat_cycle_count": len(heat_cycles), "dhw_interval_slots": interval_slots,
            "circulation_effect": "IN_MEASURED_DHW_CYCLES",
            "reasons": reasons}


def heating_profile_for_plan(selected_times, profile, past_rows=(), max_pause_slots=12):
    """Keep the profile age across short heating pauses and DHW interruptions."""
    if not profile:
        return {}
    past = sorted(past_rows, key=lambda row: row["slot_start"])
    active = [row["slot_start"] for row in past if (energy(row.get("heat_kwh")) or 0) >= .02]
    timeline = sorted(set(active) | set(selected_times))
    result, previous, age = {}, None, 0
    for when in timeline:
        if previous is None or when - previous > SLOT * (max_pause_slots + 1):
            age = 0
        result[when] = profile[min(age, len(profile) - 1)]
        age += 1
        previous = when
    return {when: result[when] for when in selected_times}
