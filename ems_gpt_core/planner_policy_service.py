"""Solve original BUY first; admit NEUTRAL only after real infeasibility.

The economic target is an output of dispatch, never a price-blind input cap.
Every solve uses the same load/PV model and unchanged hard close requirements.
"""
from __future__ import annotations

import copy
import math

from planner_dispatch_service import optimize_energy_horizon
from planner_economics_service import battery_sale_economics, assess_sale_plan_against_no_sale
from planner_validation_service import strict_database_bool


class EnergyInfeasible(RuntimeError):
    pass


def solve_buy_policy(rows, *, initial_soc_pct, capacity_kwh, reserve_pct,
                     eta_c, eta_d, degradation, min_margin, max_power_kw,
                     slot_minutes, terminal_soc_pct, required_soc_pcts,
                     sale_floor_pcts, max_grid_import_kw=12.0, soc_step_pct=0.1,
                     max_soc_pct=100.0, trace=None, checkpoint=lambda stage: None,
                     optimizer=optimize_energy_horizon, budget_check=lambda stage: None):
    original = copy.deepcopy(rows)
    required = tuple(float(v) for v in required_soc_pcts)
    if len(required) != len(rows):
        raise ValueError('POLICY_REQUIRED_LENGTH_MISMATCH')

    def attempt(name, candidates, sales=False, fixed_targets=None):
        checkpoint(name)
        budget_check(name)
        # An already-low measured SOC is not fictitiously raised to reserve.
        # Recover at the earliest physical charge opportunity; only that
        # unavoidable initial recovery corridor is below the configured floor.
        effective_required = list(required)
        recovery = math.floor(float(initial_soc_pct)/soc_step_pct+1e-9)*soc_step_pct
        if recovery < reserve_pct:
            for i,row in enumerate(candidates):
                if recovery >= reserve_pct:
                    break
                load = float(row.get('forecast_load_kwh') or 0)+float(row.get('forecast_heat_pump_load_kwh') or 0)
                surplus = max(0.0,float(row.get('forecast_pv_total_kwh') or 0)-load)
                grid = max(0.0,max_grid_import_kw*slot_minutes/60-max(0,load-float(row.get('forecast_pv_total_kwh') or 0))) if row['buy_window'] else 0
                gain = min(max_power_kw*slot_minutes/60,(surplus+grid))*eta_c
                recovery += math.floor(gain/(capacity_kwh*soc_step_pct/100)+1e-9)*soc_step_pct
                effective_required[i] = max(min(reserve_pct,recovery),
                    required[i] if required[i] > reserve_pct+1e-9 else 0)
        params = dict(initial_soc_pct=initial_soc_pct, capacity_kwh=capacity_kwh,
            reserve_pct=reserve_pct, eta_c=eta_c, eta_d=eta_d,
            degradation=degradation, min_margin=min_margin, max_power_kw=max_power_kw,
            slot_minutes=slot_minutes, sale_floor_pct=sale_floor_pcts,
            terminal_soc_pct=terminal_soc_pct, soc_step_pct=soc_step_pct,
            max_soc_pct=100.0, required_soc_pcts=effective_required,
            grid_charge_caps=[max_soc_pct]*len(candidates),
            minimum_soc_targets=fixed_targets,
            # Targets encode executable ceilings, not mandatory buys.
            target_due_indices=set(), hard_target_indices=set(),
            battery_sales_enabled=sales, allow_terminal_shortfall=False,
            max_grid_import_kw=max_grid_import_kw,
            economic_target_selection=fixed_targets is None,
            forbid_reserve_grid_load=True, preserve_buy_alternatives=True,
            minimum_grid_charge_kwh=0.02, abort_check=lambda:budget_check(name))
        try:
            result = (trace.optimizer(optimizer,name,candidates,**params) if trace
                      else optimizer(candidates,**params))
        except RuntimeError as exc:
            # Only physical infeasibility can open NEUTRAL; programming/input
            # errors must not turn every slot into BUY.
            if str(exc).startswith(('No feasible SOC state at horizon slot ',
                    'No feasible terminal SOC state for complete horizon')):
                raise EnergyInfeasible(str(exc)) from exc
            raise
        result['effective_required_soc_pcts'] = effective_required
        return result

    try:
        baseline = attempt('PRIMARY_BUY',original)
        candidates = original
        expanded = False
    except EnergyInfeasible as primary_error:
        if trace:
            trace.add('PRIMARY_DEFICIT','REJECTED',reason=str(primary_error),
                      required_soc_pcts=required, terminal_soc_pct=terminal_soc_pct)
        candidates = copy.deepcopy(original)
        eligible = []
        for i,row in enumerate(candidates):
            if (not strict_database_bool(row.get('buy_window',False),'buy_window')
                    and not strict_database_bool(row.get('sale_window',False),'sale_window')
                    and row.get('market_window') == 'NEUTRAL'):
                row.update(buy_window=True,market_window='BUY')
                eligible.append(i)
        if not eligible:
            raise EnergyInfeasible('PRIMARY_BUY_INFEASIBLE_NO_NEUTRAL') from primary_error
        # Candidates are permissions inside the optimizer, never durable BUY
        # flags. Its full-horizon objective selects the cheapest usable energy
        # before each deficit, including power, storage and site limits.
        baseline = attempt('NEUTRAL_CANDIDATES',candidates)
        selected = {i for i in eligible if baseline['flows'][i]['grid_charge_kwh'] > 1e-9}
        candidates = [dict(row, **({'buy_window':True,'market_window':'BUY'} if i in selected else {}))
                      for i,row in enumerate(original)]
        baseline = attempt('NEUTRAL_SELECTED',candidates)
        expanded = True
        if trace:
            trace.add('NEUTRAL_SELECTION','OK',eligible_indices=eligible,
                      selected_indices=selected, selected_slots=[candidates[i]['slot_start'] for i in sorted(selected)],
                      required_soc_pcts=required,terminal_soc_pct=terminal_soc_pct)

    # Fix economic ceilings from the complete, feasible no-sale solution and
    # replay under the executor's full-volume rule. This also proves that the
    # chosen economic targets are physically executable.
    targets = [max(reserve_pct, float(f['soc_end_pct'])) for f in baseline['flows']]
    baseline_replay = attempt('PRIMARY_CONTRACT' if not expanded else 'NEUTRAL_CONTRACT',
                              candidates,fixed_targets=targets)
    if baseline_replay['objective_pln'] > baseline['objective_pln']+0.001:
        raise RuntimeError('ECONOMIC_TARGET_REPLAY_DEGRADED')
    baseline = baseline_replay
    result = baseline
    # Optional sales cannot create new neutral permissions, relax minima or
    # increase the whole-horizon cost. The same physical model is reused.
    sale_rows = copy.deepcopy(candidates)
    for i,row in enumerate(sale_rows):
        if not battery_sale_economics(sale_rows,i,eta_c,eta_d,degradation,min_margin)['eligible']:
            row['sale_window'] = False
    if any(row['sale_window'] for row in sale_rows):
        try:
            sale = attempt('OPTIONAL_SALE',sale_rows,sales=True)
            assessment = assess_sale_plan_against_no_sale(sale,baseline,candidates,
                min_margin,capacity_kwh,eta_c,eta_d=eta_d,degradation=degradation)
            if assessment['eligible'] and sale['objective_pln'] <= baseline['objective_pln']+1e-9:
                sale_targets = [max(reserve_pct,float(f['soc_end_pct'])) for f in sale['flows']]
                replay = attempt('SALE_CONTRACT',sale_rows,sales=True,fixed_targets=sale_targets)
                final_assessment = assess_sale_plan_against_no_sale(replay,baseline,candidates,
                    min_margin,capacity_kwh,eta_c,eta_d=eta_d,degradation=degradation)
                if (final_assessment['eligible']
                        and replay['objective_pln'] <= baseline['objective_pln']+1e-9):
                    result = replay
        except RuntimeError as exc:
            if trace:
                trace.add('OPTIONAL_SALE','SKIPPED',reason=str(exc),action='KEEP_NO_SALE_PLAN')
    targets = [max(reserve_pct,float(f['soc_end_pct'])) for f in result['flows']]
    # Never broaden the durable window set merely because a candidate was
    # considered. Keep original BUY plus only neutral slots with real import.
    promoted = {i for i,row in enumerate(original) if not row['buy_window']
                and result['flows'][i]['grid_charge_kwh'] > 1e-9}
    final_rows = [dict(row, **({'buy_window':True,'market_window':'BUY'} if i in promoted else {}))
                  for i,row in enumerate(original)]
    return dict(rows=final_rows, optimization=result, targets=targets,
                required=result['effective_required_soc_pcts'],promoted_indices=promoted,
                expanded=expanded, original_buy_indices=[i for i,r in enumerate(original) if r['buy_window']])
