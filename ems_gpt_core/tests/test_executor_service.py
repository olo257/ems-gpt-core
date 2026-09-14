import pathlib
import tempfile
import unittest
from datetime import datetime
from threading import RLock


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from executor_service import ExecutorAdapters, battery_soc_guard_actions, build_executor


class ExecutorServiceTests(unittest.TestCase):
    def test_battery_soc_guard_stops_binary_scripts_at_planned_endpoint(self):
        self.assertEqual(battery_soc_guard_actions(60.0, 60.0, True, False), (True, False))
        self.assertEqual(battery_soc_guard_actions(30.0, 30.0, False, True), (False, True))
        self.assertEqual(battery_soc_guard_actions(45.0, 60.0, True, False), (False, False))
        self.assertEqual(battery_soc_guard_actions(None, 60.0, True, True), (True, True))

    def build_service(self, directory, db=lambda: None):
        self.options = {
            "executor_enabled": False,
            "executor_dry_run": True,
            "executor_activation_ack": "",
            "limit": 3.0,
            "hp_min_cycle_hours": 2.5,
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
            db=db,
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

    def test_hp_manual_duration_comes_only_from_configuration(self):
        executed = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params=()):
                executed.append((sql, params))

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return Cursor()

        with tempfile.TemporaryDirectory() as directory:
            service = self.build_service(directory, db=Connection)
            started = datetime.now()
            result = service.update_process_override({
                "process": "HP_HEAT_DHW", "state": "FORCE_ON", "minutes": 1,
            })
            duration = (result["valid_until"] - started).total_seconds() / 60
            self.assertGreaterEqual(duration, 149.9)
            self.assertLessEqual(duration, 150.1)
            insert_params = executed[1][1]
            self.assertEqual(insert_params[1], "HP_HEAT_DHW")
            self.assertEqual(insert_params[2], "FORCE_ON")


if __name__ == "__main__":
    unittest.main()
