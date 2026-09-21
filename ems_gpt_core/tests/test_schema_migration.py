from __future__ import annotations

import unittest
from datetime import date

import schema_service


class FakeConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeCursor:
    def __init__(self, days: int = 730):
        self.days = days
        self.rowcount = 0
        self._row = None
        self.updated_ranges = []
        self.marker_written = False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if "FROM ems_gpt_core_migrations WHERE migration_key" in normalized:
            self._row = {"n": 0}
        elif "FROM information_schema.statistics" in normalized:
            self._row = {"n": 1}
        elif "DATE(MIN(slot_start))" in normalized:
            self._row = {
                "first_day": date(2024, 1, 1),
                "last_day": date(2024, 1, 1).fromordinal(
                    date(2024, 1, 1).toordinal() + self.days - 1
                ),
            }
        elif normalized.startswith("UPDATE"):
            start, end = params
            self.updated_ranges.append((start, end))
            self.rowcount = 96
        elif normalized.startswith("INSERT INTO ems_gpt_core_migrations"):
            self.marker_written = True
        else:
            raise AssertionError(f"Unexpected SQL: {normalized}")

    def fetchone(self):
        return self._row


class SlotIdMigrationRegressionTest(unittest.TestCase):
    def test_large_history_is_split_into_daily_restart_safe_batches(self):
        conn = FakeConnection()
        cur = FakeCursor(days=730)

        result = schema_service._backfill_slot_ids_batched(conn, cur)

        expected_batches = 730 * len(schema_service._SLOT_ID_TABLES)
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(result["batches"], expected_batches)
        self.assertEqual(len(cur.updated_ranges), expected_batches)
        self.assertTrue(all((end - start).days == 1 for start, end in cur.updated_ranges))
        self.assertEqual(conn.commits, expected_batches + 2)
        self.assertEqual(conn.rollbacks, 0)
        self.assertTrue(cur.marker_written)


if __name__ == "__main__":
    unittest.main()

