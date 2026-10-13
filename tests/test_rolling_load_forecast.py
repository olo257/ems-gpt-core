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
