"""Pure, read-only integration boundary for P/E shadow comparison.

The caller supplies an immutable snapshot assembled outside this module.
No Home Assistant, SQL, network, planner or executor dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Sequence

from economic_shadow import ShadowSlot
from economic_shadow_horizon import optimize_horizon
from economic_shadow_simulator import ShadowAssumptions, simulate


@dataclass(frozen=True)
class ShadowSnapshot:
    slots: tuple[ShadowSlot, ...]
    initial_soc_pct: float
    required_soc_pct: tuple[float, ...]
    terminal_soc_pct: float
    production_sell_kwh: tuple[float, ...]
    production_buy_kwh: tuple[float, ...]
    source_id: str


def compare_snapshot(
    snapshot: ShadowSnapshot,
    assumptions: ShadowAssumptions = ShadowAssumptions(),
) -> dict:
    """Evaluate P and E against exactly the same input snapshot.

    Return a JSON-ready diagnostic only; no persistence or device commands.
    """
    n = len(snapshot.slots)
    if not snapshot.source_id.strip():
        raise ValueError("MISSING_SOURCE_ID")
    if not (len(snapshot.required_soc_pct) == len(snapshot.production_sell_kwh)
            == len(snapshot.production_buy_kwh) == n):
        raise ValueError("SNAPSHOT_LENGTH_MISMATCH")
    if not n:
        raise ValueError("EMPTY_SNAPSHOT")
    canonical = {
        "source_id": snapshot.source_id,
        "slots": [vars(s) for s in snapshot.slots],
        "initial_soc_pct": snapshot.initial_soc_pct,
        "required_soc_pct": snapshot.required_soc_pct,
        "terminal_soc_pct": snapshot.terminal_soc_pct,
        "production_sell_kwh": snapshot.production_sell_kwh,
        "production_buy_kwh": snapshot.production_buy_kwh,
    }
    fingerprint = sha256(json.dumps(canonical, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()
    p = simulate("PRODUCTION_P", snapshot.slots, snapshot.initial_soc_pct,
                 snapshot.production_sell_kwh, snapshot.production_buy_kwh,
                 assumptions, snapshot.required_soc_pct, snapshot.terminal_soc_pct)
    e = optimize_horizon(snapshot.slots, snapshot.initial_soc_pct,
                         snapshot.required_soc_pct, snapshot.terminal_soc_pct,
                         assumptions)
    # Do not report financial improvement for a plan that consumes more
    # terminal inventory than the reference production plan.
    inventory_tolerance_pct = 0.05 / assumptions.capacity_kwh * 100
    comparable = (p.feasible and e.feasible and
                  e.soc_end_pct + inventory_tolerance_pct >= p.soc_path_pct[-1])
    return {
        "mode": "SHADOW_READ_ONLY",
        "source_id": snapshot.source_id,
        "snapshot_sha256": fingerprint,
        "slots": n,
        "production": {
            "feasible": p.feasible,
            "reasons": list(p.reasons),
            "net_pln": p.net_pln,
            "soc_end_pct": p.soc_path_pct[-1],
            "sold_kwh": p.sold_kwh,
            "bought_kwh": p.bought_kwh,
        },
        "economic": {
            "feasible": e.feasible,
            "reasons": list(e.reasons),
            "net_pln": e.net_pln if e.feasible else None,
            "soc_end_pct": e.soc_end_pct if e.feasible else None,
            "sold_kwh": sum(e.sell_kwh),
            "bought_kwh": sum(e.buy_kwh),
            "states_examined": e.states_examined,
            "sell_kwh_by_slot": list(e.sell_kwh),
            "buy_kwh_by_slot": list(e.buy_kwh),
        },
        "comparable": comparable,
        "delta_pln": e.net_pln - p.net_pln if comparable else None,
        "warning": None if comparable else "INFEASIBLE_OR_TERMINAL_INVENTORY_MISMATCH",
    }
