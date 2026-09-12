"""Startup bootstrap and interrupted-run recovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
from types import SimpleNamespace
from typing import Callable


@dataclass(frozen=True)
class RecoveryAdapters:
    options: dict
    db: Callable
    qname: Callable[[str], str]
    record_event: Callable


def build_recovery(a: RecoveryAdapters):
    def bootstrap_legacy_tables() -> int:
        if not a.options.get("bootstrap_from_source", False):
            return 0
        source, target = a.options["source_db"], a.options["db_name"]
        if source == target:
            return 0
        migrated = 0
        with a.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ems_gpt_core_migrations WHERE migration_key=%s", ("legacy_tables_1_to_1",))
            if cur.fetchone():
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=%s AND table_name LIKE 'ems_gpt_%%'", (target,))
                return int(cur.fetchone()["n"])
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name LIKE 'ems_gpt_%%' ORDER BY table_name", (source,))
            tables = [row["table_name"] for row in cur.fetchall()]
            for table in tables:
                src = f"{a.qname(source)}.{a.qname(table)}"
                dst = f"{a.qname(target)}.{a.qname(table)}"
                cur.execute(f"CREATE TABLE IF NOT EXISTS {dst} LIKE {src}")
                cur.execute(f"INSERT IGNORE INTO {dst} SELECT * FROM {src}")
                migrated += 1
            cur.execute("INSERT INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                        ("legacy_tables_1_to_1", json.dumps({"source": source, "target": target, "tables": tables}, ensure_ascii=False)))
        return migrated

    def recover_interrupted_runs() -> dict:
        """Idempotently close runs that could not survive an application restart."""
        with a.db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE ems_gpt_plan_runs SET status='ABORTED_RECOVERED',
              current_stage='RECOVERY',updated_at=NOW(6),validation_status='REJECTED',
              validation_reason='application restart interrupted the run'
              WHERE status='RUNNING' AND updated_at<NOW(6)-INTERVAL 10 MINUTE""")
            plans = cur.rowcount
            cur.execute("""UPDATE ems_gpt_core_analytics_runs SET status='ABORTED_RECOVERED',
              completed_at=NOW(6),details_json=JSON_OBJECT('reason','application restart interrupted the run')
              WHERE status='RUNNING' AND started_at<NOW(6)-INTERVAL 10 MINUTE""")
            analytics = cur.rowcount
        result = {"plan_runs": plans, "analytics_runs": analytics}
        if plans or analytics:
            a.record_event("interrupted_runs_recovered", "diagnostics", result, "WARNING")
        return result

    return SimpleNamespace(
        bootstrap_legacy_tables=bootstrap_legacy_tables,
        recover_interrupted_runs=recover_interrupted_runs,
    )
