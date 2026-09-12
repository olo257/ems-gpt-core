from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


@dataclass(frozen=True)
class TimeAdapters:
    timezone: Any
    slot_minutes: int


def build_time_service(a: TimeAdapters) -> SimpleNamespace:
    if int(a.slot_minutes) <= 0:
        raise ValueError("slot_minutes must be greater than zero")

    def local_now() -> datetime:
        return datetime.now(timezone.utc).astimezone(a.timezone)

    def slot_start(now: datetime | None = None) -> datetime:
        value = (now or local_now()).astimezone(a.timezone)
        minutes = int(a.slot_minutes)
        return value.replace(
            minute=(value.minute // minutes) * minutes,
            second=0,
            microsecond=0,
        )

    return SimpleNamespace(local_now=local_now, slot_start=slot_start)
