"""Shadow-only candidate search and identical-input comparison.

No database, Home Assistant or executor dependencies. Never publishes plans.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Sequence

from economic_shadow import ShadowSlot
from economic_shadow_simulator import (
    ShadowAssumptions, ShadowResult, simulate,
)


def evaluate_shadow(
    slots: Sequence[ShadowSlot],
    initial_soc_pct: float,
    required_soc_pct: Sequence[float],
    terminal_soc_pct: float,
    assumptions: ShadowAssumptions = ShadowAssumptions(),
    production_sell_kwh: Sequence[float] | None = None,
    production_buy_kwh: Sequence[float] | None = None,
) -> dict:
    """Compare production baseline, no-trade, sale-only, and sale/rebuy.

    All variants are replayed with identical SOC/forecast/price assumptions.
    The production flows must be provided explicitly; otherwise P is unknown,
    not silently treated as a no-trade baseline.
    """
    n = len(slots)
    if len(required_soc_pct) != n:
        raise ValueError("TARGET_LENGTH_MISMATCH")
    zero = [0.0] * n

    def replay(name: str, sell: Sequence[float], buy: Sequence[float]) -> ShadowResult:
        return simulate(name, slots, initial_soc_pct, sell, buy,
                        assumptions=assumptions, required_soc_pct=required_soc_pct,
                        terminal_soc_pct=terminal_soc_pct)

    candidates = [replay("NO_TRADE", zero, zero)]
    if (production_sell_kwh is None) != (production_buy_kwh is None):
        raise ValueError("PRODUCTION_FLOWS_INCOMPLETE")
    if production_sell_kwh is not None:
        candidates.append(replay("PRODUCTION_P", production_sell_kwh, production_buy_kwh))

    # Candidate generation is bounded to price-positive SELL slots. Every
    # proposal is validated by the full replay, including load and PV.
    cap = assumptions.max_battery_kw * assumptions.slot_minutes / 60
    for i, slot in enumerate(slots):
        if not slot.sell_allowed or slot.sell_price <= 0:
            continue
        for fraction in (0.25, 0.5, 0.75, 1.0):
            sell = zero.copy()
            sell[i] = cap * fraction
            candidates.append(replay(f"SALE_ONLY_{i}_{fraction}", sell, zero))
            for j in range(i + 1, n):
                later = slots[j]
                if not later.buy_allowed or later.sell_allowed:
                    continue
                buy = zero.copy()
                buy[j] = min(cap, sell[i] /
                             (assumptions.charge_efficiency * assumptions.discharge_efficiency))
                candidates.append(replay(f"SALE_REBUY_{i}_{j}_{fraction}", sell, buy))

    # A financially meaningful comparison must preserve the same terminal\n    # battery inventory. Otherwise selling stored energy appears as free profit.\n    reference = next((c for c in candidates if c.name == 'PRODUCTION_P'), None)\n    if reference is None:\n        reference = candidates[0]\n    reference_soc = reference.soc_path_pct[-1] if reference.soc_path_pct else initial_soc_pct\n    terminal_tolerance_pct = 0.05 / assumptions.capacity_kwh * 100\n    comparable = [c for c in candidates if c.feasible and\n                  c.soc_path_pct and c.soc_path_pct[-1] >= reference_soc - terminal_tolerance_pct]\n    feasible = comparable
    best = max(feasible, key=lambda c: c.net_pln) if feasible else None
    production = next((c for c in candidates if c.name == "PRODUCTION_P"), None)
    def pack(c: ShadowResult) -> dict:
        return {"name": c.name, "feasible": c.feasible,
                "reasons": list(c.reasons), "sold_kwh": c.sold_kwh,
                "bought_kwh": c.bought_kwh, "net_pln": c.net_pln,
                "soc_end_pct": c.soc_path_pct[-1] if c.soc_path_pct else initial_soc_pct}
    return {
        "mode": "SHADOW_READ_ONLY",
        "candidate_count": len(candidates),
        "feasible_count": len(feasible),\n        "comparison_terminal_soc_pct": reference_soc,\n        "comparison_terminal_tolerance_pct": terminal_tolerance_pct,
        "best": pack(best) if best else None,
        "production": pack(production) if production else None,
        "delta_vs_production_pln": (
            best.net_pln - production.net_pln if best and production and production.feasible
            else None
        ),
        "rejected": [pack(c) for c in candidates if c not in feasible][:100],
        "assumptions": asdict(assumptions),
        "limitations": [
            "single SELL slot and optional single later BUY slot per candidate",
            "no full-horizon dynamic-programming optimization",
            "no export of surplus PV in revenue accounting",\n            "same-terminal-inventory comparison uses minimum baseline SOC; excess inventory not monetized",
            "requires complete load/PV input and physical replay before interpretation",
        ],
    }
