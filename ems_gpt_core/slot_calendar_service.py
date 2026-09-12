"""Canonical DST-safe slot calendar and relation backfill."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable


@dataclass(frozen=True)
class SlotCalendarAdapters:
    timezone: Any
    db: Callable


def build_slot_calendar(a: SlotCalendarAdapters):
    TZ, db = a.timezone, a.db

    def canonical_slots_for_day(day) -> list[dict]:
        """Return every real 15-minute instant of a Warsaw civil day (92/96/100)."""
        local_begin = datetime.combine(day, datetime.min.time(), TZ)
        local_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ)
        cursor = local_begin.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)
        rows = []
        while cursor < end_utc:
            local = cursor.astimezone(TZ)
            rows.append({
                "slot_id": cursor.strftime("%Y%m%dT%H%MZ"),
                "slot_start_utc": cursor.replace(tzinfo=None),
                "slot_end_utc": (cursor + timedelta(minutes=15)).replace(tzinfo=None),
                "slot_start_local": local.replace(tzinfo=None),
                "utc_offset_minutes": int((local.utcoffset() or timedelta()).total_seconds() // 60),
                "local_fold": int(local.fold), "local_day": day, "slot_index_local": len(rows),
            })
            cursor += timedelta(minutes=15)
        return rows


    def ensure_slot_calendar(*days) -> dict:
        inserted = 0
        counts = {}
        with db() as conn, conn.cursor() as cur:
            for day in days:
                rows = canonical_slots_for_day(day)
                counts[str(day)] = len(rows)
                for row in rows:
                    values = tuple(row[k] for k in ("slot_id","slot_start_utc","slot_end_utc","slot_start_local",
                        "utc_offset_minutes","local_fold","local_day","slot_index_local"))
                    cur.execute("""INSERT INTO ems_gpt_core_slot_calendar
                      (slot_id,slot_start_utc,slot_end_utc,slot_start_local,utc_offset_minutes,
                       local_fold,local_day,slot_index_local) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                      ON DUPLICATE KEY UPDATE slot_start_local=VALUES(slot_start_local),
                      utc_offset_minutes=VALUES(utc_offset_minutes),local_fold=VALUES(local_fold),
                      local_day=VALUES(local_day),slot_index_local=VALUES(slot_index_local)""", values)
                    inserted += cur.rowcount
                    if not row["local_fold"]:
                        for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows"):
                            cur.execute(f"""UPDATE {table} SET slot_id=%s,slot_start_utc=%s,
                              slot_start_local=%s,utc_offset_minutes=%s,local_fold=%s,
                              local_day=%s,slot_index_local=%s WHERE slot_start=%s AND slot_id IS NULL""",
                              (row["slot_id"],row["slot_start_utc"],row["slot_start_local"],
                               row["utc_offset_minutes"],row["local_fold"],row["local_day"],
                               row["slot_index_local"],row["slot_start_local"]))
        return {"days": counts, "writes": inserted}


    SLOT_RELATION_TABLES = (
        "ems_gpt_core_events", "ems_gpt_core_module_runs", "ems_gpt_core_slot_quality",
        "ems_gpt_core_process_decisions", "ems_gpt_core_commands",
        "ems_gpt_core_process_execution", "ems_gpt_core_execution_details",
    )


    def backfill_slot_relations() -> dict:
        """Populate canonical calendar and slot_id for every historical relation."""
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT DATE(slot_start) day FROM ems_gpt_slots WHERE slot_start IS NOT NULL ORDER BY day")
            days = [row["day"] for row in cur.fetchall()]
        if days:
            ensure_slot_calendar(*days)
        unresolved = {}
        updated = 0
        with db() as conn, conn.cursor() as cur:
            for table in SLOT_RELATION_TABLES:
                cur.execute(f"""UPDATE {table} t JOIN ems_gpt_core_slot_calendar c
                  ON c.slot_start_local=t.slot_start AND c.local_fold=0
                  SET t.slot_id=c.slot_id WHERE t.slot_start IS NOT NULL AND t.slot_id IS NULL""")
                updated += cur.rowcount
                cur.execute(f"SELECT COUNT(*) n FROM {table} WHERE slot_start IS NOT NULL AND slot_id IS NULL")
                unresolved[table] = int(cur.fetchone()["n"] or 0)
        return {"days": len(days), "updated": updated, "unresolved": unresolved,
                "ready": not any(unresolved.values())}

    return SimpleNamespace(
        canonical_slots_for_day=canonical_slots_for_day,
        ensure_slot_calendar=ensure_slot_calendar,
        backfill_slot_relations=backfill_slot_relations,
    )
