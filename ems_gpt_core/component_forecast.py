"""Versioned household/DHW/heating forecasts from complete measured slots.

No missing channel is interpreted as zero. Unmetered flexible consumption
invalidates household attribution. HP_DHW learns from its own power channel.
Sparse but complete household observations use an explicitly marked,
conservative low-sample fallback instead of blocking the full planner horizon.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import mean, median

MODEL_VERSION = "COMPONENT_LOAD_V8"
SLOT = timedelta(minutes=15)
WIDE_FALLBACK_MINUTES = 120


def energy(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _is_observed_slot(row):
    return (row.get("actual_mode") != "MISSING_OUTAGE"
            and str(row.get("execution_reason") or "").startswith("CORE_TELEMETRY_"))


def _has_qualified_dhw_slot(row, minimum_samples):
    """Check persisted slot quality, without depending on raw snapshot retention."""
    sample_count = int(row.get("sample_count") or 0)
    if sample_count < minimum_samples:
        reason = str(row.get("execution_reason") or "")
        prefix, suffix = "CORE_TELEMETRY_", "_SAMPLES"
        if reason.startswith(prefix) and reason.endswith(suffix):
            try:
                sample_count = max(sample_count, int(reason[len(prefix):-len(suffix)]))
            except ValueError:
                pass
    if sample_count < minimum_samples:
        return False
    coverage = energy(row.get("coverage_pct"))
    if coverage is None:
        coverage = min(100.0, sample_count * 100.0 / 15.0)
    return coverage >= 100.0 * minimum_samples / 15.0


def split_slot(row, minimum_samples=10):
    """Build base household demand without requiring flexible-load telemetry."""
    if (not _is_observed_slot(row)
            or int(row.get("sample_count") or 0) < minimum_samples
            or float(row.get("coverage_pct") or 0) < 100 * minimum_samples / 15):
        return None
    keys = ("actual_load_kwh", "actual_heating_consumed_kwh",
            "actual_dhw_consumed_kwh", "actual_cooling_consumed_kwh")
    values = [energy(row.get(key)) for key in keys]
    if any(value is None for value in values):
        return None
    total, heat, dhw, cool = values
    # Flexible PV_CWU and PV_EV are not forecast components. Exclude measured
    # EV energy from the base when available; its absence does not block a row.
    ev = energy(row.get("actual_ev_kwh")) or 0.0
    # PV_CWU has no energy channel. Skip known-on periods to avoid learning its
    # load, but an unavailable state must not invalidate the home's forecast.
    if bool(row.get("pv_cwu_on")):
        return None
    house = total - heat - dhw - ev
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
    recent_rows, historical_rows = [], []
    recent_dhw, historical_dhw = [], []
    recent_dhw_slots, historical_dhw_slots = [], []
    recent_start = now - timedelta(days=7)
    history_start = now - timedelta(days=30)
    for row in sorted(raw_rows, key=lambda item: item["slot_start"]):
        when = row["slot_start"]
        if not history_start <= when < now:
            continue
        recent = when >= recent_start
        if (_is_observed_slot(row)
                and _has_qualified_dhw_slot(row, minimum_samples)):
            dhw_kwh = energy(row.get("actual_dhw_consumed_kwh"))
            if dhw_kwh is not None:
                (recent_dhw_slots if recent else historical_dhw_slots).append(
                    {"slot_start": when, "dhw_kwh": dhw_kwh})
                # Raw per-sample values are used only to bracket complete
                # cycles. The fallback envelope above uses durable slot data.
                if int(row.get("dhw_samples") or 0) >= minimum_samples:
                    (recent_dhw if recent else historical_dhw).append(
                        {"slot_start": when, "dhw_kwh": dhw_kwh})
        part = split_slot(row, minimum_samples)
        if part is not None:
            (recent_rows if recent else historical_rows).append(part)
    dhw_cycles = complete_cycles(recent_dhw, "dhw_kwh")
    historical_dhw_cycles = complete_cycles(historical_dhw, "dhw_kwh")
    heat_cycles = complete_cycles(recent_rows, "heat_kwh", max_idle_slots=12)
    if len(dhw_cycles) >= 2:
        dhw_profile = mean_profile(dhw_cycles)
        dhw_profile_source = "RECENT_COMPLETE_CYCLES"
    elif len(historical_dhw_cycles) >= 2:
        dhw_profile = mean_profile(historical_dhw_cycles)
        dhw_profile_source = "HISTORICAL_COMPLETE_CYCLES"
    elif dhw_cycles and historical_dhw_cycles:
        dhw_profile = mean_profile(historical_dhw_cycles + dhw_cycles)
        dhw_profile_source = "MIXED_COMPLETE_CYCLES"
    elif dhw_cycles or historical_dhw_cycles:
        dhw_profile = (dhw_cycles or historical_dhw_cycles)[0]["profile"]
        dhw_profile_source = "LOW_SAMPLE_SINGLE_COMPLETE_CYCLE"
    else:
        dhw_profile = None
        dhw_profile_source = None
    # If valid HP_DHW meter samples exist but cannot be assembled into a
    # fully idle-bracketed cycle, retain the measured time-of-day envelope.
    # This fallback uses only the independent HP_DHW energy channel; PV_CWU
    # state and energy never contribute to it. The envelope keeps the higher
    # measured value across recent and older observations for each quarter.
    dhw_slot_envelope = {}
    if dhw_profile is None:
        recent_by_quarter, historical_by_quarter = {}, {}
        for row in recent_dhw_slots:
            if row["dhw_kwh"] <= 0:
                continue
            key = (row["slot_start"].hour, row["slot_start"].minute)
            recent_by_quarter[key] = max(recent_by_quarter.get(key, 0.0), row["dhw_kwh"])
        for row in historical_dhw_slots:
            if row["dhw_kwh"] <= 0:
                continue
            key = (row["slot_start"].hour, row["slot_start"].minute)
            historical_by_quarter[key] = max(
                historical_by_quarter.get(key, 0.0), row["dhw_kwh"])
        dhw_slot_envelope = {
            key: max(recent_by_quarter.get(key, 0.0),
                     historical_by_quarter.get(key, 0.0))
            for key in set(recent_by_quarter) | set(historical_by_quarter)
        }
        if recent_by_quarter:
            dhw_profile_source = "LOW_SAMPLE_RECENT_DHW_SLOT_ENVELOPE"
        elif historical_by_quarter:
            dhw_profile_source = "HISTORICAL_LOW_SAMPLE_DHW_SLOT_ENVELOPE"
    heat_profile = mean_profile(heat_cycles)
    quarters, historical_quarters = {}, {}
    for row in recent_rows:
        key = (row["slot_start"].hour, row["slot_start"].minute)
        quarters.setdefault(key, []).append(row)
    for row in historical_rows:
        key = (row["slot_start"].hour, row["slot_start"].minute)
        historical_quarters.setdefault(key, []).append(row)
    reasons = []
    if not dhw_profile and not dhw_slot_envelope:
        reasons.append("INSUFFICIENT_COMPLETE_DHW_CYCLES")
    elif dhw_slot_envelope:
        reasons.append("LOW_SAMPLE_DHW_SLOT_ENVELOPE")
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
            latest = recent_dhw[-1] if recent_dhw else None
            if latest and latest["slot_start"] == now - SLOT and latest["dhw_kwh"] >= .02 and latest["slot_start"].date() == day:
                tail = [latest]
                for prior in reversed(recent_dhw[:-1]):
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
    elif dhw_slot_envelope:
        for when in horizon:
            dhw_by_slot[when] = dhw_slot_envelope.get(
                (when.hour, when.minute), 0.0)
    result = {}
    for when in horizon:
        target_minute = when.hour * 60 + when.minute
        samples = quarters.get((when.hour, when.minute), [])
        house_source = "EXACT_QUARTER" if len(samples) >= 3 else None
        house_samples = samples
        wide_fallback_distance = None
        if house_source is None:
            nearby = []
            low_sample_nearby = []
            for (hour, minute), candidates in quarters.items():
                distance = abs(((hour * 60 + minute - target_minute + 720) % 1440) - 720)
                if distance <= 30 and candidates:
                    low_sample_nearby.append((distance, hour * 60 + minute, candidates))
                    if len(candidates) >= 3:
                        nearby.append((distance, hour * 60 + minute, candidates))
            if nearby:
                _, _, house_samples = min(nearby, key=lambda item: (item[0], item[1]))
                house_source = "NEAREST_QUALIFIED_QUARTER"
            elif samples:
                # One or two complete, quality-checked observations are still
                # better than blocking every future slot. Use the maximum so a
                # sparse sample cannot lower the forecast below its evidence.
                house_source = "LOW_SAMPLE_EXACT_QUARTER"
            elif low_sample_nearby:
                nearest_distance = min(item[0] for item in low_sample_nearby)
                nearest = [item for item in low_sample_nearby
                           if item[0] == nearest_distance]
                house_samples = [sample for _, _, candidates in nearest
                                 for sample in candidates]
                house_source = "LOW_SAMPLE_NEAREST_QUARTER"
        if house_source is None:
            hist_samples = historical_quarters.get((when.hour, when.minute), [])
            if len(hist_samples) >= 3:
                house_samples, house_source = hist_samples, "HISTORICAL_EXACT_QUARTER"
            else:
                hist_nearby = []
                for (hour, minute), candidates in historical_quarters.items():
                    distance = abs(((hour * 60 + minute - target_minute + 720) % 1440) - 720)
                    if distance <= 30 and candidates:
                        hist_nearby.append((distance, hour * 60 + minute, candidates))
                qualified = [item for item in hist_nearby if len(item[2]) >= 3]
                if qualified:
                    _, _, house_samples = min(qualified, key=lambda item: (item[0], item[1]))
                    house_source = "HISTORICAL_NEAREST_QUARTER"
                elif hist_samples:
                    house_samples, house_source = hist_samples, "HISTORICAL_LOW_SAMPLE_EXACT"
                elif hist_nearby:
                    nearest_distance = min(item[0] for item in hist_nearby)
                    house_samples = [sample for distance, _, candidates in hist_nearby
                                     if distance == nearest_distance for sample in candidates]
                    house_source = "HISTORICAL_LOW_SAMPLE_NEAREST_QUARTER"
        if house_source is None:
            # Only use wider observations after exhausting the standard window.
            # Both lookback ranges contain the same quality-checked house component.
            wider = []
            for by_quarter in (quarters, historical_quarters):
                for (hour, minute), candidates in by_quarter.items():
                    distance = abs(((hour * 60 + minute - target_minute + 720) % 1440) - 720)
                    if distance <= WIDE_FALLBACK_MINUTES:
                        wider.append((distance, candidates))
            if wider:
                wide_fallback_distance = min(distance for distance, _ in wider)
                house_samples = [sample for distance, candidates in wider
                                 if distance == wide_fallback_distance for sample in candidates]
                house_source = "LOW_SAMPLE_WIDE_NEAREST_QUARTER"
        if house_source is None:
            house_kwh = None
        elif house_source.startswith(("LOW_SAMPLE", "HISTORICAL_LOW_SAMPLE")):
            house_kwh = max(item["house_kwh"] for item in house_samples)
        else:
            house_kwh = mean(item["house_kwh"] for item in house_samples)
        result[when] = {
            "house_kwh": round(house_kwh, 6) if house_kwh is not None else None,
            "dhw_kwh": round(dhw_by_slot[when], 6)
                if dhw_profile is not None or dhw_slot_envelope else None,
            "sample_count": len(samples),
            "house_sample_count": len(house_samples),
            "house_source": house_source or "NO_QUALIFIED_QUARTER",
            "house_wide_fallback_distance_minutes": wide_fallback_distance,
        }
    return {"version": MODEL_VERSION, "slots": result, "heat_profile": heat_profile,
            "dhw_profile": dhw_profile, "dhw_profile_source": dhw_profile_source,
            "dhw_cycle_count": len(dhw_cycles),
            "historical_dhw_cycle_count": len(historical_dhw_cycles),
            "dhw_fallback_slot_count": len(dhw_slot_envelope),
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
