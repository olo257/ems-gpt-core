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
 