import pathlib
import sqlite3
import sys
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_service import elapsed_hp_plan_states, optimize_hp_heating_slots, strict_database_bool


class HistoryCursor:
    def __init__(self, conn):
        self.cursor = conn.cursor()

    def execute(self, sql, params):
        self.cursor.execute(sql.replace("%s", "?"),
                            tuple(value.isoformat(sep=" ") for value in params))

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


class HeatPumpPlanHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE ems_gpt_slots "
                          "(slot_start TEXT, plan_published INTEGER, heat_pump_window INTEGER)")
        self.addCleanup(self.conn.close)
        self.start = datetime(2026, 10, 1)
        self.cutoff = datetime(2026, 10, 1, 7, 15)

    def insert(self, slot, published, window):
        self.conn.execute("INSERT INTO ems_gpt_slots VALUES (?,?,?)", (slot, published, window))

    def read(self):
        return elapsed_hp_plan_states(HistoryCursor(self.conn), self.start, self.cutoff)

    def test_null_history_does_not_abort_or_grant_heating_credit(self):
        self.insert("2026-10-01 07:00:00", 1, None)
        states, unknown = self.read()
        self.assertEqual(states, [False])
        self.assertEqual(unknown[0]["reason"], "MISSING_HP_WINDOW")
        rows = [{"forecast_pv_total_kwh": 0, "forecast_load_kwh": 0,
                 "price_buy_pln_kwh": 1, "price_sell_pln_kwh": 0}] * 8
        selected = optimize_hp_heating_slots(rows, states, 8, 8, 4, 12, 1.5, 0.25)
        self.assertEqual(len(selected), 8)
        # Reading history must not invent a persisted HP decision.
        self.assertIsNone(self.conn.execute("SELECT heat_pump_window FROM ems_gpt_slots").fetchone()[0])

    def test_only_published_known_windows_count_and_range_is_half_open(self):
        self.insert("2026-09-30 23:45:00", 1, 1)
        self.insert("2026-10-01 00:00:00", 1, 1)
        self.insert("2026-10-01 00:15:00", 1, 0)
        self.insert("2026-10-01 00:30:00", 0, 1)
        self.insert("2026-10-01 00:45:00", None, None)
        self.insert("2026-10-01 07:15:00", 1, 1)
        states, unknown = self.read()
        self.assertEqual(states, [True, False, False, False])
        self.assertEqual([row["reason"] for row in unknown], ["UNPUBLISHED", "UNPUBLISHED"])

    def test_invalid_published_value_still_rejected(self):
        self.insert("2026-10-01 07:00:00", 1, 2)
        with self.assertRaisesRegex(RuntimeError, "INVALID_BOOLEAN:heat_pump_window:2"):
            self.read()

    def test_unknown_history_exception_does_not_weaken_current_flags(self):
        for field in ("buy_window", "sale_window", "heat_pump_window"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, "INVALID_BOOLEAN"):
                    strict_database_bool(None, field)


if __name__ == "__main__":
    unittest.main()
