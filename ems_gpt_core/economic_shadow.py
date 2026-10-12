"""Read-only shadow planner contract and comparison primitives.

No Home Assistant, database, executor, or production planner imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ShadowSlot:
    start: str
    buy_price: float
    sell_price: float
    buy_allowed: bool
    sell_allowed: bool
    load_kwh: float
    pv_kwh: float


@dataclass(frozen=True)
class ShadowCandidate:
    name: str
    sold_kwh: float
    purchased_kwh: float
    net_pln: float
    feasible: bool
    reasons: tuple[str, ...]


def compare_candidates(candidates: Sequence[ShadowCandidate]) -> dict:
    """Compare already-validated shadow outcomes, never publish them."""
    if not candidates:
        raise ValueError("EMPTY_CANDIDATES")
    eligible = [item for item in candidates if item.feasible]
    best = max(eligible, key=lambda item: item.net_pln) if eligible else None
    return {
        "recommended_shadow_only": best.name if best else None,
        "candidates": [
            {"name": c.name, "sold_kwh": c.sold_kwh,
             "purchased_kwh": c.purchased_kwh, "net_pln": c.net_pln,
             "feasible": c.feasible, "reasons": list(c.reasons)}
            for c in candidates
        ],
    }


def sale_rebuy_unit_margin(sell_price: float, buy_price: float,
                           charge_efficiency: float, discharge_efficiency: float,
                           degradation_pln_kwh: float) -> float:
    """PLN margin per exported kWh, before any required minimum margin."""
    if not 0 < charge_efficiency <= 1 or not 0 < discharge_efficiency <= 1:
        raise ValueError("INVALID_EFFICIENCY")
    return (sell_price - buy_price / (charge_efficiency * discharge_efficiency)
            - degradation_pln_kwh)


def allowed_rebuy_slots(slots: Sequence[ShadowSlot], after_index: int) -> list[int]:
    """Never price replacement using a slot without BUY permission."""
    return [i for i in range(after_index + 1, len(slots))
            if slots[i].buy_allowed and not slots[i].sell_allowed]
