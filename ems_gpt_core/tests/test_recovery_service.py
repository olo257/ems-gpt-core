import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recovery_service import RecoveryAdapters, build_recovery


class RecoveryServiceTests(unittest.TestCase):
    def test_bootstrap_is_disabled_by_default(self):
        service = build_recovery(RecoveryAdapters(
            options={"bootstrap_from_source": False},
            db=lambda: self.fail("database must not be opened"),
            qname=lambda value: value,
            record_event=lambda *args: None,
        ))
        self.assertEqual(service.bootstrap_legacy_tables(), 0)

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
