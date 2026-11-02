from __future__ import annotations

import json
import math
import time as monotonic_clock
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Callable

from planner_diagnostics_service import PlannerTrace, persist_trace
from planner_policy_service import solve_buy_policy
from ingestion_service import original_rce_windows

from config_service import deye_program_soc_baselines
from planner_hp_service import (
    elapsed_hp_plan_states,
    historical_hp_power_kw,
    hp_heating_window_indices,
    hp_temperature_eligible,
    optimize_hp_heating_slots,
    prepare_heat_pump_plan,
    persist_independent_hp_schedule,
)
from planner_validation_service import (
    strict_database_bool,
    validate_pv_forecasts,
    validate_rce_market_window,
)
from planner_dispatch_service import (
    build_soc_contracts,
    daily_close_soc_requirements,
    deterministic_soc_target_contract,
    economic_buy_charge_caps,
    economic_sell_indices,
    end_of_day_soc_target,
    optimize_energy_horizon,
    planned_buy_execution_targets,
    preserve_target_contract_after_reachability,
    preserve_active_buy_window_target,
    morning_energy_coverage,
    replenishment_soc_requirements,
    voluntary_load_without_charging_indices,
    validated_buy_deadline_targets,
    validate_physical_buy_commitments,
    active_soc_target_deadlines,
    validate_active_soc_target_deadlines,
)
from planner_economics_service import (
    assess_sale_plan_against_no_sale,
    battery_sale_economics,
    planning_tou_programs,
    tou_sale_safety_floors,
)
from planner_soc_service import (
    historical_terminal_soc,
    terminal_soc_recovery,
    current_soc_for_replan,
    project_soc_to_horizon_start,
    relax_soc_requirement_to_reachable,
    effective_required_soc_for_dispatch,
    _soc_reachability_profile,
    _unavoidable_grid_load_allowed,
    recoverable_soc_requirements,
    reachable_soc_ceiling_pcts,
    validate_recoverable_soc_requirements,
)

PLANNER_VERSION = "CORE_0_40_81"

@dataclass(frozen=True)
class PlannerAdapters:
    options: dict
    db: Callable
    local_now: Callable
    slot_start: Callable
    setting: Callable
    audit_stage: Callable
    active_tou_program: Callable
    tou_program_snapshot: Callable
    record_event: Callable
    prepare_buy_windows: Callable | None = None

def build_planner(a: PlannerAdapters):
    OPTIONS = a.options
    db = a.db
    local_now = a.local_now
    slot_start = a.slot_start
    setting = a.setting
    audit_stage = a.audit_stage
    active_tou_program = a.active_tou_program
    tou_program_snapshot = a.tou_program_snapshot
    record_event = a.record_event
    solve_energy_horizon = optimize_energy_horizon

    def _run_planner(run_type: str, attempt: dict) -> dict:
        """Run the V3 staged planner transactionally in the app-owned database."""
        started_monotonic = monotonic_clock.monotonic()
        planning_budget = max(10.0, min(600.0, float(
            OPTIONS.get("planner_deadline_seconds", 120.0))))

        def ensure_deadline(stage: str) -> None:
            attempt["failed_stage"] = stage
            attempt["trace"].add(stage, "RUNNING")
            if monotonic_clock.monotonic() - started_monotonic > planning_budget:
                raise RuntimeError(f"PLANNER_DEADLINE_EXCEEDED:{stage}:{planning_budget:.0f}s")

        def policy_budget_check(stage):
            elapsed = monotonic_clock.monotonic()-started_monotonic
            if stage in ('OPTIONAL_SALE','SALE_CONTRACT') and elapsed > planning_budget-10:
                raise RuntimeError('OPTIONAL_SALE_BUDGET_EXHAUSTED_KEEP_BASELINE')
            if elapsed > planning_budget:
                raise RuntimeError(f'PLANNER_DEADLINE_EXCEEDED:{stage}')

        trace = attempt['trace']

        def audit_stage(cur, run_id, stage, status, rows, details=""):
            trace.add(stage, status, rows=rows, details=details)
            a.audit_stage(cur, run_id, stage, status, rows, details)

        now = local_now().replace(tzinfo=None)
        cutoff = slot_start().replace(tzinfo=None) + timedelta(minutes=int(OPTIONS["slot_minutes"]))
        capacity = max(1.0, float(OPTIONS.get("battery_capacity_kwh", 15.0)))
        reserve = max(0.0, min(90.0, float(OPTIONS.get("battery_min_soc_pct", 15.0))))
        # Fresh measured SOC is aligned to cutoff below; the live slot is not
        # part of the optimizer's future horizon and must not disappear.
        soc_now = current_soc_for_replan(setting)
        eta_c = max(0.01, min(1.0, float(OPTIONS.get("battery_charge_efficiency", 0.90))))
        eta_d = max(0.01, min(1.0, float(OPTIONS.get("battery_discharge_efficiency", 0.95))))
        degradation = max(0.0, float(OPTIONS.get("battery_degradation_cost_pln_kwh", 0.08)))
        min_margin = max(0.0, float(OPTIONS.get("minimum_arbitrage_margin_pln_kwh", 0.05)))
        uncertainty_weight = max(0.0, min(2.0, float(OPTIONS.get("forecast_uncertainty_weight", 1.0))))
        fixed_forecast_correction = uncertainty_weight if bool(
            OPTIONS.get("forecast_fixed_corrections_enabled", False)) else 0.0
        floor_cap = max(reserve, min(100.0, float(OPTIONS.get("soc_floor_max_pct", 90.0))))
        target_cap = max(floor_cap, min(100.0, float(OPTIONS.get("soc_target_max_pct", 100.0))))
        flow_threshold = max(0.0, float(OPTIONS.get("planned_flow_threshold_kwh", 0.02)))
        technical_threshold = max(
            flow_threshold, float(OPTIONS.get("technical_flow_threshold_kwh", 0.05)))
        max_kw = max(0.25, float(OPTIONS.get("battery_max_power_kw", 5.0)))
        max_grid_import_kw = max(0.0, float(OPTIONS.get("grid_import_limit_kw", 12.0)))
        run_id = str(uuid.uuid4())
        attempt["planner_run_id"] = run_id
        attempt["initial_soc_pct"] = float(soc_now)
        attempt["failed_stage"] = "INPUTS"
        hp_shortfalls = []
        tou_programs = planning_tou_programs(
            tou_program_snapshot(), deye_program_soc_baselines(OPTIONS))
        with db() as conn, conn.cursor() as cur:
            active_start = cutoff - timedelta(minutes=int(OPTIONS["slot_minutes"]))
            cur.execute("""SELECT * FROM ems_gpt_slots
              WHERE slot_start=%s AND plan_stage='PUBLISHED' LIMIT 1""", (active_start,))
            active_plan_row = cur.fetchone()
            cur.execute("""SELECT captured_at,battery_charge_power_w,
              battery_discharge_power_w,pv_power_w,load_power_w
              FROM ems_gpt_telemetry_snapshots
              WHERE slot_start=%s AND captured_at<=%s
              ORDER BY captured_at DESC LIMIT 1""", (active_start, now))
            projection = project_soc_to_horizon_start(
                soc_now, now, cutoff, active_plan_row, cur.fetchone(),
                capacity, max_kw, eta_c, eta_d, int(OPTIONS["slot_minutes"]),
                float(OPTIONS.get("telemetry_degraded_seconds", 120)))
            soc_now = projection["horizon_initial_soc_pct"]
            attempt["initial_soc_pct"] = float(soc_now)
            record_event("replan_soc_time_alignment", "planner", {
                "horizon_start": str(cutoff), **projection})
            cur.execute("""SELECT slot_start,price_buy_pln_kwh,price_sell_pln_kwh,
              forecast_load_kwh,forecast_pv_total_kwh FROM ems_gpt_slots
              WHERE slot_start>=%s AND actual_recorded_at IS NULL ORDER BY slot_start""", (cutoff,))
            coverage_rows = list(cur.fetchall())
            coverage_issues = []
            expected_slot = cutoff
            for coverage_row in coverage_rows:
                if coverage_row['slot_start'] != expected_slot:
                    coverage_issues.append({'expected_slot':expected_slot, 'actual_slot':coverage_row['slot_start']})
                missing = [key for key in ('price_buy_pln_kwh','price_sell_pln_kwh',
                    'forecast_load_kwh','forecast_pv_total_kwh') if coverage_row.get(key) is None]
                if missing:
                    coverage_issues.append({'slot_start':coverage_row['slot_start'], 'missing':missing})
                expected_slot = coverage_row['slot_start'] + timedelta(minutes=int(OPTIONS['slot_minutes']))
            if not coverage_rows:
                coverage_issues.append({'missing':'ALL_HORIZON_INPUTS'})
            trace.add('INPUT_COVERAGE', 'ERROR' if coverage_issues else 'OK',
                      rows=coverage_rows, issues=coverage_issues)
            cur.execute("""SELECT * FROM ems_gpt_slots
              WHERE slot_start>=%s AND actual_recorded_at IS NULL
                AND price_source='PSE_API' AND price_buy_pln_kwh IS NOT NULL AND price_sell_pln_kwh IS NOT NULL
              ORDER BY slot_start""", (cutoff,))
            source = list(cur.fetchall())
            attempt['candidate_rows'] = [dict(row) for row in source]
            trace.add('INPUTS', 'OK' if source else 'MISSING', rows=source,
                      parameters={key: OPTIONS.get(key) for key in (
                          'battery_capacity_kwh', 'battery_min_soc_pct',
                          'battery_charge_efficiency', 'battery_discharge_efficiency',
                          'battery_max_power_kw', 'grid_import_limit_kw')},
                      initial_soc_pct=soc_now)
            if not source:
                raise RuntimeError("No open forecast rows for planner horizon")
            validate_pv_forecasts(source)
            missing_load = [str(row["slot_start"]) for row in source
                            if row.get("forecast_load_kwh") is None]
            if missing_load:
                raise RuntimeError(
                    f"MISSING_LOAD_FORECAST:{len(missing_load)}:{missing_load[0]}")
            continuity = all(
                source[i]["slot_start"] - source[i - 1]["slot_start"] == timedelta(minutes=15)
                for i in range(1, len(source))
            )
            complete_prices = len(source)
            per_day = {}
            for row in source:
                day = row.get("local_day") or row["slot_start"].date()
                per_day[str(day)] = per_day.get(str(day), 0) + 1
            if not continuity or not source:
                details = {"rows": len(source), "continuous": continuity,
                           "pse_prices": complete_prices, "per_day": per_day, "expected": "all contiguous available RCE slots"}
                record_event("planner_waiting_for_inputs", "planner", details, "WARNING")
                return {"status": "WAITING", **details}
            # RCE import is the sole writer of durable BUY/SELL windows.
            # Planner validates and consumes those flags; it never recalculates
            # or updates the source permissions.
            normalized_source = [
                validate_rce_market_window(row) for row in source
            ]
            source = original_rce_windows(cur, normalized_source, OPTIONS)
            trace.add('ORIGINAL_WINDOWS','OK',rows=source)
            cur.execute("""INSERT INTO ems_gpt_plan_runs
              (run_id,plan_day,run_type,stage_version,expected_slots,status,current_stage,created_at,updated_at)
              VALUES(%s,%s,%s,%s,%s,'RUNNING','RCE_RAW',NOW(6),NOW(6))""",
              (run_id, cutoff.date(), run_type, PLANNER_VERSION, len(source)))
            stage_columns = [
                "slot_start","slot_end","slot_id","slot_start_utc","slot_start_local","utc_offset_minutes",
                "local_fold","local_day","slot_index_local","price_sell_pln_kwh","price_buy_pln_kwh","price_source",
                "price_fetched_at","sale_window","buy_window","market_window","forecast_pv1_kwh","forecast_pv2_kwh",
                "forecast_pv_total_kwh","forecast_load_kwh","forecast_temperature_c",
                "forecast_cloud_coverage_pct","forecast_precipitation_mm","pv_correction",
                "load_correction","forecast_pv_source","planned_sell_kwh",
                "heat_pump_window",
            ]
            insert_cols = ["run_id"] + stage_columns
            placeholders = ",".join(["%s"] * len(insert_cols))
            for row in source:
                # Every run starts fail-closed. Never inherit an HP window from
                # the previously published plan before re-evaluating temperature.
                values = [run_id] + [0 if c == "heat_pump_window" else row.get(c) for c in stage_columns]
                cur.execute(f"INSERT INTO ems_gpt_plan_stage_rows ({','.join(insert_cols)}) VALUES({placeholders})", values)
            audit_stage(cur, run_id, "RCE_RAW", "OK", len(source), "durable prices copied")
            ensure_deadline("RCE_RAW")
            audit_stage(cur, run_id, "FORECAST", "OK", len(source), "PV/LOAD forecast copied from durable inputs")
            cur.execute("""UPDATE ems_gpt_plan_stage_rows SET
              grid_window=CASE WHEN COALESCE(sale_window,0)=1 THEN 'NO_BUY'
                WHEN COALESCE(buy_window,0)=1 THEN 'BUY_ALLOWED' ELSE 'NEUTRAL' END,
              windows_updated_at=NOW(6) WHERE run_id=%s""", (run_id,))
            audit_stage(cur, run_id, "WINDOWS", "OK", len(source))
            ensure_deadline("WINDOWS")
            cur.execute("SELECT * FROM ems_gpt_plan_stage_rows WHERE run_id=%s ORDER BY slot_start", (run_id,))
            rows = list(cur.fetchall())
            hp_context = prepare_heat_pump_plan(
                cur, source, rows, now, cutoff, OPTIONS, run_id, record_event)
            dhw_profile = hp_context["dhw_profile"]
            hp_fallback_kw = hp_context["hp_fallback_kw"]
            planned_hp_kw_by_day = hp_context["planned_hp_kw_by_day"]
            hp_selected_indices = hp_context["hp_selected_indices"]
            if (attempt.get('hp_schedule') or {}).get('status') == 'COMMITTED':
                cur.execute("SELECT slot_start,heat_enabled FROM ems_gpt_hp_schedule WHERE slot_start>=%s", (cutoff,))
                hp_windows = {row['slot_start']: bool(row['heat_enabled']) for row in cur.fetchall()}
                hp_selected_indices = {i for i, row in enumerate(rows) if hp_windows.get(row['slot_start'], False)}
            hp_shortfalls = hp_context["hp_shortfalls"]
            component_rows = hp_context["component_rows"]
            previous_loads = hp_context["previous_loads"]
            component_heat = hp_context["component_heat"]

            horizon_rows, sale_constraints, tou_by_index = [], [], []
            for index, row in enumerate(rows):
                work = dict(row)
                # From this boundary onward the planner sees only real bools,
                # regardless of how current or historical MariaDB rows encode
                # BOOLEAN/TINYINT values.
                work["buy_window"] = strict_database_bool(row.get("buy_window"), "buy_window")
                work["sale_window"] = strict_database_bool(row.get("sale_window"), "sale_window")
                work["forecast_load_kwh"] = max(0.0, float(row.get("forecast_load_kwh") or 0.0)*(1+0.10*fixed_forecast_correction))
                work["forecast_pv_total_kwh"] = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0)*(1-0.10*fixed_forecast_correction))
                slot_time = row.get("slot_start_local") or row["slot_start"]
                key = (0 if slot_time.weekday() < 5 else 1, slot_time.hour, slot_time.minute)
                historical_dhw_kwh = max(0.0, dhw_profile.get(key, 0.0))
                row_day = row.get("local_day") or row["slot_start"].date()
                planned_heating_kwh = (planned_hp_kw_by_day.get(row_day, hp_fallback_kw) * 0.25
                                       if index in hp_selected_indices else 0.0)
                component = component_rows.get(row["slot_start"]) or {}
                if component.get("status") == "READY":
                    historical_dhw_kwh = float(component["dhw_kwh"])
                if index in hp_selected_indices:
                    if row["slot_start"] in component_heat:
                        planned_heating_kwh = component_heat[row["slot_start"]]
                    elif float(previous_loads.get(row["slot_start"]) or 0) > 0:
                        planned_heating_kwh = float(previous_loads[row["slot_start"]])
                # HP_DHW is already included in the consumption forecast.
                # Only the separately planned HP_HEAT_DHW cycle is additional
                # load for the battery/SOC energy balance.
                work["forecast_heat_pump_load_kwh"] = planned_heating_kwh
                work["forecast_heat_pump_dhw_load_kwh"] = historical_dhw_kwh
                cur.execute("""UPDATE ems_gpt_plan_stage_rows SET
                  forecast_heat_pump_load_kwh=%s,forecast_heat_pump_dhw_load_kwh=%s
                  WHERE run_id=%s AND slot_start=%s""",
                  (round(work["forecast_heat_pump_load_kwh"], 6),
                   round(historical_dhw_kwh, 6), run_id, row["slot_start"]))
                horizon_rows.append(work)
                program = active_tou_program(row["slot_start"], tou_programs)
                tou_by_index.append(program)
                # The TOU program SOC is an executor baseline, not a planning
                # floor. Battery export is possible only in SELL, and HP has
                # priority over it: the HP slot gets a 100% sale floor while
                # the battery may still discharge to serve the HP load.
                sale_constraints.append(reserve if work["sale_window"] else 100.0)
                if index in hp_selected_indices:
                    sale_constraints[-1] = 100.0
            trace.add('HP_ENERGY_INPUT', 'OK', rows=horizon_rows,
                      selected_indices=hp_selected_indices, shortfalls=hp_shortfalls)
            planning_days = sorted({row.get("local_day") or row["slot_start"].date()
                                    for row in horizon_rows})
            terminal_day = planning_days[-1]
            history_start = planning_days[0] - timedelta(days=28)
            cur.execute("""SELECT local_day,
              SUBSTRING_INDEX(GROUP_CONCAT(soc_end_pct ORDER BY slot_start DESC),',',1) soc_end_pct,
              COUNT(*) slot_count,SUM(actual_recorded_at IS NOT NULL) terminal_count,
              SUM(actual_mode='MISSING_OUTAGE') missing_count
              FROM ems_gpt_slots
              WHERE local_day>=%s AND local_day<%s
              GROUP BY local_day
              HAVING slot_count IN (92,96,100) AND terminal_count=slot_count
                AND missing_count=0 AND soc_end_pct IS NOT NULL
              ORDER BY local_day""", (history_start, terminal_day))
            closing_history = list(cur.fetchall())
            history_weights = {
                    7: float(OPTIONS.get("soc_target_history_weight_7d_pct", 50.0)),
                    14: float(OPTIONS.get("soc_target_history_weight_14d_pct", 25.0)),
                    28: float(OPTIONS.get("soc_target_history_weight_28d_pct", 25.0)),
                }
            daily_terminal_soc = {}
            for planning_day in planning_days:
                terminal_history = historical_terminal_soc(
                    closing_history, planning_day, history_weights, reserve)
                daily_terminal_soc[planning_day] = end_of_day_soc_target(
                    float(terminal_history["soc_pct"]), reserve, target_cap)
                record_event("historical_terminal_soc_forecast", "planner", {
                    "terminal_day": str(planning_day),
                    "terminal_soc_pct": round(daily_terminal_soc[planning_day], 3),
                    "daily_close_target_band_pct": [40.0, 45.0],
                    **terminal_history,
                })
            terminal_soc = daily_terminal_soc[terminal_day]
            # Mandatory minima are independent of BUY permissions. Reusing
            # this immutable vector prevents a retry from weakening the plan.
            safety_required_soc = [reserve] * len(horizon_rows)
            daily_required_soc, daily_close_indices = daily_close_soc_requirements(
                horizon_rows, safety_required_soc, daily_terminal_soc)
            policy = solve_buy_policy(horizon_rows,
                initial_soc_pct=soc_now, capacity_kwh=capacity, reserve_pct=reserve,
                eta_c=eta_c, eta_d=eta_d, degradation=degradation,
                min_margin=min_margin, max_power_kw=max_kw,
                slot_minutes=int(OPTIONS['slot_minutes']),
                terminal_soc_pct=terminal_soc, required_soc_pcts=daily_required_soc,
                sale_floor_pcts=sale_constraints, max_grid_import_kw=max_grid_import_kw,
                soc_step_pct=0.1, max_soc_pct=target_cap, trace=trace,
                checkpoint=ensure_deadline, optimizer=solve_energy_horizon,
                budget_check=policy_budget_check)
            horizon_rows = policy['rows']
            optimization = policy['optimization']
            required_soc = policy['required']
            targets = policy['targets']
            deadline_targets = list(targets)
            optimized_floors = list(sale_constraints)
            target_due_indices = {i for i,f in enumerate(optimization['flows'])
                                  if f['grid_charge_kwh'] > flow_threshold}
            commitment = {
                'due':[r.get('slot_end') or r['slot_start'] for r in horizon_rows],
                'source':['ECONOMIC' for _ in horizon_rows],
                'reserved_pv_kwh':[0.0]*len(horizon_rows)}
            for i,row in enumerate(horizon_rows):
                rows[i].update(row)
                cur.execute("""UPDATE ems_gpt_plan_stage_rows SET buy_window=%s,
                  sale_window=%s,market_window=%s,grid_window=%s,forecast_load_kwh=%s,
                  forecast_pv_total_kwh=%s,forecast_pv1_kwh=%s,forecast_pv2_kwh=%s,forecast_heat_pump_load_kwh=%s
                  WHERE run_id=%s AND slot_start=%s""",
                  (row['buy_window'],row['sale_window'],row['market_window'],
                   'NO_BUY' if row['sale_window'] else 'BUY_ALLOWED' if row['buy_window'] else 'NEUTRAL',
                   row['forecast_load_kwh'],row['forecast_pv_total_kwh'],
                   float(row.get('forecast_pv1_kwh') or 0)*(1-0.10*fixed_forecast_correction),
                   float(row.get('forecast_pv2_kwh') or 0)*(1-0.10*fixed_forecast_correction),
                   row['forecast_heat_pump_load_kwh'],run_id,row['slot_start']))
            for stage in ('WINDOW_CANDIDATES','LOAD','PV','TARGET_COMMITMENT'):
                audit_stage(cur,run_id,stage,'OK',len(rows),
                            'PRIMARY_BUY_THEN_ECONOMIC_NEUTRAL; immutable requirements')
            record_event('economic_buy_policy_selected','planner',{
                'run_id':run_id,'expanded':policy['expanded'],
                'original_buy_indices':policy['original_buy_indices'],
                'promoted_indices':sorted(policy['promoted_indices']),
                'objective_pln':optimization['objective_pln'],
                'required_soc_pcts':required_soc,'execution_targets_pct':targets})
            attempt["candidate_rows"] = [
                {
                    "slot_start": horizon_rows[index].get("slot_start"),
                    "market_window": horizon_rows[index].get("market_window"),
                    "buy_window": horizon_rows[index].get("buy_window"),
                    "sale_window": horizon_rows[index].get("sale_window"),
                    "price_buy_pln_kwh": horizon_rows[index].get("price_buy_pln_kwh"),
                    "price_sell_pln_kwh": horizon_rows[index].get("price_sell_pln_kwh"),
                    "forecast_pv_total_kwh": horizon_rows[index].get("forecast_pv_total_kwh"),
                    "forecast_load_kwh": horizon_rows[index].get("forecast_load_kwh"),
                    "soc_start_plan_pct": flow.get("soc_start_pct"),
                    "soc_end_plan_pct": flow.get("soc_end_pct"),
                    "soc_required_pct": required_soc[index],
                    "soc_charge_target_pct": targets[index],
                    "planned_buy_kwh": flow.get("grid_charge_kwh"),
                    "planned_sell_kwh": flow.get("battery_sell_kwh"),
                    "planned_battery_discharge_kwh": flow.get("battery_discharge_internal_kwh"),
                    "planned_grid_load_kwh": flow.get("grid_load_kwh"),
                    "planned_pv_to_bat_kwh": flow.get("pv_to_bat_kwh"),
                }
                for index, flow in enumerate(optimization["flows"])
            ]
            for index, flow in enumerate(optimization["flows"]):
                row = horizon_rows[index]
                battery_sell = float(flow.get("battery_sell_kwh") or 0.0)
                grid_charge = float(flow.get("grid_charge_kwh") or 0.0)
                soc_end = float(flow.get("soc_end_pct") or 0.0)
                if (battery_sell > flow_threshold
                        and not strict_database_bool(row.get("sale_window"), "sale_window")):
                    raise RuntimeError(f"SALE_OUTSIDE_WINDOW:{index}")
                if battery_sell > flow_threshold and float(
                        row.get("price_sell_pln_kwh") or 0.0) <= 0.0:
                    raise RuntimeError(f"SALE_AT_NONPOSITIVE_PRICE:{index}")
                if (float(flow.get("battery_sell_kwh") or 0.0) > flow_threshold
                        and float(flow.get("soc_end_pct") or 0.0) + 1e-9
                        < optimized_floors[index]):
                    raise RuntimeError(
                        f"SALE_FLOOR_VIOLATION:{index}:"
                        f"{flow.get('soc_end_pct')}<{optimized_floors[index]}")
                if (battery_sell > flow_threshold
                        and soc_end + 0.01 < required_soc[index]):
                    raise RuntimeError(
                        f"SALE_REQUIRED_SOC_VIOLATION:{index}:"
                        f"{soc_end}<{required_soc[index]}")
                if grid_charge > flow_threshold and soc_end > targets[index] + 0.01:
                    raise RuntimeError(
                        f"BUY_TARGET_EXCEEDED:{index}:{soc_end}>{targets[index]}")
                if index in target_due_indices and soc_end + 0.01 < deadline_targets[index]:
                    raise RuntimeError(
                        f"SOC_TARGET_NOT_REACHED:{index}:{soc_end}<{deadline_targets[index]}")
                if soc_end + 0.01 < required_soc[index]:
                    raise RuntimeError(
                        f"SOC_REQUIRED_VIOLATION:{index}:"
                        f"{soc_end}<{required_soc[index]}")
                load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
                        + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
                pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
                delivered_battery = (
                    max(0.0, float(flow.get("battery_to_load_kwh") or 0.0))
                    + battery_sell)
                # Core battery/load balance excludes flexible PV surplus.  PV
                # export/CWU/EV/curtailment are allocated and validated in the
                # later FLEX_SURPLUS pass.
                pv_core = min(pv, load + max(0.0, float(flow.get("pv_to_bat_kwh") or 0.0)))
                lhs = (pv_core + max(0.0, float(flow.get("grid_load_kwh") or 0.0))
                       + grid_charge + delivered_battery)
                rhs = (load + max(0.0, float(flow.get("pv_to_bat_kwh") or 0.0))
                       + grid_charge + battery_sell)
                if abs(lhs - rhs) > 1e-6:
                    raise RuntimeError(
                        f"ENERGY_BALANCE_VIOLATION:{index}:{lhs}!={rhs}")
            ensure_deadline("SLOT_BALANCE")
            audit_stage(cur,run_id,"SLOT_BALANCE","OK",len(rows),
                        "core balance excludes flexible PV surplus")
            audit_stage(cur,run_id,"DISPATCH","OK",len(rows),f"objective={optimization['objective_pln']:.3f}")
            purchase_slots=sum(float(flow["grid_charge_kwh"])>flow_threshold for flow in optimization["flows"])
            audit_stage(cur,run_id,"REPLENISHMENT","OK",purchase_slots,
                        "pass 5: load, falling PV, sale preparation and recovery")
            morning_coverage = morning_energy_coverage(
                horizon_rows, optimization['flows'], capacity, reserve,
                float(OPTIONS.get('soc_replenishment_buffer_pct', 2.)),
                eta_c, eta_d, max_kw, int(OPTIONS['slot_minutes']), uncertainty_weight,
                technical_threshold)
            validation_reason = ('MORNING_ENERGY_SHORTFALL' if any(
                r['status'] == 'SHORTFALL' for r in morning_coverage) else 'OK')
            if morning_coverage:
                record_event('morning_energy_coverage', 'planner', {'reports': morning_coverage},
                             'WARNING' if validation_reason != 'OK' else 'INFO')
            # ``soc_floor`` is the stop level of a concrete, accepted battery
            # sale.  It is not copied from TOU and it never limits native-load
            # discharge.  Non-sale slots publish only the technical reserve.
            floors = [
                max(reserve, min(100.0, float(flow.get("soc_end_pct") or reserve)))
                if float(flow.get("battery_sell_kwh") or 0.0) > flow_threshold
                else reserve
                for flow in optimization["flows"]
            ]
            base=[{"row":row,**flow} for row,flow in zip(rows,optimization["flows"])]
            sale_economics = [
                battery_sale_economics(
                    horizon_rows, index, eta_c, eta_d, degradation, min_margin)
                for index in range(len(horizon_rows))
            ]
            # The planner publishes only the physical flexible-PV remainder.
            # A separate PPD run reads the frozen plan and allocates that
            # remainder to CWU, EV, export or curtailment without writing SOC.
            for i,item in enumerate(base):
                row=item["row"]
                tou_program=tou_by_index[i]
                tou_block_reason=None if tou_program else "TOU_FLOOR_UNAVAILABLE"
                target=targets[i]
                required=required_soc[i]
                item["start"],item["end"]=item["soc_start_pct"],item["soc_end_pct"]
                pv=float(row.get("forecast_pv_total_kwh") or 0)
                native_load=float(row.get("forecast_load_kwh") or 0)
                row_day = row.get("local_day") or row["slot_start"].date()
                hp_load = float(row.get('forecast_heat_pump_load_kwh') or 0.0)
                load=native_load+hp_load
                pv_surplus=max(0.0,pv-load)
                pv_to_bat=item["pv_to_bat_kwh"]
                pv_flex=item["pv_export_kwh"]
                buy=item["grid_charge_kwh"]
                item["charge"]=item["battery_charge_internal_kwh"]
                item["discharge"]=item["battery_discharge_internal_kwh"]
                item["sell"]=item["battery_sell_kwh"]
                sell_price=float(row.get("price_sell_pln_kwh") or 0)
                sell_bat=item["sell"]>flow_threshold
                economics = sale_economics[i]
                if sell_bat and not economics["eligible"]:
                    raise RuntimeError(
                        f"UNECONOMIC_BATTERY_EXPORT:{row['slot_start']}:"
                        f"sell={sell_price:.6f}:required={economics['required_sell_price']}")
                floor=floors[i]
                paired_buy=buy>flow_threshold and any(float(previous["battery_sell_kwh"])>flow_threshold for previous in optimization["flows"][:i])
                buy_purpose="POST_SALE_RECOVERY" if paired_buy else "FUTURE_LOAD_OR_SALE_PREPARATION" if buy>flow_threshold else "NONE"
                no_sell_pv=sell_price<=0
                grid_policy="BUY_ALLOWED" if buy>flow_threshold else ("NO_BUY" if sell_bat else "NEUTRAL")
                export_policy="SELL_BAT" if sell_bat else ("NO_SELL_PV" if no_sell_pv else "NEUTRAL")
                reason=(f"optimizer=FULL_HORIZON; horizon_slots={len(base)}; objective_pln={optimization['objective_pln']:.3f}; "
                        f"slot_cost_pln={item['slot_cost_pln']:.3f}; grid={grid_policy}; export={export_policy}; "
                        f"soc={item['end']:.2f}; required={required:.2f}; floor={floor:.2f}; charge_target={target:.2f}; hp_load_kwh={hp_load:.3f}; "
                        f"buy_purpose={buy_purpose}; ppd=PENDING_SEPARATE_RUN")
                if tou_block_reason:
                    reason += f"; {tou_block_reason}"
                cur.execute("""UPDATE ems_gpt_plan_stage_rows SET soc_start_plan_pct=%s,soc_end_plan_pct=%s,
                  soc_reserve_pct=%s,soc_required_pct=%s,soc_charge_target_pct=%s,
                  soc_sale_floor_pct=%s,soc_floor_pct=%s,soc_target_pct=%s,
                  soc_target_due=%s,soc_target_source=%s,
                  soc_target_reserved_pv_kwh=%s,planned_buy_kwh=%s,planned_battery_charge_kwh=%s,
                  planned_battery_discharge_kwh=%s,planned_sell_kwh=%s,
                  grid_policy_planned=%s,export_policy_planned=%s,
                  sell_bat_policy_allowed=%s,sell_pv_policy_allowed=%s,
                  planned_pv_to_bat_kwh=%s,soc_updated_at=NOW(6)
                  WHERE run_id=%s AND slot_start=%s""",
                  (round(item["start"],2),round(item["end"],2),round(reserve,2),
                   round(required,2),round(target,2),round(floor,2),round(floor,2),round(target,2),
                   commitment["due"][i],commitment["source"][i],round(commitment["reserved_pv_kwh"][i],6),round(buy,6),
                   round(item["charge"],6),round(item["discharge"],6),round(item["sell"],6),
                   grid_policy,export_policy,
                   int(bool(economics["eligible"])),int(sell_price>0.0),
                   round(pv_to_bat,6),run_id,row["slot_start"]))
                hp_window = i in hp_selected_indices
                cur.execute("""UPDATE ems_gpt_plan_stage_rows SET heat_pump_window=%s
                  WHERE run_id=%s AND slot_start=%s""", (hp_window,run_id,row["slot_start"]))
                ensure_deadline("FLEX_SURPLUS")
            audit_stage(cur,run_id,"FLEX_SURPLUS","OK",len(base),
                        "physical flexible PV remainder published for separate PPD")
            audit_stage(cur,run_id,"SOC","OK",len(base),f"pass 6: result SOC; step={optimization['soc_step_pct']:.2f}")
            cur.execute("""SELECT COUNT(*) n,
              SUM(price_buy_pln_kwh IS NULL OR price_sell_pln_kwh IS NULL) bad_price,
              SUM(soc_floor_pct IS NULL OR soc_target_pct IS NULL
                OR soc_required_pct IS NULL OR soc_charge_target_pct IS NULL
                OR soc_reserve_pct IS NULL OR soc_sale_floor_pct IS NULL
                OR soc_end_plan_pct+0.01<soc_required_pct) bad_soc,
              SUM(market_window NOT IN ('BUY','SELL','NEUTRAL')
                OR grid_policy_planned IS NULL OR export_policy_planned IS NULL
                OR planned_buy_kwh<0 OR planned_sell_kwh<0
                OR planned_battery_charge_kwh<0 OR planned_battery_discharge_kwh<0
                OR (planned_battery_charge_kwh>0.000001 AND planned_battery_discharge_kwh>0.000001)
                OR heat_pump_window NOT IN (0,1)) bad_ppd
              FROM ems_gpt_plan_stage_rows WHERE run_id=%s""",(run_id,))
            checks=cur.fetchone()
            accepted=checks["n"]==len(source) and not any(int(checks[k] or 0) for k in ("bad_price","bad_soc","bad_ppd"))
            if not accepted:
                cur.execute("UPDATE ems_gpt_plan_runs SET status='REJECTED',current_stage='VALIDATE',validated_at=NOW(6),updated_at=NOW(6),validation_status='REJECTED',validation_reason=%s WHERE run_id=%s",(json.dumps(checks,default=str),run_id))
                audit_stage(cur,run_id,"VALIDATE","REJECTED",0,json.dumps(checks,default=str))
                raise RuntimeError(f"planner validation rejected: {checks}")
            ensure_deadline("VALIDATE")
            cur.execute("""UPDATE ems_gpt_slots p JOIN ems_gpt_plan_stage_rows s
              ON s.slot_start=p.slot_start AND s.run_id=%s SET
              p.buy_window=s.buy_window,p.sale_window=s.sale_window,p.market_window=s.market_window,
              p.forecast_pv1_kwh=s.forecast_pv1_kwh,p.forecast_pv2_kwh=s.forecast_pv2_kwh,
              p.forecast_pv_total_kwh=s.forecast_pv_total_kwh,p.forecast_load_kwh=s.forecast_load_kwh,
              p.forecast_heat_pump_load_kwh=s.forecast_heat_pump_load_kwh,
              p.forecast_heat_pump_dhw_load_kwh=s.forecast_heat_pump_dhw_load_kwh,
              p.soc_start_plan_pct=s.soc_start_plan_pct,p.soc_end_plan_pct=s.soc_end_plan_pct,
              p.soc_floor_pct=s.soc_floor_pct,p.soc_target_pct=s.soc_target_pct,
              p.soc_reserve_pct=s.soc_reserve_pct,p.soc_required_pct=s.soc_required_pct,
              p.soc_charge_target_pct=s.soc_charge_target_pct,
              p.soc_sale_floor_pct=s.soc_sale_floor_pct,
              p.soc_target_due=s.soc_target_due,p.soc_target_source=s.soc_target_source,
              p.soc_target_reserved_pv_kwh=s.soc_target_reserved_pv_kwh,
              p.planned_buy_kwh=s.planned_buy_kwh,
              p.planned_sell_kwh=s.planned_sell_kwh,
              p.planned_battery_charge_kwh=s.planned_battery_charge_kwh,
              p.planned_battery_discharge_kwh=s.planned_battery_discharge_kwh,
              p.grid_policy_planned=s.grid_policy_planned,p.export_policy_planned=s.export_policy_planned,
              p.sell_bat_policy_allowed=s.sell_bat_policy_allowed,
              p.sell_pv_policy_allowed=s.sell_pv_policy_allowed,
              p.planned_pv_to_bat_kwh=s.planned_pv_to_bat_kwh,
              p.heat_pump_window=s.heat_pump_window,
              p.plan_run_id=%s,p.plan_stage='PUBLISHED',
              p.plan_stage_version=%s,p.plan_stage_updated_at=NOW(6),
              p.plan_validation_status='ACCEPTED',p.plan_validation_reason=%s,
              p.plan_published_at=NOW(6),p.plan_published=1 WHERE p.actual_recorded_at IS NULL AND p.slot_start>=%s""",
              (run_id,run_id,PLANNER_VERSION,validation_reason,cutoff))
            published=cur.rowcount
            cur.execute("""UPDATE ems_gpt_plan_runs SET status='PUBLISHED',current_stage='VALIDATE',
              validated_at=NOW(6),published_at=NOW(6),validation_status='ACCEPTED',
              validation_reason=%s,updated_at=NOW(6) WHERE run_id=%s""",(validation_reason,run_id))
            audit_stage(cur,run_id,"VALIDATE","ACCEPTED",published,"atomic publish")
        record_event("plan_published","planner",{"run_id":run_id,"run_type":run_type,"rows":published})
        for shortfall in hp_shortfalls:
            record_event("hp_minimum_heating_shortfall", "planner", shortfall, "WARNING")
        return {"run_id":run_id,"rows":published,"validation_reason":validation_reason,
                "morning_coverage":morning_coverage}

    def run_planner(run_type: str = "scheduled") -> dict:
        attempt = {
            "attempt_id": str(uuid.uuid4()),
            "run_type": str(run_type)[:32],
            "started_at": local_now().replace(tzinfo=None).isoformat(),
            "failed_stage": "STARTUP",
            "candidate_rows": [],
        }
        trace = PlannerTrace(attempt['attempt_id'])
        attempt['trace'] = trace
        result, failure = None, None
        try:
            # Separate connection/commit, including the active slot. This runs
            # before all price, forecast and SOC checks in the energy planner.
            try:
                attempt['hp_schedule'] = persist_independent_hp_schedule(
                    db, local_now().replace(tzinfo=None), slot_start().replace(tzinfo=None),
                    OPTIONS, attempt['attempt_id'], record_event)
                trace.add('HP_COMFORT', 'OK', details=attempt['hp_schedule'])
            except Exception as hp_error:
                attempt['hp_schedule'] = {'status': 'REFRESH_FAILED', 'error': str(hp_error)}
                trace.add('HP_COMFORT', 'ERROR', details=attempt['hp_schedule'])
                record_event('hp_independent_schedule_failed', 'planner',
                             attempt['hp_schedule'], 'ERROR')
            result = _run_planner(run_type, attempt)
            return result
        except Exception as exc:
            failure = exc
            import traceback
            attempt["traceback"] = traceback.format_exc()
            trace.add(attempt['failed_stage'], 'ERROR', error=str(exc))
            reason = str(exc)[:4000]
            attempt["failure_reason"] = reason
            attempt["status"] = "REJECTED"
            tokens = reason.split(":")
            evidence = getattr(exc,'planner_evidence',None) or getattr(exc.__cause__,'planner_evidence',{})
            failed_index = evidence.get('slot_index')
            if failed_index is None and len(tokens) > 1 and tokens[1].isdigit():
                failed_index = int(tokens[1])
            candidates = attempt.get("candidate_rows") or []
            failed_slot = (
                candidates[failed_index].get("slot_start")
                if failed_index is not None and failed_index < len(candidates)
                else None
            )
            try:
                with db() as conn, conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO ems_gpt_plan_attempts
                          (attempt_id,run_id,run_type,status,attempted_at,failed_stage,
                           failure_index,failed_slot,initial_soc_pct,failure_reason,
                           candidate_rows_json)
                          VALUES(%s,%s,%s,'REJECTED',NOW(6),%s,%s,%s,%s,%s,%s)""",
                        (
                            attempt["attempt_id"], attempt.get("planner_run_id"),
                            attempt["run_type"], attempt.get("failed_stage"),
                            failed_index, failed_slot, attempt.get("initial_soc_pct"),
                            reason, json.dumps(candidates, ensure_ascii=False, default=str),
                        ),
                    )
                    cur.execute(
                        """DELETE FROM ems_gpt_plan_attempts
                          WHERE attempted_at < NOW(6) - INTERVAL 90 DAY"""
                    )
            except Exception as persist_error:
                record_event("planner_attempt_persist_failed", "planner", {
                    "attempt_id": attempt["attempt_id"],
                    "planner_run_id": attempt.get("planner_run_id"),
                    "reason": str(persist_error)[:1000],
                    "original_failure": reason[:1000],
                }, "ERROR")
            raise
        finally:
            status = 'REJECTED' if failure else (
                'WAITING' if (result or {}).get('status') == 'WAITING' else 'PUBLISHED')
            payload = trace.payload(attempt, status, result, failure)
            try:
                persist_trace(db, payload)
                record_event('planner_trace_saved', 'planner', {
                    'attempt_id':attempt['attempt_id'], 'run_id':payload.get('run_id'),
                    'status':status, 'complete':payload['complete'],
                    'missing_stages':payload['missing_stages'],
                    'failed_stage':payload.get('failed_stage'),
                    'error':payload.get('error'), 'hp':payload.get('hp')},
                    'INFO' if payload['complete'] else 'WARNING')
            except Exception as trace_error:
                record_event('planner_trace_persist_failed', 'planner', {
                    'attempt_id': attempt['attempt_id'], 'error': str(trace_error),
                    'original_error': str(failure) if failure else None}, 'ERROR')

    return SimpleNamespace(
        optimize_hp_heating_slots=optimize_hp_heating_slots,
        run_planner=run_planner,
    )
