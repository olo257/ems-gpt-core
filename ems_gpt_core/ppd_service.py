from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Callable
from zoneinfo import ZoneInfo

PPD_VERSION = "CORE_0_40_5"


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


def _database_bool(value, field: str) -> bool:
    """Decode database flags without treating textual/binary zero as true."""
    if isinstance(value, (bytes, bytearray)):
        if value in (b"\x00", b"0"):
            return False
        if value in (b"\x01", b"1"):
            return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("0", "false"):
            return False
        if normalized in ("1", "true"):
            return True
    if value in (False, 0):
        return False
    if value in (True, 1):
        return True
    raise RuntimeError(f"PPD_INVALID_DATABASE_BOOL:{field}:{value!r}")


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
        float(row.get("pv_flex_kwh") or 0.0) + 1e-9 >= threshold_kwh
        and float(row.get("soc_end_pct") or 0.0) + 0.01
        >= 100.0
    )


def current_live_flexible_row(row: dict, telemetry: dict, *, now: datetime,
                              stale_seconds: int) -> dict:
    """Use a fresh measured PV surplus to open the current slot's corridor."""
    captured = telemetry.get("captured_at")
    if not isinstance(captured, datetime) or not 0 <= (now - captured).total_seconds() <= stale_seconds:
        return row
    try:
        soc = float(telemetry["soc_pct"])
        pv = float(telemetry["pv_power_w"])
        load = float(telemetry["load_power_w"])
    except (KeyError, TypeError, ValueError):
        return row
    if soc + 0.01 < 100.0 or pv <= load:
        return row
    # Forecast allocations may be zero while actual PV is already exceeding
    # household load. Execution still applies the full live power thresholds.
    return {**row, "soc_end_pct": soc,
            "pv_flex_kwh": max(float(row.get("pv_flex_kwh") or 0.0),
                               (pv - load) / 4000.0)}


def _corridor_open(row: dict) -> bool:
    """Battery export is independent from the flexible-PV corridor."""
    return True


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
    rows: list[dict], *, cwu_threshold_kwh: float, ev_threshold_kwh: float,
    corridor_threshold_kwh: float = 0.02,
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

    flexible_bounds: dict[date, tuple[int, int] | None] = {}
    # The forecast only opens a candidate corridor. The live surplus guard
    # applies the actual appliance thresholds at execution time. Requiring a
    # forecast above the EV/CWU start threshold here can block both loads
    # while measured PV is being exported at a nonpositive price.
    shared_anchors = [_anchor(row, max(0.0, corridor_threshold_kwh)) for row in rows]
    for day, indices in day_indices.items():
        flexible_bounds[day] = _best_window(indices, shared_anchors, rows)

    decisions: list[FlexiblePpdDecision] = []
    for index, row in enumerate(rows):
        day = _day_key(row)
        flexible_bound = flexible_bounds[day]
        flexible_allowed = (
            flexible_bound is not None
            and flexible_bound[0] <= index <= flexible_bound[1]
        )
        cwu_allowed = flexible_allowed
        ev_allowed = flexible_allowed
        cwu_start = rows[flexible_bound[0]]["slot_start"] if flexible_bound else None
        cwu_end = rows[flexible_bound[1]]["slot_start"] if flexible_bound else None
        ev_start = cwu_start
        ev_end = cwu_end
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
                f"full_soc_required=100.00; "
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


def plan_bound_decisions(row: dict, threshold: float) -> tuple[tuple[str, bool, str, str], ...]:
    """Publish planner-owned processes without recalculating their policy.

    Import, battery export and space heating are already part of the frozen
    energy/SOC trajectory.  PPD exposes that recommendation to the executor;
    AUTO/FORCE_ON/FORCE_OFF is applied later and remains an execution concern.
    """
    buy = float(row.get("planned_buy_kwh") or 0.0)
    sell = float(row.get("planned_sell_kwh") or 0.0)
    pv_export = float(row.get("planned_pv_export_kwh") or 0.0)
    sell_price = float(row.get("price_sell_pln_kwh") or 0.0)
    sell_bat_allowed = _database_bool(
        row.get("sell_bat_policy_allowed") or 0, "sell_bat_policy_allowed")
    sell_pv_allowed = (
        _database_bool(row.get("sell_pv_policy_allowed") or 0,
                       "sell_pv_policy_allowed")
        and sell_price > 0.0
    )
    grid_policy = str(row.get("grid_policy_planned") or "NEUTRAL")
    export_policy = str(row.get("export_policy_planned") or "NEUTRAL")
    hp_window = _database_bool(row.get("heat_pump_window"), "heat_pump_window")
    if (buy > threshold) != (grid_policy == "BUY_ALLOWED"):
        raise RuntimeError(
            f"PPD_IMPORT_PLAN_MISMATCH:{row.get('slot_start')}:"
            f"planned_buy={buy:.6f}:grid_policy={grid_policy}")
    if (sell > threshold) != (export_policy == "SELL_BAT"):
        raise RuntimeError(
            f"PPD_EXPORT_PLAN_MISMATCH:{row.get('slot_start')}:"
            f"planned_sell={sell:.6f}:export_policy={export_policy}")
    if sell > threshold and not sell_bat_allowed:
        raise RuntimeError(
            f"PPD_SELL_BAT_BLOCKED_PLAN:{row.get('slot_start')}:"
            f"planned_sell={sell:.6f}")
    return (
        ("BATTERY_IMPORT", buy > threshold, grid_policy,
         f"planner_bound; planned_buy={buy:.3f}"),
        ("SELL_BAT", sell > threshold, "ALLOWED" if sell_bat_allowed else "BLOCKED",
         f"planner_bound; policy={'ALLOWED' if sell_bat_allowed else 'BLOCKED'}; "
         f"planned_sell={sell:.3f}"),
        # Permission is price/policy based. Forecast export is a separate
        # ON/OFF flow; a fresh surplus can appear after this plan was published.
        ("SELL_PV", sell_pv_allowed,
         "ALLOWED" if sell_pv_allowed else "BLOCKED",
         f"planner_bound; planned_pv_export={pv_export:.3f}; sell_price={sell_price:.3f}"),
        ("HP_HEAT_DHW", hp_window, "ON" if hp_window else "OFF",
         "planner_bound; published_heat_pump_window"),
    )


def planner_flexible_pv_remainder(row: dict) -> float:
    """Compute physical PV remainder without reading PPD-owned allocations."""
    pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
    load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
            + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
    pv_to_bat = max(0.0, float(row.get("planned_pv_to_bat_kwh") or 0.0))
    return max(0.0, pv - load - pv_to_bat)


def build_ppd_runner(a: PpdAdapters):
    """Create a read-plan/write-decisions PPD service.

    The service never updates SOC, target, price windows or core battery flows.
    Its slot writes are process decisions and the final allocation of flexible
    PV reconstructed from planner-owned physical inputs.
    """
    options, db = a.options, a.db

    def run_ppd(plan_run_id: str | None = None, run_type: str = "scheduled") -> dict:
        ppd_run_id = str(uuid.uuid4())
        cutoff = a.slot_start().replace(tzinfo=None)
        eta_d = max(0.01, min(1.0, float(options.get("battery_discharge_efficiency", 0.95))))
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
            flexible_rows = []
            for index, row in enumerate(rows):
                # Never read the previous PPD allocation that this run replaces.
                raw_flexible = planner_flexible_pv_remainder(row)
                sell_battery = float(row.get("planned_sell_kwh") or 0.0) > threshold
                flexible_rows.append({
                    "slot_start": row["slot_start"], "local_day": row.get("local_day"),
                    "soc_end_pct": row.get("soc_end_plan_pct"),
                    "soc_target_pct": (row.get("soc_charge_target_pct")
                                       if row.get("soc_charge_target_pct") is not None
                                       else row.get("soc_target_pct")),
                    "pv_flex_kwh": raw_flexible, "sell_battery": sell_battery,
                })
            window_rows = list(flexible_rows)
            if cutoff <= rows[0]["slot_start"] <= cutoff + timedelta(minutes=15):
                local_now = datetime.now(ZoneInfo(str(options.get("timezone", "Europe/Warsaw")))).replace(tzinfo=None)
                cur.execute("""SELECT captured_at,soc_pct,pv_power_w,load_power_w
                  FROM ems_gpt_telemetry_snapshots WHERE captured_at>=%s
                  ORDER BY captured_at DESC LIMIT 1""",
                  (local_now - timedelta(seconds=max(
                      30, int(options.get("telemetry_degraded_seconds", 120)))),))
                window_rows[0] = current_live_flexible_row(
                    flexible_rows[0], cur.fetchone() or {},
                    now=local_now,
                    stale_seconds=max(30, int(options.get("telemetry_degraded_seconds", 120))))
            flexible = build_flexible_ppd(
                window_rows, cwu_threshold_kwh=cwu_threshold,
                ev_threshold_kwh=ev_threshold,
                corridor_threshold_kwh=threshold)
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
                  ppd_run_type=%s,ppd_version=%s,ppd_locked_at=NOW(6)
                  WHERE slot_start=%s AND plan_run_id=%s""",
                  (round(cwu, 6), round(ev, 6), round(pv_export, 6), round(curtail, 6),
                   recommendation, reason, run_type, PPD_VERSION,
                   row["slot_start"], plan_run_id))
                decision_row = dict(row)
                decision_row["planned_pv_export_kwh"] = pv_export
                decisions = plan_bound_decisions(decision_row, threshold) + (
                    ("PV_CWU", flex.pv_cwu_allowed, "ALLOW" if flex.pv_cwu_allowed else "BLOCK", flex.reason),
                    ("PV_EV", flex.pv_ev_allowed, "ALLOW" if flex.pv_ev_allowed else "BLOCK", flex.reason),
                )
                for process, eligible, decision, process_reason in decisions:
                    cur.execute("""INSERT INTO ems_gpt_core_process_decisions
                      (slot_start,slot_id,process_name,decision,eligible,reason,plan_run_id,
                       ppd_run_id,valid_until,connector_required,published_at)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6))
                      ON DUPLICATE KEY UPDATE decision=VALUES(decision),eligible=VALUES(eligible),
                       reason=VALUES(reason),slot_id=COALESCE(slot_id,VALUES(slot_id)),
                       plan_run_id=VALUES(plan_run_id),ppd_run_id=VALUES(ppd_run_id),
                       valid_until=VALUES(valid_until),published_at=NOW(6)""",
                      (row["slot_start"], row.get("slot_id"), process, decision, eligible,
                       process_reason[:1000], plan_run_id, ppd_run_id,
                       row["slot_start"] + timedelta(minutes=16),
                       0 if process == "SELL_PV" else 1))
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
