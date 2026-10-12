"""Isolated read-only snapshot ingestion and namespaced diagnostics.

This module intentionally has no SQL connection, Home Assistant import, or
executor integration. The caller must supply records already read from the
production plan and must explicitly provide the column mapping.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping, Sequence

from economic_shadow import ShadowSlot
from economic_shadow_integration import ShadowSnapshot, compare_snapshot
from economic_shadow_simulator import ShadowAssumptions


REQUIRED_KEYS = (
    "start", "buy_price", "sell_price", "buy_allowed", "sell_allowed",
    "load_kwh", "pv_kwh", "soc_required_pct", "production_sell_kwh",
    "production_buy_kwh",
)


def snapshot_from_records(
    records: Sequence[Mapping],
    source_id: str,
    initial_soc_pct: float,
    terminal_soc_pct: float,
    columns: Mapping[str, str] | None = None,
) -> ShadowSnapshot:
    """Normalize already-fetched slot records; fail closed on missing fields.

    The caller is responsible for correct units and mapping actual DB fields.
    """
    mapping = {key: key for key in REQUIRED_KEYS}
    if columns:
        mapping.update(columns)
    if not records:
        raise ValueError("EMPTY_RECORDS")
    slots = []
    targets = []
    sells = []
    buys = []
    previous_start = None
    for i, record in enumerate(records):
        try:
            values = {key: record[mapping[key]] for key in REQUIRED_KEYS}
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{i}:MISSING_REQUIRED_COLUMN:{exc}") from exc
        if any(values[key] is None for key in REQUIRED_KEYS):
            raise ValueError(f"{i}:NULL_VALUE")
        start = str(values["start"])
        if previous_start is not None and start <= previous_start:
            raise ValueError(f"{i}:NON_MONOTONIC_SLOT_ORDER")
        previous_start = start
        for key in ("buy_allowed", "sell_allowed"):
            if values[key] not in (True, False, 0, 1):
                raise ValueError(f"{i}:INVALID_{key.upper()}")
        slots.append(ShadowSlot(
            start=start,
            buy_price=float(values["buy_price"]),
            sell_price=float(values["sell_price"]),
            buy_allowed=bool(values["buy_allowed"]),
            sell_allowed=bool(values["sell_allowed"]),
            load_kwh=float(values["load_kwh"]),
            pv_kwh=float(values["pv_kwh"]),
        ))
        targets.append(float(values["soc_required_pct"]))
        sells.append(float(values["production_sell_kwh"]))
        buys.append(float(values["production_buy_kwh"]))
    return ShadowSnapshot(
        slots=tuple(slots),
        initial_soc_pct=float(initial_soc_pct),
        required_soc_pct=tuple(targets),
        terminal_soc_pct=float(terminal_soc_pct),
        production_sell_kwh=tuple(sells),
        production_buy_kwh=tuple(buys),
        source_id=source_id,
    )


def build_diagnostic(
    records: Sequence[Mapping],
    source_id: str,
    initial_soc_pct: float,
    terminal_soc_pct: float,
    columns: Mapping[str, str] | None = None,
    assumptions: ShadowAssumptions = ShadowAssumptions(),
) -> dict:
    snapshot = snapshot_from_records(
        records, source_id, initial_soc_pct, terminal_soc_pct, columns)
    return compare_snapshot(snapshot, assumptions)


def write_shadow_report(
    report: Mapping,
    output_directory: str | Path,
    report_name: str,
) -> Path:
    """Write only into caller-provided shadow directory.

    Refuses absolute names, path traversal, symlinks and existing files.
    Never accepts a production database connection or SQL table name.
    """
    if not report_name or Path(report_name).name != report_name:
        raise ValueError("INVALID_REPORT_NAME")
    if not report_name.endswith(".json"):
        raise ValueError("REPORT_MUST_BE_JSON")
    root = Path(output_directory)
    if root.is_symlink():
        raise ValueError("SYMLINK_SHADOW_DIRECTORY")
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve() == Path("/").resolve():
        raise ValueError("INVALID_SHADOW_DIRECTORY")
    destination = root / report_name
    payload = dict(report)
    payload["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["storage_namespace"] = "economic_shadow"
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return destination
