"""Regression: DHW belongs to household baseline, HEAT and EV do not."""
from ems_gpt_core.analytics_service import _native_load_kwh


def test_dhw_is_not_subtracted_twice():
    row = {
        "actual_load_kwh": 1.5,
        "detail_actual_ev_kwh": 0.2,
        "actual_heating_consumed_kwh": 0.4,
        "actual_dhw_consumed_kwh": 0.3,
        "actual_heat_pump_electric_kwh": 0.7,
    }
    assert _native_load_kwh(row) == 0.9


def test_no_measured_load_is_not_zero():
    assert _native_load_kwh({"actual_load_kwh": None}) is None


def test_native_load_cannot_be_negative():
    assert _native_load_kwh({
        "actual_load_kwh": 0.1,
        "actual_heating_consumed_kwh": 0.5,
        "detail_actual_ev_kwh": 0.3,
    }) == 0.0
