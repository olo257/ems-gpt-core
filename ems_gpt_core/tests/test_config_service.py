import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_service import DEFAULT_OPTIONS, deye_program_soc_baselines, load_options


class ConfigServiceTests(unittest.TestCase):
    def test_defaults_are_returned_when_files_are_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            result = load_options(root / "options.json", root / "runtime.json")
        self.assertEqual(result["timezone"], "Europe/Warsaw")
        self.assertEqual(result["slot_minutes"], 15)
        self.assertFalse(result["executor_enabled"])
        self.assertEqual(result["soc_target_history_weight_7d_pct"], 50.0)
        self.assertEqual(result["soc_target_history_weight_14d_pct"], 25.0)
        self.assertEqual(result["soc_target_history_weight_28d_pct"], 25.0)

    def test_runtime_settings_override_addon_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            options_path = root / "options.json"
            runtime_path = root / "runtime.json"
            options_path.write_text(json.dumps({"executor_enabled": False, "slot_minutes": 15}))
            runtime_path.write_text(json.dumps({"executor_enabled": True}))
            result = load_options(options_path, runtime_path)
        self.assertTrue(result["executor_enabled"])
        self.assertEqual(result["slot_minutes"], 15)

    def test_loading_does_not_mutate_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            options_path = root / "options.json"
            options_path.write_text(json.dumps({"timezone": "UTC"}))
            load_options(options_path, root / "runtime.json")
        self.assertEqual(DEFAULT_OPTIONS["timezone"], "Europe/Warsaw")

    def test_addon_soc_and_garden_entity_cannot_be_shadowed_by_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            options_path = root / "options.json"
            runtime_path = root / "runtime.json"
            options_path.write_text(json.dumps({
                "deye_program_1_soc_pct": 10,
                "garden_temperature_entity": "sensor.garden_actual",
            }))
            runtime_path.write_text(json.dumps({
                "deye_program_1_soc_pct": 20,
                "deye_program_soc_baseline_json": '{"1":20}',
                "garden_temperature_entity": "sensor.stale_garden",
            }))
            result = load_options(options_path, runtime_path)
        self.assertEqual(result["deye_program_1_soc_pct"], 10)
        self.assertEqual(result["garden_temperature_entity"], "sensor.garden_actual")
        self.assertNotIn("deye_program_soc_baseline_json", result)

    def test_program_soc_defaults_are_individual_and_validated(self):
        baselines = deye_program_soc_baselines(DEFAULT_OPTIONS)
        self.assertEqual(baselines, {
            "1": 10.0, "2": 10.0, "3": 10.0,
            "4": 10.0, "5": 10.0, "6": 30.0,
        })


if __name__ == "__main__":
    unittest.main()
