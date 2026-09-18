from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Callable


@dataclass(frozen=True)
class FlexiblePpdDecision:
    pv_cwu_allowed: bool
    pv_ev_allowed: bool
    cwu_anchor: bool
    ev_anchor: bool
    cwu_window_start: datetime | None
    cwu_window_end: datetime | None
    ev_window_start: datetime | None
    ev_window_end: datetime | None
    reason: str


def _day_key(row: dict) -> date:
    value = row.get("local_day")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value)[:10])
    slot = row["slot_start"]
    return slot.date() if isinstance(slot, datetime) else date.fromisoformat(str(slot)[:10])


def _anchor(row: dict, threshold_kwh: float) -> bool:
    """An anchor proves that a flexible load window is useful.

    Target calculation is deliberately upstream of this function.  This
    module only reads the published SOC path; it never returns a target or a
    load forecast that could feed target calculation back into the planner.
    """
    return (
        not bool(row.get("sell_battery"))
        and bool(row.get("flexible_is_economic", True))
        and float(row.get("pv_flex_kwh") or 0.0) + 1e-9 >= threshold_kwh
        and float(row.get("soc_end_pct") or 0.0) + 0.01
        >= float(row.get("soc_target_pct") or 0.0)
    )


def _corridor_open(row: dict) -> bool:
    """Hard policy conflicts split a window; weak PV forecasts do not."""
    return (
        not bool(row.get("sell_battery"))
        and bool(row.get("flexible_is_economic", True))
    )


def _best_window(indices: list[int], anchors: list[bool], rows: list[dict]) -> tuple[int, int] | None:
    corridors: list[list[int]] = []
    current: list[int] = []
    for index in indices:
        if _corridor_open(rows[index]):
            current.append(index)
        elif current:
            corridors.append(current)
            current = []
    if current:
        corridors.append(current)
    candidates = []
    for corridor in corridors:
        hits = [index for index in corridor if anchors[index]]
        if hits:
            score = sum(float(rows[index].get("pv_flex_kwh") or 0.0) for index in hits)
            candidates.append((score, len(hits), -hits[0], hits[0], hits[-1]))
    if not candidates:
        return None
    best = max(candidates)
    return best[3], best[4]


def build_flexible_ppd(
    rows: list[dict], *, cwu_threshold_kwh: float, ev_threshold_kwh: float
) -> list[FlexiblePpdDecision]:
    """Build one continuous PV_CWU and PV_EV permission window per local day.

    Individual weak forecast slots inside a window stay allowed.  Runtime
    automations decide whether the appliance can actually run from measured
    PV surplus.  Re-running this pure function every planner cycle naturally
    moves a not-yet-started window when actual SOC differs from the old plan.
    """
    if not rows:
        return []
    cwu_threshold_kwh = max(0.0, float(cwu_threshold_kwh))
    ev_threshold_kwh = max(0.0, float(ev_threshold_kwh))
    cwu_anchors = [_anchor(row, cwu_threshold_kwh) for row in rows]
    ev_anchors = [_anchor(row, ev_threshold_kwh) for row in rows]

    day_indices: dict[date, list[int]] = {}
    for index, row in enumerate(rows):
        day_indices.setdefault(_day_key(row), []).append(index)

    cwu_bounds: dict[date, tuple[int, int] | None] = {}
    ev_bounds: dict[date, tuple[int, int] | None] = {}
    for day, indices in day_indices.items():
        cwu_bounds[day] = _best_window(indices, cwu_anchors, rows)
        ev_bounds[day] = _best_window(indices, ev_anchors, rows)

    decisions: list[FlexiblePpdDecision] = []
    for index, row in enumerate(rows):
        day = _day_key(row)
        cwu_bound = cwu_bounds[day]
        ev_bound = ev_bounds[day]
        cwu_allowed = cwu_bound is not None and cwu_bound[0] <= index <= cwu_bound[1]
        ev_allowed = ev_bound is not None and ev_bound[0] <= index <= ev_bound[1]
        cwu_start = rows[cwu_bound[0]]["slot_start"] if cwu_bound else None
        cwu_end = rows[cwu_bound[1]]["slot_start"] if cwu_bound else None
        ev_start = rows[ev_bound[0]]["slot_start"] if ev_bound else None
        ev_end = rows[ev_bound[1]]["slot_start"] if ev_bound else None
        decisions.append(FlexiblePpdDecision(
            pv_cwu_allowed=cwu_allowed,
            pv_ev_allowed=ev_allowed,
            cwu_anchor=cwu_anchors[index],
            ev_anchor=ev_anchors[index],
            cwu_window_start=cwu_start,
            cwu_window_end=cwu_end,
            ev_window_start=ev_start,
            ev_window_end=ev_end,
            reason=(
                f"target_read_only={float(row.get('soc_target_pct') or 0.0):.2f}; "
                f"soc_end={float(row.get('soc_end_pct') or 0.0):.2f}; "
                f"pv_flex={float(row.get('pv_flex_kwh') or 0.0):.3f}; "
                f"cwu_window={cwu_start}..{cwu_end}; ev_window={ev_start}..{ev_end}"
            ),
        ))
    return decisions


@dataclass(frozen=True)
class PpdAdapters:
    options: dict
    db: Callable
    slot_start: Callable
    record_event: Callable


def _next_buy_prices(rows: list[dict]) -> list[float | None]:
    result: list[float | None] = [None] * len(rows)
    best = None
    for index in range(len(rows) - 1, -1, -1):
        result[index] = best
        price = rows[index].get("price_buy_pln_kwh")
        if price is not None:
            best = float(price) if best is None else min(best, float(price))
    return result


def plan_bound_decisions(row: dict, threshold: float) -> tuple[tuple[str, bool, str, str], ...]:
    """Publish planner-owned processes without recalculating their policy.

    Import, battery export and space heating are already part of the frozen
    energy/SOC trajectory.  PPD exposes that recommendation to the executor;
    AUTO/FORCE_ON/FORCE_OFF is applied later and remains an execution concern.
    """
    buy = float(row.get("planned_buy_kwh") or 0.0)
    sell = float(row.get("planned_sell_kwh") or 0.0)
    grid_policy = str(row.get("grid_policy_planned") or "NEUTRAL")
    export_policy = str(row.get("export_policy_planned") or "NEUTRAL")
    hp_window = bool(row.get("heat_pump_window"))
    if (buy > threshold) != (grid_policy == "BUY_ALLOWED"):
        raise RuntimeError(
            f"PPD_IMPORT_PLAN_MISMATCH:{row.get('slot_start')}:"
            f"planned_buy={buy:.6f}:grid_policy={grid_policy}")
    if (sell > threshold) != (export_policy == "SELL_BAT"):
        raise RuntimeError(
            f"PPD_EXPORT_PLAN_MISMATCH:{row.get('slot_start')}:"
            f"planned_sell={sell:.6f}:export_policy={export_policy}")
    return (
        ("BATTERY_IMPORT", buy > threshold, grid_policy,
         f"planner_bound; planned_buy={buy:.3f}"),
        ("BATTERY_EXPORT", sell > threshold, export_policy,
         f"planner_bound; planned_sell={sell:.3f}"),
        ("HP_HEAT_DHW", hp_window, "ON" if hp_window else "OFF",
         "planner_bound; published_heat_pump_window"),
    )


def build_ppd_runner(a: PpdAdapters):
    """Create a read-plan/write-decisions PPD service.

    The service never updates SOC, target, price windows or core battery flows.
    Its only slot writes are presentation/policy fields and allocation of the
    already-published flexible PV remainder.
    """
    options, db = a.options, a.db

    def run_ppd(plan_run_id: str | None = None, run_type: str = "scheduled") -> dict:
        ppd_run_id = str(uuid.uuid4())
        cutoff = a.slot_start().replace(tzinfo=None)
        eta_d = max(0.01, min(1.0, float(options.get("battery_discharge_efficiency", 0.95))))
        margin = max(0.0, float(options.get("minimum_arbitrage_margin_pln_kwh", 0.05)))
        threshold = max(0.0, float(options.get("planned_flow_threshold_kwh", 0.02)))
        technical_threshold = max(
            threshold, float(options.get("technical_flow_threshold_kwh", 0.05)))
        cwu_threshold = max(0.0, float(options.get("pv_cwu_min_surplus_kw", 2.0))) * .25
        ev_threshold = max(0.0, float(options.get("pv_ev_min_surplus_kw", 1.5))) * .25
        with db() as conn, conn.cursor() as cur:
            if plan_run_id is None:
                cur.execute("""SELECT run_id FROM ems_gpt_plan_runs
                  WHERE status='PUBLISHED' ORDER BY published_at DESC LIMIT 1""")
                latest = cur.fetchone()
                if not latest:
                    raise RuntimeError("PPD_NO_PUBLISHED_PLAN")
                plan_run_id = str(latest["run_id"])
            cur.execute("""INSERT INTO ems_gpt_core_module_runs
              (run_id,module_name,run_type,status,started_at,slot_start,input_watermark)
              VALUES(%s,'ppd',%s,'RUNNING',NOW(6),%s,%s)""",
              (ppd_run_id, run_type, cutoff, plan_run_id))
            cur.execute("""SELECT * FROM ems_gpt_slots
              WHERE actual_recorded_at IS NULL AND slot_start>=%s
                AND plan_run_id=%s AND plan_stage='PUBLISHED'
              ORDER BY slot_start""", (cutoff, plan_run_id))
            rows = list(cur.fetchall())
            if not rows:
                raise RuntimeError(f"PPD_PLAN_ROWS_MISSING:{plan_run_id}")
            next_buy = _next_buy_prices(rows)
            flexible_rows = []
            for index, row in enumerate(rows):
                raw_flexible = sum(max(0.0, float(row.get(field) or 0.0)) for field in (
                    "planned_pv_export_kwh", "planned_pv_to_cwu_kwh",
                    "planned_pv_to_ev_kwh", "planned_pv_curtail_kwh"))
                sell_battery = float(row.get("planned_sell_kwh") or 0.0) > threshold
                replacement = next_buy[index]
                flexible_rows.append({
                    "slot_start": row["slot_start"], "local_day": row.get("local_day"),
                    "soc_end_pct": row.get("soc_end_plan_pct"),
                    "soc_target_pct": (row.get("soc_charge_target_pct")
                                       if row.get("soc_charge_target_pct") is not None
                                       else row.get("soc_target_pct")),
                    "pv_flex_kwh": raw_flexible, "sell_battery": sell_battery,
                    "flexible_is_economic": (replacement is None or
                        float(row.get("price_sell_pln_kwh") or 0.0) <= replacement + margin),
                })
            flexible = build_flexible_ppd(
                flexible_rows, cwu_threshold_kwh=cwu_threshold,
                ev_threshold_kwh=ev_threshold)
            decision_count = 0
            for index, (row, flex) in enumerate(zip(rows, flexible)):
                buy = float(row.get("planned_buy_kwh") or 0.0)
                sell = float(row.get("planned_sell_kwh") or 0.0)
                load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                        + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
                pv_to_load = min(
                    load, max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0)))
                battery_to_load = max(
                    0.0, float(row.get("planned_battery_discharge_kwh") or 0.0) * eta_d
                    - sell)
                grid_load = max(0.0, load - pv_to_load - battery_to_load)
                reserve = float(row.get("soc_reserve_pct") or 15.0)
                soc_start = float(row.get("soc_start_plan_pct") or reserve)
                if (grid_load > technical_threshold and buy <= threshold
                        and soc_start > reserve + 0.01):
                    raise RuntimeError(
                        f"PPD_VOLUNTARY_GRID_LOAD_NOT_NEUTRAL:{row['slot_start']}:{grid_load}")
                pv_flex = float(flexible_rows[index]["pv_flex_kwh"])
                cwu = min(pv_flex, 0.625) if flex.cwu_anchor else 0.0
                after_cwu = max(0.0, pv_flex - cwu)
                ev = after_cwu if flex.ev_anchor and after_cwu >= ev_threshold else 0.0
                after_flex = max(0.0, after_cwu - ev)
                sell_price = float(row.get("price_sell_pln_kwh") or 0.0)
                pv_export = after_flex if sell_price > 0.0 else 0.0
                curtail = after_flex - pv_export
                grid_policy = str(row.get("grid_policy_planned") or "NEUTRAL")
                export_policy = str(row.get("export_policy_planned") or "NEUTRAL")
                recommendation = ("Zakup ładowanie" if buy > threshold else
                    "Sprzedaż z baterii" if sell > threshold else "Sprzedaż PV" if pv_export > threshold else
                    "Ładowanie PV" if float(row.get("planned_battery_charge_kwh") or 0.0) > threshold else
                    "Autokonsumpcja PV" if float(row.get("forecast_pv_total_kwh") or 0.0) > threshold else
                    "Autokonsumpcja z baterii" if battery_to_load > threshold else
                    "Zasilanie z sieci" if grid_load > technical_threshold else
                    "Neutralny")
                reason = (f"ppd_run={ppd_run_id}; plan_run={plan_run_id}; target_read_only; "
                          f"grid={grid_policy}; export={export_policy}; {flex.reason}")[:255]
                cur.execute("""UPDATE ems_gpt_slots SET planned_pv_to_cwu_kwh=%s,
                  planned_pv_to_ev_kwh=%s,planned_pv_export_kwh=%s,
                  planned_pv_curtail_kwh=%s,recommendation=%s,ppd_reason=%s,
                  ppd_run_type=%s,ppd_version='CORE_0_36_11',ppd_locked_at=NOW(6)
                  WHERE slot_start=%s AND plan_run_id=%s""",
                  (round(cwu, 6), round(ev, 6), round(pv_export, 6), round(curtail, 6),
                   recommendation, reason, run_type,
                   row["slot_start"], plan_run_id))
                decisions = plan_bound_decisions(row, threshold) + (
                    ("PV_CWU", flex.pv_cwu_allowed, "ALLOW" if flex.pv_cwu_allowed else "BLOCK", flex.reason),
                    ("PV_EV", flex.pv_ev_allowed, "ALLOW" if flex.pv_ev_allowed else "BLOCK", flex.reason),
                )
                for process, eligible, decision, process_reason in decisions:
                    cur.execute("""INSERT INTO ems_gpt_core_process_decisions
                      (slot_start,slot_id,process_name,decision,eligible,reason,plan_run_id,
                       ppd_run_id,valid_until,connector_required,published_at)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,NOW(6))
                      ON DUPLICATE KEY UPDATE decision=VALUES(decision),eligible=VALUES(eligible),
                       reason=VALUES(reason),slot_id=COALESCE(slot_id,VALUES(slot_id)),
                       plan_run_id=VALUES(plan_run_id),ppd_run_id=VALUES(ppd_run_id),
                       valid_until=VALUES(valid_until),published_at=NOW(6)""",
                      (row["slot_start"], row.get("slot_id"), process, decision, eligible,
                       process_reason[:1000], plan_run_id, ppd_run_id,
                       row["slot_start"] + timedelta(minutes=16)))
                    decision_count += 1
            cur.execute("""UPDATE ems_gpt_core_module_runs SET status='COMPLETED',
              completed_at=NOW(6),output_version=%s,reason=%s WHERE run_id=%s""",
              (f"PPD:{ppd_run_id}", json.dumps({"plan_run_id": plan_run_id,
               "rows": len(rows), "decisions": decision_count}), ppd_run_id))
        result = {"status": "COMPLETED", "run_id": ppd_run_id,
                  "plan_run_id": plan_run_id, "rows": len(rows),
                  "decisions": decision_count}
        a.record_event("ppd_completed", "ppd", result)
        return result

    return SimpleNamespace(run_ppd=run_ppd)
