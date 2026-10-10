"""Shared physical slot balance used by status and diagnostics."""
from __future__ import annotations


def calculate_slot_energy_balance(row: dict, eta_c: float, eta_d: float) -> dict:
    """Return a consistent AC-side supply/demand balance for one plan slot."""
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    pv = max(0.0, float(row.get("forecast_pv_total_kwh") or 0.0))
    load = (max(0.0, float(row.get("forecast_load_kwh") or 0.0))
            + max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)))
    pv_to_battery = max(0.0, float(row.get("planned_pv_to_bat_kwh") or 0.0))
    pv_cwu = max(0.0, float(row.get("planned_pv_to_cwu_kwh") or 0.0))
    pv_ev = max(0.0, float(row.get("planned_pv_to_ev_kwh") or 0.0))
    pv_export = max(0.0, float(row.get("planned_pv_export_kwh") or 0.0))
    flexible_pv = pv_cwu + pv_ev + pv_export
    pv_to_load = min(pv, load)
    pv_used = min(pv, load + pv_to_battery + flexible_pv)
    battery_output = max(0.0, float(row.get("planned_battery_discharge_kwh") or 0.0)) * discharge_efficiency
    battery_sell = max(0.0, float(row.get("planned_sell_kwh") or 0.0))
    battery_to_load = max(0.0, battery_output - battery_sell)
    grid_load = max(0.0, load - pv_to_load - battery_to_load)
    planned_buy = max(0.0, float(row.get("planned_buy_kwh") or 0.0))
    grid_import = planned_buy + grid_load
    battery_charge_input = max(0.0, float(row.get("planned_battery_charge_kwh") or 0.0)) / charge_efficiency
    supply = pv_used + battery_output + grid_import
    demand = load + pv_to_battery + battery_charge_input + battery_sell + flexible_pv
    return {
        "forecast_pv_total_kwh": round(pv, 6),
        "forecast_load_kwh": round(max(0.0, float(row.get("forecast_load_kwh") or 0.0)), 6),
        "forecast_heat_pump_load_kwh": round(max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)), 6),
        "total_load_kwh": round(load, 6),
        "planned_battery_discharge_kwh": round(max(0.0, float(row.get("planned_battery_discharge_kwh") or 0.0)), 6),
        "planned_buy_kwh": round(planned_buy, 6),
        "planned_battery_charge_kwh": round(max(0.0, float(row.get("planned_battery_charge_kwh") or 0.0)), 6),
        "planned_sell_kwh": round(battery_sell, 6),
        "planned_pv_to_bat_kwh": round(pv_to_battery, 6),
        "planned_pv_to_cwu_kwh": round(pv_cwu, 6),
        "planned_pv_to_ev_kwh": round(pv_ev, 6),
        "planned_pv_export_kwh": round(pv_export, 6),
        "grid_load_kwh": round(grid_load, 6),
        "pv_balance_kwh": round(pv_used, 6),
        "battery_discharge_output_kwh": round(battery_output, 6),
        "grid_import_kwh": round(grid_import, 6),
        "battery_charge_input_kwh": round(battery_charge_input, 6),
        "supply_kwh": round(supply, 6),
        "demand_kwh": round(demand, 6),
        "difference_kwh": round(supply - demand, 6),
        "unplanned_grid_load_kwh": round(grid_load, 6),
    }


def assess_published_plan(rows: list[dict], eta_c: float, eta_d: float,
                          balance_tolerance_kwh: float = 0.02,
                          soc_tolerance_pct: float = 0.05,
                          unplanned_grid_tolerance_kwh: float = 0.05) -> dict:
    """Check that published rows balance and satisfy their SOC safety floor."""
    if not rows:
        return {"ok": False, "row_count": 0, "balance_violations": 0,
                "unplanned_grid_load_violations": 0,
                "soc_required_shortfalls": 0, "maximum_balance_error_kwh": None}
    balances = [calculate_slot_energy_balance(row, eta_c, eta_d) for row in rows]
    errors = [abs(float(item["difference_kwh"])) for item in balances]
    balance_violations = sum(value > balance_tolerance_kwh for value in errors)
    unplanned_grid_load_violations = sum(
        float(item["unplanned_grid_load_kwh"]) > unplanned_grid_tolerance_kwh
        for item in balances)
    shortfalls = sum(
        row.get("soc_end_plan_pct") is not None
        and row.get("soc_required_pct") is not None
        and float(row["soc_end_plan_pct"]) + soc_tolerance_pct < float(row["soc_required_pct"])
        for row in rows
    )
    return {
        "ok": balance_violations == 0 and unplanned_grid_load_violations == 0 and shortfalls == 0,
        "row_count": len(rows),
        "balance_violations": balance_violations,
        "unplanned_grid_load_violations": unplanned_grid_load_violations,
        "soc_required_shortfalls": shortfalls,
        "maximum_balance_error_kwh": round(max(errors), 6),
        "balance_tolerance_kwh": balance_tolerance_kwh,
        "unplanned_grid_load_tolerance_kwh": unplanned_grid_tolerance_kwh,
        "soc_tolerance_pct": soc_tolerance_pct,
    }
