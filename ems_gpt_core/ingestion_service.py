"""External PV, weather and RCE data ingestion."""
from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlencode

from config_service import deye_program_soc_baselines
from planner_dispatch_service import replenishment_soc_requirements
from planner_validation_service import strict_database_bool


def select_pv_profile(slots: list[dict], profiles: list[dict], month: int) -> tuple[list[float], int | None]:
    """Use one complete learned month, nearest on the circular calendar."""
    by_month = {}
    for row in profiles:
        value = row.get("mean_share")
        if value is None or not math.isfinite(float(value)) or float(value) < 0:
            continue
        by_month.setdefault(int(row["month_no"]), {})[
            (int(row["hour_no"]), int(row["minute_no"]))] = float(value)
    # Prefer the preceding month in a tie; December is next to January.
    candidates = sorted(by_month, key=lambda m: (
        min((month-m) % 12, (m-month) % 12), (month-m) % 12, m))
    for candidate in candidates:
        profile = by_month[candidate]
        keys = [(row["slot_start"].hour, row["slot_start"].minute) for row in slots]
        if all(key in profile for key in keys):
            weights = [profile[key] for key in keys]
            if sum(weights) > 0:
                return weights, candidate
    return [], None


def complete_pv_forecast(row: dict) -> bool:
    values = [row.get(key) for key in (
        "forecast_pv1_kwh", "forecast_pv2_kwh", "forecast_pv_total_kwh")]
    return (all(value is not None and math.isfinite(float(value)) and float(value) >= 0
                for value in values)
            and abs(float(values[0]) + float(values[1]) - float(values[2])) <= 0.000002)


def preserve_stored_pv_forecasts(cur, lower: datetime, upper: datetime) -> dict:
    """Recover only the same slot from accepted published SQL snapshots."""
    cur.execute("""SELECT slot_start,forecast_pv1_kwh,forecast_pv2_kwh,forecast_pv_total_kwh
      FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s
      AND actual_recorded_at IS NULL ORDER BY slot_start""", (lower, upper))
    slots = list(cur.fetchall())
    missing = {row["slot_start"] for row in slots if not complete_pv_forecast(row)}
    recovered = 0
    if missing:
        cur.execute("""SELECT s.slot_start,s.forecast_pv1_kwh,s.forecast_pv2_kwh,
          s.forecast_pv_total_kwh,s.forecast_pv_source,s.pv_correction
          FROM ems_gpt_plan_stage_rows s JOIN ems_gpt_plan_runs r ON r.run_id=s.run_id
          WHERE s.slot_start>=%s AND s.slot_start<%s AND r.status='PUBLISHED'
          AND r.validation_status='ACCEPTED' ORDER BY r.published_at DESC""", (lower, upper))
        for row in cur.fetchall():
            if row["slot_start"] not in missing or not complete_pv_forecast(row):
                continue
            cur.execute("""UPDATE ems_gpt_slots SET forecast_pv1_kwh=%s,forecast_pv2_kwh=%s,
              forecast_pv_total_kwh=%s,forecast_pv_source=%s,pv_correction=%s
              WHERE slot_start=%s AND actual_recorded_at IS NULL""",
              (row["forecast_pv1_kwh"], row["forecast_pv2_kwh"], row["forecast_pv_total_kwh"],
               row.get("forecast_pv_source"), row.get("pv_correction"), row["slot_start"]))
            missing.remove(row["slot_start"])
            recovered += 1
    return {"status": "STORED_FORECAST" if slots and not missing else "WAITING_STORED_FORECAST",
            "slots": len(slots), "missing_slots": len(missing), "recovered_slots": recovered}


def derive_price_windows(prices: list[dict], eta_c: float, eta_d: float,
                         degradation: float, min_margin: float,
                         buy_tolerance: float,
                         minimum_buy_slots: int = 1) -> list[tuple[bool, bool]]:
    """Derive economic valleys and peaks over the complete price horizon."""
    if not prices:
        return []
    buys = [float(row["buy"]) for row in prices]
    sale_flags = []
    for index, current in enumerate(prices):
        later_buy = buys[index + 1:]
        replacement = min(later_buy) if later_buy else None
        required_sell = (replacement / (eta_c * eta_d) + degradation + min_margin
                         if replacement is not None else None)
        sale_flags.append(bool(
            float(current["sell"]) > 0.0
            and required_sell is not None
            and float(current["sell"]) >= required_sell
        ))
    buy_candidates: set[int] = set()
    # A 4-hour neighbourhood suppresses insignificant quarter-hour noise.
    # Without this prominence test, every tiny local dip expands by tolerance
    # and overlapping expansions can mark almost the complete day as BUY.
    radius = min(16, max(0, len(prices) - 1))
    for index in range(len(prices)):
        neighbourhood = buys[max(0, index-radius):min(len(buys), index+radius+1)]
        prominent = max(neighbourhood) - buys[index] >= buy_tolerance - 1e-9
        if prominent and buys[index] <= min(neighbourhood) + 1e-9:
            left = right = index
            while left > 0 and buys[left - 1] <= buys[index] + buy_tolerance:
                left -= 1
            while right + 1 < len(prices) and buys[right + 1] <= buys[index] + buy_tolerance:
                right += 1
            buy_candidates.update(range(left, right + 1))

    minimum_buy_slots = max(1, int(minimum_buy_slots))
    pending = sorted(buy_candidates)
    clusters = []
    while pending:
        cluster = [pending.pop(0)]
        while pending and pending[0] == cluster[-1] + 1:
            cluster.append(pending.pop(0))
        clusters.append(cluster)
    for cluster in clusters:
        left, right = cluster[0], cluster[-1]
        while sum(not sale_flags[i] for i in range(left, right + 1)) < minimum_buy_slots:
            choices = []
            if left > 0 and not sale_flags[left - 1]:
                choices.append((buys[left - 1], "left"))
            if right + 1 < len(prices) and not sale_flags[right + 1]:
                choices.append((buys[right + 1], "right"))
            if not choices:
                break
            _, side = min(choices)
            if side == "left":
                left -= 1
            else:
                right += 1
            buy_candidates.update(range(left, right + 1))

    windows = []
    for index, current in enumerate(prices):
        sale_window = sale_flags[index]
        # BUY and SELL are mutually exclusive permissions.
        buy_window = index in buy_candidates and not sale_window
        windows.append((sale_window, buy_window))
    return windows


def extend_night_buy_capacity(
        rows: list[dict], capacity_kwh: float, reserve_pct: float, buffer_pct: float,
        eta_c: float, eta_d: float, max_power_kw: float, slot_minutes: int,
        uncertainty_weight: float, degradation: float, min_margin: float,
        grid_import_limit_kw: float = 12.0,
        earliest_slot_start: datetime | None = None) -> dict:
    """Give the price valley enough night capacity for the following morning.

    This only opens permissions next to existing night BUY valleys. Load/PV
    determine capacity; morning avoided import prices bound economics. SELL,
    completed slots and daytime permissions are never overwritten here.
    """
    work = [dict(row) for row in rows]
    if earliest_slot_start is not None and earliest_slot_start.tzinfo is not None:
        # HA supplies the cutoff as Warsaw-aware time; DB slots are local naive.
        earliest_slot_start = earliest_slot_start.replace(tzinfo=None)
    changed, reports = set(), []
    hours = slot_minutes/60.0
    quantum = capacity_kwh*.001
    # Prepare later nights first: earlier bridges may rely on their finite
    # charge capacity, never just on the existence of a later BUY flag.
    for day in sorted({row['slot_start'].date() for row in work}, reverse=True):
        night = [i for i, row in enumerate(work)
                 if row['slot_start'].date() == day and row['slot_start'].hour < 6
                 and (earliest_slot_start is None or row['slot_start'] >= earliest_slot_start)
                 and row.get('actual_recorded_at') is None]
        selected = {i for i in night if strict_database_bool(work[i].get('buy_window', False), 'buy_window')
                    and not strict_database_bool(work[i].get('sale_window', False), 'sale_window')}
        if not selected:
            hp_forecast_kwh = sum(
                max(0., float(row.get('forecast_heat_pump_load_kwh') or 0.))
                for row in work if row['slot_start'].date() == day
                and row['slot_start'].hour >= 7
                and row.get('actual_recorded_at') is None)
            if hp_forecast_kwh > 0.001:
                reports.append({
                    'day': str(day),
                    'status': 'NO_NIGHT_BUY_WINDOW_FOR_HP_RESERVE',
                    'hp_forecast_kwh': round(hp_forecast_kwh, 3),
                })
            continue
        morning = [i for i, row in enumerate(work)
                   if row['slot_start'].date() == day and row['slot_start'].hour >= 6
                   and (earliest_slot_start is None or row['slot_start'] >= earliest_slot_start)
                   and row.get('actual_recorded_at') is None]
        if not morning:
            reports.append({'day': str(day), 'status': 'WAITING_MORNING_FORECAST'})
            continue
        boundary = next((i for i in morning if strict_database_bool(
            work[i].get('buy_window', False), 'buy_window') and not strict_database_bool(
            work[i].get('sale_window', False), 'sale_window')), morning[-1]+1)
        checked = work[min(night):]
        if (not checked or any(row.get(key) is None for row in checked
                              for key in ('forecast_load_kwh', 'forecast_pv_total_kwh'))
                or any(checked[i]['slot_start']-checked[i-1]['slot_start'] != timedelta(minutes=slot_minutes)
                       for i in range(1, len(checked)))):
            reports.append({'day': str(day), 'status': 'WAITING_COMPLETE_FORECAST'})
            continue

        def deficit(i):
            row = work[i]
            return max(0., float(row['forecast_load_kwh'])
                       + float(row.get('forecast_heat_pump_load_kwh') or 0.)
                       - float(row['forecast_pv_total_kwh']))

        demand = sum(deficit(i) for i in morning if i < boundary)
        if demand <= 1e-9:
            continue
        avoided_price = sum(deficit(i)*float(work[i]['price_buy_pln_kwh'])
                            for i in morning if i < boundary)/demand
        # Make the night BUY bridge carry a dedicated HP reserve from 07:00
        # until the first later daytime BUY. Surplus PV after household load
        # can serve HP first; remaining heat is reserved in battery kWh.
        hp_bridge = [i for i in morning if i < boundary
                     and work[i]['slot_start'].hour >= 7]
        hp_uncertainty = max(0., min(2., float(uncertainty_weight))) * .10
        hp_reserve_kwh = sum(
            max(0., float(work[i].get('forecast_heat_pump_load_kwh') or 0.)
                - max(0., float(work[i].get('forecast_pv_total_kwh') or 0.)
                      - float(work[i].get('forecast_load_kwh') or 0.)))
            / max(.01, eta_d) * (1. + hp_uncertainty)
            for i in hp_bridge
        )
        added = []
        while True:
            left, right = min(selected), max(selected)
            required_after = replenishment_soc_requirements(
                work[right:], capacity_kwh, reserve_pct, buffer_pct,
                eta_c, eta_d, max_power_kw, slot_minutes, uncertainty_weight)[0]
            hp_required_after = (capacity_kwh * (reserve_pct + buffer_pct) / 100.
                                 + hp_reserve_kwh) / capacity_kwh * 100.
            required_after = max(required_after, hp_required_after)
            gap_load = sum(deficit(i)/eta_d*(1.+.1*uncertainty_weight)
                           for i in range(left, right+1) if i not in selected)
            needed = max(0., capacity_kwh*(min(100., required_after)-reserve_pct)/100.) + gap_load
            available = sum(math.floor(min(max_power_kw*hours,
                max(0., grid_import_limit_kw*hours-deficit(i)))*eta_c/quantum+1e-9)*quantum
                for i in selected)
            if available + 1e-9 >= needed:
                status = 'BATTERY_CAPACITY_LIMIT' if required_after > 100.+1e-9 else 'CAPACITY_READY'
                break
            neighbours = {j for i in selected for j in (i-1, i+1)
                          if j in night and j not in selected
                          and not strict_database_bool(work[j].get('sale_window', False), 'sale_window')
                          and work[j].get('actual_recorded_at') is None}
            affordable = [j for j in neighbours if float(work[j]['price_buy_pln_kwh'])/(eta_c*eta_d)
                          + degradation + min_margin <= avoided_price + 1e-9]
            if not affordable:
                status = 'ECONOMIC_OR_NIGHT_CAPACITY_LIMIT'
                break
            index = min(affordable, key=lambda i: (float(work[i]['price_buy_pln_kwh']), i))
            work[index].update(buy_window=True, sale_window=False, market_window='BUY')
            selected.add(index)
            changed.add(index)
            added.append(index)
        reports.append({'day': str(day), 'status': status,
                        'window_end': str(work[max(selected)]['slot_end']),
                        'coverage_until': str(work[boundary-1]['slot_end']),
                        'forecast_horizon_end': str(work[-1]['slot_end']),
                        'requirement_source': 'FULL_HORIZON_FINITE_FUTURE_BUY',
                        'required_soc_after_window_pct': required_after,
                        'battery_capacity_shortfall_pct': max(0., required_after-100.),
                        'required_charge_kwh': needed, 'available_charge_kwh': available,
                        'hp_reserve_kwh': round(hp_reserve_kwh, 3),
                        'hp_reserve_until': str(work[boundary-1]['slot_end']),
                        'window_capacity_shortfall_kwh': max(0., needed-available),
                        'avoided_morning_price_pln_kwh': avoided_price,
                        'added_slots': [str(work[i]['slot_start']) for i in added]})
    return {'rows': work, 'changed_indices': sorted(changed), 'reports': reports}


def extend_buy_capacity_before_sell(
        rows: list[dict], capacity_kwh: float, reserve_pct: float, buffer_pct: float,
        eta_c: float, eta_d: float, max_power_kw: float, slot_minutes: int,
        uncertainty_weight: float, grid_import_limit_kw: float = 12.0,
        earliest_slot_start: datetime | None = None) -> dict:
    """Open enough adjacent BUY slots before SELL to meet forecast SOC needs."""
    work = [dict(row) for row in rows]
    if earliest_slot_start is not None and earliest_slot_start.tzinfo is not None:
        # HA supplies the cutoff as Warsaw-aware time; DB slots are local naive.
        earliest_slot_start = earliest_slot_start.replace(tzinfo=None)
    changed, reports = set(), []
    hours = slot_minutes / 60.0
    quantum = max(0.001, capacity_kwh * 0.001)
    days = sorted({row['slot_start'].date() for row in work})

    def flag(row, key):
        return strict_database_bool(row.get(key, False), key)

    def deficit(row):
        return max(0.0, float(row['forecast_load_kwh'])
                   + float(row.get('forecast_heat_pump_load_kwh') or 0.0)
                   - float(row['forecast_pv_total_kwh']))

    for day in days:
        indices = [i for i, row in enumerate(work)
                   if row['slot_start'].date() == day and 6 <= row['slot_start'].hour < 22
                   and (earliest_slot_start is None or row['slot_start'] >= earliest_slot_start)
                   and row.get('actual_recorded_at') is None]
        starts = [i for i in indices if flag(work[i], 'sale_window')
                  and (i == 0 or work[i-1]['slot_start'].date() != day
                       or not flag(work[i-1], 'sale_window'))]
        for sell_start in starts:
            buys = [i for i in indices if i < sell_start and flag(work[i], 'buy_window')
                    and not flag(work[i], 'sale_window')]
            if not buys:
                reports.append({'sell_start': str(work[sell_start]['slot_start']),
                                'status': 'NO_PRE_SELL_BUY_WINDOW'})
                continue
            selected = set([max(buys)])
            while selected:
                right = max(selected)
                requirements = replenishment_soc_requirements(
                    work[right:], capacity_kwh, reserve_pct, buffer_pct,
                    eta_c, eta_d, max_power_kw, slot_minutes, uncertainty_weight)
                required = max(0.0, capacity_kwh
                               * (min(100.0, requirements[0]) - reserve_pct) / 100.0)
                available = sum(math.floor(min(
                    max_power_kw * hours,
                    max(0.0, grid_import_limit_kw * hours - deficit(work[i])))
                    * eta_c / quantum + 1e-9) * quantum for i in selected)
                if available + 1e-9 >= required:
                    status = 'SELL_ENERGY_READY'
                    break
                neighbours = {j for i in selected for j in (i-1, i+1)
                              if j in indices and j < sell_start and j not in selected
                              and not flag(work[j], 'buy_window')
                              and not flag(work[j], 'sale_window')
                              and work[j].get('actual_recorded_at') is None
                              and work[j].get('price_buy_pln_kwh') is not None}
                if not neighbours:
                    status = 'SELL_COVERAGE_CAPACITY_LIMIT'
                    break
                j = min(neighbours, key=lambda k: (float(work[k]['price_buy_pln_kwh']), k))
                work[j].update(buy_window=True, sale_window=False, market_window='BUY')
                selected.add(j)
                changed.add(j)
            reports.append({'sell_start': str(work[sell_start]['slot_start']),
                            'status': status, 'required_charge_kwh': required,
                            'available_charge_kwh': available,
                            'added_slots': [str(work[i]['slot_start'])
                                            for i in sorted(selected) if i in changed]})
    return {'rows': work, 'changed_indices': sorted(changed), 'reports': reports}


def extend_buy_capacity_for_minimum_soc(
        rows: list[dict], initial_soc_pct: float | None, capacity_kwh: float,
        reserve_pct: float, buffer_pct: float, eta_c: float, eta_d: float,
        max_power_kw: float, slot_minutes: int, uncertainty_weight: float,
        grid_import_limit_kw: float = 12.0,
        earliest_slot_start: datetime | None = None) -> dict:
    """Promote the cheapest safe NEUTRAL slots to BUY when the SOC bridge is short.

    A shortfall below the configured minimum is not a reason to stop planning or
    lower the requirement. Open a real BUY permission early enough to recover
    the reserve, using the cheapest eligible neutral slot before the projected
    reserve breach. Never overwrite SELL, completed, or price-incomplete slots.
    """
    work = [dict(row) for row in rows]
    if earliest_slot_start is not None and earliest_slot_start.tzinfo is not None:
        earliest_slot_start = earliest_slot_start.replace(tzinfo=None)
    if initial_soc_pct is None or not math.isfinite(float(initial_soc_pct)):
        return {"rows": work, "changed_indices": [],
                "reports": [{"status": "WAITING_INITIAL_SOC"}]}
    initial = max(0.0, min(100.0, float(initial_soc_pct)))
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(90.0, float(reserve_pct)))
    eta_charge = max(0.01, float(eta_c))
    eta_discharge = max(0.01, float(eta_d))
    max_power = max(0.0, float(max_power_kw))
    hours = max(0.0, float(slot_minutes) / 60.0)
    max_import = max(0.0, float(grid_import_limit_kw))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    changed: set[int] = set()
    reports: list[dict] = []

    def flag(row, key):
        return strict_database_bool(row.get(key, False), key)

    def load_kwh(row):
        return (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))

    def pv_kwh(row):
        return max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))

    def eligible_neutral(index):
        row = work[index]
        start = row["slot_start"]
        return (
            (earliest_slot_start is None or start >= earliest_slot_start)
            and row.get("actual_recorded_at") is None
            and not flag(row, "buy_window")
            and not flag(row, "sale_window")
            and row.get("price_buy_pln_kwh") is not None
            and math.isfinite(float(row["price_buy_pln_kwh"]))
        )

    def projected_reserve_breach():
        soc = initial
        for index, row in enumerate(work):
            if ((earliest_slot_start is not None and row["slot_start"] < earliest_slot_start)
                    or row.get("actual_recorded_at") is not None):
                continue
            load = load_kwh(row)
            pv = pv_kwh(row)
            if flag(row, "buy_window") and not flag(row, "sale_window"):
                grid_headroom = max(0.0, max_import * hours - max(0.0, load - pv))
                charge_input = min(max_power * hours, grid_headroom)
                soc = min(100.0, soc + charge_input * eta_charge / capacity * 100.0)
            deficit = max(0.0, load - pv) / eta_discharge * (1.0 + uncertainty)
            surplus = max(0.0, pv - load) * eta_charge * (1.0 - uncertainty)
            soc += (surplus - deficit) / capacity * 100.0
            if soc < reserve - 1e-9:
                return index
        return None

    base_requirement = replenishment_soc_requirements(
        work, capacity, reserve, buffer_pct, eta_charge, eta_discharge,
        max_power, slot_minutes, uncertainty_weight)
    required_before = base_requirement[0] if base_requirement else reserve
    initial_breach = projected_reserve_breach()
    if initial_breach is None:
        return {"rows": work, "changed_indices": [],
                "reports": [{"status": "RESERVE_BRIDGE_COVERED",
                             "initial_soc_pct": round(initial, 3),
                             "required_soc_pct": round(required_before, 3)}]}

    # Add only enough recovery permissions to eliminate projected reserve
    # breaches. Comparing initial SOC with a whole-horizon requirement here
    # can keep promoting neutral slots long after a usable path exists.
    for _ in range(len(work)):
        breach = projected_reserve_breach()
        if breach is None:
            break
        candidates = [
            i for i in range(min(breach, len(work) - 1) + 1)
            if i not in changed and eligible_neutral(i)
        ]
        if not candidates:
            reports.append({
                "status": "NO_NEUTRAL_BUY_BEFORE_RESERVE_BREACH",
                "initial_soc_pct": round(initial, 3),
                "reserve_pct": round(reserve, 3),
                "reserve_breach_slot": str(work[breach]["slot_start"]),
            })
            break
        if initial < reserve - 1e-9 and not changed:
            selected = min(candidates)
        else:
            selected = min(
                candidates,
                key=lambda i: (float(work[i]["price_buy_pln_kwh"]), i))
        work[selected].update(buy_window=True, sale_window=False, market_window="BUY")
        changed.add(selected)
        reports.append({
            "status": "NEUTRAL_PROMOTED_TO_BUY",
            "slot_start": str(work[selected]["slot_start"]),
            "price_buy_pln_kwh": float(work[selected]["price_buy_pln_kwh"]),
            "initial_soc_pct": round(initial, 3),
            "reserve_pct": round(reserve, 3),
            "projected_breach_slot": str(work[breach]["slot_start"]),
        })

    final_requirement = replenishment_soc_requirements(
        work, capacity, reserve, buffer_pct, eta_charge, eta_discharge,
        max_power, slot_minutes, uncertainty_weight)
    required_after = final_requirement[0] if final_requirement else reserve
    final_breach = projected_reserve_breach()
    reports.append({
        "status": ("RESERVE_RECOVERY_BUY_CAPACITY_READY"
                   if final_breach is None
                   else "RESERVE_RECOVERY_CAPACITY_SHORTFALL"),
        "initial_soc_pct": round(initial, 3),
        "reserve_pct": round(reserve, 3),
        "required_soc_pct": round(required_after, 3),
        "reserve_breach_slot": (
            str(work[final_breach]["slot_start"]) if final_breach is not None else None),
        "added_slots": [str(work[i]["slot_start"]) for i in sorted(changed)],
    })
    return {"rows": work, "changed_indices": sorted(changed), "reports": reports}



@dataclass(frozen=True)
class IngestionAdapters:
    options: dict
    timezone: Any
    pv_forecast_entities: dict
    db: Callable
    local_now: Callable
    slot_start: Callable
    canonical_slots_for_day: Callable
    number: Callable
    ha_state: Callable
    ha_service_response: Callable
    record_event: Callable


def build_ingestion(a: IngestionAdapters):
    OPTIONS, TZ, PV_FORECAST_ENTITIES = a.options, a.timezone, a.pv_forecast_entities
    db, local_now, slot_start = a.db, a.local_now, a.slot_start
    canonical_slots_for_day, number, ha_state = a.canonical_slots_for_day, a.number, a.ha_state
    ha_service_response, record_event = a.ha_service_response, a.record_event

    def ensure_night_buy_capacity(rows: list[dict], cur, initial_soc_pct=None) -> dict:
        restored_slots = []
        if hasattr(cur, "fetchone"):
            # Undo neutral BUY permissions promoted by the prior SOC-recovery
            # pass. Skip slots whose RCE price has been refreshed afterwards.
            cur.execute("""SELECT created_at,payload_json FROM ems_gpt_core_events
              WHERE event_type='night_buy_capacity' ORDER BY created_at DESC LIMIT 1""", ())
            previous_event = cur.fetchone() or {}
            previous_payload = previous_event.get("payload_json") or {}
            if isinstance(previous_payload, str):
                try:
                    previous_payload = json.loads(previous_payload)
                except (TypeError, ValueError):
                    previous_payload = {}
            promoted = set()
            for report in previous_payload.get("reports", []):
                if not isinstance(report, dict):
                    continue
                if report.get("status") == "NEUTRAL_PROMOTED_TO_BUY" and report.get("slot_start"):
                    promoted.add(str(report["slot_start"]))
                promoted.update(str(slot) for slot in (report.get("added_slots") or []))
            event_created_at = previous_event.get("created_at")
            if isinstance(event_created_at, str):
                try:
                    event_created_at = datetime.fromisoformat(event_created_at.replace("Z", "+00:00"))
                except ValueError:
                    event_created_at = None
            if getattr(event_created_at, "tzinfo", None) is not None:
                event_created_at = event_created_at.replace(tzinfo=None)
            if event_created_at is not None and promoted:
                for row in rows:
                    if (str(row.get("slot_start")) not in promoted
                            or row.get("actual_recorded_at") is not None
                            or row.get("price_source") != "PSE_API"
                            or strict_database_bool(row.get("sale_window", False), "sale_window")
                            or not strict_database_bool(row.get("buy_window", False), "buy_window")):
                        continue
                    fetched_at = row.get("price_fetched_at")
                    if fetched_at is None:
                        continue
                    if isinstance(fetched_at, str):
                        try:
                            fetched_at = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                    if getattr(fetched_at, "tzinfo", None) is not None:
                        fetched_at = fetched_at.replace(tzinfo=None)
                    if fetched_at > event_created_at:
                        continue
                    cur.execute("""UPDATE ems_gpt_slots
                      SET buy_window=0,market_window='NEUTRAL'
                      WHERE slot_start=%s AND actual_recorded_at IS NULL
                        AND price_source='PSE_API' AND sale_window=0 AND buy_window=1""",
                      (row["slot_start"],))
                    if cur.rowcount != 1:
                        raise RuntimeError("PRIOR_SOC_RECOVERY_BUY_PERMISSION_CHANGED")
                    row.update(buy_window=False, market_window="NEUTRAL")
                    restored_slots.append(str(row["slot_start"]))
        night = extend_night_buy_capacity(
            rows, float(OPTIONS.get('battery_capacity_kwh', 15.)),
            float(OPTIONS.get('battery_min_soc_pct', 15.)),
            float(OPTIONS.get('soc_replenishment_buffer_pct', 2.)),
            float(OPTIONS.get('battery_charge_efficiency', .9)),
            float(OPTIONS.get('battery_discharge_efficiency', .95)),
            float(OPTIONS.get('battery_max_power_kw', 5.)), int(OPTIONS.get('slot_minutes', 15)),
            float(OPTIONS.get('forecast_uncertainty_weight', 1.)),
            float(OPTIONS.get('battery_degradation_cost_pln_kwh', .08)),
            float(OPTIONS.get('minimum_arbitrage_margin_pln_kwh', .05)),
            float(OPTIONS.get('grid_import_limit_kw', 12.)), slot_start())
        result = extend_buy_capacity_before_sell(
            night['rows'], float(OPTIONS.get('battery_capacity_kwh', 15.)),
            float(OPTIONS.get('battery_min_soc_pct', 15.)),
            float(OPTIONS.get('soc_replenishment_buffer_pct', 2.)),
            float(OPTIONS.get('battery_charge_efficiency', .9)),
            float(OPTIONS.get('battery_discharge_efficiency', .95)),
            float(OPTIONS.get('battery_max_power_kw', 5.)), int(OPTIONS.get('slot_minutes', 15)),
            float(OPTIONS.get('forecast_uncertainty_weight', 1.)),
            float(OPTIONS.get('grid_import_limit_kw', 12.)), slot_start())
        recovery = extend_buy_capacity_for_minimum_soc(
            result['rows'], initial_soc_pct,
            float(OPTIONS.get('battery_capacity_kwh', 15.)),
            float(OPTIONS.get('battery_min_soc_pct', 15.)),
            float(OPTIONS.get('soc_replenishment_buffer_pct', 2.)),
            float(OPTIONS.get('battery_charge_efficiency', .9)),
            float(OPTIONS.get('battery_discharge_efficiency', .95)),
            float(OPTIONS.get('battery_max_power_kw', 5.)),
            int(OPTIONS.get('slot_minutes', 15)),
            float(OPTIONS.get('forecast_uncertainty_weight', 1.)),
            float(OPTIONS.get('grid_import_limit_kw', 12.)), slot_start())
        result['rows'] = recovery['rows']
        result['changed_indices'] = sorted(set(night['changed_indices'])
                                           | set(result['changed_indices'])
                                           | set(recovery['changed_indices']))
        result['reports'] = night['reports'] + result['reports'] + recovery['reports']
        if restored_slots:
            result['reports'].append({'status': 'PRIOR_RECOVERY_BUY_WINDOWS_RESTORED',
                                      'slots': restored_slots})
        if result['changed_indices']:
            for index in result['changed_indices']:
                cur.execute("""UPDATE ems_gpt_slots SET buy_window=1,market_window='BUY'
                  WHERE slot_start=%s AND actual_recorded_at IS NULL
                    AND price_source='PSE_API' AND sale_window=0""",
                    (result['rows'][index]['slot_start'],))
                if cur.rowcount != 1:
                    raise RuntimeError('NIGHT_BUY_PERMISSION_CHANGED_DURING_PREPARATION')
        if result['reports'] or restored_slots:
            record_event('night_buy_capacity', 'rce_import', {'reports': result['reports']})
        return result

    def refresh_pv_forecast() -> dict:
        """Disaggregate Open-Meteo daily PV energy using an actual-production profile."""
        today = local_now().date()
        results = {}
        with db() as conn, conn.cursor() as cur:
            pv1_scale = pv2_scale = 1.0
            correction_source = "DISABLED"
            correction_loaded = False
            for label, target in (("today", today), ("tomorrow", today+timedelta(days=1))):
                lower = max(slot_start().replace(tzinfo=None), datetime.combine(target, datetime.min.time())) if label == "today" else datetime.combine(target, datetime.min.time())
                upper = datetime.combine(target+timedelta(days=1), datetime.min.time())
                pv1 = number(ha_state(PV_FORECAST_ENTITIES[label][0]))
                pv2 = number(ha_state(PV_FORECAST_ENTITIES[label][1]))
                if (pv1 is None or pv2 is None or not math.isfinite(pv1)
                        or not math.isfinite(pv2) or pv1 < 0 or pv2 < 0):
                    stored = preserve_stored_pv_forecasts(cur, lower, upper)
                    results[label] = {**stored, "status": stored["status"] if stored["status"] == "STORED_FORECAST" else "WAITING_SOURCE",
                                      "reason": "SOURCE_UNAVAILABLE"}
                    continue
                if not correction_loaded:
                    correction_loaded = True
                    if bool(OPTIONS.get("forecast_history_corrections_enabled", True)):
                        cur.execute("""SELECT suggested_pv1_scale,suggested_pv2_scale,metric_confidence_pct
                          FROM ems_gpt_core_analytics_runs WHERE status='COMPLETED'
                          ORDER BY completed_at DESC LIMIT 1""")
                        correction = cur.fetchone() or {}
                        if float(correction.get("metric_confidence_pct") or 0) >= 80.0:
                            pv1_scale = min(1.5, max(0.5, float(correction.get("suggested_pv1_scale") or 1.0)))
                            pv2_scale = min(1.5, max(0.5, float(correction.get("suggested_pv2_scale") or 1.0)))
                            correction_source = "ANALYTICS_LATEST"
                cur.execute("""SELECT s.slot_start FROM ems_gpt_slots s
                  WHERE s.slot_start>=%s AND s.slot_start<%s AND s.actual_recorded_at IS NULL
                  ORDER BY s.slot_start""", (lower, upper))
                slots = list(cur.fetchall())
                if not slots:
                    results[label] = {"status": "NO_OPEN_SLOTS", "slots": 0}
                    continue
                profile_month = None
                if pv1 + pv2 == 0:
                    # An explicit zero from both sources is valid even without
                    # learned daytime weights (e.g. remaining energy at night).
                    weights = [0.0] * len(slots)
                else:
                    cur.execute("""SELECT month_no,hour_no,minute_no,mean_share
                      FROM ems_gpt_core_pv_profiles WHERE sample_days>0""")
                    weights, profile_month = select_pv_profile(slots, list(cur.fetchall()), target.month)
                weight_sum = sum(weights)
                if not weights:
                    stored = preserve_stored_pv_forecasts(cur, lower, upper)
                    results[label] = {**stored, "status": stored["status"] if stored["status"] == "STORED_FORECAST" else "WAITING_PROFILE",
                                      "reason": "PROFILE_UNAVAILABLE"}
                    continue
                profile_source = ("SOURCE_ZERO" if profile_month is None else
                                  "CURRENT_MONTH" if profile_month == target.month else "NEAREST_LEARNED_MONTH")
                for row, weight in zip(slots, weights):
                    share = weight/weight_sum if weight_sum > 0 else 0.0
                    a, b = pv1*pv1_scale*share, pv2*pv2_scale*share
                    cur.execute("""UPDATE ems_gpt_slots SET forecast_pv1_kwh=%s,forecast_pv2_kwh=%s,
                      forecast_pv_total_kwh=%s,forecast_pv_source='OPEN_METEO_PROFILE_V1',
                      pv_correction=%s WHERE slot_start=%s AND actual_recorded_at IS NULL""",
                      (round(a, 6), round(b, 6), round(a+b, 6),
                       round((pv1*pv1_scale+pv2*pv2_scale)/max(0.001,pv1+pv2), 4), row["slot_start"]))
                results[label] = {"status": "OK", "slots": len(slots), "pv1_kwh": pv1,
                                  "pv2_kwh": pv2, "pv1_scale": pv1_scale,
                                  "pv2_scale": pv2_scale, "correction_source": correction_source,
                                  "profile_source": profile_source, "profile_month": profile_month}
        record_event("pv_forecast_refreshed", "analytics", results)
        return results
    
    
    def refresh_weather_forecast() -> dict:
        response = ha_service_response("weather", "get_forecasts", {"entity_id": "weather.dom", "type": "hourly"}, return_response=True) or {}
        service_response = response.get("service_response", response)
        forecast = (service_response.get("weather.dom") or {}).get("forecast", [])
        updated = 0
        with db() as conn, conn.cursor() as cur:
            for item in forecast:
                try:
                    hour = datetime.fromisoformat(str(item["datetime"]).replace("Z", "+00:00")).astimezone(TZ).replace(tzinfo=None)
                    cur.execute("""UPDATE ems_gpt_slots SET forecast_temperature_c=%s,
                      forecast_cloud_coverage_pct=%s,forecast_precipitation_mm=%s
                      WHERE slot_start>=%s AND slot_start<%s AND actual_recorded_at IS NULL""",
                      (item.get("temperature"), item.get("cloud_coverage"), item.get("precipitation"),
                       hour, hour+timedelta(hours=1)))
                    updated += cur.rowcount
                except (KeyError, TypeError, ValueError):
                    continue
        result = {"status": "OK" if forecast else "WAITING_SOURCE", "hours": len(forecast), "slots_updated": updated, "source": "OPEN_METEO"}
        record_event("weather_forecast_refreshed", "analytics", result, "INFO" if forecast else "WARNING")
        return result
    
    
    def refresh_rce(day=None) -> dict:
        target = day or (local_now().date()+timedelta(days=1))
        calendar = canonical_slots_for_day(target)
        calendar_by_local = {r["slot_start_local"]: r for r in calendar if not r["local_fold"]}
        expected = len(calendar)
        margin=max(0.0,float(OPTIONS.get("purchase_margin_pln_kwh", 0.59)))
        params={"$select":"dtime,period,rce_pln,business_date,publication_ts",
                "$filter":f"business_date eq '{target:%Y-%m-%d}'","$first":200}
        req=urllib.request.Request("https://api.raporty.pse.pl/api/rce-pln?"+urlencode(params),
                                   headers={"Accept":"application/json","User-Agent":"EMS-GPT-Core/0.4"})
        with urllib.request.urlopen(req,timeout=30) as response: payload=json.load(response)
        unique={}
        for item in payload.get("value",[]):
            try:
                end=datetime.fromisoformat(str(item["dtime"]).replace("Z","+00:00"))
                if end.tzinfo is None: end=end.replace(tzinfo=TZ)
                start=end.astimezone(TZ)-timedelta(minutes=15)
                if start.date()!=target: continue
                raw=float(item["rce_pln"])/1000
                pub=item.get("publication_ts")
                unique[start.replace(tzinfo=None)]={"sell":raw,"buy":raw+margin,"publication":pub}
            except Exception:
                continue
        ordered=sorted(unique)
        eta_c=max(.01,min(1.0,float(OPTIONS.get("battery_charge_efficiency",.90))))
        eta_d=max(.01,min(1.0,float(OPTIONS.get("battery_discharge_efficiency",.95))))
        degradation=max(0,float(OPTIONS.get("battery_degradation_cost_pln_kwh",.08)))
        min_margin=max(0,float(OPTIONS.get("minimum_arbitrage_margin_pln_kwh",.05)))
        buy_tolerance=max(0.0,float(OPTIONS.get("buy_window_tolerance_pln_kwh",.05)))
        baseline=deye_program_soc_baselines(OPTIONS)
        terminal_pct=max([float(value) for value in baseline.values()] or [15.0])
        capacity=max(1.0,float(OPTIONS.get("battery_capacity_kwh",15.0)))
        reserve=max(0.0,float(OPTIONS.get("battery_min_soc_pct",15.0)))
        max_kw=max(0.25,float(OPTIONS.get("battery_max_power_kw",5.0)))
        slot_hours=max(1,int(OPTIONS.get("slot_minutes",15)))/60.0
        minimum_buy_slots=max(1,math.ceil(
            capacity*max(0.0,terminal_pct-reserve)/100.0
            /max(0.001,max_kw*slot_hours*eta_c)))
        price_windows=derive_price_windows(
            [unique[s] for s in ordered],eta_c,eta_d,degradation,min_margin,
            buy_tolerance,minimum_buy_slots)
        for s,(sale_window,buy_window) in zip(ordered,price_windows):
            unique[s]["sale_window"]=sale_window
            unique[s]["buy_window"]=buy_window
            unique[s]["market_window"]=(
                "SELL" if sale_window else "BUY" if buy_window else "NEUTRAL")
        with db() as conn,conn.cursor() as cur:
            for s,v in unique.items():
                canonical=calendar_by_local.get(s)
                cur.execute("""INSERT INTO ems_gpt_slots(slot_start,slot_end,price_sell_pln_kwh,price_buy_pln_kwh,
                  sale_window,buy_window,market_window,price_source,price_fetched_at,price_publication_at,
                  slot_id,slot_start_utc,slot_start_local,utc_offset_minutes,local_fold,local_day,slot_index_local)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,'PSE_API',NOW(6),%s,%s,%s,%s,%s,%s,%s,%s)
                  ON DUPLICATE KEY UPDATE price_sell_pln_kwh=IF(actual_recorded_at IS NULL,VALUES(price_sell_pln_kwh),price_sell_pln_kwh),
                  price_buy_pln_kwh=IF(actual_recorded_at IS NULL,VALUES(price_buy_pln_kwh),price_buy_pln_kwh),
                  sale_window=IF(actual_recorded_at IS NULL,VALUES(sale_window),sale_window),
                  buy_window=IF(actual_recorded_at IS NULL,VALUES(buy_window),buy_window),
                  market_window=IF(actual_recorded_at IS NULL,VALUES(market_window),market_window),
                  price_fetched_at=IF(actual_recorded_at IS NULL,NOW(6),price_fetched_at),
                  price_publication_at=IF(actual_recorded_at IS NULL,VALUES(price_publication_at),price_publication_at),
                  price_source=IF(actual_recorded_at IS NULL,'PSE_API',price_source),
                  slot_id=COALESCE(slot_id,VALUES(slot_id)),slot_start_utc=COALESCE(slot_start_utc,VALUES(slot_start_utc)),
                  slot_start_local=COALESCE(slot_start_local,VALUES(slot_start_local)),
                  utc_offset_minutes=COALESCE(utc_offset_minutes,VALUES(utc_offset_minutes)),
                  local_day=COALESCE(local_day,VALUES(local_day)),slot_index_local=COALESCE(slot_index_local,VALUES(slot_index_local))""",
                  (s,s+timedelta(minutes=15),v["sell"],v["buy"],v["sale_window"],v["buy_window"],v["market_window"],v["publication"],
                   canonical["slot_id"] if canonical else None,canonical["slot_start_utc"] if canonical else None,s,
                   canonical["utc_offset_minutes"] if canonical else None,canonical["local_fold"] if canonical else 0,
                   target,canonical["slot_index_local"] if canonical else None))
            # Window flags are a property of the complete currently available
            # price horizon. Recalculate every open slot after either day is
            # imported so yesterday's partial-horizon flag cannot survive.
            cur.execute("""SELECT slot_start,price_sell_pln_kwh,price_buy_pln_kwh
              FROM ems_gpt_slots WHERE actual_recorded_at IS NULL
                AND price_source='PSE_API' AND price_sell_pln_kwh IS NOT NULL
                AND price_buy_pln_kwh IS NOT NULL ORDER BY slot_start""")
            open_prices = list(cur.fetchall())
            open_windows = derive_price_windows(
                [{"sell": row["price_sell_pln_kwh"], "buy": row["price_buy_pln_kwh"]}
                 for row in open_prices],
                eta_c, eta_d, degradation, min_margin, buy_tolerance,
                minimum_buy_slots)
            for row, (sale_window, buy_window) in zip(open_prices, open_windows):
                market_window = "SELL" if sale_window else "BUY" if buy_window else "NEUTRAL"
                cur.execute("""UPDATE ems_gpt_slots
                  SET sale_window=%s,buy_window=%s,market_window=%s
                  WHERE slot_start=%s AND actual_recorded_at IS NULL""",
                  (sale_window, buy_window, market_window, row["slot_start"]))
        result={"day":str(target),"rows":len(unique),"expected":expected,
                "status":"OK" if len(unique)==expected else "PARTIAL","margin":margin}
        record_event("rce_refreshed","core",result)
        return result

    return SimpleNamespace(
        ensure_night_buy_capacity=ensure_night_buy_capacity,
        refresh_pv_forecast=refresh_pv_forecast,
        refresh_weather_forecast=refresh_weather_forecast,
        refresh_rce=refresh_rce,
    )
