import pathlib
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database_service import build_database


class DatabaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.database = build_database({}, ZoneInfo("Europe/Warsaw"))

    def test_qname_accepts_expected_identifiers(self):
        self.assertEqual(self.database.qname("ems_gpt_core_events"), "`ems_gpt_core_events`")

    def test_qname_rejects_sql_syntax(self):
        for value in ("events;DROP", "schema.table", "events name", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.database.qname(value)


if __name__ == "__main__":
    unittest.main()
