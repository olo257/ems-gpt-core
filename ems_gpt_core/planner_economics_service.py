"""TOU floor and battery-sale economics for planner decisions."""

from __future__ import annotations

from planner_validation_service import strict_database_bool


def planning_tou_programs(live_programs: list[dict] | None, baselines: dict[str, float]) -> list[dict]:
    """Use live TOU times but immutable configured SOC baselines for planning."""
    if not isinstance(live_programs, list) or not live_programs:
        raise RuntimeError("TOU_PROGRAMS_UNAVAILABLE")
    if any(not isinstance(program, dict) for program in live_programs):
        raise RuntimeError("TOU_PROGRAMS_INVALID")
    result = []
    for live in live_programs:
        program = int(live["program"])
        if str(program) not in baselines:
            raise RuntimeError(f"DEYE_PROGRAM_SOC_BASELINE_MISSING:{program}")
        normalized = dict(live)
        normalized["soc"] = max(0.0, min(100.0, float(baselines[str(program)])))
        result.append(normalized)
    return result


def tou_sale_safety_floors(rows: list[dict], tou_by_index: list[dict | None],
                           flows: list[dict], capacity_kwh: float,
                           reserve_pct: float, eta_d: float,
                           uncertainty_weight: float,
                           max_override_minutes: int,
                           slot_minutes: int,
                           daily_required_soc: list[float]) -> list[float]:
    """Protect TOU safety SOC from deliberate export.

    Normally a sale must leave the active TOU baseline plus the battery energy
    needed by native loads until a later program deliberately lowers that
    baseline.  Programs 5 and 6 may use a lower floor only when the economic
    plan contains a real, near-term grid charge on the same local day and the
    accepted trajectory still closes that day at its required SOC.
    """
    if not (len(rows) == len(tou_by_index) == len(flows) == len(daily_required_soc)):
        raise ValueError("TOU_SALE_FLOOR_LENGTH_MISMATCH")
    capacity = max(0.001, float(capacity_kwh))
    reserve = max(0.0, min(100.0, float(reserve_pct)))
    efficiency = max(0.01, float(eta_d))
    uncertainty = max(0.0, min(2.0, float(uncertainty_weight))) * 0.10
    max_slots = max(1, int(max_override_minutes) // max(1, int(slot_minutes)))
    selected_buys = {
        i for i, flow in enumerate(flows)
        if float(flow.get("grid_charge_kwh") or 0.0) > 1e-9
    }
    floors = [100.0] * len(rows)

    for i, row in enumerate(rows):
        if not strict_database_bool(row.get("sale_window", False), "sale_window"):
            continue
        if max(0.0, float(row.get("forecast_heat_pump_load_kwh") or 0.0)) > 1e-9:
            # Critical HP load outranks battery exports during SELL. The
            # floor blocks selling stored energy, not discharge to serve load.
            floors[i] = 100.0
            continue
        program = tou_by_index[i]
        if not program:
            continue
        baseline = max(reserve, min(100.0, float(program["soc"])))
        program_number = int(program["program"])
        row_day = row.get("local_day") or row["slot_start"].date()

        day_end = i
        while (day_end + 1 < len(rows)
               and (rows[day_end + 1].get("local_day")
                    or rows[day_end + 1]["slot_start"].date()) == row_day):
            day_end += 1
        close_safe = (
            float(flows[day_end].get("soc_end_pct") or reserve) + 0.01
            >= float(daily_required_soc[day_end])
        )
        near_buy = next((j for j in range(i + 1, min(day_end, i + max_slots) + 1)
                         if j in selected_buys), None)
        if program_number in {5, 6} and near_buy is not None and close_safe:
            # The constrained pass still enforces the continuous SOC contract;
            # this only permits the executor to follow it below the TOU baseline.
            floors[i] = reserve
            continue

        stop = day_end + 1
        for j in range(i + 1, day_end + 1):
            future_program = tou_by_index[j]
            if future_program and float(future_program["soc"]) < baseline - 1e-9:
                stop = j
                break
        bridge_internal = 0.0
        for j in range(i + 1, stop):
            future = rows[j]
            load = (max(0.0, float(future.get("forecast_load_kwh") or 0.0))
                    + max(0.0, float(future.get("forecast_heat_pump_load_kwh") or 0.0)))
            pv = max(0.0, float(future.get("forecast_pv_total_kwh") or 0.0))
            bridge_internal += max(0.0, load - pv) / efficiency * (1.0 + uncertainty)
        floors[i] = min(100.0, baseline + bridge_internal / capacity * 100.0)
    return floors


def battery_sale_economics(rows: list[dict], index: int, eta_c: float, eta_d: float,
                           degradation: float, min_margin: float) -> dict:
    """Describe whether a slot pays for restoring the exported battery energy."""
    future_buys = [
        float(row["price_buy_pln_kwh"])
        for row in rows[index + 1:]
        if row.get("price_buy_pln_kwh") is not None
    ]
    replacement = min(future_buys) if future_buys else None
    sell_now = float(rows[index].get("price_sell_pln_kwh") or 0.0)
    required_sell = (
        replacement / (eta_c * eta_d) + degradation + min_margin
        if replacement is not None else None
    )
    return {
        "replacement_buy_price": replacement,
        "required_sell_price": required_sell,
        "expected_margin": (
            sell_now - replacement / (eta_c * eta_d) - degradation
            if replacement is not None else None
        ),
        "eligible": (sell_now > 0.0 and required_sell is not None
                     and sell_now >= required_sell),
    }


def assess_sale_plan_against_no_sale(sale_plan: dict, no_sale_plan: dict,
                                     rows: list[dict], min_margin: float,
                                     capacity_kwh: float, eta_c: float,
                                     tolerance_kwh: float = 0.05,
                                     eta_d: float = 0.95,
                                     degradation: float = 0.08) -> dict:
    """Check the full cost of export against keeping the energy for native load.

    The counterfactual has the same forecast and required terminal SOC. Both
    objectives include the grid purchases actually selected by each plan.
    Any remaining inventory difference is valued separately; grid supply to
    the house may not increase as a consequence of battery sale.
    """
    sales = sale_plan["flows"]
    baseline = no_sale_plan["flows"]
    if len(sales) != len(rows) or len(baseline) != len(rows):
        raise ValueError("SALE_COUNTERFACTUAL_LENGTH_MISMATCH")
    sold = sum(max(0.0, float(f.get("battery_sell_kwh") or 0.0)) for f in sales)
    extra_native_import = sum(max(0.0, float(a.get("grid_load_kwh") or 0.0)
                                  - float(b.get("grid_load_kwh") or 0.0))
                              for a, b in zip(sales, baseline))
    extra_grid_charge = sum(max(0.0, float(a.get("grid_charge_kwh") or 0.0)
                                - float(b.get("grid_charge_kwh") or 0.0))
                            for a, b in zip(sales, baseline))
    extra_grid_charge_cost = sum(
        max(0.0, float(a.get("grid_charge_kwh") or 0.0)
            - float(b.get("grid_charge_kwh") or 0.0))
        * float(row.get("price_buy_pln_kwh") or 0.0)
        for a, b, row in zip(sales, baseline, rows))

    # Keep a FIFO cost basis for battery energy charged from the grid. Without
    # this, SOC above the terminal target can look free: a plan can buy energy,
    # later export it below cost, and still meet the terminal SOC contract.
    grid_inventory: list[list[float]] = []  # internal kWh, cost PLN/internal kWh
    grid_load_inventory: list[list[float]] = []  # native-load kWh, avoided import PLN/kWh
    grid_load_sale_loss = 0.0
    grid_origin_sale_kwh = 0.0
    grid_origin_sale_revenue = 0.0
    grid_origin_sale_cost = 0.0
    grid_origin_sale_loss = 0.0
    charge_efficiency = max(0.01, float(eta_c))
    discharge_efficiency = max(0.01, float(eta_d))
    for flow, row in zip(sales, rows):
        grid_charge = max(0.0, float(flow.get("grid_charge_kwh") or 0.0))
        if grid_charge > 1e-9:
            grid_inventory.append([
                grid_charge * charge_efficiency,
                float(row.get("price_buy_pln_kwh") or 0.0) / charge_efficiency,
            ])
        native_discharge = (max(0.0, float(flow.get("battery_to_load_kwh") or 0.0))
                            / discharge_efficiency)
        while native_discharge > 1e-9 and grid_inventory:
            used = min(native_discharge, grid_inventory[0][0])
            grid_inventory[0][0] -= used
            native_discharge -= used
            if grid_inventory[0][0] <= 1e-9:
                grid_inventory.pop(0)
        grid_load = max(0.0, float(flow.get("grid_load_kwh") or 0.0))
        if grid_load > tolerance_kwh + 1e-9:
            grid_load_inventory.append([
                grid_load, float(row.get("price_buy_pln_kwh") or 0.0),
            ])
        battery_sale = max(0.0, float(flow.get("battery_sell_kwh") or 0.0))
        sale_internal = battery_sale / discharge_efficiency
        sale_output_remaining = battery_sale
        sale_price = float(row.get("price_sell_pln_kwh") or 0.0)
        while sale_output_remaining > 1e-9 and grid_load_inventory:
            used = min(sale_output_remaining, grid_load_inventory[0][0])
            required_price = (grid_load_inventory[0][1]
                              + float(degradation) + float(min_margin))
            grid_load_sale_loss += used * max(0.0, required_price - sale_price)
            grid_load_inventory[0][0] -= used
            sale_output_remaining -= used
            if grid_load_inventory[0][0] <= 1e-9:
                grid_load_inventory.pop(0)
        while sale_internal > 1e-9 and grid_inventory:
            used = min(sale_internal, grid_inventory[0][0])
            revenue = used * sale_price * discharge_efficiency
            cost = used * grid_inventory[0][1]
            grid_origin_sale_kwh += used
            grid_origin_sale_revenue += revenue
            grid_origin_sale_cost += cost
            grid_origin_sale_loss += max(
                0.0, cost + used * discharge_efficiency
                * (float(degradation) + float(min_margin)) - revenue)
            grid_inventory[0][0] -= used
            sale_internal -= used
            if grid_inventory[0][0] <= 1e-9:
                grid_inventory.pop(0)
    gross_gain = float(no_sale_plan["objective_pln"]) - float(sale_plan["objective_pln"])
    ending_energy_shortfall = max(0.0,
        (float(baseline[-1].get("soc_end_pct") or 0.0)
         - float(sales[-1].get("soc_end_pct") or 0.0))
        * capacity_kwh / 100.0)
    last_sale = max((i for i, flow in enumerate(sales)
                     if float(flow.get("battery_sell_kwh") or 0.0) > 1e-9),
                    default=-1)
    future_rows = rows[last_sale + 1:]
    feasible_buy_prices = [float(row["price_buy_pln_kwh"])
                           for row in future_rows
                           if row.get("price_buy_pln_kwh") is not None
                           and strict_database_bool(row.get("buy_window", False), "buy_window")]
    future_prices = [float(row["price_buy_pln_kwh"])
                     for row in future_rows if row.get("price_buy_pln_kwh") is not None]
    # Without a permitted BUY, energy kept in the battery can avoid the
    # expensive future native-load tariff. A theoretical cheap price outside
    # BUY is not a feasible replacement for depleted terminal inventory.
    replacement_price = (min(feasible_buy_prices) if feasible_buy_prices else
                         max(future_prices) if future_prices else None)
    ending_energy_cost = (
        0.0 if ending_energy_shortfall <= 1e-9 else
        ending_energy_shortfall * replacement_price / max(0.01, eta_c)
        if replacement_price is not None else None)
    gain = (gross_gain - ending_energy_cost
            if ending_energy_cost is not None else None)
    # The optimizer already charges degradation and minimum margin per kWh of
    # export. The positive counterfactual gain is an independent final check.
    eligible = (sold <= 1e-9 or
                (gain is not None and gain > 1e-6
                 and extra_native_import <= tolerance_kwh + 1e-9
                 and grid_origin_sale_loss <= 1e-6
                 and grid_load_sale_loss <= 1e-6))
    return {"eligible": eligible, "sold_kwh": round(sold, 6),
            "gross_gain_pln": round(gross_gain, 6),
            "net_gain_pln": round(gain, 6) if gain is not None else None,
            "ending_energy_shortfall_kwh": round(ending_energy_shortfall, 6),
            "ending_energy_replacement_cost_pln": (
                round(ending_energy_cost, 6) if ending_energy_cost is not None else None),
            "extra_native_import_kwh": round(extra_native_import, 6),
            "extra_grid_charge_kwh": round(extra_grid_charge, 6),
            "extra_grid_charge_cost_pln": round(extra_grid_charge_cost, 6),
            "grid_load_sale_loss_pln": round(grid_load_sale_loss, 6),
            "grid_origin_sale_kwh": round(grid_origin_sale_kwh, 6),
            "grid_origin_sale_revenue_pln": round(grid_origin_sale_revenue, 6),
            "grid_origin_sale_cost_pln": round(grid_origin_sale_cost, 6),
            "grid_origin_sale_loss_pln": round(grid_origin_sale_loss, 6),
            "minimum_margin_pln_kwh": min_margin}
