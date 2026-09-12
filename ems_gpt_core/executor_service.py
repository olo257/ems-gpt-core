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


def build_executor(a: ExecutorAdapters):
    OPTIONS, OPERATIONAL_SETTINGS = a.options, a.operational_settings
    RUNTIME_SETTINGS_PATH, LOCK, STATE = a.runtime_settings_path, a.lock, a.state
    record_event, local_now, db, slot_start = a.record_event, a.local_now, a.db, a.slot_start
    tou_program_snapshot, active_tou_program = a.tou_program_snapshot, a.active_tou_program
    number, ha_state, ha_service_response = a.number, a.ha_state, a.ha_service_response

    def settings_payload() -> dict:
        return {key: {"value": float(OPTIONS[key]), "min": spec[0], "max": spec[1], "label": spec[2], "group": spec[3]}
                for key, spec in OPERATIONAL_SETTINGS.items()}
    
    
    def update_operational_settings(payload: dict) -> dict:
        changed = {}
        for key, raw in payload.items():
            if key not in OPERATIONAL_SETTINGS:
                raise ValueError(f"unknown setting: {key}")
            low, high, _, _ = OPERATIONAL_SETTINGS[key]
            value = float(raw)
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            changed[key] = value
        if not changed:
            raise ValueError("no settings supplied")
        current = {}
        try:
            current = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
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
            try:
                service_map = json.loads(str(OPTIONS.get("connector_service_map_json") or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("connector_service_map_json is invalid") from exc
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
    OVERRIDE_STATES = ("AUTO", "FORCE_ON", "FORCE_OFF")
    
    
    def enable_production_on_startup() -> dict:
        """Start LIVE when the complete guarded script connector is available."""
        try:
            service_map = json.loads(str(OPTIONS.get("connector_service_map_json") or "{}"))
        except json.JSONDecodeError:
            service_map = {}
        missing = [p for p in PROCESS_NAMES if not all(
            isinstance(service_map.get(p, {}).get(state), str)
            and service_map[p][state].startswith("script.")
            for state in ("ON", "OFF")
        )]
        changed = {
            "executor_enabled": not missing,
            "executor_dry_run": bool(missing),
            "executor_activation_ack": "" if missing else "EMS_CONNECTOR_ACCEPTED",
        }
        state = "OFF" if missing else "LIVE"
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
        minutes = int(payload.get("minutes") or 60)
        if not 1 <= minutes <= 1440:
            raise ValueError("minutes must be between 1 and 1440")
        if process == "HP_HEAT_DHW" and requested == "FORCE_ON":
            minimum_hp_minutes = max(1, int(float(OPTIONS.get("hp_min_cycle_hours", 2.0)) * 60 + 0.999999))
            if minutes < minimum_hp_minutes:
                raise ValueError(f"HP_HEAT_DHW FORCE_ON must last at least {minimum_hp_minutes} minutes")
        now = local_now().replace(tzinfo=None)
        indefinite_block = process == "HP_HEAT_DHW" and requested == "FORCE_OFF"
        override_until = datetime(9999, 12, 31, 23, 59, 59) if indefinite_block else now + timedelta(minutes=minutes)
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
        result = {"process": process, "state": requested, "override_id": override_id,
                  "valid_until": override_until if override_id and not indefinite_block else None,
                  "indefinite": bool(override_id and indefinite_block)}
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
            cur.execute("""SELECT d.*,o.requested_state,o.override_id FROM ems_gpt_core_process_decisions d
              LEFT JOIN ems_gpt_core_process_overrides o ON o.process_name=d.process_name
               AND o.status='ACTIVE' AND o.valid_from<=%s AND o.valid_until>%s
              WHERE d.slot_start=%s ORDER BY d.process_name""", (now, now, start))
            for row in cur.fetchall():
                planned_on = bool(row["eligible"])
                requested = row.get("requested_state")
                external_hp_on = (row["process_name"] == "HP_HEAT_DHW"
                                  and requested != "FORCE_OFF"
                                  and not planned_on
                                  and externally_started_hp_is_running(cur, now))
                effective_on = (True if requested == "FORCE_ON" else
                                False if requested == "FORCE_OFF" else
                                True if external_hp_on else planned_on)
                decision = "ON" if effective_on else "OFF"
                source = "OVERRIDE" if requested else "EXTERNAL_MANUAL" if external_hp_on else "PLAN"
                if external_hp_on:
                    record_event("external_hp_control_preserved", "executor", {
                        "process": "HP_HEAT_DHW", "decision": "HOLD_ON",
                        "reason": "fresh compressor run without recent EMS ON command"
                    })
                    continue
                command_id = str(uuid.uuid4())
                safety = {"executor_enabled": True, "dry_run": dry_run, "connector_required": True,
                          "soc_programs_1_6_write_allowed": False, "override_id": row.get("override_id")}
                cur.execute("""INSERT IGNORE INTO ems_gpt_core_commands
                  (command_id,slot_start,slot_id,process_name,decision,plan_version,created_at,expires_at,
                   source,status,safety_json) VALUES(%s,%s,%s,%s,%s,%s,NOW(6),%s,%s,%s,%s)""",
                  (command_id, start, row.get("slot_id"), row["process_name"], decision, row["plan_run_id"], end, source,
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
        try:
            service_map = json.loads(str(OPTIONS.get("connector_service_map_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("connector_service_map_json is invalid") from exc
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
                    live_programs = tou_program_snapshot()
                    live_program = active_tou_program(now, live_programs)
                    live_soc = number(ha_state("sensor.inverter_battery"))
                    if live_program is None or live_soc is None or live_soc <= float(live_program["soc"]) + 0.01:
                        reason = ("TOU_FLOOR_UNAVAILABLE" if live_program is None or live_soc is None else
                                  f"TOU_FLOOR_BLOCK: program={live_program['program']}, soc={live_program['soc']:.0f}%")
                        off_entity = process_map.get("OFF")
                        safe_response = ha_service_response("script", "turn_on", {"entity_id": off_entity}) \
                            if isinstance(off_entity, str) and off_entity.startswith("script.") else None
                        cur.execute("UPDATE ems_gpt_core_commands SET status='REJECTED',acknowledgement_json=%s WHERE command_id=%s",
                                    (json.dumps({"reason": reason, "live_soc": live_soc,
                                                 "safe_off_dispatched": safe_response is not None}), command["command_id"]))
                        record_event("battery_export_blocked_by_tou_floor", "executor",
                                     {"reason": reason, "live_soc": live_soc}, "WARNING")
                        continue
                response = ha_service_response("script", "turn_on", {"entity_id": entity_id})
                if response is None:
                    cur.execute("UPDATE ems_gpt_core_commands SET status='FAILED',acknowledgement_json=%s WHERE command_id=%s",
                                (json.dumps({"reason": "HA_SERVICE_FAILED", "entity_id": entity_id}), command["command_id"]))
                    continue
                cur.execute("""UPDATE ems_gpt_core_commands SET status='DISPATCHED',dispatched_at=NOW(6),
                  acknowledgement_json=%s WHERE command_id=%s AND status='READY_FOR_CONNECTOR'""",
                  (json.dumps({"entity_id": entity_id, "ha_response": response}, ensure_ascii=False, default=str), command["command_id"]))
                dispatched += cur.rowcount
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
    )
