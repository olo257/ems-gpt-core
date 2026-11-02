"""Authorize only an existing published current-slot BUY after replanning fails."""
from __future__ import annotations

import math
from datetime import timedelta


def fallback_import_reason(plan, telemetry, now, start, slot_minutes,
                           threshold=0.02, max_age_seconds=120):
    if (not plan or not plan.get('plan_run_id') or plan.get('plan_stage') != 'PUBLISHED'
            or plan.get('published_run_status') != 'PUBLISHED'
            or plan.get('plan_validation_status') != 'ACCEPTED'):
        return 'NO_ACCEPTED_CURRENT_PLAN'
    if (plan.get('slot_start') != start or plan.get('slot_end') != start+timedelta(minutes=slot_minutes)
            or not start <= now < plan['slot_end']):
        return 'PLAN_SLOT_NOT_CURRENT'
    published = plan.get('plan_published_at')
    if published is None or published > now:
        return 'PLAN_PUBLICATION_UNAVAILABLE'
    # Flags are decoded strictly; a malformed permission never authorizes BUY.
    if plan.get('buy_window') not in (True, 1, '1', b'1') or plan.get('sale_window') not in (False, 0, '0', b'0'):
        return 'BUY_PERMISSION_MISSING_OR_SELL'
    if plan.get('grid_policy_planned') != 'BUY_ALLOWED':
        return 'GRID_POLICY_NOT_BUY'
    if plan.get('requested_state') == 'FORCE_OFF':
        return 'MANUAL_FORCE_OFF'
    try:
        buy = float(plan['planned_buy_kwh'])
        raw_target = plan.get('soc_charge_target_pct')
        target = float(raw_target if raw_target is not None else plan['soc_target_pct'])
        soc = float(telemetry['soc_pct'])
        captured = telemetry['captured_at']
        if not all(math.isfinite(v) for v in (buy,target,soc)) or not (0 <= soc <= 100 and 0 < target <= 100):
            return 'INVALID_TARGET_OR_SOC'
        if not 0 <= (now-captured).total_seconds() <= max_age_seconds:
            return 'TELEMETRY_STALE_OR_FUTURE'
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'TARGET_OR_TELEMETRY_UNAVAILABLE'
    if buy <= threshold:
        return 'NO_PLANNED_BATTERY_IMPORT'
    if soc >= target-0.01:
        return 'TARGET_ALREADY_REACHED'
    return None
