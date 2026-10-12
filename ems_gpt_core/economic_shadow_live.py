"""Read-only production-slot bridge for economic shadow diagnostics.

Never executes optimization or changes the production planner. A snapshot is
reported as NOT_COMPARABLE until all relevant energy flows can be modeled.
"""
from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Mapping, Sequence

REQUIRED = (
    "slot_start", "price_buy_pln_kwh", "price_sell_pln_kwh",
    "forecast_pv_total_kwh", "forecast_load_kwh",
    "planned_buy_kwh", "planned_sell_kwh", "planned_pv_export_kwh",
    "planned_pv_to_bat_kwh", "planned_pv_to_cwu_kwh",
    "planned_pv_to_ev_kwh", "planned_pv_curtail_kwh",
    "soc_start_plan_pct", "soc_end_plan_pct", "soc_required_pct",
    "market_window",
)
ENERGY = (
    "forecast_pv_total_kwh", "forecast_load_kwh", "planned_buy_kwh",
    "planned_sell_kwh", "planned_pv_export_kwh", "planned_pv_to_bat_kwh",
    "planned_pv_to_cwu_kwh", "planned_pv_to_ev_kwh",
    "planned_pv_curtail_kwh",
)

def inspect_production_slots(records: Sequence[Mapping]) -> dict:
    """Inspect actual P slots without inventing missing inputs or claiming savings."""
    if not records:
        return {"mode": "SHADOW_READ_ONLY", "status": "NO_DATA",
                "comparable": False, "slots": 0, "reasons": ["NO_FUTURE_SLOTS"]}
    reasons = []
    prev = None
    totals = {key: 0.0 for key in ENERGY}
    for i, record in enumerate(records):
        missing = [key for key in REQUIRED if record.get(key) is None]
        if missing:
            reasons.append(f"{i}:MISSING:{','.join(missing)}")
            continue
        start = str(record["slot_start"])
        if prev is not None and start <= prev:
            reasons.append(f"{i}:NON_MONOTONIC_SLOTS")
        prev = start
        try:
            values = {key: float(record[key]) for key in ENERGY}
            values.update({key: float(record[key]) for key in
                           ("price_buy_pln_kwh", "price_sell_pln_kwh",
                            "soc_start_plan_pct", "soc_end_plan_pct",
                            "soc_required_pct")})
        except (ValueError, TypeError, OverflowError):
            reasons.append(f"{i}:INVALID_NUMBER")
            continue
        if not all(isfinite(v) for v in values.values()):
            reasons.append(f"{i}:NON_FINITE")
            continue
        if any(values[k] < 0 for k in ENERGY):
            reasons.append(f"{i}:NEGATIVE_ENERGY")
            continue
        if any(not 0 <= values[k] <= 100 for k in
               ("soc_start_plan_pct", "soc_end_plan_pct", "soc_required_pct")):
            reasons.append(f"{i}:INVALID_SOC")
        for key in ENERGY:
            totals[key] += values[key]
    # Existing E simulator nets PV against load and does not yet account for
    # curtailment, PV-only export, EV, CWU, HP scheduling and load grid import.
    # Comparing it to the production ledger now would yield false economics.
    return {
        "mode": "SHADOW_READ_ONLY",
        "status": "INVALID_SOURCE" if reasons else "DATA_READY_MODEL_INCOMPLETE",
        "comparable": False,
        "slots": len(records),
        "first_slot": str(records[0].get("slot_start")),
        "last_slot": str(records[-1].get("slot_start")),
        "production_totals_kwh": {k: round(v, 3) for k, v in totals.items()},
        "reasons": reasons[:20] if reasons else [
            "E_MODEL_MISSING_PV_EXPORT_CURTAILMENT_AND_FLEXIBLE_LOADS",
            "P_E_ECONOMIC_DELTA_NOT_YET_COMPARABLE",
        ],
        "delta_pln": None,
    }
