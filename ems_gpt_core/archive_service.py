"""Explicit, recoverable archival of the approved legacy EMS tables."""
from __future__ import annotations

import json


CONFIRMATION = "ARCHIVE_AND_DROP_45"
ARCHIVE_PREFIX = "archive_20260913__"
LEGACY_TABLES = (
    "ems_gpt_ai_advice", "ems_gpt_ai_runs", "ems_gpt_analysis_audit",
    "ems_gpt_analysis_daily_quality", "ems_gpt_analysis_day_reconciliation",
    "ems_gpt_analysis_forecast_snapshots", "ems_gpt_analysis_hourly",
    "ems_gpt_analysis_hour_quality", "ems_gpt_analysis_load_forecasts",
    "ems_gpt_analysis_profiles", "ems_gpt_analysis_runs",
    "ems_gpt_analysis_slot_quality", "ems_gpt_analysis_stage_runs",
    "ems_gpt_analysis_watermarks", "ems_gpt_schema_migrations",
    "ems_gpt_v3_action_events", "ems_gpt_v3_action_state", "ems_gpt_v3_ai_runs",
    "ems_gpt_v3_analysis_backtest_runs", "ems_gpt_v3_analysis_backtest_slots",
    "ems_gpt_v3_analysis_backtest_summary", "ems_gpt_v3_analysis_corrections",
    "ems_gpt_v3_analysis_directional_weights", "ems_gpt_v3_analysis_hierarchy_weights",
    "ems_gpt_v3_analysis_hourly_facts", "ems_gpt_v3_analysis_load_attribution",
    "ems_gpt_v3_analysis_model_weights", "ems_gpt_v3_analysis_pv_policy",
    "ems_gpt_v3_analysis_quality", "ems_gpt_v3_comparison", "ems_gpt_v3_daily",
    "ems_gpt_v3_diagnostic_runs", "ems_gpt_v3_execution_baselines",
    "ems_gpt_v3_hours", "ems_gpt_v3_ingest_runs", "ems_gpt_v3_parameters",
    "ems_gpt_v3_plan_runs", "ems_gpt_v3_plan_slots", "ems_gpt_v3_plan_stage_runs",
    "ems_gpt_v3_rce", "ems_gpt_v3_schema_migrations", "ems_gpt_v3_slots",
    "ems_gpt_v3_slot_calendar", "ems_gpt_v3_slot_model_eligibility",
    "ems_gpt_v3_writer_contracts",
)


def archive_legacy_tables(*, db, qname, schema_name: str, confirmation: str,
                          backup_id: str, log) -> dict:
    if confirmation != CONFIRMATION:
        raise ValueError("explicit archive confirmation is required")
    if not backup_id or len(backup_id) > 64:
        raise ValueError("verified backup id is required")
    if len(LEGACY_TABLES) != 45 or len(set(LEGACY_TABLES)) != 45:
        raise RuntimeError("legacy table allowlist contract failed")

    allowed = set(LEGACY_TABLES)
    archive_names = {table: f"{ARCHIVE_PREFIX}{table}" for table in LEGACY_TABLES}
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
            (schema_name,),
        )
        existing = {row["table_name"] for row in cur.fetchall()}
        missing_pairs = [table for table in LEGACY_TABLES
                         if table not in existing and archive_names[table] not in existing]
        if missing_pairs:
            raise RuntimeError(f"source and archive both absent: {missing_pairs}")

        cur.execute(
            """SELECT table_name,referenced_table_name FROM information_schema.key_column_usage
                 WHERE table_schema=%s AND referenced_table_name IS NOT NULL""",
            (schema_name,),
        )
        external_fks = [row for row in cur.fetchall()
                        if (row["table_name"] in allowed) != (row["referenced_table_name"] in allowed)]
        if external_fks:
            raise RuntimeError("external foreign-key dependency blocks archival")

        dependency_queries = (
            "SELECT table_name object_name,view_definition definition FROM information_schema.views WHERE table_schema=%s",
            "SELECT trigger_name object_name,action_statement definition FROM information_schema.triggers WHERE trigger_schema=%s",
            "SELECT routine_name object_name,routine_definition definition FROM information_schema.routines WHERE routine_schema=%s",
            "SELECT event_name object_name,event_definition definition FROM information_schema.events WHERE event_schema=%s",
        )
        for sql in dependency_queries:
            cur.execute(sql, (schema_name,))
            for row in cur.fetchall():
                definition = str(row.get("definition") or "").lower()
                if any(table.lower() in definition for table in LEGACY_TABLES):
                    raise RuntimeError(f"SQL object dependency blocks archival: {row['object_name']}")

    actions = []
    for table in LEGACY_TABLES:
        archive = archive_names[table]
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name IN (%s,%s)",
                (schema_name, table, archive),
            )
            present = {row["table_name"] for row in cur.fetchall()}
            if table not in present and archive in present:
                actions.append({"table": table, "archive": archive, "status": "ALREADY_ARCHIVED"})
                continue
            if archive not in present:
                cur.execute(f"CREATE TABLE {qname(archive)} LIKE {qname(table)}")
                cur.execute(f"INSERT INTO {qname(archive)} SELECT * FROM {qname(table)}")
            cur.execute(f"SELECT COUNT(*) n FROM {qname(table)}")
            source_rows = int(cur.fetchone()["n"])
            cur.execute(f"SELECT COUNT(*) n FROM {qname(archive)}")
            archive_rows = int(cur.fetchone()["n"])
            if source_rows != archive_rows:
                raise RuntimeError(f"archive row-count mismatch for {table}")
            cur.execute(f"DROP TABLE {qname(table)}")
            action = {"table": table, "archive": archive, "status": "ARCHIVED",
                      "verified_rows": source_rows, "backup_id": backup_id}
            actions.append(action)
            log.warning("database_archive_table %s", json.dumps(action, ensure_ascii=False))

    result = {"status": "COMPLETED", "backup_id": backup_id,
              "approved_table_count": 45, "actions": actions}
    log.warning("database_archive_summary %s", json.dumps({
        "status": "COMPLETED", "backup_id": backup_id,
        "approved_table_count": 45, "action_count": len(actions),
    }, ensure_ascii=False))
    return result
