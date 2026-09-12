import pathlib
import tempfile
import unittest
from datetime import datetime
from threading import RLock


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from executor_service import ExecutorAdapters, build_executor


class ExecutorServiceTests(unittest.TestCase):
    def build_service(self, directory):
        self.options = {
            "executor_enabled": False,
            "executor_dry_run": True,
            "executor_activation_ack": "",
            "limit": 3.0,
        }
        self.state = {"modules": {}, "executor": "OFF"}
        return build_executor(ExecutorAdapters(
            options=self.options,
            operational_settings={"limit": (0.0, 10.0, "Limit", "test")},
            runtime_settings_path=pathlib.Path(directory) / "runtime-settings.json",
            lock=RLock(),
            state=self.state,
            record_event=lambda *args: None,
            local_now=datetime.now,
            db=lambda: None,
            slot_start=lambda value: value,
            tou_program_snapshot=lambda: [],
            active_tou_program=lambda *args: None,
            number=lambda value: value,
            ha_state=lambda entity: None,
            ha_service_response=lambda *args, **kwargs: None,
        ))

    def test_settings_are_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build_service(directory)
            result = service.update_operational_settings({"limit": 4})
            self.assertEqual(result["changed"], {"limit": 4.0})
            self.assertEqual(service.settings_payload()["limit"]["value"], 4.0)
            with self.assertRaisesRegex(ValueError, "between 0.0 and 10.0"):
                service.update_operational_settings({"limit": 11})

    def test_off_mode_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.build_service(directory)
            result = service.update_executor_mode({"mode": "OFF"})
            self.assertEqual(result, {"mode": "OFF"})
            self.assertFalse(self.options["executor_enabled"])
            self.assertTrue(self.options["executor_dry_run"])
            self.assertEqual(self.state["executor"], "OFF")


if __name__ == "__main__":
    unittest.main()
