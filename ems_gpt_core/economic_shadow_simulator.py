"""Deterministic, side-effect-free 15-minute shadow candidate simulator.

This module deliberately does not import the production planner, database,
Home Assistant or executor. It is a replay/validation engine, not a publisher.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence

from economic_shadow import ShadowSlot, sale_rebuy_unit_margin


@dataclass(frozen=True)
class ShadowAssumptions:
    capacity_kwh: float = 15.0
    max_battery_kw: float = 5.0
    slot_minutes: int = 15
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    degradation_pln_kwh: float = 0.08
    minimum_margin_pln_kwh: float = 0.05
    minimum_soc_pct: float = 15.0


@dataclass(frozen=True)
class ShadowResult:
    name: str
    feasible: bool
    reasons: tuple[str, ...]
    soc_path_pct: tuple[float, ...]
    sold_kwh: float
    bought_kwh: float
    net_pln: float


def simulate(
    name: str,
    slots: Sequence[ShadowSlot],
    initial_soc_pct: float,
    sell_kwh: Sequence[float],
    buy_kwh: Sequence[float],
    assumptions: ShadowAssumptions = ShadowAssumptions(),
    required_soc_pct: Sequence[float] | None = None,
    terminal_soc_pct: float | None = None,
) -> ShadowResult:
    """Replay a candidate with battery charge/discharge, PV and native load.

    sell_kwh is exported AC energy; buy_kwh is AC grid charging.
    PV surplus charges the battery; PV deficits consume battery first.
    No unplanned grid import for household loads is permitted.
    """
    if len(slots) != len(sell_kwh) or len(slots) != len(buy_kwh):
        raise ValueError("SLOT_LENGTH_MISMATCH")
    if required_soc_pct is not None and len(required_soc_pct) != len(slots):
        raise ValueError("TARGET_LENGTH_MISMATCH")
    a = assumptions
    if (a.capacity_kwh <= 0 or a.max_battery_kw <= 0 or a.slot_minutes <= 0
            or not 0 < a.charge_efficiency <= 1
            or not 0 < a.discharge_efficiency <= 1):
        raise ValueError("INVALID_ASSUMPTIONS")
    if not 0 <= initial_soc_pct <= 100:
        raise ValueError("INVALID_INITIAL_SOC")
    energy = a.capacity_kwh * initial_soc_pct / 100
    limit = a.max_battery_kw * a.slot_minutes / 60
    reasons = []
    soc_path = []
    revenue = 0.0
    sold = bought = 0.0
    for i, (slot, sell, buy) in enumerate(zip(slots, sell_kwh, buy_kwh)):
        sell = float(sell)
        buy = float(buy)
        if sell < 0 or buy < 0 or slot.load_kwh < 0 or slot.pv_kwh < 0:
            reasons.append(f"{i}:NEGATIVE_FLOW")
            soc_path.append(100 * energy / a.capacity_kwh)
            continue
        if sell > 0 and (not slot.sell_allowed or slot.sell_price <= 0):
            reasons.append(f"{i}:SELL_NOT_ALLOWED")
        if buy > 0 and not slot.buy_allowed:
            reasons.append(f"{i}:BUY_NOT_ALLOWED")
        if sell > 0 and buy > 0:
            reasons.append(f"{i}:BUY_SELL_CONFLICT")
        # Physical PV and native load are netted before battery dispatch.
        pv_surplus = max(0.0, slot.pv_kwh - slot.load_kwh)
        load_deficit = max(0.0, slot.load_kwh - slot.pv_kwh)
        discharge_ac = load_deficit + sell
        charge_ac = pv_surplus + buy
        if discharge_ac > limit + 1e-8:
            reasons.append(f"{i}:DISCHARGE_POWER")
        if charge_ac > limit + 1e-8:
            reasons.append(f"{i}:CHARGE_POWER")
        if charge_ac > 0 and discharge_ac > 0:
            reasons.append(f"{i}:SIMULTANEOUS_CHARGE_DISCHARGE")
        energy += charge_ac * a.charge_efficiency
        energy -= discharge_ac / a.discharge_efficiency
        if energy < a.capacity_kwh * a.minimum_soc_pct / 100 - 1e-8:
            reasons.append(f"{i}:SOC_RESERVE")
        if energy > a.capacity_kwh + 1e-8:
            reasons.append(f"{i}:SOC_OVERFLOW")
        soc = energy / a.capacity_kwh * 100
        if required_soc_pct is not None and soc + 1e-8 < required_soc_pct[i]:
            reasons.append(f"{i}:SOC_REQUIRED")
        soc_path.append(soc)
        revenue += sell * slot.sell_price - buy * slot.buy_price
        revenue -= sell * a.degradation_pln_kwh
        sold += sell
        bought += buy
    if terminal_soc_pct is not None and (not soc_path or soc_path[-1] + 1e-8 < terminal_soc_pct):
        reasons.append("TERMINAL_SOC")
    return ShadowResult(name, not reasons, tuple(reasons), tuple(soc_path),
                        sold, bought, revenue)


def propose_one_slot_arbitrage(
    slots: Sequence[ShadowSlot],
    assumptions: ShadowAssumptions = ShadowAssumptions(),
) -> tuple[list[float], list[float]]:
    """Price-only proposal. Always validate with simulate before using results.

    Choose one eligible SELL and one later BUY, respecting both slot power
    caps. Does not imply that the battery has sufficient available energy.
    """
    sell = [0.0] * len(slots)
    buy = [0.0] * len(slots)
    max_ac = assumptions.max_battery_kw * assumptions.slot_minutes / 60
    best = None
    for i, sale in enumerate(slots):
        if not sale.sell_allowed or sale.sell_price <= 0:
            continue
        for j in range(i + 1, len(slots)):
            purchase = slots[j]
            if not purchase.buy_allowed or purchase.sell_allowed:
                continue
            margin = sale_rebuy_unit_margin(
                sale.sell_price, purchase.buy_price,
                assumptions.charge_efficiency, assumptions.discharge_efficiency,
                assumptions.degradation_pln_kwh)
            if margin < assumptions.minimum_margin_pln_kwh:
                continue
            if best is None or margin > best[0]:
                best = (margin, i, j)
    if best:
        _, i, j = best
        # Restoring the energy consumed by export needs AC purchase / eta_c/eta_d.
        sell[i] = max_ac * assumptions.charge_efficiency * assumptions.discharge_efficiency
        buy[j] = max_ac
    return sell, buy
