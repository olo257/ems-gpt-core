from datetime import datetime, timedelta
from ems_gpt_core.rolling_load_forecast import rolling_slot_forecast


def test_recent_seven_days_only_and_median():
    now = datetime(2026, 10, 13, 12)
    slot = datetime(2026, 10, 14, 8)
    samples = [
        {"slot_start": now - timedelta(days=i, hours=4), "native_load_kwh": v}
        for i, v in [(1, 0.2), (2, 0.3), (3, 1.9), (4, 0.25), (9, 30)]
    ]
    out = rolling_slot_forecast(samples, [slot], now=now)
    assert out[slot]["forecast_kwh"] == 0.275
    assert out[slot]["sample_count"] == 4


def test_missing_data_does_not_fabricate_zero():
    now = datetime(2026, 10, 13, 12)
    slot = datetime(2026, 10, 14, 8)
    out = rolling_slot_forecast([], [slot], now=now)
    assert out[slot]["forecast_kwh"] is None
    assert out[slot]["source"] == "insufficient_recent_history"


def test_outage_and_future_readings_excluded():
    now = datetime(2026, 10, 13, 12)
    slot = datetime(2026, 10, 14, 8)
    samples = [
        {"slot_start": now - timedelta(hours=4), "native_load_kwh": 3, "actual_mode": "MISSING_OUTAGE"},
        {"slot_start": now + timedelta(hours=20), "native_load_kwh": 7},
    ]
    assert rolling_slot_forecast(samples, [slot], now=now)[slot]["forecast_kwh"] is None


def test_recent_slot_samples_are_not_replaced_by_weekday_only_profiles():
    now = datetime(2026, 10, 13, 12)
    target = datetime(2026, 10, 14, 9, 15)
    samples = [
        {"slot_start": datetime(2026, 10, d, 9, 15), "native_load_kwh": 0.3}
        for d in (7, 8, 9, 10, 11, 12)
    ]
    out = rolling_slot_forecast(samples, [target], now=now)
    assert out[target]["forecast_kwh"] == 0.3
    assert out[target]["sample_count"] == 6


def test_agd_spike_does_not_become_a_separate_energy_buffer():
    now = datetime(2026, 10, 13, 12)
    target = datetime(2026, 10, 14, 9, 15)
    values = (0.3, 0.3, 0.3, 1.8, 0.3, 0.3)
    samples = [
        {"slot_start": datetime(2026, 10, day, 9, 15), "native_load_kwh": value}
        for day, value in zip((7, 8, 9, 10, 11, 12), values)
    ]
    assert rolling_slot_forecast(samples, [target], now=now)[target]["forecast_kwh"] == 0.3
