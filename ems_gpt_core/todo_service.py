"""Durable Observer and diagnostics TODO lifecycle.

This module owns persistence state transitions.  The application supplies its
database, clock and audit adapters, so the service stays independent from the
HTTP server and scheduler.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable


@dataclass(frozen=True)
class TodoService:
    db: Callable
    local_now: Callable
    record_event: Callable

    def create(self, module: str, title: str, details: str, severity: str = "INFO",
               source_ref: str | None = None, require_consecutive_days: bool = False) -> str:
        """Create or refresh one issue; Observer promotes it after three consecutive days."""
        todo_id = str(uuid.uuid4())
        today = self.local_now().date()
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM ems_gpt_core_todo WHERE module_name=%s AND title=%s
              AND status IN ('WATCHING','OPEN','SUGGESTED') ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
                        (module[:32], title[:300]))
            existing = cur.fetchone()
            if existing:
                last_seen = existing.get("last_seen_day") or existing.get("local_day")
                consecutive = int(existing.get("consecutive_days") or 1)
                if last_seen != today:
                    consecutive = consecutive + 1 if last_seen == today - timedelta(days=1) else 1
                status = "SUGGESTED" if require_consecutive_days and consecutive >= 3 else \
                         "WATCHING" if require_consecutive_days else "OPEN"
                cur.execute("""UPDATE ems_gpt_core_todo SET local_day=%s,last_seen_day=%s,
                  occurrence_count=occurrence_count+1,consecutive_days=%s,severity=%s,details=%s,
                  source_ref=%s,status=%s WHERE todo_id=%s""",
                            (today, today, consecutive, severity[:12], details, source_ref,
                             status, existing["todo_id"]))
                return existing["todo_id"]
            status = "WATCHING" if require_consecutive_days else "OPEN"
            cur.execute("""INSERT INTO ems_gpt_core_todo
              (todo_id,created_at,local_day,severity,module_name,title,details,status,source_ref,
               first_seen_day,last_seen_day,occurrence_count,consecutive_days)
              VALUES(%s,NOW(6),%s,%s,%s,%s,%s,%s,%s,%s,%s,1,1)""",
              (todo_id, today, severity[:12], module[:32], title[:300], details, status,
               source_ref, today, today))
        return todo_id

    def reconcile_observer(self, active_titles: list[str]) -> int:
        today = self.local_now().date()
        with self.db() as conn, conn.cursor() as cur:
            if active_titles:
                placeholders = ",".join(["%s"] * len(active_titles))
                cur.execute(f"""UPDATE ems_gpt_core_todo SET status='ARCHIVED',archived_at=NOW(6)
                  WHERE module_name='ai_observer' AND status IN ('WATCHING','OPEN','SUGGESTED')
                    AND last_seen_day<%s AND title NOT IN ({placeholders})""", (today, *active_titles))
            else:
                cur.execute("""UPDATE ems_gpt_core_todo SET status='ARCHIVED',archived_at=NOW(6)
                  WHERE module_name='ai_observer' AND status IN ('WATCHING','OPEN','SUGGESTED')
                    AND last_seen_day<%s""", (today,))
            return cur.rowcount

    def reconcile_diagnostics(self, active_titles: list[str]) -> int:
        with self.db() as conn, conn.cursor() as cur:
            if active_titles:
                placeholders = ",".join(["%s"] * len(active_titles))
                cur.execute(f"""UPDATE ems_gpt_core_todo SET status='RESOLVED',resolved_at=NOW(6)
                  WHERE module_name='diagnostics' AND status IN ('OPEN','SUGGESTED')
                    AND title NOT IN ({placeholders})""", tuple(active_titles))
            else:
                cur.execute("""UPDATE ems_gpt_core_todo SET status='RESOLVED',resolved_at=NOW(6)
                  WHERE module_name='diagnostics' AND status IN ('OPEN','SUGGESTED')""")
            return cur.rowcount

    def maintain_archive(self) -> dict:
        today = self.local_now().date()
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE ems_gpt_core_todo SET status='ARCHIVED',archived_at=NOW(6)
              WHERE status IN ('ACCEPTED','REJECTED','RESOLVED') AND local_day<%s""", (today,))
            reviewed = cur.rowcount
            cur.execute("""UPDATE ems_gpt_core_todo SET status='ARCHIVED',archived_at=NOW(6)
              WHERE status='WATCHING' AND last_seen_day<%s""", (today-timedelta(days=1),))
            stale = cur.rowcount
        if reviewed or stale:
            self.record_event("todo_nightly_archive", "ai_observer",
                              {"reviewed": reviewed, "stale": stale})
        return {"reviewed": reviewed, "stale": stale}

    def review(self, payload: dict, actor: str) -> dict:
        todo_id = str(payload.get("todo_id") or "")
        decision = str(payload.get("decision") or "").upper()
        note = str(payload.get("note") or "")[:1000]
        if not todo_id:
            raise ValueError("todo_id is required")
        if decision not in {"ACCEPTED", "REJECTED", "RESOLVED"}:
            raise ValueError("decision must be ACCEPTED, REJECTED or RESOLVED")
        with self.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM ems_gpt_core_todo WHERE todo_id=%s FOR UPDATE", (todo_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("unknown todo_id")
            if row["status"] == "ARCHIVED":
                raise ValueError("archived todo cannot be reviewed")
            cur.execute("""UPDATE ems_gpt_core_todo SET status=%s,reviewed_at=NOW(6),reviewed_by=%s,
              review_note=%s,resolved_at=CASE WHEN %s='RESOLVED' THEN NOW(6) ELSE resolved_at END
              WHERE todo_id=%s""", (decision, actor[:100], note, decision, todo_id))
        result = {"todo_id": todo_id, "status": decision, "reviewed_by": actor[:100]}
        self.record_event("todo_reviewed", "ai_observer", result)
        return result
