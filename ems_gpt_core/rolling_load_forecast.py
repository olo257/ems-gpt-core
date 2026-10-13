"""Seven-day, 15-minute load forecast utilities.

Pure functions for seven-day baseline forecasting. DHW remains in the
baseline; separately planned HEAT and EV are excluded by the caller.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable, Mapping


def rolling_slot_forecast(
    samples: Iterable[Mapping],
    horizon: Iterable[datetime],
    *,
    now: datetime,
    lookback_days: int = 7,
    min_samples: int = 3,
) -> dict[datetime, dict]:
    """Estimate native load per quarter-hour using only recent completed slots.

    Each sample has slot_start and native_load_kwh. Missing/outage and negative
    values are ignored. No energy margin is applied here or to appliances.
    """
    if lookback_days < 1 or lookback_days > 7:
        raise ValueError("lookback_days must be in 1..7")
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    cutoff = now - timedelta(days=lookback_days)
    by_quarter = defaultdict(list)
    all_recent = []
    for sample in samples:
        when = sample.get("slot_start")
        value = sample.get("native_load_kwh")
        if not isinstance(when, datetime) or value is None or not cutoff <= when < now:
            continue
        if sample.get("actual_mode") == "MISSING_OUTAGE":
            continue
        try:
            energy = float(value)
        except (TypeError, ValueError):
            continue
        if not 0 <= energy < float("inf"):
            continue
        if when.minute not in (0, 15, 30, 45):
            continue
        by_quarter[(when.hour, when.minute)].append(energy)
        all_recent.append(energy)
    fallback = median(all_recent) if all_recent else None
    result = {}
    for when in horizon:
        values = by_quarter.get((when.hour, when.minute), [])
        if len(values) >= min_samples:
            estimate = median(values)
            source = "rolling_7d_slot_median"
        elif fallback is not None:
            # Do not fall back to stale weekday profiles or invented demand.
            estimate = fallback
            source = "rolling_7d_fallback"
        else:
            estimate = None
            source = "insufficient_recent_history"
        result[when] = {
            "forecast_kwh": None if estimate is None else round(estimate, 6),
            "sample_count": len(values),
            "source": source,
        }
    return result
