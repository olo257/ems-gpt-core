# Economic Planner E — shadow architecture (draft)

Status: isolated development branch; no production integration or execution rights.

## Invariants
- Planner P remains the sole publisher to ems_gpt_slots and the executor/PPD.
- Planner E reads a frozen input snapshot with source run_id, timestamp, current measured SOC, RCE, PV/load/HP forecasts and battery parameters.
- Planner E writes only its own namespaced storage; no writes to production slots, HA entities, PPD or inverter.
- No automatic promotion, fallback switch, or modification of Planner P.
- An independent physical validator must reject SOC/balance/window/power violations.
- Comparative results must use identical inputs, terminal SOC and cost assumptions.

## Development sequence
1. Read-only shadow calculation with immutable inputs.
2. Three candidate scenarios: baseline P, sale without rebuy, sale with permitted later BUY.
3. Counterfactual full-horizon comparison and explicit rejection reasons.
4. Shadow persistence and WWW comparison view.
5. Replay of 2026-10-13, regression tests and 7–14 days shadow validation.
6. Separate approval before any activation or changes to Planner P.

## Economics
- Grid BUY only inside permitted BUY windows; SELL only inside permitted SELL windows.
- Account for 5 kW battery power cap, charge/discharge efficiencies, degradation and margin.
- Maintain safe SOC until next PV/BUY opportunity and at the terminal horizon.
- Compare terminal battery inventory on a consistent valuation basis.
- Reject any candidate increasing unplanned household grid import.

This document specifies isolation and test criteria; it does not change production planner behavior.
