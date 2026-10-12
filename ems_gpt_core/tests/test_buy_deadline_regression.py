"""Regression: a BUY deadline must never silently lower the requested SOC."""
import pytest

from planner_service import optimize_energy_horizon


def slot(buy, load=0.0):
    return {
        "buy_window": buy, "sale_window": False,
        "forecast_load_kwh": load, "forecast_heat_pump_load_kwh": 0.0,
        "forecast_pv_total_kwh": 0.0,
        "price_buy_pln_kwh": 0.50, "price_sell_pln_kwh": 0.0,
    }


def dispatch(rows, initial, targets, due):
    return optimize_energy_horizon(
        rows, initial, 15.0, 15.0, 1.0, 1.0,
        0.0, 0.0, 5.0, 15, [15.0] * len(rows),
        15.0, 0.1, 100.0,
        minimum_soc_targets=targets,
        hard_target_indices=set(),
        target_due_indices=due,
        required_soc_pcts=[15.0] * len(rows),
        battery_sales_enabled=False,
        max_grid_import_kw=12.0,
    )


def test_buy_21_to_28_percent_is_feasible():
    result = dispatch([slot(True)], 21.0, [28.0], {0})
    assert result["flows"][0]["grid_charge_kwh"] > 0.0
    assert result["flows"][0]["soc_end_pct"] >= 28.0


def test_unreachable_buy_deadline_is_not_silently_lowered():
    with pytest.raises(RuntimeError, match="BUY_TARGET_UNREACHABLE"):
        dispatch([slot(True)], 21.0, [50.0], {0})


def test_no_buy_outside_buy_window():
    result = dispatch([slot(False)], 21.0, [28.0], set())
    assert result["flows"][0]["grid_charge_kwh"] == 0.0
