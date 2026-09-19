"""Guarded executor, operator overrides and command lifecycle."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


def scalar_number(value) -> float | None:
    """Parse a numeric SQL scalar without using the HA-state adapter."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def database_bool(value, field: str) -> bool:
    """Decode a SQL flag and reject ambiguous truthy representations."""
    if isinstance(value, (bytes, bytearray)):
        if value in (b"\x00", b"0"):
            return False
        if value in (b"\x01", b"1"):
            return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("0", "false"):
            return False
        if normalized in ("1", "true"):
            return True
    if value in (False, 0):
        return False
    if value in (True, 1):
        return True
    raise RuntimeError(f"EXECUTOR_INVALID_DATABASE_BOOL:{field}:{value!r}")


def battery_soc_guard_actions(live_soc: float | None, planned_end_soc: float | None,
                              import_active: bool, export_active: bool) -> tuple[bool, bool]:
    """Return (stop_import, stop_export) at the quantitative slot SOC boundary."""
    if live_soc is None or planned_end_soc is None:
        return bool(import_active), bool(export_active)
    return (
        bool(import_active and live_soc >= planned_end_soc),
        bool(export_active and live_soc <= planned_end_soc),
    )


def battery_import_guard_reason(live_soc, target, planned_buy, flow_threshold: float) -> str | None:
    """Reject grid mode unless the battery can absorb the planned purchase."""
    if target is None or live_soc is None or planned_buy is None:
        return "IMPORT_PLAN_OR_SOC_UNAVAILABLE"
    if float(planned_buy) <= float(flow_threshold):
        return f"IMPORT_FLOW_BELOW_THRESHOLD:{float(planned_buy):.6f}"
    if float(live_soc) >= float(target) - 0.01:
        return f"IMPORT_TARGET_ALREADY_REACHED:live={float(live_soc):.2f};target={float(target):.2f}"
    return None


@dataclass(frozen=True)
class ExecutorAdapters:
    options: dict
    operational_settings: dict
    runtime_settings_path: Path
    lock: Any
    state: dict
    record_event: Callable
    local_now: Callable
    db: Callable
    slot_start: Callable
    tou_program_snapshot: Callable
    active_tou_program: Callable
    number: Callable
    ha_state: Callable
    ha_service_response: Callable
    config_settings: dict | None = None


def build_executor(a: ExecutorAdapters):
    OPTIONS, OPERATIONAL_SETTINGS, CONFIG_SETTINGS = a.options, a.operational_settings, a.config_settings or {}

    process_script_keys = {
        "BATTERY_IMPORT": {"ON": "executor_battery_import_on_script", "OFF": "executor_battery_import_off_script"},
        "BATTERY_EXPORT": {"ON": "executor_battery_export_on_script", "OFF": "executor_battery_export_off_script"},
        "PV_CWU": {"ON": "executor_pv_cwu_on_script", "OFF": "executor_pv_cwu_off_script"},
        "PV_EV": {"ON": "executor_pv_ev_on_script", "OFF": "executor_pv_ev_off_script"},
        "HP_HEAT_DHW": {"ON": "executor_hp_heat_dhw_on_script", "OFF": "executor_hp_heat_dhw_off_script"},
    }

    def configured_service_map() -> dict:
        """Return explicit script settings, with the legacy JSON as migration fallback."""
        try:
            legacy = json.loads(str(OPTIONS.get("connector_service_map_json") or "{}"))
        except json.JSONDecodeError:
            legacy = {}
        service_map = {}
        for process, states in process_script_keys.items():
            service_map[process] = {}
            for state, key in states.items():
                explicit = str(OPTIONS.get(key) or "").strip()
                service_map[process][state] = explicit or legacy.get(process, {}).get(state)
        return service_map
    RUNTIME_SETTINGS_PATH, LOCK, STATE = a.runtime_settings_path, a.lock, a.state
    record_event, local_now, db, slot_start = a.record_event, a.local_now, a.db, a.slot_start
    tou_program_snapshot, active_tou_program = a.tou_program_snapshot, a.active_tou_program
    number, ha_state, ha_service_response = a.number, a.ha_state, a.ha_service_response
    program_restore_path = RUNTIME_SETTINGS_PATH.with_name("battery_program_soc_restore.json")

    def read_program_restore() -> dict[str, float]:
        try:
            payload = json.loads(program_restore_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return {str(key): float(value) for key, value in payload.items()}

    def write_program_restore(payload: dict[str, float]) -> None:
        temporary = program_restore_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, program_restore_path)

    def set_active_program_target(now: datetime, target_pct: float) -> dict:
        """Persist the original TOU target, then apply the quantitative plan target."""
        program = active_tou_program(now, tou_program_snapshot())
        if not program:
            raise RuntimeError("ACTIVE_TOU_PROGRAM_UNAVAILABLE")
        program_number = int(program["program"])
        entity_id = f"number.inverter_program_{program_number}_soc"
        live_value = number(ha_state(entity_id))
        if live_value is None:
            raise RuntimeError("ACTIVE_TOU_SOC_UNAVAILABLE")
        restore = read_program_restore()
        try:
            configured_baselines = json.loads(str(OPTIONS.get(
                "deye_program_soc_baseline_json", "{}")))
        except json.JSONDecodeError as exc:
            raise RuntimeError("DEYE_PROGRAM_SOC_BASELINE_INVALID") from exc
        configured_original = configured_baselines.get(str(program_number), live_value)
        restore.setdefault(str(program_number), float(configured_original))
        write_program_restore(restore)
        requested = max(0.0, min(100.0, float(target_pct)))
        response = ha_service_response("number", "set_value", {
            "entity_id": entity_id, "value": round(requested),
        })
        if response is None:
            raise RuntimeError("ACTIVE_TOU_SOC_WRITE_FAILED")
        return {"program": program_number, "entity_id": entity_id,
                "original_soc_pct": restore[str(program_number)],
                "target_soc_pct": round(requested)}

    def set_active_program_charging(now: datetime, option: str) -> dict:
        """Set the active Deye TOU program charging source explicitly."""
        program = active_tou_program(now, tou_program_snapshot())
        if not program:
            raise RuntimeError("ACTIVE_TOU_PROGRAM_UNAVAILABLE")
        program_number = int(program["program"])
        entity_id = f"select.inverter_program_{program_number}_charging"
        response = ha_service_response("select", "select_option", {
            "entity_id": entity_id, "option": option,
        })
        if response is None:
            raise RuntimeError("ACTIVE_TOU_CHARGING_WRITE_FAILED")
        return {"program": program_number, "entity_id": entity_id, "option": option}

    def restore_program_targets() -> list[dict]:
        """Restore every TOU target changed by EMS; retain failed entries for retry."""
        restore = read_program_restore()
        restored, pending = [], {}
        for program_number, original in restore.items():
            entity_id = f"number.inverter_program_{int(program_number)}_soc"
            response = ha_service_response("number", "set_value", {
                "entity_id": entity_id, "value": round(float(original)),
            })
            if response is None:
                pending[program_number] = original
            else:
                restored.append({"program": int(program_number),
                                 "entity_id": entity_id,
                                 "restored_soc_pct": float(original)})
        write_program_restore(pending)
        return restored

    def restore_program_targets_if_idle() -> list[dict]:
        """Restore only after Deye confirms that neither grid charge nor export is active."""
        grid_state = ha_state("switch.inverter_battery_grid_charging") or {}
        mode_state = ha_state("select.inverter_work_mode") or {}
        grid_off = str(grid_state.get("state") or "").lower() == "off"
        export_off = str(mode_state.get("state") or "") != "Export First"
        restore = read_program_restore()
        program_grid_disabled = all(
            str((ha_state(f"select.inverter_program_{int(program)}_charging") or {}).get("state") or "") == "Disabled"
            for program in restore
        )
        if not (grid_off and export_off and program_grid_disabled):
            return []
        return restore_program_targets()

    def settings_payload() -> dict:
        result = {key: {"value": float(OPTIONS[key]), "min": spec[0], "max": spec[1],
                        "step": 0.01, "type": "number", "label": spec[2], "group": spec[3]}
                  for key, spec in OPERATIONAL_SETTINGS.items()}
        for key, spec in CONFIG_SETTINGS.items():
            result[key] = {**spec, "value": OPTIONS.get(key)}
        return result
    
    
    def update_operational_settings(payload: dict) -> dict:
        changed = {}
        for key, raw in payload.items():
            if key not in OPERATIONAL_SETTINGS and key not in CONFIG_SETTINGS:
                raise ValueError(f"unknown setting: {key}")
            if key in OPERATIONAL_SETTINGS:
                low, high, _, _ = OPERATIONAL_SETTINGS[key]
                value = float(raw)
                if not low <= value <= high:
                    raise ValueError(f"{key} must be between {low} and {high}")
            else:
                spec = CONFIG_SETTINGS[key]
                setting_type = spec["type"]
                if setting_type == "boolean":
                    if not isinstance(raw, bool):
                        raise ValueError(f"{key} must be boolean")
                    value = raw
                elif setting_type == "number":
                    value = float(raw)
                    if not float(spec["min"]) <= value <= float(spec["max"]):
                        raise ValueError(f"{key} must be between {spec['min']} and {spec['max']}")
                    if float(spec.get("step", 0.01)).is_integer():
                        value = int(value)
                elif setting_type == "select":
                    value = str(raw)
                    if value not in spec["options"]:
                        raise ValueError(f"{key} has invalid option")
                else:
                    value = str(raw).strip()
                    if setting_type == "time":
                        try:
                            hour, minute = (int(part) for part in value.split(":"))
                        except (TypeError, ValueError):
                            raise ValueError(f"{key} must use HH:MM")
                        if not (0 <= hour <= 23 and 0 <= minute <= 59):
                            raise ValueError(f"{key} must use HH:MM")
                        value = f"{hour:02d}:{minute:02d}"
                    elif setting_type == "entity" and value and "." not in value:
                        raise ValueError(f"{key} must be an entity_id")
            changed[key] = value
        if not changed:
            raise ValueError("no settings supplied")
        current = {}
        try:
            current = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        candidate = {**OPTIONS, **current, **changed}
        history_weight_keys = (
            "soc_target_history_weight_7d_pct",
            "soc_target_history_weight_14d_pct",
            "soc_target_history_weight_28d_pct",
        )
        history_weight_sum = sum(float(candidate.get(key, 0.0)) for key in history_weight_keys)
        if (any(key in changed for key in history_weight_keys)
                and abs(history_weight_sum - 100.0) > 0.001):
            raise ValueError("Wagi końcowego SOC 7/14/28 dni muszą sumować się do 100%")
        if candidate.get("backup_enabled"):
            local_path = str(candidate.get("backup_local_directory") or "")
            omv_path = str(candidate.get("backup_omv_directory") or "")
            if not (local_path == "/backup" or local_path.startswith("/backup/")):
                raise ValueError("backup_local_directory must be under /backup")
            if not (omv_path == "/media" or omv_path.startswith("/media/")):
                raise ValueError("backup_omv_directory must be under /media")
            if local_path == omv_path:
                raise ValueError("local and OMV backup directories must differ")
        for appliance in ("dishwasher", "large_fridge", "small_fridge", "freezer", "washer", "dryer"):
            prefix = f"appliance_{appliance}_"
            if candidate.get(prefix + "enabled") and not str(candidate.get(prefix + "energy_entity") or "").strip():
                raise ValueError(f"{prefix}energy_entity is required when enabled")
        current.update(changed)
        temporary = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, RUNTIME_SETTINGS_PATH)
        with LOCK:
            OPTIONS.update(changed)
        record_event("operational_settings_updated", "core", {"changed": changed})
        return {"changed": changed, "settings": settings_payload()}
    
    
    def update_executor_mode(payload: dict, requested_by: str = "operator") -> dict:
        requested = str(payload.get("mode", "")).upper()
        if requested not in {"LIVE", "OFF"}:
            raise ValueError("mode must be LIVE or OFF")
        if requested == "LIVE":
            if payload.get("confirmation") != "EMS_CONNECTOR_ACCEPTED":
                raise ValueError("explicit executor confirmation required")
            service_map = configured_service_map()
            missing = [p for p in PROCESS_NAMES if not all(
                isinstance(service_map.get(p, {}).get(state), str)
                and service_map[p][state].startswith("script.")
                for state in ("ON", "OFF")
            )]
            if missing:
                raise ValueError("missing safe script mapping: " + ", ".join(missing))
            changed = {
                "executor_enabled": True,
                "executor_dry_run": False,
                "executor_activation_ack": "EMS_CONNECTOR_ACCEPTED",
            }
            state = "LIVE"
        else:
            changed = {
                "executor_enabled": False,
                "executor_dry_run": True,
                "executor_activation_ack": "",
            }
            state = "OFF"
        try:
            current = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = {}
        current.update(changed)
        temporary = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, RUNTIME_SETTINGS_PATH)
        with LOCK:
            OPTIONS.update(changed)
            STATE["executor"] = state
            STATE["modules"]["executor"] = state
        record_event("executor_mode_updated", "executor", {
            "mode": state, "requested_by": requested_by
        }, "WARNING" if state == "LIVE" else "INFO")
        return {"mode": state}
    
    
    PROCESS_NAMES = ("BATTERY_IMPORT", "BATTERY_EXPORT", "PV_CWU", "PV_EV", "HP_HEAT_DHW")
    # Reserved management contract.  It is deliberately excluded from
    # PROCESS_NAMES until the summer control policy and safe scripts exist.
    FUTURE_PROCESS_NAMES = ("COOL_DHW",)
    OVERRIDE_STATES = ("AUTO", "FORCE_ON", "FORCE_OFF")
    
    
    def enable_production_on_startup() -> dict:
        """Start LIVE when the complete guarded script connector is available."""
        service_map = configured_service_map()
        missing = [p for p in PROCESS_NAMES if not all(
            isinstance(service_map.get(p, {}).get(state), str)
            and service_map[p][state].startswith("script.")
            for state in ("ON", "OFF")
        )]
        configured_enabled = bool(OPTIONS.get("executor_enabled", False))
        live = configured_enabled and not missing
        changed = {
            "executor_enabled": live,
            "executor_dry_run": not live,
            "executor_activation_ack": "EMS_CONNECTOR_ACCEPTED" if live else "",
        }
        state = "LIVE" if live else "OFF"
        try:
            current = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = {}
        current.update(changed)
        temporary = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, RUNTIME_SETTINGS_PATH)
        with LOCK:
            OPTIONS.update(changed)
            STATE["executor"] = state
            STATE["modules"]["executor"] = state
        return {"mode": state, "missing_safe_script_mappings": missing}
    
    
    def update_process_override(payload: dict, requested_by: str = "operator") -> dict:
        process = str(payload.get("process") or "").upper()
        requested = str(payload.get("state") or "").upper()
        if process not in PROCESS_NAMES:
            raise ValueError("unknown process")
        if requested not in OVERRIDE_STATES:
            raise ValueError("state must be AUTO, FORCE_ON or FORCE_OFF")
        reason = str(payload.get("reason") or "operator panel")[:1000]
        now = local_now().replace(tzinfo=None)
        indefinite_override = requested in {"FORCE_ON", "FORCE_OFF"}
        override_until = datetime(9999, 12, 31, 23, 59, 59)
        with db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE ems_gpt_core_process_overrides SET status='CANCELLED',cancelled_at=NOW(6)
              WHERE process_name=%s AND status='ACTIVE'""", (process,))
            if requested == "AUTO":
                override_id = None
            else:
                override_id = str(uuid.uuid4())
                cur.execute("""INSERT INTO ems_gpt_core_process_overrides
                  (override_id,process_name,requested_state,requested_at,valid_from,valid_until,
                   requested_by,reason,status) VALUES(%s,%s,%s,NOW(6),%s,%s,%s,%s,'ACTIVE')""",
                  (override_id, process, requested, now, override_until, requested_by[:100], reason))
        control_revision = uuid.uuid4().hex
        try:
            runtime_settings = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            runtime_settings = {}
        runtime_settings["_process_control_revision"] = control_revision
        temporary = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(runtime_settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, RUNTIME_SETTINGS_PATH)
        with LOCK:
            OPTIONS["_process_control_revision"] = control_revision
        result = {"process": process, "state": requested, "override_id": override_id,
                  "valid_until": override_until if override_id and not indefinite_override else None,
                  "indefinite": bool(override_id and indefinite_override)}
        record_event("process_override_updated", "operator", result)
        return result
    
    
    def expire_process_overrides() -> int:
        with db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE ems_gpt_core_process_overrides SET status='EXPIRED'
              WHERE status='ACTIVE' AND valid_until<=NOW(6)""")
            expired = cur.rowcount
        if expired:
            record_event("process_overrides_expired", "operator", {"count": expired})
        return expired
    
    
    def expire_stale_commands() -> int:
        """Close every unacknowledged command after TTL, including already dispatched rows."""
        with db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE ems_gpt_core_commands SET status='EXPIRED',
              acknowledgement_json=COALESCE(acknowledgement_json,JSON_OBJECT('reason','TTL_EXPIRED'))
              WHERE status IN ('READY_FOR_CONNECTOR','DISPATCHED','ACCEPTED') AND expires_at<=NOW(6)""")
            expired = cur.rowcount
        if expired:
            record_event("commands_expired", "executor", {"count": expired})
        return expired
    
    
    def externally_started_hp_is_running(cur, now: datetime) -> bool:
        """Return true only for a fresh compressor run not initiated by EMS.
    
        A FORCE_OFF override always remains authoritative and is handled by the
        caller.  Requiring compressor frequency avoids treating standby power as
        an external heating cycle.  A recent EMS ON command keeps ownership with
        EMS, so the planned end of an automatic cycle can still issue OFF.
        """
        cur.execute("""SELECT hp_compressor_frequency_hz,captured_at
          FROM ems_gpt_telemetry_snapshots
          WHERE captured_at>=%s ORDER BY captured_at DESC LIMIT 1""",
          (now - timedelta(minutes=3),))
        sample = cur.fetchone()
        if not sample or float(sample.get("hp_compressor_frequency_hz") or 0) <= 0:
            return False
        cur.execute("""SELECT decision,created_at FROM ems_gpt_core_commands
          WHERE process_name='HP_HEAT_DHW' AND status<>'DRY_RUN'
          ORDER BY created_at DESC LIMIT 1""")
        command = cur.fetchone()
        return not (command and command.get("decision") == "ON"
                    and command.get("created_at") >= now - timedelta(minutes=30))
    
    
    def stage_executor_commands() -> dict:
        """Build auditable commands; never call HA unless the guarded executor is explicitly enabled."""
        if not bool(OPTIONS.get("executor_enabled", False)):
            with LOCK:
                STATE["executor"] = "OFF"
                STATE["modules"]["executor"] = "OFF"
            return {"status": "OFF", "staged": 0}
        dry_run = bool(OPTIONS.get("executor_dry_run", True))
        live_accepted = OPTIONS.get("executor_activation_ack") == "EMS_CONNECTOR_ACCEPTED"
        if not dry_run and not live_accepted:
            with LOCK:
                STATE["executor"] = "ACTIVATION_ACK_REQUIRED"
                STATE["modules"]["executor"] = "ACTIVATION_ACK_REQUIRED"
            return {"status": "ACTIVATION_ACK_REQUIRED", "staged": 0}
        now = local_now().replace(tzinfo=None)
        start = slot_start().replace(tzinfo=None)
        end = start + timedelta(minutes=int(OPTIONS.get("slot_minutes", 15)) + 1)
        staged = 0
        with db() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.*,s.heat_pump_window planner_heat_pump_window,
              o.requested_state,o.override_id FROM ems_gpt_core_process_decisions d
              JOIN ems_gpt_slots s ON s.slot_start=d.slot_start
               AND s.plan_run_id=d.plan_run_id AND s.plan_stage='PUBLISHED'
              LEFT JOIN ems_gpt_core_process_overrides o ON o.process_name=d.process_name
               AND o.status='ACTIVE' AND o.valid_from<=%s AND o.valid_until>%s
              WHERE d.slot_start=%s AND d.ppd_run_id IS NOT NULL
              ORDER BY d.process_name""", (now, now, start))
            for row in cur.fetchall():
                planned_on = database_bool(row["eligible"], "eligible")
                if row["process_name"] == "HP_HEAT_DHW":
                    planner_window = database_bool(
                        row.get("planner_heat_pump_window"), "heat_pump_window")
                    if planned_on and not planner_window:
                        record_event("hp_decision_blocked_by_planner_window", "executor", {
                            "slot_start": str(start), "plan_run_id": str(row["plan_run_id"]),
                        }, "ERROR")
                    planned_on = planned_on and planner_window
                requested = row.get("requested_state")
                effective_on = (True if requested == "FORCE_ON" else
                                False if requested == "FORCE_OFF" else planned_on)
                decision = "ON" if effective_on else "OFF"
                source = "OVERRIDE" if requested else "PLAN"
                command_id = str(uuid.uuid4())
                plan_version = (str(row["plan_run_id"]) + ":" + str(row.get("ppd_run_id") or "NO_PPD") + ":"
                                + str(OPTIONS.get("_process_control_revision") or "base"))
                battery_flow = row["process_name"] in {"BATTERY_IMPORT", "BATTERY_EXPORT"}
                safety = {"executor_enabled": True, "dry_run": dry_run, "connector_required": True,
                          "soc_programs_1_6_write_allowed": battery_flow,
                          "soc_restore_required": battery_flow,
                          "override_id": row.get("override_id")}
                cur.execute("""INSERT IGNORE INTO ems_gpt_core_commands
                  (command_id,slot_start,slot_id,process_name,decision,plan_version,created_at,expires_at,
                   source,status,safety_json) VALUES(%s,%s,%s,%s,%s,%s,NOW(6),%s,%s,%s,%s)""",
                  (command_id, start, row.get("slot_id"), row["process_name"], decision, plan_version, end, source,
                   "DRY_RUN" if dry_run else "READY_FOR_CONNECTOR", json.dumps(safety)))
                staged += cur.rowcount
        state = "DRY_RUN" if dry_run else "LIVE"
        with LOCK:
            STATE["executor"] = state
            STATE["modules"]["executor"] = state
        return {"status": state, "staged": staged}
    
    
    def dispatch_ready_commands() -> dict:
        """Fail-closed HA adapter. It can only invoke explicitly mapped script entities."""
        if not bool(OPTIONS.get("executor_enabled", False)) or bool(OPTIONS.get("executor_dry_run", True)):
            return {"status": "DISABLED_OR_DRY_RUN", "dispatched": 0}
        if OPTIONS.get("executor_activation_ack") != "EMS_CONNECTOR_ACCEPTED":
            return {"status": "ACTIVATION_ACK_REQUIRED", "dispatched": 0}
        service_map = configured_service_map()
        now = local_now().replace(tzinfo=None)
        current_slot = slot_start().replace(tzinfo=None)
        dispatched = 0
        with db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE ems_gpt_core_commands SET status='EXPIRED',
              acknowledgement_json=COALESCE(acknowledgement_json,JSON_OBJECT('reason','TTL_EXPIRED'))
              WHERE status IN ('READY_FOR_CONNECTOR','DISPATCHED','ACCEPTED') AND expires_at<=%s""", (now,))
            cur.execute("""SELECT * FROM ems_gpt_core_commands WHERE status='READY_FOR_CONNECTOR'
              AND slot_start=%s AND expires_at>%s ORDER BY created_at""", (current_slot, now))
            for command in cur.fetchall():
                if command["process_name"] == "HP_HEAT_DHW" and command["decision"] == "ON":
                    cur.execute("""SELECT heat_pump_window FROM ems_gpt_slots
                      WHERE slot_start=%s AND plan_stage='PUBLISHED' LIMIT 1""",
                      (current_slot,))
                    hp_plan = cur.fetchone() or {}
                    if not database_bool(hp_plan.get("heat_pump_window"), "heat_pump_window"):
                        cur.execute("""UPDATE ems_gpt_core_commands SET status='REJECTED',
                          acknowledgement_json=%s WHERE command_id=%s""",
                          (json.dumps({"reason": "HP_PLANNER_WINDOW_OFF"}),
                           command["command_id"]))
                        record_event("hp_command_blocked_by_planner_window", "executor", {
                            "slot_start": str(current_slot), "command_id": command["command_id"],
                        }, "ERROR")
                        continue
                process_map = service_map.get(command["process_name"], {}) if isinstance(service_map, dict) else {}
                entity_id = process_map.get(command["decision"])
                if not isinstance(entity_id, str) or not entity_id.startswith("script."):
                    cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                                (json.dumps({"reason": "MISSING_OR_INVALID_ALLOWLIST_MAPPING"}), command["command_id"]))
                    continue
                if "inverter_program_" in entity_id or "soc" in entity_id.lower():
                    cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                                (json.dumps({"reason": "PROTECTED_SOC_PROGRAM_MAPPING"}), command["command_id"]))
                    continue
                if command["process_name"] == "BATTERY_EXPORT" and command["decision"] == "ON":
                    live_soc = number(ha_state("sensor.inverter_battery"))
                    cur.execute("""SELECT soc_floor_pct FROM ems_gpt_slots
                      WHERE slot_start=%s LIMIT 1""", (current_slot,))
                    floor_row = cur.fetchone() or {}
                    plan_floor = scalar_number(floor_row.get("soc_floor_pct"))
                    if live_soc is None or plan_floor is None or live_soc <= plan_floor + 0.01:
                        reason = ("PLAN_FLOOR_UNAVAILABLE" if live_soc is None or plan_floor is None else
                                  f"PLAN_FLOOR_BLOCK: soc_floor={plan_floor:.2f}%")
                        off_entity = process_map.get("OFF")
                        safe_response = ha_service_response("script", "turn_on", {"entity_id": off_entity}) \
                            if isinstance(off_entity, str) and off_entity.startswith("script.") else None
                        cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                                    (json.dumps({"reason": reason, "live_soc": live_soc,
                                                 "safe_off_dispatched": safe_response is not None}), command["command_id"]))
                        record_event("battery_export_blocked_by_tou_floor", "executor",
                                     {"reason": reason, "live_soc": live_soc}, "WARNING")
                        continue
                target_update = None
                restored_targets = []
                if command["process_name"] == "BATTERY_IMPORT" and command["decision"] == "ON":
                    cur.execute("""SELECT soc_charge_target_pct,soc_target_pct,soc_end_plan_pct,planned_buy_kwh FROM ems_gpt_slots
                      WHERE slot_start=%s LIMIT 1""", (current_slot,))
                    plan_target = cur.fetchone() or {}
                    target = (plan_target.get("soc_charge_target_pct")
                              if plan_target.get("soc_charge_target_pct") is not None
                              else plan_target.get("soc_target_pct"))
                    if target is None:
                        target = plan_target.get("soc_end_plan_pct")
                    live_soc = number(ha_state("sensor.inverter_battery"))
                    planned_buy = number(plan_target.get("planned_buy_kwh"))
                    flow_threshold = float(OPTIONS.get("planned_flow_threshold_kwh", 0.02))
                    guard_buy = (max(flow_threshold * 2.0, 0.001)
                                 if command.get("source") == "OVERRIDE" else planned_buy)
                    guard_reason = battery_import_guard_reason(
                        live_soc, target, guard_buy, flow_threshold)
                    if guard_reason:
                        off_entity = process_map.get("OFF")
                        safe_response = ha_service_response("script", "turn_on", {"entity_id": off_entity}) \
                            if isinstance(off_entity, str) and off_entity.startswith("script.") else None
                        try:
                            set_active_program_charging(now, "Disabled")
                        except RuntimeError:
                            pass
                        restored_targets = restore_program_targets_if_idle()
                        cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                                    (json.dumps({"reason": guard_reason, "live_soc": live_soc,
                                                 "target_soc": target, "planned_buy_kwh": planned_buy,
                                                 "safe_off_dispatched": safe_response is not None,
                                                 "tou_targets_restored": restored_targets}), command["command_id"]))
                        record_event("battery_import_blocked_by_live_target", "executor", {
                            "reason": guard_reason, "slot_start": current_slot,
                        }, "WARNING")
                        continue
                    try:
                        set_active_program_charging(now, "Grid")
                        target_update = set_active_program_target(now, float(target))
                    except (RuntimeError, TypeError, ValueError) as exc:
                        cur.execute("UPDATE ems_gpt_core_commands SET status='FAILED',acknowledgement_json=%s WHERE command_id=%s",
                                    (json.dumps({"reason": str(exc)}), command["command_id"]))
                        record_event("battery_import_target_failed", "executor", {
                            "reason": str(exc), "slot_start": current_slot,
                        }, "ERROR")
                        continue
                if command["process_name"] == "BATTERY_EXPORT" and command["decision"] == "ON":
                    cur.execute("""SELECT soc_floor_pct FROM ems_gpt_slots
                      WHERE slot_start=%s LIMIT 1""", (current_slot,))
                    export_plan = cur.fetchone() or {}
                    try:
                        set_active_program_charging(now, "Disabled")
                        target_update = set_active_program_target(
                            now, float(export_plan.get("soc_floor_pct")))
                    except (RuntimeError, TypeError, ValueError) as exc:
                        cur.execute("UPDATE ems_gpt_core_commands SET status='FAILED',acknowledgement_json=%s WHERE command_id=%s",
                                    (json.dumps({"reason": str(exc)}), command["command_id"]))
                        record_event("battery_export_floor_failed", "executor", {
                            "reason": str(exc), "slot_start": current_slot,
                        }, "ERROR")
                        continue
                response = ha_service_response("script", "turn_on", {"entity_id": entity_id})
                if response is None:
                    cur.execute("UPDATE ems_gpt_core_commands SET status='FAILED',acknowledgement_json=%s WHERE command_id=%s",
                                (json.dumps({"reason": "HA_SERVICE_FAILED", "entity_id": entity_id}), command["command_id"]))
                    if target_update is not None:
                        off_entity = process_map.get("OFF")
                        if isinstance(off_entity, str) and off_entity.startswith("script."):
                            ha_service_response("script", "turn_on", {"entity_id": off_entity})
                        restore_program_targets_if_idle()
                    continue
                if command["process_name"] in {"BATTERY_IMPORT", "BATTERY_EXPORT"} and command["decision"] == "OFF":
                    try:
                        set_active_program_charging(now, "Disabled")
                    except RuntimeError as exc:
                        record_event("battery_program_grid_disable_failed", "executor", {
                            "reason": str(exc), "slot_start": current_slot,
                        }, "ERROR")
                    restored_targets = restore_program_targets_if_idle()
                cur.execute("""UPDATE ems_gpt_core_commands SET status='DISPATCHED',dispatched_at=NOW(6),
                  acknowledgement_json=%s WHERE command_id=%s AND status='READY_FOR_CONNECTOR'""",
                  (json.dumps({"entity_id": entity_id, "ha_response": response,
                               "tou_target_update": target_update,
                               "tou_targets_restored": restored_targets},
                              ensure_ascii=False, default=str), command["command_id"]))
                dispatched += cur.rowcount
            # Scripts are binary for the duration of a slot. Re-check the live
            # SOC every scheduler minute so a partial final slot stops at the
            # quantitative SOC endpoint produced by PPD.
            cur.execute("""SELECT soc_end_plan_pct FROM ems_gpt_slots
              WHERE slot_start=%s LIMIT 1""", (current_slot,))
            plan = cur.fetchone() or {}
            live_soc = number(ha_state("sensor.inverter_battery"))
            try:
                planned_end_soc = float(plan["soc_end_plan_pct"])
            except (KeyError, TypeError, ValueError):
                planned_end_soc = None
            grid_state = ha_state("switch.inverter_battery_grid_charging") or {}
            mode_state = ha_state("select.inverter_work_mode") or {}
            stop_import, stop_export = battery_soc_guard_actions(
                live_soc, planned_end_soc,
                str(grid_state.get("state") or "").lower() == "on",
                str(mode_state.get("state") or "") == "Export First",
            )
            guard_actions = []
            if stop_import:
                entity_id = service_map.get("BATTERY_IMPORT", {}).get("OFF")
                if isinstance(entity_id, str) and entity_id.startswith("script."):
                    response = ha_service_response("script", "turn_on", {"entity_id": entity_id})
                    if response is not None:
                        guard_actions.append("BATTERY_IMPORT_OFF")
                        try:
                            set_active_program_charging(now, "Disabled")
                            guard_actions.append("TOU_GRID_DISABLED")
                        except RuntimeError:
                            pass
                        restored = restore_program_targets_if_idle()
                        if restored:
                            guard_actions.append("TOU_SOC_RESTORED")
            if stop_export:
                entity_id = service_map.get("BATTERY_EXPORT", {}).get("OFF")
                if isinstance(entity_id, str) and entity_id.startswith("script."):
                    response = ha_service_response("script", "turn_on", {"entity_id": entity_id})
                    if response is not None:
                        guard_actions.append("BATTERY_EXPORT_OFF")
                        try:
                            set_active_program_charging(now, "Disabled")
                            guard_actions.append("TOU_GRID_DISABLED")
                        except RuntimeError:
                            pass
                        restored = restore_program_targets_if_idle()
                        if restored:
                            guard_actions.append("TOU_SOC_RESTORED")
            if guard_actions:
                record_event("battery_soc_guard_applied", "executor", {
                    "actions": guard_actions, "live_soc": live_soc,
                    "planned_end_soc": plan.get("soc_end_plan_pct"), "slot_start": current_slot,
                })
        return {"status": "DISPATCHED", "dispatched": dispatched}
    
    
    def acknowledge_command(payload: dict) -> dict:
        command_id = str(payload.get("command_id") or "")
        status = str(payload.get("status") or "").upper()
        if not command_id:
            raise ValueError("command_id is required")
        if status not in {"ACCEPTED", "EXECUTED", "REJECTED", "FAILED"}:
            raise ValueError("invalid acknowledgement status")
        now = local_now().replace(tzinfo=None)
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM ems_gpt_core_commands WHERE command_id=%s FOR UPDATE", (command_id,))
            command = cur.fetchone()
            if not command:
                raise ValueError("unknown command_id")
            if command["expires_at"] <= now and status in {"ACCEPTED", "EXECUTED"}:
                cur.execute("UPDATE ems_gpt_core_commands SET status='EXPIRED',acknowledgement_json=%s WHERE command_id=%s",
                            (json.dumps(payload, ensure_ascii=False), command_id))
                raise ValueError("expired command cannot be accepted or executed")
            terminal = command["status"] in {"EXECUTED", "REJECTED", "FAILED", "EXPIRED"}
            if terminal and command["status"] != status:
                raise ValueError(f"command already terminal: {command['status']}")
            cur.execute("""UPDATE ems_gpt_core_commands SET status=%s,
              acknowledged_at=CASE WHEN %s='ACCEPTED' THEN COALESCE(acknowledged_at,NOW(6)) ELSE acknowledged_at END,
              executed_at=CASE WHEN %s='EXECUTED' THEN COALESCE(executed_at,NOW(6)) ELSE executed_at END,
              acknowledgement_json=%s WHERE command_id=%s""",
              (status, status, status, json.dumps(payload, ensure_ascii=False), command_id))
        result = {"command_id": command_id, "status": status}
        record_event("connector_acknowledgement", "executor", result, "INFO" if status in {"ACCEPTED", "EXECUTED"} else "WARNING")
        return result
    
    

    return SimpleNamespace(
        settings_payload=settings_payload,
        update_operational_settings=update_operational_settings,
        update_executor_mode=update_executor_mode,
        enable_production_on_startup=enable_production_on_startup,
        update_process_override=update_process_override,
        expire_process_overrides=expire_process_overrides,
        expire_stale_commands=expire_stale_commands,
        externally_started_hp_is_running=externally_started_hp_is_running,
        stage_executor_commands=stage_executor_commands,
        dispatch_ready_commands=dispatch_ready_commands,
        acknowledge_command=acknowledge_command,
        set_active_program_target=set_active_program_target,
        set_active_program_charging=set_active_program_charging,
        restore_program_targets=restore_program_targets,
        restore_program_targets_if_idle=restore_program_targets_if_idle,
    )
