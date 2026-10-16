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
    flexible_command_plan_version,
    flexible_surplus_runtime_decisions,
    flexible_process_is_allowed,
    scalar_number,
)


class ExecutorServiceTests(unittest.TestCase):
    def test_flexible_runtime_requires_explicit_planner_allow(self):
        blocked = [{"process_name": "PV_CWU", "eligible": 0, "decision": "BLOCK"}]
        allowed = [{"process_name": "PV_CWU", "eligible": 1, "decision": "ALLOW"}]
        self.assertFalse(flexible_process_is_allowed(blocked, "PV_CWU"))
        self.assertFalse(flexible_process_is_allowed(blocked, "PV_EV"))
        self.assertTrue(flexible_process_is_allowed(allowed, "PV_CWU"))
        self.assertFalse(flexible_process_is_allowed(
            [{"process_name": "PV_CWU", "eligible": 1, "decision": "BLOCK"}],
            "PV_CWU"))

    def test_live_surplus_transition_key_is_stable_and_fits_database(self):
        value = flexible_command_plan_version(
            "1" * 36, "2" * 36, "3" * 32, "ON")
        self.assertLessEqual(len(value), 80)
        self.assertEqual(value, flexible_command_plan_version(
            "1" * 36, "2" * 36, "3" * 32, "ON"))

    def test_live_surplus_keeps_cwu_and_ev_outside_load_and_soc(self):
        decision = flexible_surplus_runtime_decisions(
            pv_power_w=7000, load_power_w=1500, battery_charge_power_w=1000,
            ev_power_w=0, pv_cwu_on=False, live_soc_pct=100,
            target_soc_pct=70, cwu_allowed=True, ev_allowed=True,
            cwu_threshold_kw=2.0, ev_threshold_kw=1.5)
        self.assertTrue(decision["pv_cwu"])
        self.assertTrue(decision["pv_ev"])
        self.assertEqual(decision["surplus_kw"], 5.5)
        self.assertEqual(decision["after_cwu_kw"], 3.5)

    def test_live_surplus_applies_cwu_before_ev(self):
        decision = flexible_surplus_runtime_decisions(
            pv_power_w=4840, load_power_w=1425, battery_charge_power_w=3236,
            ev_power_w=0, pv_cwu_on=False, live_soc_pct=100,
            target_soc_pct=70, cwu_allowed=True, ev_allowed=True,
            cwu_threshold_kw=2.0, ev_threshold_kw=1.5)
        self.assertTrue(decision["pv_cwu"])
        self.assertFalse(decision["pv_ev"])
        self.assertEqual(decision["surplus_kw"], 3.415)
        self.assertEqual(decision["battery_charge_kw_observed"], 3.236)

    def test_live_surplus_fails_closed_below_full_soc_or_without_telemetry(self):
        below = flexible_surplus_runtime_decisions(
            pv_power_w=7000, load_power_w=1000, battery_charge_power_w=0,
            ev_power_w=0, pv_cwu_on=False, live_soc_pct=99.9,
            target_soc_pct=70, cwu_allowed=True, ev_allowed=True,
            cwu_threshold_kw=2.0, ev_threshold_kw=1.5)
        missing = flexible_surplus_runtime_decisions(
            pv_power_w=None, load_power_w=1000, battery_charge_power_w=0,
            ev_power_w=0, pv_cwu_on=False, live_soc_pct=80,
            target_soc_pct=70, cwu_allowed=True, ev_allowed=True,
            cwu_threshold_kw=2.0, ev_threshold_kw=1.5)
        self.assertEqual((below["pv_cwu"], below["pv_ev"]), (False, False))
        self.assertEqual((missing["pv_cwu"], missing["pv_ev"]), (False, False))
        self.assertEqual(below["reason"], "LIVE_SOC_BELOW_100_PERCENT")

    def test_live_surplus_hysteresis_adds_back_running_flexible_loads(self):
        decision = flexible_surplus_runtime_decisions(
            pv_power_w=5000, load_power_w=5500, battery_charge_power_w=0,
            ev_power_w=1500, pv_cwu_on=True, live_soc_pct=100,
            target_soc_pct=70, cwu_allowed=True, ev_allowed=True,
            cwu_threshold_kw=2.0, ev_threshold_kw=1.5,
            hysteresis_ratio=0.80)
        self.assertTrue(decision["pv_cwu"])
        self.assertFalse(decision["pv_ev"])

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

    def test_battery_import_guard_uses_charge_target_instead_of_slot_close_soc(self):
        # A slot may plan to end above its required charging target. Import
        # must stop at the target while export keeps its separate endpoint.
        self.assertEqual(
            battery_soc_guard_actions(61.0, 80.0, True, False,
                                      import_target_soc=60.0),
            (True, False),
        )
        self.assertEqual(
            battery_soc_guard_actions(59.0, 80.0, True, False,
                                      import_target_soc=60.0),
            (False, False),
        )
        self.assertEqual(
            battery_soc_guard_actions(50.0, 80.0, True, False,
                                      import_target_soc=None),
            (True, False),
        )

    def build_service(self, directory, db=lambda: None, slot_start_fn=None):
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
            slot_start=slot_start_fn or (lambda value: value),
            tou_program_snapshot=lambda: [],
            active_tou_program=lambda *args: None,
            number=lambda value: value,
            ha_state=lambda entity: None,
            ha_service_response=lambda *args, **kwargs: None,
        ))

    def test_planner_failure_stages_off_for_extra_loads_and_selects_dhw(self):
        executed = []
        class Cursor:
            rowcount = 0
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, params=()):
                self.rowcount = 1 if "INSERT IGNORE INTO ems_gpt_core_commands" in sql else 0
                executed.append((" ".join(sql.split()), params, self.rowcount))
            def fetchone(self):
                return {"slot_id": "20261003T1900Z"}

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def cursor(self): return Cursor()

        now = datetime.now().replace(second=0, microsecond=0)
        current_slot = now.replace(minute=(now.minute // 15) * 15)
        with tempfile.TemporaryDirectory() as directory:
            service = self.build_service(
                directory, db=Connection,
                slot_start_fn=lambda: current_slot)
            self.options.update({
                "executor_enabled": True,
                "executor_dry_run": False,
                "executor_activation_ack": "EMS_CONNECTOR_ACCEPTED",
            })
            self.state.update({
                "executor": "LIVE",
                "planner_failure_latched": "SOC_SAFETY_BRIDGE_UNREACHABLE:0",
            })
            result = service.stage_executor_commands()

        inserts = [row for row in executed
                   if "INSERT IGNORE INTO ems_gpt_core_commands" in row[0]]
        self.assertEqual(result["status"], "PLANNER_FAILURE_FAIL_SAFE")
        self.assertEqual(result["staged"], 3)
        self.assertEqual([row[1][3] for row in inserts],
                         ["PV_CWU", "PV_EV", "HP_HEAT_DHW"])
        self.assertTrue(all("'OFF'" in row[0] for row in inserts))
        self.assertTrue(all(row[1][6] == "READY_FOR_CONNECTOR" for row in inserts))
        self.assertTrue(all("PLANNER_FAIL_SAFE" in row[0] for row in inserts))
        self.assertFalse(any("SELECT d.*" in row[0] for row in executed))

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


class BatteryDispatchConflictTests(unittest.TestCase):
    def dispatch(self, directory, commands, grid_on=False, fail_off=False,
                 authorization=None, live_soc="22", telemetry=True,
                 fail_grid_write=False, latched=False, switch_off_only=False,
                 registers_match=False):
        calls = []
        events = []
        self.events = events
        now = datetime(2026, 10, 14, 13, 0)
        states = {
            "sensor.inverter_battery": {"state": live_soc} if live_soc is not None else None,
            "switch.inverter_battery_grid_charging": {"state": "on" if grid_on else "off"},
            "select.inverter_work_mode": {"state": "Zero Export To Load"},
        }
        for program in range(1, 7):
            states[f"number.inverter_program_{program}_soc"] = {
                "state": "32" if registers_match or switch_off_only else "80"}
            states[f"select.inverter_program_{program}_charging"] = {
                "state": "Grid" if grid_on else "Disabled"}
        if switch_off_only:
            states["switch.inverter_battery_grid_charging"] = {"state": "off"}
        class Cursor:
            rowcount = 1
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, sql, params=()): self.sql = sql
            def fetchall(self): return commands
            def fetchone(self):
                if "SELECT c.*,s.plan_run_id" in self.sql:
                    return authorization
                if "SELECT captured_at FROM" in self.sql:
                    return {"captured_at": now} if telemetry else None
                return {"soc_charge_target_pct": 32.4, "soc_target_pct": 32.4,
                        "soc_end_plan_pct": 27.1, "planned_buy_kwh": 1.239796}
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def cursor(self): return Cursor()
        def service(domain, action, data):
            calls.append((domain, action, dict(data)))
            if domain == "script":
                if action == "export_off" and fail_off: return None
                if action == "import_on":
                    states["switch.inverter_battery_grid_charging"] = {"state": "on"}
                elif action == "import_off":
                    states["switch.inverter_battery_grid_charging"] = {"state": "off"}
                elif action == "export_on":
                    states["select.inverter_work_mode"] = {"state": "Export First"}
            elif domain == "select":
                if not fail_grid_write or data["option"] != "Grid":
                    states[data["entity_id"]] = {"state": data["option"]}
            elif domain == "number":
                states[data["entity_id"]] = {"state": str(data["value"])}
            return {"ok": True}
        executor = build_executor(ExecutorAdapters(
            options={"executor_enabled": True, "executor_dry_run": False,
                     "executor_activation_ack": "EMS_CONNECTOR_ACCEPTED",
                     "executor_battery_import_on_script": "script.import_on",
                     "executor_battery_import_off_script": "script.import_off",
                     "executor_battery_export_on_script": "script.export_on",
                     "executor_battery_export_off_script": "script.export_off",
                     **{f"deye_program_{p}_soc_pct": 10 for p in range(1, 7)}},
            operational_settings={}, runtime_settings_path=pathlib.Path(directory) / "runtime.json",
            lock=RLock(), state={"modules": {}, "planner_failure_latched": latched},
            record_event=lambda *args: events.append(args),
            local_now=lambda: now, db=Connection, slot_start=lambda: now,
            tou_program_snapshot=lambda: [], active_tou_program=lambda *_: {"program": 3},
            number=lambda value: float(value["state"]) if value else None,
            ha_state=lambda entity: states.get(entity), ha_service_response=service))
        return executor.dispatch_ready_commands(), states, calls

    def command(self, process, decision):
        return {"command_id": process, "process_name": process, "decision": decision,
                "plan_version": "plan:ppd", "source": "PLAN"}

    def test_buy_survives_sell_off_in_same_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            _, states, calls = self.dispatch(directory, [
                self.command("BATTERY_IMPORT", "ON"), self.command("SELL_BAT", "OFF")])
        for program in range(1, 7):
            self.assertEqual(states[f"select.inverter_program_{program}_charging"]["state"], "Grid")
            self.assertEqual(states[f"number.inverter_program_{program}_soc"]["state"], "32")
        self.assertEqual(states["switch.inverter_battery_grid_charging"]["state"], "on")
        self.assertEqual([call[1] for call in calls if call[0] == "script"], ["export_off", "import_on"])

    def test_later_sell_off_preserves_running_buy_without_new_on(self):
        with tempfile.TemporaryDirectory() as directory:
            _, states, calls = self.dispatch(directory, [self.command("SELL_BAT", "OFF")], grid_on=True)
        self.assertEqual(states["select.inverter_program_3_charging"]["state"], "Grid")
        self.assertFalse(any(call[2].get("option") == "Disabled" for call in calls))

    def test_import_off_disables_grid_source(self):
        with tempfile.TemporaryDirectory() as directory:
            _, states, _ = self.dispatch(directory, [self.command("BATTERY_IMPORT", "OFF")], grid_on=True)
        for program in range(1, 7):
            self.assertEqual(states[f"select.inverter_program_{program}_charging"]["state"], "Disabled")
            self.assertEqual(states[f"number.inverter_program_{program}_soc"]["state"], "10")
        self.assertEqual(states["switch.inverter_battery_grid_charging"]["state"], "off")

    def test_failed_off_blocks_new_buy(self):
        with tempfile.TemporaryDirectory() as directory:
            result, states, calls = self.dispatch(directory, [
                self.command("BATTERY_IMPORT", "ON"), self.command("SELL_BAT", "OFF")], fail_off=True)
        self.assertEqual(result["dispatched"], 0)
        self.assertFalse(any(call[1] == "import_on" for call in calls))
        self.assertEqual(states["switch.inverter_battery_grid_charging"]["state"], "off")

    def authorization(self, **changes):
        row = {**self.command("BATTERY_IMPORT", "ON"), "status": "DISPATCHED",
               "expires_at": datetime(2026, 10, 14, 13, 16),
               "plan_run_id": "plan", "plan_stage": "PUBLISHED", "buy_window": 1,
               "sale_window": 0, "grid_policy_planned": "BUY_ALLOWED",
               "planned_buy_kwh": 1.239796, "charge_target": 32.4,
               "eligible": 1, "ppd_decision": "BUY_ALLOWED", "requested_state": None}
        return {**row, **changes}

    def test_buy_restarts_after_registers_revert_without_new_command(self):
        # Reproduce the incident: ON already dispatched; inverter reverts to
        # OFF/Disabled while SOC remains below the window's charging target.
        with tempfile.TemporaryDirectory() as directory:
            _, states, calls = self.dispatch(directory, [], authorization=self.authorization())
        self.assertEqual(states["switch.inverter_battery_grid_charging"]["state"], "on")
        for p in range(1, 7):
            self.assertEqual(states[f"select.inverter_program_{p}_charging"]["state"], "Grid")
            self.assertEqual(states[f"number.inverter_program_{p}_soc"]["state"], "32")
        self.assertEqual(self.events[-1][0], "battery_import_reconciled")
        self.assertTrue(self.events[-1][2]["confirmed"])

    def test_buy_repair_requires_current_permission_and_fresh_telemetry(self):
        denied = [dict(decision="OFF"), dict(status="REJECTED"),
                  dict(status="FAILED"), dict(requested_state="FORCE_OFF"),
                  dict(plan_version="old:ppd"), dict(plan_stage="REJECTED"),
                  dict(buy_window=0), dict(sale_window=1), dict(eligible=0), dict(eligible=None),
                  dict(ppd_decision="NO_BUY"), dict(grid_policy_planned="NO_BUY"),
                  dict(planned_buy_kwh=0), dict(charge_target=None),
                  dict(charge_target=float("nan")),
                  dict(expires_at=datetime(2026, 10, 14, 12, 59))]
        for changes in denied:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                _, _, calls = self.dispatch(directory, [], authorization=self.authorization(**changes))
                self.assertEqual(calls, [])
        for kwargs in ({"telemetry": False}, {"latched": True},
                       {"live_soc": None}, {"live_soc": "nan"}, {"live_soc": "33"}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                _, _, calls = self.dispatch(directory, [], authorization=self.authorization(), **kwargs)
                self.assertEqual(calls, [])

    def test_buy_readback_does_not_treat_accepted_write_as_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            self.dispatch(directory, [], authorization=self.authorization(), fail_grid_write=True)
        self.assertFalse(self.events[-1][2]["confirmed"])
        self.assertEqual(self.events[-1][3], "WARNING")

    def test_global_switch_off_is_repaired_even_when_all_programs_are_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            _, states, calls = self.dispatch(directory, [], grid_on=True,
                switch_off_only=True, authorization=self.authorization())
        self.assertEqual(states["switch.inverter_battery_grid_charging"]["state"], "on")
        self.assertEqual([(d, a) for d, a, _ in calls], [("script", "import_on")])
        self.assertEqual(self.events[-1][2]["before"]["grid"], "off")
        self.assertEqual(self.events[-1][2]["after"]["grid"], "on")

    def test_healthy_buy_does_not_rewrite_registers_every_minute(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, calls = self.dispatch(directory, [], grid_on=True,
                registers_match=True, authorization=self.authorization())
        self.assertEqual(calls, [])
        self.assertEqual(self.events, [])

    def test_soc_read_failure_stops_all_programs_and_explains_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            _, states, calls = self.dispatch(directory, [], grid_on=True, live_soc=None,
                                             authorization=self.authorization())
        self.assertEqual(states["switch.inverter_battery_grid_charging"]["state"], "off")
        self.assertTrue(all(states[f"select.inverter_program_{p}_charging"]["state"] == "Disabled"
                            for p in range(1, 7)))
        self.assertEqual(self.events[-1][2]["reason"], "IMPORT_SOC_UNAVAILABLE")
        self.assertEqual(self.events[-1][2]["import_target_soc"], 32.4)
        self.assertFalse(any(call[1] == "import_on" for call in calls))


if __name__ == "__main__":
    unittest.main()
