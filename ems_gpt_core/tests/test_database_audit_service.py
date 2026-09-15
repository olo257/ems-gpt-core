import unittest
from contextlib import contextmanager

from database_audit_service import audit_slot_columns, audit_v3_tables, catalog_database_tables


class Cursor:
    def __init__(self):
        self.rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        normalized = " ".join(sql.split()).lower()
        if "from information_schema.tables" in normalized:
            self.rows = [{
                "table_name": "ems_gpt_v3_slots", "table_type": "BASE TABLE", "engine": "InnoDB",
                "table_rows": 2, "data_length": 1024, "index_length": 512, "data_free": 0,
                "create_time": None, "update_time": None, "table_collation": "utf8mb4_unicode_ci",
            }]
        elif "from information_schema.columns" in normalized:
            self.rows = [{"table_name": "ems_gpt_v3_slots", "column_name": "updated_at", "data_type": "datetime", "ordinal_position": 1}]
        elif "from information_schema.views" in normalized or "from information_schema.triggers" in normalized or "from information_schema.routines" in normalized or "from information_schema.events" in normalized or "from information_schema.key_column_usage" in normalized:
            self.rows = []
        elif "count(*)" in normalized:
            self.rows = [{"n": 2}]
        elif "max(`updated_at`)" in normalized:
            self.rows = [{"v": "2026-09-13T06:00:00"}]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()

    def cursor(self):
        return self.cursor_instance


class Log:
    def __init__(self):
        self.messages = []

    def info(self, message, payload):
        self.messages.append((message, payload))


class DatabaseAuditTests(unittest.TestCase):
    def test_slot_column_audit_is_read_only_and_checks_duplicate_contracts(self):
        connection = Connection()
        cursor = connection.cursor_instance

        def execute(sql, params=None):
            cursor.executed.append((sql, params))
            normalized = " ".join(sql.split()).lower()
            if "from information_schema.columns" in normalized:
                names = (
                    "buy_window", "sale_window", "grid_policy_planned",
                    "grid_buy_allowed", "grid_no_buy", "grid_neutral",
                )
                cursor.rows = [{
                    "column_name": name, "ordinal_position": index,
                    "column_type": "tinyint(1)", "is_nullable": "NO",
                    "column_default": "0", "column_key": "", "extra": "",
                } for index, name in enumerate(names, 1)]
            elif "count(*)" in normalized:
                cursor.rows = [{"n": 0 if " where " in normalized else 3}]
            else:
                raise AssertionError(sql)

        cursor.execute = execute

        @contextmanager
        def db():
            yield connection

        result = audit_slot_columns(db=db, schema_name="ems_gpt", log=Log())

        self.assertTrue(result["read_only"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["column_count"], 6)
        self.assertEqual(result["consistency_checks"]["market_window_overlap"], 0)
        statements = "\n".join(sql for sql, _ in cursor.executed).lower()
        for verb in ("delete", "update", "insert", "alter", "drop"):
            self.assertNotIn(f" {verb} ", f" {statements} ")

    def test_catalog_does_not_scan_table_contents(self):
        connection = Connection()

        @contextmanager
        def db():
            yield connection

        result = catalog_database_tables(db=db, schema_name="ems_gpt", log=Log())

        self.assertFalse(result["content_scanned"])
        self.assertEqual(result["object_count"], 1)
        statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed).lower()
        self.assertNotIn("count(*)", statements)
        self.assertNotIn(" max(", statements)

    def test_audit_is_read_only_and_reports_exact_inventory(self):
        connection = Connection()

        @contextmanager
        def db():
            yield connection

        log = Log()
        result = audit_v3_tables(
            db=db, qname=lambda value: f"`{value}`", schema_name="ems_gpt", log=log,
        )

        self.assertTrue(result["read_only"])
        self.assertEqual(result["object_count"], 1)
        row = result["objects"][0]
        self.assertEqual(row["table_name"], "ems_gpt_v3_slots")
        self.assertEqual(row["exact_row_count"], 2)
        self.assertEqual(row["size_bytes"], 1536)
        self.assertEqual(row["last_write_at"], "2026-09-13T06:00:00")
        statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
        self.assertNotIn(" delete ", f" {statements.lower()} ")
        self.assertNotIn(" update ", f" {statements.lower()} ")
        self.assertNotIn(" insert ", f" {statements.lower()} ")
        self.assertNotIn(" alter ", f" {statements.lower()} ")
        self.assertEqual(len(log.messages), 2)


if __name__ == "__main__":
    unittest.main()
