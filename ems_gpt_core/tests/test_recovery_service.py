import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recovery_service import RecoveryAdapters, build_recovery
from materialization_service import MaterializationAdapters, build_materializations


class MissingSlotCursor:
    def __init__(self, slots):
        self.slots = slots
        self.result = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.rowcount = 0
        if "SELECT slot_start,slot_end FROM ems_gpt_slots" in sql:
            self.result = [row for row in self.slots if not row.get("closed")]
        elif "FROM ems_gpt_telemetry_snapshots" in sql:
            self.result = [{"samples": 0}]
        elif "UPDATE ems_gpt_slots SET actual_recorded_at" in sql:
            target = next(row for row in self.slots if row["slot_start"] == params[0])
            if not target.get("closed"):
                target["closed"] = True
                self.rowcount = 1
            self.result = []
        else:
            self.result = []

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0]


class FakeDatabase:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_value


class RecoveryServiceTests(unittest.TestCase):
    def test_bootstrap_is_disabled_by_default(self):
        service = build_recovery(RecoveryAdapters(
            options={"bootstrap_from_source": False},
            db=lambda: self.fail("database must not be opened"),
            qname=lambda value: value,
            record_event=lambda *args: None,
        ))
        self.assertEqual(service.bootstrap_legacy_tables(), 0)

    def test_multislot_outage_is_closed_once_without_inventing_measurements(self):
        from datetime import datetime, timedelta
        start = datetime(2026, 9, 13, 23, 45)
        slots = [{"slot_start": start + timedelta(minutes=15 * index),
                  "slot_end": start + timedelta(minutes=15 * (index + 1))}
                 for index in range(4)]
        cursor = MissingSlotCursor(slots)
        service = build_materializations(MaterializationAdapters(
            options={"telemetry_learning_coverage_pct": 80.0}, app_version="0.31.0",
            db=FakeDatabase(cursor), local_now=lambda: datetime(2026, 9, 14, 1, 0),
            slot_start=lambda: datetime(2026, 9, 14, 1, 0), record_event=lambda *args: None,
        ))
        self.assertEqual(service.close_finished_slots(), 4)
        self.assertEqual(service.close_finished_slots(), 0)
        self.assertTrue(all(row["closed"] for row in slots))

    def test_same_source_and_target_do_not_open_database(self):
        service = build_recovery(RecoveryAdapters(
            options={"bootstrap_from_source": True, "source_db": "ems", "db_name": "ems"},
            db=lambda: self.fail("database must not be opened"),
            qname=lambda value: value,
            record_event=lambda *args: None,
        ))
        self.assertEqual(service.bootstrap_legacy_tables(), 0)


if __name__ == "__main__":
    unittest.main()
