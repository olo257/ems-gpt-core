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
