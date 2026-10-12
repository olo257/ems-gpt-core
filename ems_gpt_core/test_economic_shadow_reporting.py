"""Isolated read-only tests for economic shadow diagnostics."""
import tempfile
import unittest
from pathlib import Path

from economic_shadow_reporting import (
    snapshot_from_records, write_shadow_report,
)


class ShadowReportingTests(unittest.TestCase):
    def records(self):
        return [
            dict(start="2026-10-13T17:45:00+02:00", buy_price=2.1,
                 sell_price=1.5, buy_allowed=False, sell_allowed=True,
                 load_kwh=0, pv_kwh=0, soc_required_pct=15,
                 production_sell_kwh=0, production_buy_kwh=0),
            dict(start="2026-10-13T23:45:00+02:00", buy_price=0.5,
                 sell_price=0.1, buy_allowed=True, sell_allowed=False,
                 load_kwh=0, pv_kwh=0, soc_required_pct=15,
                 production_sell_kwh=0, production_buy_kwh=0),
        ]

    def test_snapshot_contains_production_baseline(self):
        snapshot = snapshot_from_records(self.records(), "test", 90, 90)
        self.assertEqual(len(snapshot.slots), 2)
        self.assertEqual(snapshot.production_sell_kwh, (0.0, 0.0))

    def test_missing_column_fails_closed(self):
        rows = self.records()
        del rows[0]["pv_kwh"]
        with self.assertRaises(ValueError):
            snapshot_from_records(rows, "test", 90, 90)

    def test_order_is_validated(self):
        with self.assertRaises(ValueError):
            snapshot_from_records(list(reversed(self.records())), "test", 90, 90)

    def test_writer_cannot_overwrite_or_traverse(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = write_shadow_report({"mode": "SHADOW_READ_ONLY"},
                                              folder, "sample.json")
            self.assertTrue(destination.exists())
            with self.assertRaises(FileExistsError):
                write_shadow_report({}, folder, "sample.json")
            with self.assertRaises(ValueError):
                write_shadow_report({}, folder, "../unsafe.json")


if __name__ == "__main__":
    unittest.main()
