"""Full-horizon, read-only economic shadow dynamic program.

Discretized battery inventory and AC trade actions. No side effects.
Final actions are replayed with the exact continuous-energy simulator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from economic_shadow import ShadowSlot
from economic_shadow_simulator import ShadowAssumptions, simulate


@dataclass(frozen=True)
class HorizonPlan:
    feasible: bool
    reasons: tuple[str, ...]
    sell_kwh: tuple[float, ...]
    buy_kwh: tuple[float, ...]
    net_pln: float
    soc_end_pct: float
    states_examined: int


def optimize_horizon(
    slots: Sequence[ShadowSlot],
    initial_soc_pct: float,
    required_soc_pct: Sequence[float],
    terminal_soc_pct: float,
    assumptions: ShadowAssumptions = ShadowAssumptions(),
    energy_step_kwh: float = 0.25,
) -> HorizonPlan:
    """Search 0/BUY/SELL at every slot, preserving hard energy constraints.

    Energy is discretized conservatively: transition SOC floors downward,
    and the final selected path must pass exact replay. No production writes.
    """
    n = len(slots)
    if len(required_soc_pct) != n:
        raise ValueError("TARGET_LENGTH_MISMATCH")
    a = assumptions
    if not 0 < energy_step_kwh <= a.capacity_kwh:
        raise ValueError("INVALID_ENERGY_STEP")
    if not 0 <= initial_soc_pct <= 100 or not 0 <= terminal_soc_pct <= 100:
        raise ValueError("INVALID_SOC")
    if any(not 0 <= t <= 100 for t in required_soc_pct):
        raise ValueError("INVALID_REQUIRED_SOC")
    cap = a.max_battery_kw * a.slot_minutes / 60
    steps = int(cap / energy_step_kwh)
    actions = [energy_step_kwh * k for k in range(1, steps + 1)]
    initial_energy = a.capacity_kwh * initial_soc_pct / 100
    # State -> (objective PLN, exact energy, sell path, buy path).
    # Quantization deliberately keeps only the highest-profit path per bin.
    states = {round(initial_energy / energy_step_kwh): (0.0, initial_energy, (), ())}
    examined = 0
    for i, slot in enumerate(slots):
        if slot.load_kwh < 0 or slot.pv_kwh < 0:
            raise ValueError("NEGATIVE_FORECAST")
        surplus = max(0.0, slot.pv_kwh - slot.load_kwh)
        deficit = max(0.0, slot.load_kwh - slot.pv_kwh)
        options = [(0.0, 0.0)]
        if slot.sell_allowed and slot.sell_price > 0 and surplus <= 1e-9:
            options.extend((v, 0.0) for v in actions if v + deficit <= cap + 1e-9)
        if slot.buy_allowed and not slot.sell_allowed and deficit <= 1e-9:
            options.extend((0.0, v) for v in actions if v + surplus <= cap + 1e-9)
        next_states = {}
        for score, energy, sold_path, bought_path in states.values():
            for sell, buy in options:
                examined += 1
                discharge = deficit + sell
                charge = surplus + buy
                if discharge > cap + 1e-9 or charge > cap + 1e-9:
                    continue
                if discharge > 1e-9 and charge > 1e-9:
                    continue
                new_energy = energy + charge * a.charge_efficiency - discharge / a.discharge_efficiency
                minimum = a.capacity_kwh * max(a.minimum_soc_pct, required_soc_pct[i]) / 100
                if new_energy < minimum - 1e-9 or new_energy > a.capacity_kwh + 1e-9:
                    continue
                new_score = score + sell * (slot.sell_price - a.degradation_pln_kwh) - buy * slot.buy_price
                # Use finer binning than action grid to reduce pruning error.
                key = round(new_energy / (energy_step_kwh / 4))
                previous = next_states.get(key)
                if previous is None or new_score > previous[0] + 1e-9:
                    next_states[key] = (new_score, new_energy,
                                        sold_path + (sell,), bought_path + (buy,))
        states = next_states
        if not states:
            return HorizonPlan(False, (f"{i}:NO_FEASIBLE_STATE",), (), (), 0.0, 0.0, examined)
    min_end = a.capacity_kwh * terminal_soc_pct / 100
    ranked = sorted((v for v in states.values() if v[1] >= min_end - 1e-9),
                    key=lambda x: x[0], reverse=True)
    for score, energy, sold, bought in ranked:
        check = simulate("E_HORIZON", slots, initial_soc_pct, sold, bought,
                         assumptions=a, required_soc_pct=required_soc_pct,
                         terminal_soc_pct=terminal_soc_pct)
        if check.feasible:
            return HorizonPlan(True, (), sold, bought, check.net_pln,
                               check.soc_path_pct[-1] if n else initial_soc_pct,
                               examined)
    return HorizonPlan(False, ("NO_EXACT_REPLAY_VALID_CANDIDATE",), (), (),
                       0.0, 0.0, examined)
