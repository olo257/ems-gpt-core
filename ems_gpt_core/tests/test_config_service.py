import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config_service import DEFAULT_OPTIONS, load_options


class ConfigServiceTests(unittest.TestCase):
    def test_defaults_are_returned_when_files_are_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            result = load_options(root / "options.json", root / "runtime.json")
        self.assertEqual(result["timezone"], "Europe/Warsaw")
        self.assertEqual(result["slot_minutes"], 15)
        self.assertFalse(result["executor_enabled"])

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


if __name__ == "__main__":
    unittest.main()
