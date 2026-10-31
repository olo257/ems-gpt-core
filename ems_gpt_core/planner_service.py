from __future__ import annotations

import json
import math
import time as monotonic_clock
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Callable

from config_service import deye_program_soc_baselines
from planner_hp_service import (
    elapsed_hp_plan_states,
    historical_hp_power_kw,
    hp_heating_window_indices,
    hp_temperature_eligible,
    optimize_hp_heating_slots,
    prepare_heat_pump_plan,
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

PLANNER_VERSION = "CORE_0_40_72"

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

    def _run_planner(run_type: str, attempt: dict) -> dict:
        """Run the V3 staged planner transactionally in the app-owned database."""
        started_monotonic = monotonic_clock.monotonic()
        planning_budget = max(10.0, min(600.0, float(
            OPTIONS.get("planner_deadline_seconds", 120.0))))

        def ensure_deadline(stage: str) -> None:
            attempt["failed_stage"] = stage
            if monotonic_clock.monotonic() - started_monotonic > planning_budget:
                raise RuntimeError(f"PLANNER_DEADLINE_EXCEEDED:{stage}:{planning_budget:.0f}s")

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
            cur.execute("""SELECT * FROM ems_gpt_slots
              WHERE slot_start>=%s AND actual_recorded_at IS NULL
                AND price_source='PSE_API' AND price_buy_pln_kwh IS NOT NULL AND price_sell_pln_kwh IS NOT NULL
              ORDER BY slot_start""", (cutoff,))
            source = list(cur.fetchall())
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
            source = normalized_source
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
            if a.prepare_buy_windows is not None:
                night_capacity = a.prepare_buy_windows(horizon_rows, cur, soc_now)
                horizon_rows = night_capacity["rows"]
                for index in night_capacity["changed_indices"]:
                    row = horizon_rows[index]
                    rows[index].update(buy_window=True, sale_window=False, market_window='BUY')
                    cur.execute("""UPDATE ems_gpt_plan_stage_rows SET
                      buy_window=1,sale_window=0,market_window='BUY'
                      WHERE run_id=%s AND slot_start=%s""", (run_id, row['slot_start']))
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
            safety_required_soc = replenishment_soc_requirements(
                horizon_rows, capacity, reserve,
                float(OPTIONS.get("soc_replenishment_buffer_pct", 2.0)),
                eta_c, eta_d, max_kw, int(OPTIONS["slot_minutes"]), uncertainty_weight)
            recovery = validate_recoverable_soc_requirements(
                horizon_rows, safety_required_soc, soc_now, reserve, target_cap,
                capacity_kwh=capacity, eta_c=eta_c, eta_d=eta_d,
                max_power_kw=max_kw, slot_minutes=int(OPTIONS["slot_minutes"]),
                soc_step_pct=0.10, max_grid_import_kw=max_grid_import_kw)
            safety_required_soc = recovery["required_soc_pcts"]
            if recovery["relaxed_indices"]:
                record_event("soc_safety_bridge_recovery", "planner", {
                    "initial_soc_pct": round(float(soc_now), 3),
                    "reserve_pct": round(float(reserve), 3),
                    "recovery_buy_slot": str(
                        horizon_rows[recovery["recovery_buy_index"]]["slot_start"]),
                    "relaxed_slot_count": len(recovery["relaxed_indices"]),
                    "relaxed_until_buy_indices": recovery["relaxed_indices"],
                }, "WARNING")
            daily_required_soc, daily_close_indices = daily_close_soc_requirements(
                horizon_rows, safety_required_soc, daily_terminal_soc)
            target_contract = deterministic_soc_target_contract(
                horizon_rows, safety_required_soc, daily_required_soc,
                capacity, reserve, eta_c, eta_d, max_kw,
                int(OPTIONS["slot_minutes"]), uncertainty_weight)
            target_recovery = validate_recoverable_soc_requirements(
                horizon_rows, target_contract["raw_required"], soc_now, reserve,
                target_cap, capacity_kwh=capacity, eta_c=eta_c, eta_d=eta_d,
                max_power_kw=max_kw, slot_minutes=int(OPTIONS["slot_minutes"]),
                soc_step_pct=0.10, max_grid_import_kw=max_grid_import_kw)
            target_contract = preserve_target_contract_after_reachability(
                target_contract, target_recovery, daily_close_indices)
            target_contract = preserve_active_buy_window_target(
                horizon_rows, active_plan_row, target_contract, target_cap, flow_threshold)
            active_buy_commitment = target_contract["active_buy_target_preserved"]
            active_buy_shortfall = False
            if active_buy_commitment:
                ceiling = target_recovery["reachable_soc_ceiling_pcts"][active_buy_commitment["end_index"]]
                active_buy_shortfall = active_buy_commitment["execution_target_pct"] > ceiling + 1e-9
                record_event("active_buy_target_preserved", "planner", {
                    **active_buy_commitment, "physical_ceiling_pct": ceiling,
                    "physical_shortfall_pct": max(0.0, active_buy_commitment["execution_target_pct"] - ceiling),
                }, "WARNING" if active_buy_shortfall else "INFO")
            if target_contract["charge_target_shortfalls"] or target_contract["over_capacity_indices"]:
                record_event("soc_charge_target_physical_limit", "planner", {
                    "over_capacity_slot_count": len(target_contract["over_capacity_indices"]),
                    "maximum_raw_required_soc_pct": max(target_contract["raw_required"], default=reserve),
                    "buy_windows": [
                        {**shortfall,
                         "slot_start": str(horizon_rows[shortfall["start_index"]]["slot_start"]),
                         "slot_end": str(horizon_rows[shortfall["end_index"]]["slot_end"])}
                        for shortfall in target_contract["charge_target_shortfalls"]],
                    "action": "EXECUTABLE_BUY_CEILING_KEEP_REQUESTED_SHORTFALL",
                }, "WARNING")
            if target_recovery.get("unreachable_indices"):
                unreachable = target_recovery["unreachable_indices"]
                close_shortfalls = [
                    {
                        "slot_start": str(horizon_rows[index]["slot_start"]),
                        "requested_soc_pct": round(
                            target_contract["requested_required"][index], 3),
                        "reachable_soc_pct": round(
                            target_contract["reachable_required"][index], 3),
                        "shortfall_pct": round(max(
                            0.0,
                            target_contract["requested_required"][index]
                            - target_contract["reachable_required"][index]), 3),
                    }
                    for index in sorted(daily_close_indices & set(unreachable))
                ]
                record_event("soc_target_reachability", "planner", {
                    "unreachable_slot_count": len(unreachable),
                    "first_unreachable_slot": str(
                        horizon_rows[unreachable[0]]["slot_start"]),
                    "daily_close_shortfalls": close_shortfalls,
                    "action": "PRESERVE_DAILY_TARGET_MAXIMIZE_REACHABLE_SOC",
                    "battery_sales_disabled": bool(
                        target_recovery.get("disable_battery_sales")),
                }, "WARNING")
            terminal_soc = max(terminal_soc, target_contract["required"][-1])
            record_event("replenishment_soc_safety", "planner", {
                "minimum_soc_pct": reserve,
                "buffer_pp": float(OPTIONS.get("soc_replenishment_buffer_pct", 2.0)),
                "maximum_required_soc_pct": max(safety_required_soc),
                "historical_target_applies": "DAILY_CLOSE_ONLY",
            })
            audit_stage(cur,run_id,"WINDOW_CANDIDATES","OK",len(rows),"pass 1: full horizon")
            audit_stage(cur,run_id,"LOAD","OK",len(rows),"pass 2: native and controllable load")
            audit_stage(cur,run_id,"PV","OK",len(rows),"pass 3: corrected PV balance")
            ensure_deadline("PV")
            # Derive SOC requirements from load, PV and RCE windows before
            # running any dispatch optimization. The seed below is constrained
            # by that immutable contract and has battery sales disabled.
            optimized_floors = list(sale_constraints)
            internal_soc_step=0.10
            # Start from the physically recoverable daily contract. Using the
            # unbounded historical target here can trigger a fallback that
            # silently replaces today's close goal with the technical reserve.
            enforced_daily_required = list(target_contract["required"])
            waived_daily_closes = set()
            battery_sales_enabled = not bool(recovery.get("disable_battery_sales")) and not bool(
                target_recovery.get("disable_battery_sales")) and not active_buy_shortfall
            if recovery.get("capacity_limited_indices"):
                record_event("soc_bridge_limited_to_physical_capacity", "planner", {
                    "physical_soc_cap_pct": 100.0,
                    "limit_reason": ("UNREACHABLE_REQUIRED_SOC"
                                     if recovery.get("unreachable_indices")
                                     else "PHYSICAL_CAPACITY"),
                    "unreachable_slot_count": len(recovery.get("unreachable_indices") or []),
                    "limited_slot_count": len(recovery["capacity_limited_indices"]),
                    "first_limited_slot": str(
                        horizon_rows[recovery["capacity_limited_indices"][0]]["slot_start"]),
                    "last_limited_slot": str(
                        horizon_rows[recovery["capacity_limited_indices"][-1]]["slot_start"]),
                    "action": "BATTERY_SALES_DISABLED_RESIDUAL_NATIVE_LOAD_TO_GRID",
                }, "WARNING")
            terminal_shortfall_allowed = False
            target_due_indices = set(target_contract["buy_due_indices"])
            charge_targets = list(target_contract["charge_targets"])
            grid_charge_caps = economic_buy_charge_caps(
                horizon_rows, charge_targets, target_cap, capacity, eta_c,
                eta_d, max_kw, int(OPTIONS["slot_minutes"]))
            enforced_daily_required = list(target_contract["required"])
            while True:
                try:
                    target_seed = optimize_energy_horizon(
                        horizon_rows,soc_now,capacity,reserve,eta_c,eta_d,
                        degradation,min_margin,max_kw,int(OPTIONS["slot_minutes"]),
                        optimized_floors,terminal_soc,internal_soc_step,target_cap,
                        minimum_soc_targets=charge_targets,
                        hard_target_indices=set(),
                        target_due_indices=target_due_indices,
                        required_soc_pcts=enforced_daily_required,
                        battery_sales_enabled=False,
                        allow_terminal_shortfall=terminal_shortfall_allowed,
                        max_grid_import_kw=max_grid_import_kw,
                        grid_charge_caps=grid_charge_caps,
                        minimum_grid_charge_kwh=flow_threshold,
                        allow_buy_target_shortfall=True)
                    break
                except RuntimeError as exc:
                    # A BUY due target can be infeasible even though the
                    # relaxed continuous SOC bridge is feasible. Do not retry
                    # by lowering the target or publishing a misleading plan.
                    # Record the physical shortfall and preserve the last
                    # accepted production plan (the run transaction rolls back).
                    if str(exc).startswith("BUY_TARGET_UNREACHABLE:"):
                        parts = str(exc).split(":")
                        details = dict(part.split("=", 1) for part in parts[1:]
                                       if "=" in part)
                        failed_index = int(details["slot"])
                        record_event("buy_target_unreachable", "planner", {
                            "slot_start": str(horizon_rows[failed_index]["slot_start"]),
                            "requested_soc_pct": float(details["requested"]),
                            "reachable_soc_pct": float(details["reachable"]),
                            "shortfall_pct": round(
                                float(details["requested"]) -
                                float(details["reachable"]), 3),
                            "action": "KEEP_LAST_ACCEPTED_PLAN_REVIEW_BUY_WINDOWS",
                            "battery_sales_disabled": True,
                            "rce_windows_unchanged": True,
                        }, "ERROR")
                        raise RuntimeError(
                            "BUY_TARGET_UNREACHABLE_PRESERVED_PLAN:"
                            f"slot={horizon_rows[failed_index]['slot_start']}:"
                            f"requested={details['requested']}:"
                            f"reachable={details['reachable']}"
                        ) from exc
                    terminal_failure = (
                        str(exc) == "No feasible terminal SOC state for complete horizon"
                    )
                    if terminal_failure:
                        if battery_sales_enabled:
                            battery_sales_enabled = False
                            record_event("battery_sales_disabled_for_terminal_soc", "planner", {
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "reason": str(exc),
                            }, "WARNING")
                            continue
                        recovery_action, recovered_terminal = terminal_soc_recovery(
                            terminal_soc, safety_required_soc[-1], battery_sales_enabled,
                            current_day=terminal_day == local_now().date())
                        if recovery_action == "RELAX_HISTORICAL_TARGET":
                            record_event("historical_terminal_soc_relaxed_for_feasibility", "planner", {
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "safety_soc_pct": safety_required_soc[-1],
                                "reason": str(exc),
                            }, "WARNING")
                            terminal_soc = recovered_terminal
                            continue
                        if terminal_day != local_now().date():
                            raise
                        terminal_shortfall_allowed = True
                        record_event("current_day_terminal_soc_best_effort", "planner", {
                            "terminal_day": str(terminal_day),
                            "requested_soc_pct": terminal_soc,
                            "reason": str(exc),
                        }, "WARNING")
                        continue
                    prefix = "No feasible SOC state at horizon slot "
                    if not str(exc).startswith(prefix):
                        raise
                    failed_index = int(str(exc)[len(prefix):])
                    if battery_sales_enabled:
                        battery_sales_enabled = False
                        record_event("battery_sales_disabled_for_daily_soc", "planner", {
                            "slot_start": str(horizon_rows[failed_index]["slot_start"]),
                            "requested_soc_pct": daily_required_soc[failed_index],
                            "reason": str(exc),
                        }, "WARNING")
                        continue
                    requested_required = enforced_daily_required[failed_index]
                    reachable_required = target_contract["reachable_required"][failed_index]
                    recoverable_required = relax_soc_requirement_to_reachable(
                        requested_required, reachable_required, reserve)
                    if recoverable_required >= requested_required - 1e-9:
                        raise RuntimeError(f"SOC_SAFETY_BRIDGE_UNREACHABLE:{failed_index}") from exc
                    enforced_daily_required[failed_index] = recoverable_required
                    is_daily_close = failed_index in daily_close_indices
                    daily_close_indices.discard(failed_index)
                    waived_daily_closes.add(failed_index)
                    record_event("soc_bridge_requirement_unreachable", "planner", {
                        "slot_start": str(horizon_rows[failed_index]["slot_start"]),
                        "requested_soc_pct": round(requested_required, 3),
                        "safety_soc_pct": round(safety_required_soc[failed_index], 3),
                        "reachable_soc_pct": round(recoverable_required, 3),
                        "shortfall_pct": round(max(
                            0.0, requested_required - recoverable_required), 3),
                        "daily_close": is_daily_close,
                        "reason": str(exc),
                    }, "WARNING")
            if target_seed.get("buy_target_shortfalls"):
                record_event("buy_charge_target_shortfall_safe_plan", "planner", {
                    "run_id": run_id, "slots": target_seed["buy_target_shortfalls"],
                    "action": "REJECT_PLAN_PRESERVE_LAST_PUBLISHED"}, "ERROR")
                raise RuntimeError("BUY_TARGET_UNREACHABLE_AFTER_WINDOW_EXPANSION")
            if target_seed.get("terminal_shortfall_pct", 0.0) > 1e-9:
                requested_terminal_soc = terminal_soc
                terminal_soc = float(target_seed["achieved_terminal_soc_pct"])
                record_event("current_day_terminal_soc_unreachable", "planner", {
                    "terminal_day": str(terminal_day),
                    "requested_soc_pct": requested_terminal_soc,
                    "achieved_soc_pct": terminal_soc,
                    "shortfall_pct": target_seed["terminal_shortfall_pct"],
                    "battery_sales_enabled": battery_sales_enabled,
                }, "WARNING")

            def optimize_remaining_pass(
                    pass_name: str, floors: list[float],
                    minimum_targets: list[float] | None = None,
                    hard_indices: set[int] | None = None,
                    due_indices: set[int] | None = None,
                    required_pcts: list[float] | None = None) -> dict:
                """Apply the terminal-SOC recovery contract to every later pass."""
                nonlocal battery_sales_enabled, terminal_shortfall_allowed, terminal_soc
                requested_terminal_soc = terminal_soc
                target_commitment_fallback_level = 0
                # Never downgrade a failed BUY commitment into a lower target.
                # Keep the previously accepted plan if no physically feasible
                # allocation exists under the immutable RCE permissions.
                original_import_caps = (None if minimum_targets is None
                                        else list(minimum_targets))
                original_required_pcts = (None if required_pcts is None
                                          else list(required_pcts))
                while True:
                    try:
                        result = optimize_energy_horizon(
                            horizon_rows,soc_now,capacity,reserve,eta_c,eta_d,
                            degradation,min_margin,max_kw,int(OPTIONS["slot_minutes"]),
                            floors,terminal_soc,internal_soc_step,target_cap,
                            minimum_targets,hard_indices,due_indices,required_pcts,
                            battery_sales_enabled=battery_sales_enabled,
                            allow_terminal_shortfall=terminal_shortfall_allowed,
                            max_grid_import_kw=max_grid_import_kw,
                            grid_charge_caps=grid_charge_caps,
                            minimum_grid_charge_kwh=flow_threshold,
                            allow_buy_target_shortfall=True)
                        break
                    except RuntimeError as exc:
                        slot_error_prefix = "No feasible SOC state at horizon slot "
                        # A failed SOC bridge must not be repaired by removing
                        # BUY deadlines or lowering the required energy.
                        # This preserves the last accepted plan rather than
                        # publishing a horizon that buys too little.
                        if (pass_name == "TARGET_COMMITMENT"
                                and battery_sales_enabled
                                and str(exc).startswith(slot_error_prefix)):
                            battery_sales_enabled = False
                            record_event("battery_sales_disabled_for_target_commitment", "planner", {
                                "stage": pass_name,
                                "reason": str(exc),
                            }, "WARNING")
                            continue
                        if str(exc) != "No feasible terminal SOC state for complete horizon":
                            raise
                        if battery_sales_enabled:
                            battery_sales_enabled = False
                            record_event("battery_sales_disabled_for_terminal_soc", "planner", {
                                "stage": pass_name,
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "reason": str(exc),
                            }, "WARNING")
                            continue
                        recovery_action, recovered_terminal = terminal_soc_recovery(
                            terminal_soc, safety_required_soc[-1], battery_sales_enabled,
                            current_day=terminal_day == local_now().date())
                        if recovery_action == "RELAX_HISTORICAL_TARGET":
                            record_event("historical_terminal_soc_relaxed_for_feasibility", "planner", {
                                "stage": pass_name,
                                "terminal_day": str(terminal_day),
                                "requested_soc_pct": terminal_soc,
                                "safety_soc_pct": safety_required_soc[-1],
                                "reason": str(exc),
                            }, "WARNING")
                            terminal_soc = recovered_terminal
                            continue
                        if terminal_day != local_now().date():
                            raise
                        terminal_shortfall_allowed = True
                        record_event("current_day_terminal_soc_best_effort", "planner", {
                            "stage": pass_name,
                            "terminal_day": str(terminal_day),
                            "requested_soc_pct": terminal_soc,
                            "reason": str(exc),
                        }, "WARNING")
                if result.get("terminal_shortfall_pct", 0.0) > 1e-9:
                    terminal_soc = float(result["achieved_terminal_soc_pct"])
                    record_event("current_day_terminal_soc_unreachable", "planner", {
                        "stage": pass_name,
                        "terminal_day": str(terminal_day),
                        "requested_soc_pct": requested_terminal_soc,
                        "achieved_soc_pct": terminal_soc,
                        "shortfall_pct": result["terminal_shortfall_pct"],
                        "battery_sales_enabled": battery_sales_enabled,
                    }, "WARNING")
                # Keep the post-dispatch assertions aligned with recovery.
                # A retry can remove optional deadlines locally while keeping
                # the import cap; publishing against the original set would
                # turn that deliberate recovery into SOC_TARGET_NOT_REACHED.
                result["effective_target_due_indices"] = set(due_indices or ())
                result["effective_required_soc_pcts"] = list(
                    required_pcts if required_pcts is not None
                    else [reserve] * len(horizon_rows))
                return result
            # Convert the immutable TOU baselines into sale-only safety floors.
            # Iterate once after applying them because a permitted 5/6 bridge
            # override is valid only while the resulting plan retains its real
            # near-term battery BUY.
            for _ in range(2):
                guarded_floors = tou_sale_safety_floors(
                    horizon_rows, tou_by_index, target_seed["flows"],
                    capacity, reserve, eta_d, uncertainty_weight,
                    int(OPTIONS.get("tou_bridge_override_max_minutes", 180)),
                    int(OPTIONS["slot_minutes"]), enforced_daily_required)
                if all(abs(a - b) < 1e-9
                       for a, b in zip(guarded_floors, optimized_floors)):
                    break
                optimized_floors = guarded_floors
                target_seed = optimize_remaining_pass(
                    "TOU_GUARD_ITERATION", optimized_floors,
                    required_pcts=enforced_daily_required)
            final_guarded = tou_sale_safety_floors(
                horizon_rows, tou_by_index, target_seed["flows"],
                capacity, reserve, eta_d, uncertainty_weight,
                int(OPTIONS.get("tou_bridge_override_max_minutes", 180)),
                int(OPTIONS["slot_minutes"]), enforced_daily_required)
            if any(abs(a - b) >= 1e-9
                   for a, b in zip(final_guarded, optimized_floors)):
                # A BUY must never validate its own relaxation circularly. If
                # the two-pass result is not stable, retain the stricter floor.
                optimized_floors = [max(a, b)
                                    for a, b in zip(final_guarded, optimized_floors)]
                target_seed = optimize_remaining_pass(
                    "TOU_GUARD_FINAL", optimized_floors,
                    required_pcts=enforced_daily_required)
            for sale_guard_attempt in range(2):
                commitment = target_contract
                required_soc = list(enforced_daily_required)
                charge_targets = list(target_contract["charge_targets"])
                # Requested close goals remain in diagnostics. Do not restore
                # an unreachable BUY ceiling after relaxing a daily floor.
                target_due_indices = set(target_contract["buy_due_indices"])
                selected_buy_indices = set(target_contract["selected_buy_indices"])
                optimization=optimize_remaining_pass(
                    "TARGET_COMMITMENT", optimized_floors, charge_targets, set(),
                    target_due_indices, required_soc)
                target_due_indices = active_soc_target_deadlines(
                    optimization, target_due_indices)
                sell_indices = {
                    index for index, flow in enumerate(optimization["flows"])
                    if float(flow.get("battery_sell_kwh") or 0.0) > flow_threshold
                }
                unbacked_load_indices = voluntary_load_without_charging_indices(
                    horizon_rows, optimization["flows"],
                    technical_threshold, flow_threshold)
                if not sell_indices:
                    voluntary_import = bool(unbacked_load_indices)
                    if voluntary_import and battery_sales_enabled:
                        # A grid supplied native load is an allowed exception
                        # only when it protects energy for a later sale. If the
                        # selected route has no sale, replan with that exception
                        # disabled instead of publishing a hidden house import.
                        battery_sales_enabled = False
                        target_seed = optimize_remaining_pass(
                            "GRID_LOAD_WITHOUT_SALE_GUARD", optimized_floors,
                            required_pcts=enforced_daily_required)
                        ensure_deadline("GRID_LOAD_WITHOUT_SALE_GUARD")
                        continue
                    if voluntary_import:
                        record_event("voluntary_native_import_rejected", "planner", {
                            "slots": [
                                {"slot_start": str(horizon_rows[index]["slot_start"]),
                                 "buy_window": horizon_rows[index].get("buy_window"),
                                 "sale_window": horizon_rows[index].get("sale_window"),
                                 "grid_charge_kwh": optimization["flows"][index].get("grid_charge_kwh"),
                                 "voluntary_grid_load_kwh": optimization["flows"][index].get("voluntary_grid_load_kwh")}
                                for index in sorted(unbacked_load_indices)],
                            "charge_threshold_kwh": flow_threshold,
                            "technical_threshold_kwh": technical_threshold,
                        }, "ERROR")
                        raise RuntimeError("VOLUNTARY_GRID_LOAD_WITHOUT_SALE")
                    break
                voluntary_without_later_sale = any(
                    index in unbacked_load_indices
                    and not any(sale_index > index for sale_index in sell_indices)
                    for index, flow in enumerate(optimization["flows"])
                )
                if voluntary_without_later_sale:
                    battery_sales_enabled = False
                    target_seed = optimize_remaining_pass(
                        "GRID_LOAD_AFTER_SALE_GUARD", optimized_floors,
                        required_pcts=enforced_daily_required)
                    ensure_deadline("GRID_LOAD_AFTER_SALE_GUARD")
                    continue
                try:
                    no_sale_optimization = optimize_energy_horizon(
                        horizon_rows, soc_now, capacity, reserve, eta_c, eta_d,
                        degradation, min_margin, max_kw, int(OPTIONS["slot_minutes"]),
                        optimized_floors, terminal_soc, internal_soc_step, target_cap,
                        charge_targets, set(), target_due_indices, required_soc,
                        battery_sales_enabled=False,
                        allow_terminal_shortfall=terminal_shortfall_allowed,
                        max_grid_import_kw=max_grid_import_kw,
                        grid_charge_caps=grid_charge_caps,
                        minimum_grid_charge_kwh=flow_threshold,
                        allow_buy_target_shortfall=True)
                    sale_assessment = assess_sale_plan_against_no_sale(
                        optimization, no_sale_optimization, horizon_rows,
                        min_margin, capacity, eta_c, technical_threshold,
                        eta_d=eta_d, degradation=degradation)
                except RuntimeError as exc:
                    sale_assessment = {"eligible": False, "reason": str(exc)}
                record_event("battery_sale_counterfactual", "planner", {
                    "run_id": run_id, **sale_assessment,
                    "comparison": "same_forecast_and_terminal_target",
                }, "INFO" if sale_assessment["eligible"] else "WARNING")
                if sale_assessment["eligible"]:
                    break
                battery_sales_enabled = False
                target_seed = optimize_remaining_pass(
                    "SALE_GUARD_REPLAN", optimized_floors,
                    required_pcts=enforced_daily_required)
                ensure_deadline("SALE_GUARD_REPLAN")
            else:
                raise RuntimeError("SALE_GUARD_REPLAN_EXHAUSTED")
            effective_required = optimization.get("effective_required_soc_pcts")
            if effective_required is not None:
                required_soc = effective_required_soc_for_dispatch(
                    enforced_daily_required, effective_required)
            targets=list(optimization.get("effective_target_pcts",charge_targets))
            unreachable_buy_targets = [
                {
                    "slot_start": str(horizon_rows[index]["slot_start"]),
                    "requested_soc_pct": round(float(charge_targets[index]), 3),
                    "achievable_soc_pct": round(float(optimization["flows"][index]["soc_end_pct"]), 3),
                    "shortfall_pct": round(max(
                        0.0, float(charge_targets[index]) - float(optimization["flows"][index]["soc_end_pct"])), 3),
                    "planned_import_kwh": round(float(
                        optimization["flows"][index].get("grid_charge_kwh") or 0.0), 3),
                }
                for index in sorted(target_due_indices)
                if float(optimization["flows"][index]["soc_end_pct"]) + 0.01 < float(charge_targets[index])
            ]
            if unreachable_buy_targets:
                record_event("buy_soc_target_shortfall", "planner", {
                    "slots": unreachable_buy_targets,
                    "action": "REJECT_PLAN_PRESERVE_LAST_PUBLISHED",
                }, "ERROR")
                raise RuntimeError("BUY_TARGET_UNREACHABLE_AFTER_OPTIMIZATION")
            validated_deadline_targets = validated_buy_deadline_targets(
                optimization, targets, required_soc, battery_sales_enabled)
            target_due_indices = validate_active_soc_target_deadlines(
                optimization, target_due_indices, validated_deadline_targets)
            physical_buy_audit = validate_physical_buy_commitments(
                horizon_rows, optimization["flows"], target_due_indices,
                validated_deadline_targets, max_kw, int(OPTIONS["slot_minutes"]),
                capacity, eta_c)
            record_event("buy_physical_commitment_validated", "planner", {
                "run_id": run_id, **physical_buy_audit,
            })
            deadline_targets = list(validated_deadline_targets)
            targets = planned_buy_execution_targets(
                horizon_rows, optimization["flows"], targets, flow_threshold)
            record_event("economic_buy_targets", "planner", {
                "run_id": run_id,
                "optional_caps_pct": grid_charge_caps,
                "execution_targets_pct": targets,
                "required_deadline_targets_pct": deadline_targets,
                "policy": "CHEAPER_BUY_REPLACES_DEARER_BUY_SHARED_RUN_TARGET",
            })
            audit_stage(cur,run_id,"TARGET_COMMITMENT","OK",len(rows),
                        f"deterministic SOC contracts; selected_buy_slots={len(selected_buy_indices)}")
            record_event("daily_plan_variant_selected","planner",{
                "run_id":run_id,"terminal_soc_pct":round(terminal_soc,3),
                "selected":"DETERMINISTIC_SOC_CONTRACT_V4",
                "target_seed_objective_pln":float(target_seed["objective_pln"]),
                "final_dispatch_objective_pln":float(optimization["objective_pln"]),
                "target_source":"FORECAST_LOAD_PV_AND_RCE_WINDOWS",
            })
            ensure_deadline("TARGET_COMMITMENT")
            ensure_deadline("DISPATCH")
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
                hp_load = (planned_hp_kw_by_day.get(row_day, hp_fallback_kw) * 0.25
                           if i in hp_selected_indices else 0.0)
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
        try:
            return _run_planner(run_type, attempt)
        except Exception as exc:
            reason = str(exc)[:4000]
            attempt["failure_reason"] = reason
            attempt["status"] = "REJECTED"
            tokens = reason.split(":")
            failed_index = None
            if len(tokens) > 1 and tokens[1].isdigit():
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

    return SimpleNamespace(
        optimize_hp_heating_slots=optimize_hp_heating_slots,
        run_planner=run_planner,
    )
