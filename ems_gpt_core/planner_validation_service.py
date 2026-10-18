import math

"""Shared validation primitives for planner input normalization."""

def strict_database_bool(value, field: str) -> bool:
    """Normalize MariaDB BOOLEAN/TINYINT once and reject ambiguous values."""
    if isinstance(value, bool):
        return value
    if value == 0 or str(value).strip().lower() in {"0", "false"}:
        return False
    if value == 1 or str(value).strip().lower() in {"1", "true"}:
        return True
    raise RuntimeError(f"INVALID_BOOLEAN:{field}:{value!r}")


def validate_rce_market_window(row: dict) -> dict:
    """Validate and consume the window assignment persisted by the RCE importer."""
    if row.get("sale_window") is None or row.get("buy_window") is None:
        raise RuntimeError(f"RCE_PRICE_WINDOW_MISSING:{row.get('slot_start')}")
    sale = strict_database_bool(row.get("sale_window"), "sale_window")
    buy = strict_database_bool(row.get("buy_window"), "buy_window")
    if sale and buy:
        raise RuntimeError(f"WINDOW_OVERLAP:{row.get('slot_start')}")
    expected = "SELL" if sale else "BUY" if buy else "NEUTRAL"
    persisted = row.get("market_window")
    if persisted is not None and str(persisted).upper() != expected:
        raise RuntimeError(
            f"RCE_MARKET_WINDOW_MISMATCH:{row.get('slot_start')}:"
            f"{persisted!r}!={expected}")
    normalized = dict(row)
    normalized["sale_window"] = sale
    normalized["buy_window"] = buy
    normalized["market_window"] = expected
    return normalized

def validate_pv_forecasts(rows: list[dict]) -> None:
    """Unknown PV is never a zero-production scenario for dispatch or SOC."""
    for row in rows:
        values = [row.get(key) for key in (
            "forecast_pv1_kwh", "forecast_pv2_kwh", "forecast_pv_total_kwh")]
        if any(value is None for value in values):
            raise RuntimeError(f"MISSING_PV_FORECAST:{row['slot_start']}")
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in values):
            raise RuntimeError(f"INVALID_PV_FORECAST:{row['slot_start']}")
        if abs(float(values[0]) + float(values[1]) - float(values[2])) > 0.000002:
            raise RuntimeError(f"INCONSISTENT_PV_FORECAST:{row['slot_start']}")
