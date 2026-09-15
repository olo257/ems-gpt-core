"""Shared runtime state and serialized heavy-job coordination."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from types import SimpleNamespace
from typing import Any


def build_runtime(app_name: str, app_version: str, log: Any):
    lock = threading.Lock()
    heavy_job_lock = threading.RLock()

    def run_serialized(job_name: str, func, *args, **kwargs):
        """Serialize heavy database jobs across engine and HTTP worker threads."""
        started = time.monotonic()
        with heavy_job_lock:
            waited = round(time.monotonic() - started, 3)
            if waited >= 1.0:
                log.warning("heavy job %s waited %.3fs for lock", job_name, waited)
            return func(*args, **kwargs)

    state = {
        "app": app_name,
        "version": app_version,
        "status": "STARTING",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "database": "CONNECTING",
        "ha_input": "CONNECTING",
        "executor": "CONNECTOR_REQUIRED",
        "active_slot": None,
        "last_heartbeat": None,
        "last_telemetry_attempt": None,
        "last_telemetry_success": None,
        "telemetry_age_seconds": None,
        "telemetry_consecutive_failures": 0,
        "readiness": "STARTING",
        "last_error": None,
        "rce": {"status": "NOT_RUN"},
        "migrated_tables": 0,
        "modules": {
            "core": "STARTING", "planner": "STARTING", "ppd": "STARTING",
            "analytics": "STARTING", "ai_observer": "SHADOW_READ_ONLY",
            "executor": "CONNECTOR_REQUIRED",
            "appliances": "STARTING", "backup": "DISABLED",
            "diagnostics": "STARTING",
        },
        "module_details": {
            "core": {"activity": "Uruchamianie", "updated_at": None},
            "planner": {"activity": "Oczekiwanie na pierwszy plan", "updated_at": None},
            "ppd": {"activity": "Oczekiwanie na plan", "updated_at": None},
            "analytics": {"activity": "Oczekiwanie", "updated_at": None},
            "ai_observer": {"activity": "Tylko odczyt", "updated_at": None},
            "executor": {"activity": "Oczekiwanie na konfigurację", "updated_at": None},
            "appliances": {"activity": "Oczekiwanie", "updated_at": None},
            "backup": {"activity": "Wyłączony", "updated_at": None},
            "diagnostics": {"activity": "Oczekiwanie", "updated_at": None},
        },
        "recovery_contract": "CORE_RECOVERY_0_24_2_R6",
    }
    return SimpleNamespace(lock=lock, state=state, run_serialized=run_serialized)
