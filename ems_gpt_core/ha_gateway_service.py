"""Home Assistant state/service gateway and read-only Deye TOU snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from types import SimpleNamespace
from typing import Any
import urllib.request


@dataclass(frozen=True)
class HomeAssistantAdapters:
    supervisor_token: str | None
    ha_api: str
    log: Any


def build_home_assistant_gateway(a: HomeAssistantAdapters):
    def ha_state(entity_id: str) -> dict | None:
        if not a.supervisor_token:
            return None
        req = urllib.request.Request(
            f"{a.ha_api}/states/{entity_id}",
            headers={"Authorization": f"Bearer {a.supervisor_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)
        except Exception:
            return None

    def ha_service_response(
        domain: str, service: str, payload: dict, *, return_response: bool = False
    ) -> dict | None:
        if not a.supervisor_token:
            return None
        service_url = f"{a.ha_api}/services/{domain}/{service}"
        if return_response:
            service_url += "?return_response"
        req = urllib.request.Request(
            service_url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {a.supervisor_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        except Exception as exc:
            a.log.warning("HA service response failed: %s.%s: %s", domain, service, exc)
            return None

    def number(state: dict | None, attribute: str | None = None) -> float | None:
        if not isinstance(state, dict):
            return None
        try:
            value = state.get("attributes", {}).get(attribute) if attribute else state.get("state")
            return float(value) if value not in (None, "unknown", "unavailable", "") else None
        except (TypeError, ValueError):
            return None

    def tou_program_snapshot() -> list[dict] | None:
        """Read the six Deye TOU boundaries and SOC floors without writing them."""
        programs = []
        for index in range(1, 7):
            time_state = ha_state(f"time.inverter_program_{index}_time")
            soc_state = ha_state(f"number.inverter_program_{index}_soc")
            raw_time = str((time_state or {}).get("state") or "")
            try:
                hour, minute = [int(value) for value in raw_time.split(":")[:2]]
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except (TypeError, ValueError):
                return None
            soc = number(soc_state)
            if soc is None or not 0 <= soc <= 100:
                return None
            programs.append({"program": index, "minute": hour * 60 + minute, "soc": float(soc)})
        programs.sort(key=lambda item: (item["minute"], item["program"]))
        return programs

    def active_tou_program(moment: datetime, programs: list[dict] | None) -> dict | None:
        """Resolve the Deye wall-clock TOU program for a local slot or live instant."""
        if not programs:
            return None
        minute = moment.hour * 60 + moment.minute
        active = programs[-1]
        for program in programs:
            if program["minute"] <= minute:
                active = program
            else:
                break
        return active

    return SimpleNamespace(
        ha_state=ha_state,
        ha_service_response=ha_service_response,
        number=number,
        tou_program_snapshot=tou_program_snapshot,
        active_tou_program=active_tou_program,
    )
