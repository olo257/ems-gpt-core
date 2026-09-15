"""Read-only MariaDB inventory used for safe legacy-table archiving."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any


TEMPORAL_TYPES = {"date", "datetime", "timestamp"}


def _serial(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def catalog_database_tables(*, db, schema_name: str, log) -> dict:
    """Return a lightweight schema-wide catalog without scanning table contents."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name,table_type,engine,table_rows,data_length,index_length,
                      data_free,create_time,update_time,table_collation
                 FROM information_schema.tables
                WHERE table_schema=%s
                ORDER BY table_name""",
            (schema_name,),
        )
        objects = cur.fetchall()

        definitions = []
        definition_queries = (
            ("VIEW", "SELECT table_name object_name,view_definition definition FROM information_schema.views WHERE table_schema=%s"),
            ("TRIGGER", "SELECT trigger_name object_name,action_statement definition FROM information_schema.triggers WHERE trigger_schema=%s"),
            ("ROUTINE", "SELECT routine_name object_name,routine_definition definition FROM information_schema.routines WHERE routine_schema=%s"),
            ("EVENT", "SELECT event_name object_name,event_definition definition FROM information_schema.events WHERE event_schema=%s"),
        )
        for object_type, sql in definition_queries:
            cur.execute(sql, (schema_name,))
            for row in cur.fetchall():
                definitions.append((object_type, row["object_name"], str(row.get("definition") or "").lower()))

        cur.execute(
            """SELECT table_name,column_name,constraint_name,
                      referenced_table_name,referenced_column_name
                 FROM information_schema.key_column_usage
                WHERE table_schema=%s AND referenced_table_name IS NOT NULL""",
            (schema_name,),
        )
        foreign_keys = cur.fetchall()

    rows = []
    for obj in objects:
        table = obj["table_name"]
        table_lower = table.lower()
        dependencies = [
            {"type": object_type, "name": object_name}
            for object_type, object_name, definition in definitions
            if table_lower in definition and object_name != table
        ]
        for fk in foreign_keys:
            if fk["referenced_table_name"] == table:
                dependencies.append({
                    "type": "FOREIGN_KEY_INBOUND", "name": fk["table_name"],
                    "column": fk["column_name"], "constraint": fk["constraint_name"],
                })
            if fk["table_name"] == table:
                dependencies.append({
                    "type": "FOREIGN_KEY_OUTBOUND", "name": fk["referenced_table_name"],
                    "column": fk["column_name"],
                    "referenced_column": fk["referenced_column_name"],
                    "constraint": fk["constraint_name"],
                })
        rows.append({
            "table_name": table,
            "table_type": obj["table_type"],
            "engine": obj.get("engine"),
            "size_bytes": int(obj.get("data_length") or 0) + int(obj.get("index_length") or 0),
            "estimated_row_count": obj.get("table_rows"),
            "create_time": _serial(obj.get("create_time")),
            "information_schema_update_time": _serial(obj.get("update_time")),
            "sql_dependencies": dependencies,
            "content_scanned": False,
        })

    result = {
        "status": "OK", "schema": schema_name, "read_only": True,
        "content_scanned": False, "object_count": len(rows), "objects": rows,
    }
    log.info("database_catalog_summary %s", json.dumps({
        "status": "OK", "schema": schema_name, "read_only": True,
        "content_scanned": False, "object_count": len(rows),
    }, ensure_ascii=False, default=str))
    for row in rows:
        log.info("database_catalog_table %s", json.dumps(row, ensure_ascii=False, default=str))
    return result


def audit_slot_columns(*, db, schema_name: str, log) -> dict:
    """Inventory the canonical slot table and quantify known duplicate fields.

    This endpoint is intentionally read-only.  A destructive schema migration
    must use its results as a precondition instead of guessing from source code.
    """
    table = "ems_gpt_slots"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name,ordinal_position,column_type,is_nullable,
                      column_default,column_key,extra
                 FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s
                ORDER BY ordinal_position""",
            (schema_name, table),
        )
        columns = list(cur.fetchall())
        names = {row["column_name"] for row in columns}

        cur.execute(f"SELECT COUNT(*) n FROM `{table}`")
        row_count = int(cur.fetchone()["n"])

        populated = {}
        for name in sorted(names):
            cur.execute(f"SELECT COUNT(*) n FROM `{table}` WHERE `{name}` IS NOT NULL")
            populated[name] = int(cur.fetchone()["n"])

        check_sql = {
            "market_window_overlap": "COALESCE(buy_window,0)=1 AND COALESCE(sale_window,0)=1",
            "grid_policy_flag_mismatch": """(grid_policy_planned='BUY_ALLOWED')<>COALESCE(grid_buy_allowed,0)
                OR (grid_policy_planned='NO_BUY')<>COALESCE(grid_no_buy,0)
                OR (grid_policy_planned='NEUTRAL')<>COALESCE(grid_neutral,0)""",
            "battery_export_flag_mismatch": """(export_policy_planned='SELL_BAT')<>COALESCE(sell_bat_allowed,0)
                OR (export_policy_planned<>'SELL_BAT')<>COALESCE(no_sell_bat,0)""",
            "pv_export_flag_mismatch": """(export_policy_planned='SELL_PV')<>COALESCE(sell_pv_allowed,0)
                OR (export_policy_planned='NO_SELL_PV')<>COALESCE(no_sell_pv,0)""",
            "pv_to_bat_flag_mismatch": "(COALESCE(planned_pv_to_bat_kwh,0)>0.000001)<>COALESCE(pv_to_bat_planned,0)",
            "pv_to_cwu_flag_mismatch": "(COALESCE(planned_pv_to_cwu_kwh,0)>0.000001)<>COALESCE(pv_to_cwu_planned,0)",
            "pv_to_ev_flag_mismatch": "(COALESCE(planned_pv_to_ev_kwh,0)>0.000001)<>COALESCE(pv_to_ev_planned,0)",
            "pv_export_quantity_flag_mismatch": "(COALESCE(planned_pv_export_kwh,0)>0.000001)<>COALESCE(pv_export_planned,0)",
            "pv_curtail_quantity_flag_mismatch": "(COALESCE(planned_pv_curtail_kwh,0)>0.000001)<>COALESCE(pv_curtail_planned,0)",
        }
        referenced_columns = (
            "buy_window", "sale_window", "grid_policy_planned",
            "grid_buy_allowed", "grid_no_buy", "grid_neutral",
            "export_policy_planned", "sell_bat_allowed", "no_sell_bat",
            "sell_pv_allowed", "no_sell_pv", "planned_pv_to_bat_kwh",
            "planned_pv_to_cwu_kwh", "planned_pv_to_ev_kwh",
            "planned_pv_export_kwh", "planned_pv_curtail_kwh",
            "pv_to_bat_planned", "pv_to_cwu_planned", "pv_to_ev_planned",
            "pv_export_planned", "pv_curtail_planned",
        )

        scopes = {"all_history": None}
        if "actual_recorded_at" in names:
            scopes["open_slots"] = "actual_recorded_at IS NULL"
        if {"actual_recorded_at", "plan_stage_version"}.issubset(names):
            scopes["current_contract_open_slots"] = (
                "actual_recorded_at IS NULL AND plan_stage_version='CORE_0_33_0'"
            )

        scope_checks = {}
        for scope, scope_predicate in scopes.items():
            where = f" WHERE {scope_predicate}" if scope_predicate else ""
            cur.execute(f"SELECT COUNT(*) n FROM `{table}`{where}")
            scope_result = {"row_count": int(cur.fetchone()["n"]), "checks": {}}
            for key, predicate in check_sql.items():
                referenced = {token for token in referenced_columns if token in predicate}
                if referenced.issubset(names):
                    conjunction = f"({scope_predicate}) AND ({predicate})" if scope_predicate else predicate
                    cur.execute(f"SELECT COUNT(*) n FROM `{table}` WHERE {conjunction}")
                    scope_result["checks"][key] = int(cur.fetchone()["n"])
                else:
                    scope_result["checks"][key] = None
            scope_checks[scope] = scope_result

        checks = scope_checks["all_history"]["checks"]

    duplicate_groups = {
        "time_projection": [name for name in (
            "slot_start", "slot_end", "slot_id", "slot_start_utc",
            "slot_start_local", "utc_offset_minutes", "local_fold",
            "local_day", "slot_index_local") if name in names],
        "market_window": [name for name in (
            "buy_window", "sale_window", "grid_window") if name in names],
        "grid_policy": [name for name in (
            "grid_policy_planned", "grid_buy_allowed", "grid_no_buy",
            "grid_neutral") if name in names],
        "export_policy": [name for name in (
            "export_policy_planned", "sell_bat_allowed", "no_sell_bat",
            "sell_pv_allowed", "no_sell_pv") if name in names],
        "pv_flow_flags": [name for name in (
            "pv_to_bat_planned", "pv_to_cwu_planned", "pv_to_ev_planned",
            "pv_export_planned", "pv_curtail_planned",
            "planned_pv_to_bat_kwh", "planned_pv_to_cwu_kwh",
            "planned_pv_to_ev_kwh", "planned_pv_export_kwh",
            "planned_pv_curtail_kwh") if name in names],
    }
    result = {
        "status": "OK", "schema": schema_name, "table": table,
        "read_only": True, "row_count": row_count,
        "column_count": len(columns), "columns": columns,
        "populated_rows": populated, "duplicate_groups": duplicate_groups,
        "consistency_checks": checks, "scope_checks": scope_checks,
    }
    log.info("slot_column_audit_summary %s", json.dumps({
        "table": table, "row_count": row_count, "column_count": len(columns),
        "duplicate_groups": duplicate_groups, "scope_checks": scope_checks,
    }, ensure_ascii=False, default=str))
    for column in columns:
        detail = dict(column)
        detail["populated_rows"] = populated[column["column_name"]]
        log.info("slot_column_audit_column %s", json.dumps(detail, ensure_ascii=False, default=str))
    return result


def audit_v3_tables(*, db, qname, schema_name: str, log) -> dict:
    """Inventory every object containing ``v3`` without changing the database."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name,table_type,engine,table_rows,data_length,index_length,
                      data_free,create_time,update_time,table_collation
                 FROM information_schema.tables
                WHERE table_schema=%s AND LOWER(table_name) LIKE '%%v3%%'
                ORDER BY table_name""",
            (schema_name,),
        )
        objects = cur.fetchall()

        cur.execute(
            """SELECT table_name,column_name,data_type,ordinal_position
                 FROM information_schema.columns
                WHERE table_schema=%s AND LOWER(table_name) LIKE '%%v3%%'
                ORDER BY table_name,ordinal_position""",
            (schema_name,),
        )
        temporal_columns: dict[str, list[str]] = {}
        for column in cur.fetchall():
            if str(column["data_type"]).lower() in TEMPORAL_TYPES:
                temporal_columns.setdefault(column["table_name"], []).append(column["column_name"])

        sql_dependencies: dict[str, list[dict]] = {}

        def add_dependency(table: str, dependency: dict) -> None:
            sql_dependencies.setdefault(table, []).append(dependency)

        names = [row["table_name"] for row in objects]
        for table in names:
            pattern = f"%{table}%"
            dependency_queries = (
                ("VIEW", """SELECT table_name object_name FROM information_schema.views
                              WHERE table_schema=%s AND view_definition LIKE %s"""),
                ("TRIGGER", """SELECT trigger_name object_name FROM information_schema.triggers
                                 WHERE trigger_schema=%s AND action_statement LIKE %s"""),
                ("ROUTINE", """SELECT routine_name object_name FROM information_schema.routines
                                 WHERE routine_schema=%s AND routine_definition LIKE %s"""),
                ("EVENT", """SELECT event_name object_name FROM information_schema.events
                               WHERE event_schema=%s AND event_definition LIKE %s"""),
            )
            for object_type, sql in dependency_queries:
                cur.execute(sql, (schema_name, pattern))
                for dependency in cur.fetchall():
                    if dependency["object_name"] != table:
                        add_dependency(table, {"type": object_type, "name": dependency["object_name"]})

            cur.execute(
                """SELECT table_name,column_name,constraint_name
                     FROM information_schema.key_column_usage
                    WHERE table_schema=%s AND referenced_table_schema=%s
                      AND referenced_table_name=%s""",
                (schema_name, schema_name, table),
            )
            for dependency in cur.fetchall():
                add_dependency(table, {
                    "type": "FOREIGN_KEY_INBOUND", "name": dependency["table_name"],
                    "column": dependency["column_name"], "constraint": dependency["constraint_name"],
                })

            cur.execute(
                """SELECT referenced_table_name,referenced_column_name,column_name,constraint_name
                     FROM information_schema.key_column_usage
                    WHERE table_schema=%s AND table_name=%s
                      AND referenced_table_name IS NOT NULL""",
                (schema_name, table),
            )
            for dependency in cur.fetchall():
                add_dependency(table, {
                    "type": "FOREIGN_KEY_OUTBOUND", "name": dependency["referenced_table_name"],
                    "column": dependency["column_name"],
                    "referenced_column": dependency["referenced_column_name"],
                    "constraint": dependency["constraint_name"],
                })

        rows = []
        for obj in objects:
            table = obj["table_name"]
            table_type = obj["table_type"]
            exact_rows = None
            count_error = None
            last_write_candidates = {}
            last_write_error = None
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {qname(table)}")
                exact_rows = int(cur.fetchone()["n"])
            except Exception as exc:  # keep the remaining inventory readable
                count_error = type(exc).__name__

            columns = temporal_columns.get(table, [])
            try:
                for column in columns:
                    cur.execute(f"SELECT MAX({qname(column)}) AS v FROM {qname(table)}")
                    value = cur.fetchone()["v"]
                    if value is not None:
                        last_write_candidates[column] = _serial(value)
            except Exception as exc:
                last_write_error = type(exc).__name__

            last_write = max(last_write_candidates.values(), default=None)
            row = {
                "table_name": table,
                "table_type": table_type,
                "engine": obj.get("engine"),
                "size_bytes": int(obj.get("data_length") or 0) + int(obj.get("index_length") or 0),
                "data_bytes": int(obj.get("data_length") or 0),
                "index_bytes": int(obj.get("index_length") or 0),
                "data_free_bytes": int(obj.get("data_free") or 0),
                "exact_row_count": exact_rows,
                "estimated_row_count": obj.get("table_rows"),
                "create_time": _serial(obj.get("create_time")),
                "information_schema_update_time": _serial(obj.get("update_time")),
                "last_write_at": last_write,
                "last_write_candidates": last_write_candidates,
                "sql_dependencies": sql_dependencies.get(table, []),
                "count_error": count_error,
                "last_write_error": last_write_error,
            }
            rows.append(row)

    result = {
        "status": "OK",
        "schema": schema_name,
        "read_only": True,
        "object_count": len(rows),
        "objects": rows,
    }
    log.info("database_audit_v3_summary %s", json.dumps({
        "status": result["status"], "schema": schema_name,
        "read_only": True, "object_count": len(rows),
    }, ensure_ascii=False, default=str))
    for row in rows:
        log.info("database_audit_v3_table %s", json.dumps(row, ensure_ascii=False, default=str))
    return result
