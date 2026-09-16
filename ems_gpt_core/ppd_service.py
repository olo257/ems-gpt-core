from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


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
