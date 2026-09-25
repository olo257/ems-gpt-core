import pathlib
import tempfile
import unittest
from datetime import datetime
from threading import RLock


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from executor_service import (
    ExecutorAdapters,
    battery_import_guard_reason,
    battery_soc_guard_actions,
    build_executor,
    scalar_number,
)


class ExecutorServiceTests(unittest.TestCase):
    def test_sql_floor_scalar_does_not_use_ha_state_parser(self):
        self.assertEqual(scalar_number(55.65), 55.65)
        self.assertEqual(scalar_number("40.00"), 40.0)
        self.assertIsNone(scalar_number(None))
        self.assertIsNone(scalar_number({"state": "40"}))

    def test_import_requires_headroom_and_material_planned_energy(self):
        self.assertIsNone(battery_import_guard_reason(60.0, 75.0, 0.5, 0.02))
        self.assertIn("TARGET_ALREADY_REACHED", battery_import_guard_reason(75.0, 75.0, 0.5, 0.02))
        self.assertIn("FLOW_BELOW_THRESHOLD", battery_import_guard_reason(60.0, 75.0, 0.02, 0.02))
        self.assertEqual(
            battery_import_guard_reason(None, 75.0, 0.5, 0.02),
            "IMPORT_PLAN_OR_SOC_UNAVAILABLE",
        )

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

    def test_manual_override_is_indefinite_until_mode_changes(self):
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
            result = service.update_process_override({
                "process": "HP_HEAT_DHW", "state": "FORCE_ON", "minutes": 1,
            })
            self.assertIsNone(result["valid_until"])
            self.assertTrue(result["indefinite"])
            insert_params = executed[1][1]
            self.assertEqual(insert_params[1], "HP_HEAT_DHW")
            self.assertEqual(insert_params[2], "FORCE_ON")

    def test_program_soc_target_is_persisted_and_restored(self):
        states = {"number.inverter_program_4_soc": {"state": "100"}}
        calls = []

        def service(domain, action, data, **_kwargs):
            calls.append((domain, action, data.copy()))
            states[data["entity_id"]] = {"state": str(data["value"])}
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            service_under_test = build_executor(ExecutorAdapters(
                options={"executor_enabled": True, "executor_dry_run": False,
                         **{f"deye_program_{program}_soc_pct":
                            (40 if program == 4 else 10)
                            for program in range(1, 7)}},
                operational_settings={},
                runtime_settings_path=pathlib.Path(directory) / "runtime-settings.json",
                lock=RLock(), state={"modules": {}, "executor": "LIVE"},
                record_event=lambda *args: None, local_now=datetime.now,
                db=lambda: None, slot_start=lambda value: value,
                tou_program_snapshot=lambda: [{"program": 4, "soc": 40}],
                active_tou_program=lambda *_args: {"program": 4, "soc": 40},
                number=lambda value: float(value["state"]) if value else None,
                ha_state=lambda entity: states.get(entity),
                ha_service_response=service,
            ))
            update = service_under_test.set_active_program_target(datetime.now(), 82.0)
            self.assertEqual(update["original_soc_pct"], 40.0)
            self.assertEqual(states["number.inverter_program_4_soc"]["state"], "82")
            # A later slot must retain the first baseline, not snapshot the EMS value.
            service_under_test.set_active_program_target(datetime.now(), 90.0)
            restored = service_under_test.restore_program_targets()
            self.assertEqual(restored[0]["restored_soc_pct"], 40.0)
            self.assertEqual(states["number.inverter_program_4_soc"]["state"], "40")
            self.assertFalse((pathlib.Path(directory) / "battery_program_soc_restore.json").read_text().strip() == "")
            self.assertEqual(calls[-1][2]["value"], 40)

    def test_restart_ignores_stale_restore_soc_and_uses_current_addon_option(self):
        states = {"number.inverter_program_1_soc": {"state": "20"}}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "battery_program_soc_restore.json").write_text('{"1":20}')

            def service_call(_domain, _action, data, **_kwargs):
                states[data["entity_id"]] = {"state": str(data["value"])}
                return {"ok": True}

            executor = build_executor(ExecutorAdapters(
                options={f"deye_program_{program}_soc_pct":
                         (10 if program == 1 else 30)
                         for program in range(1, 7)},
                operational_settings={},
                runtime_settings_path=path / "runtime-settings.json",
                lock=RLock(), state={"modules": {}, "executor": "LIVE"},
                record_event=lambda *args: None, local_now=datetime.now,
                db=lambda: None, slot_start=lambda value: value,
                tou_program_snapshot=lambda: [],
                active_tou_program=lambda *_args: {"program": 1, "soc": 20},
                number=lambda value: float(value["state"]) if value else None,
                ha_state=lambda entity: states.get(entity),
                ha_service_response=service_call,
            ))

            restored = executor.restore_program_targets()
            self.assertEqual(restored[0]["restored_soc_pct"], 10.0)
            self.assertEqual(states["number.inverter_program_1_soc"]["state"], "10")
            self.assertEqual((path / "battery_program_soc_restore.json").read_text(), "{}")

    def test_program_soc_is_not_restored_while_grid_or_export_is_active(self):
        states = {
            "number.inverter_program_4_soc": {"state": "25"},
            "switch.inverter_battery_grid_charging": {"state": "on"},
            "select.inverter_work_mode": {"state": "Zero Export To Load"},
            "select.inverter_program_4_charging": {"state": "Disabled"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "battery_program_soc_restore.json").write_text('{"4":40}')
            service = build_executor(ExecutorAdapters(
                options={}, operational_settings={},
                runtime_settings_path=path / "runtime-settings.json", lock=RLock(),
                state={"modules": {}, "executor": "LIVE"}, record_event=lambda *args: None,
                local_now=datetime.now, db=lambda: None, slot_start=lambda value: value,
                tou_program_snapshot=lambda: [], active_tou_program=lambda *_args: None,
                number=lambda value: float(value["state"]) if value else None,
                ha_state=lambda entity: states.get(entity),
                ha_service_response=lambda *_args, **_kwargs: {"ok": True},
            ))
            self.assertEqual(service.restore_program_targets_if_idle(), [])
            states["switch.inverter_battery_grid_charging"] = {"state": "off"}
            states["select.inverter_work_mode"] = {"state": "Export First"}
            self.assertEqual(service.restore_program_targets_if_idle(), [])

    def test_program_soc_restore_requires_program_grid_disabled(self):
        states = {
            "number.inverter_program_4_soc": {"state": "25"},
            "switch.inverter_battery_grid_charging": {"state": "off"},
            "select.inverter_work_mode": {"state": "Zero Export To Load"},
            "select.inverter_program_4_charging": {"state": "Grid"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "battery_program_soc_restore.json").write_text('{"4":40}')
            service = build_executor(ExecutorAdapters(
                options={}, operational_settings={}, runtime_settings_path=path / "runtime-settings.json",
                lock=RLock(), state={"modules": {}, "executor": "LIVE"}, record_event=lambda *args: None,
                local_now=datetime.now, db=lambda: None, slot_start=lambda value: value,
                tou_program_snapshot=lambda: [{"program": 4, "soc": 25}],
                active_tou_program=lambda *_args: {"program": 4, "soc": 25},
                number=lambda value: float(value["state"]) if value else None,
                ha_state=lambda entity: states.get(entity),
                ha_service_response=lambda *_args, **_kwargs: {"ok": True},
            ))
            self.assertEqual(service.restore_program_targets_if_idle(), [])


if __name__ == "__main__":
    unittest.main()
