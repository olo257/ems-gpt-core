"""Minute scheduler for EMS-GPT Core."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def publish_current_slot_prices(db: Callable, current_slot: datetime,
                                ha_service_response: Callable,
                                ha_state: Callable | None = None) -> dict:
    """Publish and, when possible, verify both active-slot price helpers."""
    start = current_slot.replace(tzinfo=None)
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT price_buy_pln_kwh,price_sell_pln_kwh
          FROM ems_gpt_slots WHERE slot_start=%s LIMIT 1""", (start,))
        row = cur.fetchone()
    if not row or row.get("price_buy_pln_kwh") is None or row.get("price_sell_pln_kwh") is None:
        return {"status": "MISSING_SLOT_PRICE", "slot_start": start.isoformat()}
    prices = {
        "input_number.ems_gpt_cena_zakupu_biezaca": round(
            float(row["price_buy_pln_kwh"]), 3),
        "input_number.ems_gpt_cena_sprzedazy_biezaca": round(
            float(row["price_sell_pln_kwh"]), 3),
    }
    for entity_id, value in prices.items():
        result = ha_service_response(
            "input_number", "set_value",
            {"entity_id": entity_id, "value": value},
        )
        if result is None:
            return {"status": "HA_WRITE_FAILED", "slot_start": start.isoformat(),
                    "entity_id": entity_id}
        if ha_state is not None:
            state = ha_state(entity_id) or {}
            try:
                actual = float(state.get("state"))
            except (TypeError, ValueError):
                actual = None
            if actual is None or abs(actual - value) > 0.0005:
                attributes = state.get("attributes") or {}
                return {
                    "status": "HA_VERIFY_FAILED",
                    "slot_start": start.isoformat(),
                    "entity_id": entity_id,
                    "expected": value,
                    "actual": actual,
                    "helper_min": attributes.get("min"),
                    "helper_max": attributes.get("max"),
                }
    return {"status": "OK", "slot_start": start.isoformat(), "prices": prices}


def rce_event_keys(clock: datetime) -> tuple[str, ...]:
    """Return valid completion markers for the price day visible after restart."""
    if clock.hour >= 14:
        return (f"RCE_{clock.date()}_NEXT",)
    return (f"RCE_{clock.date()}_TODAY",
            f"RCE_{clock.date() - timedelta(days=1)}_NEXT")


def should_run_slot_replan(clock: datetime, current_slot: datetime,
                           last_published_at: datetime | None,
                           rce_ready: bool) -> bool:
    """Run at most once per open slot, after inputs settle and only with valid RCE."""
    if not rce_ready or clock.hour == 0:
        return False
    minute_in_slot = clock.minute % 15
    if minute_in_slot < 2 or minute_in_slot > 6:
        return False
    if last_published_at is None:
        return True
    published = last_published_at.replace(tzinfo=None)
    return published < current_slot.replace(tzinfo=None)


def update_telemetry_health(state: dict, lock: Any, telemetry_ok: bool,
                            now_utc: datetime, degraded_after: int = 120,
                            stale_after: int = 300) -> dict:
    """Update readiness without allowing the scheduler heartbeat to mask HA loss."""
    with lock:
        state["last_telemetry_attempt"] = now_utc.isoformat()
        if telemetry_ok:
            state["last_telemetry_success"] = now_utc.isoformat()
            state["telemetry_consecutive_failures"] = 0
        else:
            state["telemetry_consecutive_failures"] = int(
                state.get("telemetry_consecutive_failures") or 0) + 1
        last_success = state.get("last_telemetry_success")
        try:
            age = max(0.0, (now_utc - datetime.fromisoformat(last_success)).total_seconds())
        except (TypeError, ValueError):
            age = None
        state["telemetry_age_seconds"] = None if age is None else round(age, 1)
        if telemetry_ok:
            state["ha_input"] = "CONNECTED"
            state["readiness"] = "READY"
        elif age is None or age >= stale_after:
            state["ha_input"] = "STALE"
            state["readiness"] = "STALE_TELEMETRY"
        elif age >= degraded_after or state["telemetry_consecutive_failures"] >= 2:
            state["ha_input"] = "DEGRADED"
            state["readiness"] = "DEGRADED"
        else:
            state["ha_input"] = "PARTIAL"
            state["readiness"] = "DEGRADED"
        return {"age": age, "readiness": state["readiness"]}


@dataclass(frozen=True)
class SchedulerAdapters:
    options: dict
    state: dict
    lock: Any
    log: Any
    db: Callable
    local_now: Callable
    slot_start: Callable
    capture_telemetry: Callable
    close_finished_slots: Callable
    backfill_execution_details: Callable
    run_serialized: Callable
    rebuild_recovery_materializations: Callable
    learn_missing_load: Callable
    expire_process_overrides: Callable
    expire_stale_commands: Callable
    maintain_todo_archive: Callable
    ensure_slot_calendar: Callable
    refresh_pv_forecast: Callable
    refresh_weather_forecast: Callable
    record_event: Callable
    refresh_rce: Callable
    complete_rce_cycle: Callable
    run_planner: Callable
    run_ppd: Callable
    stage_executor_commands: Callable
    dispatch_ready_commands: Callable
    run_analytics: Callable
    run_ai_observer: Callable
    generate_diagnostic_report: Callable
    capture_appliances: Callable
    maintain_backup: Callable
    publish_current_prices: Callable


def run_scheduler(a: SchedulerAdapters) -> None:
    """Run scheduling only; all domain operations arrive through explicit adapters."""
    previous = None
    last_price_publish_slot = None
    recovery_rebuild_day = None
    todo_archive_day = None
    def module_activity(module: str, activity: str, status: str | None = None) -> None:
        with a.lock:
            if status is not None:
                a.state["modules"][module] = status
            a.state.setdefault("module_details", {})[module] = {
                "activity": activity,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
    while True:
        clock = a.local_now()
        start = a.slot_start(clock)
        key = start.isoformat()
        error = None
        planner_health = a.state.get("modules", {}).get("planner", "STARTING")
        ppd_health = a.state.get("modules", {}).get("ppd", "STARTING")
        try:
            telemetry_ok = a.capture_telemetry()
            health = update_telemetry_health(
                a.state, a.lock, telemetry_ok, datetime.now(timezone.utc),
                int(a.options.get("telemetry_degraded_seconds", 120)),
                int(a.options.get("telemetry_stale_seconds", 300)),
            )
            a.close_finished_slots()
            a.backfill_execution_details()
            if recovery_rebuild_day is None or (clock.hour == 1 and recovery_rebuild_day != clock.date()):
                a.run_serialized("recovery_materializations", a.rebuild_recovery_materializations,
                                 int(a.options.get("recovery_lookback_days", 7)))
                recovery_rebuild_day = clock.date()
            a.learn_missing_load()
            a.expire_process_overrides()
            a.expire_stale_commands()
            if clock.hour == 0 and todo_archive_day != clock.date():
                a.maintain_todo_archive()
                todo_archive_day = clock.date()
            if key != previous:
                a.ensure_slot_calendar(clock.date(), clock.date() + timedelta(days=1))
                # Closing slots and rebuilding their hourly/daily projections belong
                # to the same transition.  The rebuild is idempotent and also fills
                # gaps left by downtime without inventing telemetry.
                a.run_serialized("slot_materializations", a.rebuild_recovery_materializations, 1)
                a.refresh_pv_forecast()
                a.refresh_weather_forecast()
                a.record_event("slot_opened", "core",
                               {"slot_start": key, "recovered_after_restart": previous is None})
                previous = key
            # A transient HA failure at slot opening must not leave a stale price
            # for the next 15 minutes. Retry every scheduler cycle until both
            # helpers are written and read-back verification succeeds.
            if last_price_publish_slot != key:
                price_result = a.publish_current_prices(start)
                with a.lock:
                    a.state["current_prices"] = price_result
                if price_result.get("status") == "OK":
                    last_price_publish_slot = key
                else:
                    a.log.warning("current slot price publication failed; retry in 60s: %s",
                                  price_result)
            minute = clock.minute
            hour = clock.hour
            with a.db() as conn, conn.cursor() as cur:
                cur.execute("SELECT MAX(published_at) last_run FROM ems_gpt_plan_runs WHERE status='PUBLISHED'")
                last_run = cur.fetchone()["last_run"]
                rce_keys = rce_event_keys(clock)
                rce_key = rce_keys[0]
                placeholders = ",".join(["%s"] * len(rce_keys))
                cur.execute(f"""SELECT created_at,payload_json FROM ems_gpt_core_events
                  WHERE event_type IN ({placeholders}) ORDER BY created_at DESC LIMIT 1""", rce_keys)
                prior_rce = cur.fetchone()
                rce_done = prior_rce is not None
            if rce_done and a.state.get("rce", {}).get("status") == "NOT_RUN":
                try:
                    prior_result = json.loads(prior_rce.get("payload_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    prior_result = {}
                with a.lock:
                    a.state["rce"] = {
                        "status": "ALREADY_COMPLETED",
                        "target_day": prior_result.get("day"),
                        "rows": prior_result.get("rows"),
                        "expected": prior_result.get("expected"),
                        "completed_at": prior_rce.get("created_at"),
                    }
                a.log.info("RCE state restored: status=ALREADY_COMPLETED rows=%s/%s target=%s",
                           prior_result.get("rows"), prior_result.get("expected"),
                           prior_result.get("day"))
            rce_due = 14 <= hour <= 16 and minute % 10 == 0
            if rce_due and not rce_done:
                target = clock.date() + timedelta(days=1)
                with a.lock:
                    a.state["rce"] = {
                        "status": "RUNNING", "target_day": str(target),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }
                a.log.info("RCE import started: target=%s", target)
                try:
                    result = a.refresh_rce(target)
                    a.record_event("rce_import_attempt", "core", result,
                                   "INFO" if result["status"] == "OK" else "WARNING")
                    completed = result
                    if result["status"] == "OK":
                        a.record_event(rce_key, "core", result)
                        completed = a.complete_rce_cycle(result, "rce_import")
                    planner_status = completed.get("planner", {}).get("status")
                    planner_health = "RUNNING" if planner_status == "ACCEPTED" else "DEGRADED"
                    with a.lock:
                        a.state["rce"] = {
                            "status": result.get("status"), "target_day": str(target),
                            "rows": result.get("rows"), "expected": result.get("expected"),
                            "planner_status": planner_status,
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    a.log.info("RCE import completed: status=%s rows=%s/%s planner=%s",
                               result.get("status"), result.get("rows"),
                               result.get("expected"), planner_status)
                except Exception as exc:
                    with a.lock:
                        a.state["rce"] = {
                            "status": "ERROR", "target_day": str(target),
                            "error": str(exc),
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    a.log.exception("RCE import failed: target=%s", target)
                    raise
            if should_run_slot_replan(clock, start, last_run, rce_done):
                module_activity("planner", "Przeliczanie planu dla bieżącego slotu", "RUNNING")
                module_activity("ppd", "Oczekiwanie na wynik planera", "WAITING")
                try:
                    replan = a.run_serialized("planner", a.run_planner, "slot_replan")
                    planner_health = "RUNNING" if replan.get("status") != "WAITING" else "WAITING"
                    module_activity("planner", "Plan opublikowany", planner_health)
                    if planner_health == "RUNNING":
                        module_activity("ppd", "Wyliczanie decyzji z opublikowanego planu", "RUNNING")
                        try:
                            ppd = a.run_serialized(
                                "ppd", a.run_ppd, replan.get("run_id"), "slot_replan")
                            ppd_health = "RUNNING"
                            module_activity("ppd", "Decyzje PPD opublikowane", "RUNNING")
                        except Exception as ppd_exc:
                            ppd = {"status": "ERROR", "error": str(ppd_exc)}
                            ppd_health = "DEGRADED"
                            module_activity("ppd", f"Błąd PPD: {ppd_exc}", "DEGRADED")
                            a.record_event("ppd_run_failed", "ppd", {
                                "slot_start": key, "plan_run_id": replan.get("run_id"),
                                "error": str(ppd_exc),
                            }, "ERROR")
                            a.log.exception("PPD run failed after published plan: slot=%s", key)
                    else:
                        ppd = {"status": "WAITING_FOR_PLAN"}
                        ppd_health = "WAITING"
                        module_activity("ppd", "Oczekiwanie na plan", "WAITING")
                    with a.lock:
                        a.state["planner_failure_latched"] = None
                    a.record_event("slot_replan_completed", "planner", {
                        "slot_start": key, **replan, "ppd": ppd,
                    })
                except Exception as exc:
                    planner_health = "DEGRADED"
                    ppd_health = "WAITING"
                    module_activity("planner", f"Błąd przeliczenia: {exc}", "DEGRADED")
                    module_activity("ppd", "Zachowano ostatnie poprawne decyzje", "DEGRADED")
                    with a.lock:
                        a.state["planner_failure_latched"] = str(exc)
                    a.record_event("slot_replan_failed", "planner", {
                        "slot_start": key, "error": str(exc),
                        "last_published_at": last_run,
                    }, "ERROR")
                    a.log.exception("slot replan failed: slot=%s", key)
            # Never issue fresh control commands from stale or absent HA input.
            if telemetry_ok:
                a.stage_executor_commands()
                a.dispatch_ready_commands()
            else:
                a.log.warning("HA telemetry unavailable; executor dispatch suppressed")
            appliance_result = a.capture_appliances()
            backup_result = a.maintain_backup()
            if minute < 15:
                with a.db() as conn, conn.cursor() as cur:
                    cur.execute("SELECT MAX(completed_at) v FROM ems_gpt_core_analytics_runs WHERE status='COMPLETED'")
                    last_analytics = cur.fetchone()["v"]
                if last_analytics is None or a.local_now().replace(tzinfo=None) - last_analytics >= timedelta(minutes=50):
                    analytics_result = a.run_serialized("analytics", a.run_analytics)
                    a.run_ai_observer(analytics_result.get("run_id"))
            if (hour, minute) in ((2, 8), (8, 8), (14, 23), (20, 8)):
                with a.db() as conn, conn.cursor() as cur:
                    cur.execute("SELECT MAX(created_at) v FROM ems_gpt_core_diagnostic_reports")
                    last_diag = cur.fetchone()["v"]
                if last_diag is None or a.local_now().replace(tzinfo=None) - last_diag >= timedelta(minutes=10):
                    a.run_serialized("diagnostics", a.generate_diagnostic_report, "scheduled")
            with a.lock:
                if a.state.get("planner_failure_latched"):
                    planner_health = "DEGRADED"
                a.state["database"] = "CONNECTED"
                a.state["status"] = "RUNNING" if health["readiness"] == "READY" else "DEGRADED"
                a.state["appliances"] = appliance_result
                a.state["backup"] = backup_result
                a.state["modules"].update(
                    core="RUNNING", planner=planner_health,
                    ppd=ppd_health,
                    analytics="RUNNING",
                    diagnostics="RUNNING",
                    appliances="RUNNING" if appliance_result.get("status") == "OK" else appliance_result.get("status"),
                    backup=backup_result.get("status"),
                    ai_observer="DISABLED" if not a.options.get("ai_observer_enabled") else "SHADOW_READ_ONLY",
                )
                stamp = datetime.now(timezone.utc).isoformat()
                details = a.state.setdefault("module_details", {})
                details["core"] = {"activity": "Cykl kontrolny zakończony", "updated_at": stamp}
                details["executor"] = {"activity": "Sterowanie wyłączone" if a.state.get("executor") == "OFF" else "Realizacja zatwierdzonego planu", "updated_at": stamp}
                details["appliances"] = {"activity": "Odczyt liczników urządzeń", "updated_at": stamp}
                details["backup"] = {"activity": "Wyłączony" if backup_result.get("status") == "DISABLED" else "Kontrola kopii zapasowej", "updated_at": stamp}
        except Exception as exc:
            error = str(exc)
            a.log.exception("engine cycle failed")
            with a.lock:
                a.state["database"] = "ERROR"
                a.state["status"] = "DEGRADED"
                a.state["readiness"] = "ENGINE_ERROR"
        with a.lock:
            a.state["active_slot"] = key
            a.state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            a.state["last_error"] = error
        time.sleep(60)
