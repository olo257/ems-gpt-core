import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import planner_hp_service


class HeatPumpProfileImportTests(unittest.TestCase):
    def test_json_parser_is_available_for_heat_profile_loading(self):
        self.assertEqual(
            planner_hp_service.json.loads('{"source":"forecast"}'),
            {"source": "forecast"},
        )


if __name__ == "__main__":
    unittest.main()
