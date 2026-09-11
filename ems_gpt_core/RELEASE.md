# EMS-GPT Core 0.25.11

- Binary HP policy: `heat_pump_window` is the only HP planner decision.
- When the night minimum is below the configured threshold, schedule at least 10 hours of Heat+DHW per local day.
- Prefer one continuous run; every run is at least 2 hours and internal breaks are 1–3 hours.
- Hourly replans count elapsed effective states, including manual FORCE_ON/BLOCK overrides, and optimize only the remaining obligation.
- HP cost uses grid purchase price, PV opportunity cost and configured planned HP power; loads never issue purchase decisions.
- Manual control origin is persisted as AUTO, MANUAL_FORCE_ON, MANUAL_BLOCK or EXTERNAL_MANUAL.
- Removes retired MANUAL_CIRCULATION and HP_DHW process records and legacy HP PPD columns; DHW/HP telemetry remains intact.
- PPD contract updated to CORE_0_4_1.

Rollback source: `/share/ems-gpt-core-rollback/0.25.10-before-0.25.11`.
