"""Tests for deterministic snapshot P/E shadow diagnostics."""
import unittest

from economic_shadow import ShadowSlot
from economic_shadow_integration import ShadowSnapshot, compare_snapshot


class ShadowIntegrationTests(unittest.TestCase):
    def snapshot(self):
        return ShadowSnapshot(
            slots=(
                ShadowSlot("17:45", 2, 1.8, False, True, 0, 0),
                ShadowSlot("23:45", 0.5, 0.1, True, False, 0, 0),
            ),
            initial_soc_pct=90,
            required_soc_pct=(15, 15),
            terminal_soc_pct=90,
            production_sell_kwh=(0, 0),
            production_buy_kwh=(0, 0),
            source_id="test:2026-10-13",
        )

    def test_repeatable_fingerprint_and_read_only_mode(self):
        first = compare_snapshot(self.snapshot())
        second = compare_snapshot(self.snapshot())
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(first["mode"], "SHADOW_READ_ONLY")
        self.assertTrue(first["production"]["feasible"])

    def test_incomplete_snapshot_rejected(self):
        original = self.snapshot()
        broken = ShadowSnapshot(original.slots, 90, (15,), 90,
                                (0, 0), (0, 0), "test")
        with self.assertRaises(ValueError):
            compare_snapshot(broken)

    def test_different_snapshot_has_different_fingerprint(self):
        original = self.snapshot()
        changed = ShadowSnapshot(original.slots, 91, original.required_soc_pct,
                                 original.terminal_soc_pct, original.production_sell_kwh,
                                 original.production_buy_kwh, original.source_id)
        self.assertNotEqual(compare_snapshot(original)["snapshot_sha256"],
                            compare_snapshot(changed)["snapshot_sha256"])


if __name__ == "__main__":
    unittest.main()
