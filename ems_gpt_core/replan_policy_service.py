"""Cheap event checks; full optimization runs only at market boundaries or faults."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Callable

from planner_validation_service import strict_database_bool


def market_kind(row: dict | None) -> str:
    row = row or {}
    if strict_database_bool(row.get("sale_window", False), "sale_window"):
        return "SELL"
    if strict_database_bool(row.get("buy_window", False), "buy_window"):
        return "BUY"
    return "NEUTRAL"


def replan_reasons(clock: datetime, current_slot: datetime, last_published_at: datetime | None,
                   rows: list[dict], sample: dict | None, forecast_changes: list[dict],
                   heating_counts: list[dict], options: dict,
                   recovered: bool = False, planner_failed: bool = False) -> dict:
    """Compare telemetry at its timestamp and matching forecast slots, never raw slot ends."""
    now, start = clock.replace(tzinfo=None), current_slot.replace(tzinfo=None)
    step = timedelta(minutes=int(options.get("slot_minutes", 15)))
    by_time = {r["slot_start"]: r for r in rows}
    previous, active, following = (by_time.get(start+offset) for offset in (-step, timedelta(), step))
    reasons = []
    settled = 2 <= (now-start).total_seconds()/60 < 7
    if settled and following and market_kind(following) in {"BUY", "SELL"}:
        if market_kind(active) != market_kind(following):
            reasons.append("BEFORE_"+market_kind(following))
    if last_published_at is None:
        reasons.append("NO_PUBLISHED_PLAN")
    elif following and not following.get("plan_published"):
        reasons.append("MISSING_FUTURE_PLAN")
    if recovered:
        reasons.append("TELEMETRY_RECOVERED")
    if planner_failed:
        reasons.append("PLANNER_RETRY")

    measured = expected = None
    sample = sample or {}
    captured = sample.get("captured_at")
    fresh = isinstance(captured, datetime) and 0 <= (now-captured).total_seconds() <= float(
        options.get("telemetry_degraded_seconds", 120))
    raw_soc = sample.get("soc_pct")
    if fresh and raw_soc is not None and math.isfinite(float(raw_soc)) and 0 <= float(raw_soc) <= 100:
        measured = float(raw_soc)
        if active and active.get("plan_published"):
            left, right = active.get("soc_start_plan_pct"), active.get("soc_end_plan_pct")
            if left is not None and right is not None:
                fraction = max(0., min(1., (captured-start).total_seconds()/step.total_seconds()))
                expected = float(left)+(float(right)-float(left))*fraction
                if abs(measured-expected) >= float(options.get("replan_soc_deviation_pct", 5.)):
                    reasons.append("SOC_DEVIATION")
        reserve = float(options.get("battery_min_soc_pct", 15.))
        discharge = max(0., float(sample.get("battery_discharge_power_w") or 0.))
        charge = max(0., float(sample.get("battery_charge_power_w") or 0.))
        projected = measured + (charge-discharge)/1000*step.total_seconds()/3600 / max(
            .001, float(options.get("battery_capacity_kwh", 15.))) * 100
        if measured < reserve or (discharge > charge and projected < reserve):
            reasons.append("SOC_RESERVE_AT_RISK")

    # A closed BUY is checked even when no replan is needed. Check actual battery
    # charge, not total grid import (which also includes household consumption).
    after_buy = bool(previous and market_kind(previous) == "BUY" and market_kind(active) != "BUY")
    buy_check = None
    if after_buy:
        planned = float(previous.get("planned_battery_charge_kwh") or 0.)
        actual = previous.get("actual_battery_charge_kwh")
        missing = max(0., planned-float(actual)) if actual is not None else None
        target = previous.get("soc_end_plan_pct")
        end_soc = previous.get("soc_end_pct")
        gap = max(0., float(target)-float(end_soc)) if target is not None and end_soc is not None else None
        required = previous.get("soc_required_pct")
        coverage_gap = max(0., float(required)-float(end_soc)) if required is not None and end_soc is not None else None
        missing_energy = (coverage_gap*float(options.get("battery_capacity_kwh", 15.))/100
                          if coverage_gap is not None else None)
        shortfall = ((missing is not None and missing >= .25) or (gap is not None and gap >= 2.)
                     or (missing_energy is not None and missing_energy >= .25))
        buy_check = {"window_end": start.isoformat(), "planned_charge_kwh": planned,
                     "actual_charge_kwh": actual, "missing_charge_kwh": missing,
                     "soc_shortfall_pct": gap, "required_soc_pct": required,
                     "actual_end_soc_pct": end_soc, "coverage_shortfall_kwh": missing_energy,
                     "status": "SHORTFALL" if shortfall else (
                         "WAITING_TELEMETRY" if missing is None or gap is None else "ON_PLAN")}
        if shortfall:
            reasons.append("BUY_EXECUTION_SHORTFALL")

    # Absolute slot differences retain changes in timing that totals would hide.
    changes = {}
    for key in ("load", "pv"):
        valid = [r for r in forecast_changes if r.get("current_"+key) is not None
                 and r.get("planned_"+key) is not None]
        delta = sum(abs(float(r["current_"+key])-float(r["planned_"+key])) for r in valid)
        baseline = sum(abs(float(r["planned_"+key])) for r in valid)
        changes[key] = round(delta, 4)
        if delta >= float(options.get("replan_forecast_change_kwh", 1.)) and delta >= baseline*float(
                options.get("replan_forecast_change_fraction", .2)):
            reasons.append(key.upper()+"_FORECAST_CHANGED")
    if any(int(r.get("below_now") or 0) >= 3 and int(r.get("below_at_publication") or 0) < 3
           for r in heating_counts):
        reasons.append("HP_HEATING_QUALIFIED")
    return {"reasons": reasons, "measured_soc_pct": measured, "expected_soc_pct": expected,
            "forecast_changes_kwh": changes, "buy_completion": buy_check}


def inspect_replan_inputs(db: Callable, clock: datetime, current_slot: datetime,
                          last_published_at: datetime | None, options: dict,
                          recovered: bool = False, planner_failed: bool = False) -> dict:
    """Read durable published baselines; comparisons survive scheduler restarts."""
    now, start = clock.replace(tzinfo=None), current_slot.replace(tzinfo=None)
    step = timedelta(minutes=int(options.get("slot_minutes", 15)))
    publication = last_published_at or start-timedelta(days=2)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT slot_start,buy_window,sale_window,plan_published,
          soc_start_plan_pct,soc_end_plan_pct,soc_end_pct,soc_required_pct,
          planned_battery_charge_kwh,actual_battery_charge_kwh
          FROM ems_gpt_slots WHERE slot_start BETWEEN %s AND %s ORDER BY slot_start""",
          (start-step, start+step))
        rows = list(cur.fetchall())
        cur.execute("""SELECT captured_at,soc_pct,battery_charge_power_w,battery_discharge_power_w
          FROM ems_gpt_telemetry_snapshots WHERE captured_at<=%s AND captured_at>=%s
          ORDER BY captured_at DESC LIMIT 1""", (now, now-timedelta(minutes=2)))
        sample = cur.fetchone()
        cur.execute("""SELECT p.forecast_load_kwh current_load,s.forecast_load_kwh planned_load,
          p.forecast_pv_total_kwh current_pv,s.forecast_pv_total_kwh planned_pv
          FROM ems_gpt_slots p JOIN ems_gpt_plan_stage_rows s
            ON s.run_id=p.plan_run_id AND s.slot_start=p.slot_start
          WHERE p.plan_published=1 AND p.slot_start>=%s AND p.slot_start<%s""",
          (start+step, start+timedelta(hours=6)))
        forecasts = list(cur.fetchall())
        # Same actual-temperature qualification and local evening/day mapping as
        # prepare_heat_pump_plan. No HP optimization runs in this monitor.
        cur.execute("""SELECT DATE(slot_start + INTERVAL 5 HOUR) heating_day,
          SUM(CASE WHEN actual_temperature_c < %s THEN 1 ELSE 0 END) below_now,
          SUM(CASE WHEN actual_temperature_c < %s AND actual_recorded_at<=%s THEN 1 ELSE 0 END) below_at_publication
          FROM ems_gpt_slots
          WHERE DATE(slot_start + INTERVAL 5 HOUR) BETWEEN %s AND %s
            AND (TIME(slot_start)>='19:00:00' OR TIME(slot_start)<'06:00:00')
            AND slot_start<=%s AND actual_temperature_c IS NOT NULL
          GROUP BY DATE(slot_start + INTERVAL 5 HOUR)""",
          (float(options.get("night_heating_threshold_c", 10.)),
           float(options.get("night_heating_threshold_c", 10.)), publication,
           start.date(), (start+timedelta(days=1)).date(), now))
        heating = list(cur.fetchall())
    return replan_reasons(clock, current_slot, last_published_at, rows, sample,
                          forecasts, heating, options, recovered, planner_failed)
