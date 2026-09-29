"""Read-only agent mailbox for operator <-> EMS analysis agent communication.

The mailbox deliberately has no planner, executor, PPD, or Home Assistant
adapter. An agent may read the curated EMS context and return analysis text;
it cannot write plans or dispatch commands through this module.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any


MAX_MESSAGE_CHARS = 4000
MAX_REPLY_CHARS = 12000
MAX_CONTEXT_ROWS = 96
MAX_HISTORY_ROWS = 28 * 96
_SLOT_CONTEXT_FIELDS = {
    "slot_start", "plan_run_id", "recommendation", "market_window",
    "price_buy_pln_kwh", "price_sell_pln_kwh", "forecast_pv1_kwh",
    "forecast_pv2_kwh", "forecast_pv_total_kwh", "actual_pv1_kwh",
    "actual_pv2_kwh", "actual_pv_total_kwh", "forecast_load_kwh",
    "actual_load_kwh", "actual_native_load_kwh", "forecast_heat_pump_load_kwh",
    "planned_buy_kwh", "actual_buy_kwh", "planned_sell_kwh",
    "planned_battery_charge_kwh", "actual_battery_charge_kwh",
    "planned_battery_discharge_kwh", "actual_battery_discharge_kwh",
    "planned_pv_to_bat_kwh", "planned_pv_to_cwu_kwh", "planned_pv_to_ev_kwh",
    "planned_pv_export_kwh", "soc_start_plan_pct", "soc_end_plan_pct",
    "soc_before_pct", "soc_after_pct", "soc_reserve_pct", "soc_required_pct",
    "soc_charge_target_pct", "soc_sale_floor_pct", "soc_target_due",
    "actual_grid_export_kwh", "actual_ev_kwh", "actual_dhw_kwh",
    "soc_start_pct", "soc_min_pct", "soc_delta_pct", "coverage_pct",
    "export_attribution", "actual_recorded_at", "data_quality_status",
}


def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_MUST_BE_TEXT")
    value = value.strip()
    if not value:
        raise ValueError(f"{field}_EMPTY")
    if len(value) > limit or "\x00" in value:
        raise ValueError(f"{field}_TOO_LONG_OR_INVALID")
    return value


def submit_message(text: str, actor: str, *, db, now: datetime) -> dict:
    body = _text(text, MAX_MESSAGE_CHARS, "MESSAGE")
    actor = _text(actor, 100, "ACTOR")
    message_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ems_gpt_core_agent_messages
               (message_id,thread_id,role_name,actor_name,message_text,status,created_at)
               VALUES(%s,%s,'operator',%s,%s,'PENDING',%s)""",
            (message_id, thread_id, actor, body, now),
        )
    return {"status": "PENDING", "message_id": message_id, "thread_id": thread_id}


def list_messages(*, db, limit: int = 100, thread_id: str | None = None) -> list[dict]:
    limit = max(1, min(200, int(limit)))
    with db() as conn, conn.cursor() as cur:
        if thread_id:
            cur.execute(
                """SELECT message_id,thread_id,role_name,actor_name,message_text,
                          status,created_at,completed_at
                   FROM ems_gpt_core_agent_messages WHERE thread_id=%s
                   ORDER BY created_at,message_id LIMIT %s""",
                (thread_id, limit),
            )
        else:
            cur.execute(
                """SELECT message_id,thread_id,role_name,actor_name,message_text,
                          status,created_at,completed_at
                   FROM ems_gpt_core_agent_messages
                   ORDER BY created_at DESC,message_id DESC LIMIT %s""",
                (limit,),
            )
        return list(cur.fetchall())


def claim_messages(agent_id: str, *, db, limit: int = 5) -> list[dict]:
    agent_id = _text(agent_id, 80, "AGENT_ID")
    limit = max(1, min(20, int(limit)))
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT message_id,thread_id,actor_name,message_text,created_at
               FROM ems_gpt_core_agent_messages
               WHERE role_name='operator' AND (status='PENDING' OR
                 (status='CLAIMED' AND claimed_at < DATE_SUB(NOW(6), INTERVAL 10 MINUTE)))
               ORDER BY created_at,message_id LIMIT %s FOR UPDATE""",
            (limit,),
        )
        rows = list(cur.fetchall())
        for row in rows:
            cur.execute(
                """UPDATE ems_gpt_core_agent_messages
                   SET status='CLAIMED',claimed_by=%s,claimed_at=NOW(6)
                   WHERE message_id=%s AND (status='PENDING' OR
                     (status='CLAIMED' AND claimed_at < DATE_SUB(NOW(6), INTERVAL 10 MINUTE)))""",
                (agent_id, row["message_id"]),
            )
        conn.commit()
    return rows


def submit_reply(message_id: str, agent_id: str, text: str, *, db, now: datetime) -> dict:
    message_id = _text(message_id, 36, "MESSAGE_ID")
    agent_id = _text(agent_id, 80, "AGENT_ID")
    body = _text(text, MAX_REPLY_CHARS, "REPLY")
    reply_id = str(uuid.uuid4())
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT thread_id FROM ems_gpt_core_agent_messages
               WHERE message_id=%s AND role_name='operator'
                 AND status='CLAIMED' AND claimed_by=%s FOR UPDATE""",
            (message_id, agent_id),
        )
        parent = cur.fetchone()
        if not parent:
            conn.rollback()
            raise PermissionError("MESSAGE_NOT_CLAIMED_BY_AGENT")
        cur.execute(
            """INSERT INTO ems_gpt_core_agent_messages
               (message_id,thread_id,role_name,actor_name,message_text,status,created_at,completed_at)
               VALUES(%s,%s,'agent',%s,%s,'COMPLETED',%s,%s)""",
            (reply_id, parent["thread_id"], agent_id, body, now, now),
        )
        cur.execute(
            """UPDATE ems_gpt_core_agent_messages SET status='COMPLETED',completed_at=%s
               WHERE message_id=%s AND status='CLAIMED' AND claimed_by=%s""",
            (now, message_id, agent_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise PermissionError("MESSAGE_CLAIM_CHANGED")
        conn.commit()
    return {"status": "COMPLETED", "message_id": reply_id,
            "thread_id": parent["thread_id"], "reply_to": message_id}


def read_context(*, db, now: datetime, state: dict, state_lock=None,
                 limit: int = MAX_CONTEXT_ROWS) -> dict:
    """Expose only planner, execution and analytics facts needed for review."""
    limit = max(1, min(MAX_CONTEXT_ROWS, int(limit)))
    current = now.replace(tzinfo=None)
    history_start = current - timedelta(days=28)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM ems_gpt_slots WHERE actual_recorded_at IS NULL
               AND slot_start >= %s ORDER BY slot_start LIMIT %s""",
            (current, limit),
        )
        future = list(cur.fetchall())
        cur.execute(
            """SELECT s.*,d.actual_grid_export_kwh,d.actual_ev_kwh,d.actual_dhw_kwh,
                      d.soc_start_pct,d.soc_min_pct,d.soc_delta_pct,d.coverage_pct,
                      d.export_attribution
               FROM ems_gpt_slots s
               LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
               WHERE s.actual_recorded_at IS NOT NULL AND s.slot_start >= %s
               ORDER BY s.slot_start DESC LIMIT %s""",
            (history_start, MAX_HISTORY_ROWS),
        )
        history = list(cur.fetchall())
        cur.execute(
            """SELECT run_id,started_at,completed_at,status,slots_scanned,
                      complete_slots,pv_wape_pct,load_wape_pct,import_wape_pct,
                      export_wape_pct,quality_score,details_json
               FROM ems_gpt_core_analytics_runs ORDER BY started_at DESC LIMIT 14"""
        )
        analytics = list(cur.fetchall())
        cur.execute(
            """SELECT run_id,role_name,source_ref,started_at,completed_at,status,
                      result_json,auto_score,decision
               FROM ems_gpt_core_ai_runs ORDER BY started_at DESC LIMIT 14"""
        )
        ai_runs = list(cur.fetchall())
    with (state_lock or _NullLock()):
        modules = dict(state.get("modules") or {})
        module_details = dict(state.get("module_details") or {})
    return {
        "mode": "READ_ONLY_ANALYSIS",
        "permissions": ["READ_PLAN", "READ_EXECUTION", "READ_ANALYTICS", "WRITE_AGENT_REPLY"],
        "forbidden": ["PLAN_WRITE", "PPD_WRITE", "COMMAND_WRITE", "HA_SERVICE_CALL", "SETTINGS_WRITE"],
        "current_state": {"modules": modules, "module_details": module_details},
        "future_slots": [{k: v for k, v in row.items() if k in _SLOT_CONTEXT_FIELDS}
                         for row in future],
        "completed_slots": [{k: v for k, v in row.items() if k in _SLOT_CONTEXT_FIELDS}
                            for row in history],
        "history_days": 28,
        "analytics_runs": analytics,
        "observer_runs": ai_runs,
    }


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
