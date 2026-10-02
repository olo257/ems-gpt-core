"""Read-only EMS Observer service."""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

from observer_details import format_observer_todo, metric_label


def _number(value, default=None):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _history_key(row):
    recorded = row.get("actual_recorded_at")
    slot = row.get("slot_start")
    if isinstance(recorded, str):
        try:
            recorded = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
        except ValueError:
            recorded = None
    if isinstance(slot, str):
        try:
            slot = datetime.fromisoformat(slot.replace("Z", "+00:00"))
        except ValueError:
            slot = None
    recorded = recorded or slot
    if not isinstance(recorded, datetime) or not isinstance(slot, datetime):
        return None
    if recorded.tzinfo is not None:
        recorded = recorded.astimezone(timezone.utc).replace(tzinfo=None)
    if slot.tzinfo is not None:
        slot = slot.astimezone(timezone.utc).replace(tzinfo=None)
    return recorded, slot


def audit_operational_rows(history_rows, future_rows, *, options, incident_rows=None):
    """Build evidence-based, read-only checks from plans and slot execution."""
    suggestions = []
    incident_rows = history_rows if incident_rows is None else incident_rows

    def add(metric, value, threshold, severity, suggestion, evidence):
        suggestions.append({"metric": metric, "value": value, "threshold": threshold,
                            "severity": severity, "suggestion": suggestion,
                            "evidence": evidence})

    zero_price = []
    immediate_zero_price = False
    for row in [*incident_rows, *future_rows]:
        price = _number(row.get("price_sell_pln_kwh"))
        if price is None or price > 0:
            continue
        planned = (_number(row.get("planned_sell_kwh"), 0) or 0) + \
                  (_number(row.get("planned_pv_export_kwh"), 0) or 0)
        executed = (_number(row.get("actual_sell_kwh"), 0) or 0) + \
                   (_number(row.get("actual_pv_export_kwh"), 0) or 0)
        if planned > 0.001 or executed > 0.001:
            zero_price.append({"slot_start": row.get("slot_start"), "price_sell_pln_kwh": price,
                               "planned_export_kwh": round(planned, 4),
                               "actual_export_kwh": round(executed, 4),
                               "market_window": row.get("market_window")})
            if row in future_rows and planned > 0.001:
                immediate_zero_price = True
    if zero_price:
        completed = any(item["actual_export_kwh"] > 0.001 for item in zero_price)
        add("export_at_nonpositive_price", len(zero_price), 0,
            "CRITICAL" if completed or immediate_zero_price else "WARNING",
            "Zablokuj sprzedaż przy cenie <= 0 PLN/kWh i sprawdź decyzję SELL_PV/SELL_BAT. Observer nie zmienia sterowania.",
            zero_price[:12])

    threshold_kwh = max(0.0, float(options.get("technical_flow_threshold_kwh", 0.05)))
    off_window_buys = [row for row in incident_rows
                       if str(row.get("market_window") or "").upper() != "BUY"
                       and (_number(row.get("actual_buy_kwh"), 0) or 0) > threshold_kwh]
    if off_window_buys:
        add("import_outside_buy_window", len(off_window_buys), threshold_kwh, "CRITICAL",
            "Zweryfikuj import energii z sieci poza oknem BUY; wartości poniżej progu technicznego są pomijane.",
            [{"slot_start": row.get("slot_start"), "market_window": row.get("market_window"),
              "actual_buy_kwh": row.get("actual_buy_kwh"), "planned_buy_kwh": row.get("planned_buy_kwh")}
             for row in off_window_buys[:12]])

    # PV bias is evaluated by local day, not by an isolated noisy slot.
    by_day = defaultdict(lambda: {"forecast": 0.0, "actual": 0.0, "slots": 0})
    for row in history_rows:
        day = _day(row.get("slot_start"))
        forecast = _number(row.get("forecast_pv_total_kwh"))
        actual = _number(row.get("actual_pv_total_kwh"))
        if day is None or forecast is None or actual is None:
            continue
        by_day[day]["forecast"] += max(0.0, forecast)
        by_day[day]["actual"] += max(0.0, actual)
        by_day[day]["slots"] += 1
    recent_days = sorted(by_day)[-7:]
    sample = [by_day[day] for day in recent_days]
    forecast_sum = sum(day["forecast"] for day in sample)
    actual_sum = sum(day["actual"] for day in sample)
    under_days = [day.isoformat() for day in recent_days
                  if by_day[day]["slots"] >= 8
                  and by_day[day]["actual"] - by_day[day]["forecast"] >= 0.5
                  and by_day[day]["actual"] > by_day[day]["forecast"] * 1.2]
    pv_under_pct = (100 * (actual_sum - forecast_sum) / forecast_sum
                    if forecast_sum > 1.0 else None)
    if len(under_days) >= 3 and pv_under_pct is not None and pv_under_pct >= 20:
        add("pv_forecast_underestimation_7d", round(pv_under_pct, 2), 20, "WARNING",
            "Prognoza PV jest systematycznie zaniżana. Sprawdź osobno PV1 i PV2 oraz korektę prognozy; nie zmieniaj planera na podstawie pojedynczego dnia.",
            {"days": [day.isoformat() for day in recent_days], "underestimated_days": under_days,
             "forecast_pv_kwh": round(forecast_sum, 3), "actual_pv_kwh": round(actual_sum, 3),
             "daily": [{"day": day.isoformat(), **{key: round(value, 3) if isinstance(value, float) else value
                                                    for key, value in by_day[day].items()}}
                       for day in recent_days]})

    for series_name, forecast_key, actual_key, label in (
        ("pv1", "forecast_pv1_kwh", "actual_pv1_kwh", "PV1"),
        ("pv2", "forecast_pv2_kwh", "actual_pv2_kwh", "PV2"),
        ("load", "forecast_load_kwh", "actual_load_kwh", "zużycia"),
    ):
        daily_series = defaultdict(lambda: {"forecast": 0.0, "actual": 0.0, "slots": 0})
        for row in history_rows:
            day = _day(row.get("slot_start"))
            forecast = _number(row.get(forecast_key))
            actual = _number(row.get(actual_key))
            if day is not None and forecast is not None and actual is not None:
                daily_series[day]["forecast"] += max(0.0, forecast)
                daily_series[day]["actual"] += max(0.0, actual)
                daily_series[day]["slots"] += 1
        days = sorted(daily_series)[-7:]
        forecasts = sum(daily_series[day]["forecast"] for day in days)
        actuals = sum(daily_series[day]["actual"] for day in days)
        repeat_days = [day.isoformat() for day in days
                       if daily_series[day]["slots"] >= 8
                       and daily_series[day]["actual"] - daily_series[day]["forecast"] >= 0.5
                       and daily_series[day]["actual"] > daily_series[day]["forecast"] * 1.2]
        bias = 100 * (actuals - forecasts) / forecasts if forecasts > 1.0 else None
        if len(repeat_days) >= 3 and bias is not None and bias >= 20:
            add(f"{series_name}_forecast_underestimation_7d", round(bias, 2), 20, "WARNING",
                f"Prognoza {label} jest systematycznie zaniżana. Sprawdź jej źródło i korektę; nie zmieniaj automatycznie planera.",
                {"days": [day.isoformat() for day in days], "underestimated_days": repeat_days,
                 "forecast_kwh": round(forecasts, 3), "actual_kwh": round(actuals, 3)})

    hp_outside = []
    for row in [*incident_rows, *future_rows]:
        hp = _number(row.get("forecast_heat_pump_load_kwh"), 0) or 0
        slot = row.get("slot_start")
        if hp <= 0.02 or not isinstance(slot, datetime):
            continue
        if (str(row.get("market_window") or "").upper() == "SELL"
                or slot.hour < 7 or slot.hour >= 19):
            hp_outside.append({"slot_start": slot, "market_window": row.get("market_window"),
                               "forecast_heat_pump_load_kwh": hp})
    if hp_outside:
        add("heat_pump_outside_window", len(hp_outside), 0, "WARNING",
            "Plan HP_HEAT_DHW zawiera energię poza dozwolonym oknem 07:00–19:00 lub w SELL.",
            hp_outside[:12])

    completed_days = defaultdict(list)
    for row in incident_rows:
        day = _day(row.get("slot_start"))
        if day:
            completed_days[day].append(row)
    for day in sorted(completed_days)[-1:]:
        rows = sorted(completed_days[day], key=lambda row: row.get("slot_start"))
        close = rows[-1]
        slot = close.get("slot_start")
        soc_start = _number(close.get("soc_start_pct"))
        soc_delta = _number(close.get("soc_delta_pct"))
        actual_close = soc_start + soc_delta if soc_start is not None and soc_delta is not None else None
        if isinstance(slot, datetime) and slot.hour >= 23 and slot.minute >= 30 and actual_close is not None and actual_close < 35:
            add("end_of_day_soc_below_target_range", round(actual_close, 2), 35, "WARNING",
                "Rzeczywisty SOC na koniec doby jest ponad 5 pp poniżej oczekiwanego poziomu około 40%. Sprawdź target i zabezpieczenie porannego okna.",
                {"day": day.isoformat(), "slot_start": slot,
                 "soc_start_pct": soc_start, "soc_delta_pct": soc_delta,
                 "actual_soc_close_pct": round(actual_close, 2), "expected_soc_pct": 40})

    future_days = defaultdict(list)
    for row in future_rows:
        slot = row.get("slot_start")
        if isinstance(slot, datetime):
            future_days[slot.date()].append(row)
    for day, rows in future_days.items():
        closing_rows = [row for row in rows
                        if isinstance(row.get("slot_start"), datetime)
                        and row["slot_start"].hour >= 23 and row["slot_start"].minute >= 30]
        if not closing_rows:
            continue
        close = max(closing_rows, key=lambda row: row["slot_start"])
        planned_close = _number(close.get("soc_end_plan_pct"))
        required = _number(close.get("soc_required_pct"))
        if planned_close is not None and required is not None and planned_close + 0.5 < required:
            add("planned_end_soc_below_required", round(required - planned_close, 2), 0.5, "WARNING",
                "Planowany SOC na zamknięciu doby jest niższy od wymaganego. Sprawdź plan zakupu i ciągłość do porannego okna.",
                {"day": day.isoformat(), "slot_start": close["slot_start"],
                 "soc_end_plan_pct": planned_close, "soc_required_pct": required})

    return suggestions, {"pv_forecast_7d_bias_pct": None if pv_under_pct is None else round(pv_under_pct, 2),
                         "pv_underestimated_days_7d": len(under_days),
                         "audited_history_slots": len(history_rows),
                         "audited_new_history_slots": len(incident_rows),
                         "audited_future_slots": len(future_rows)}


def run_ai_observer(source_ref: str | None = None, *, options, db, create_todo, reconcile_observer_todos, record_event) -> dict:
    """Read-only shadow observer: persist evidence and suggestions, never mutate plan or controls."""
    if not bool(options.get("ai_observer_enabled", False)):
        return {"status": "DISABLED"}
    with db() as conn, conn.cursor() as cur:
        if source_ref:
            cur.execute("SELECT * FROM ems_gpt_core_analytics_runs WHERE run_id=%s AND status='COMPLETED'", (source_ref,))
        else:
            cur.execute("SELECT * FROM ems_gpt_core_analytics_runs WHERE status='COMPLETED' ORDER BY completed_at DESC LIMIT 1")
        analytics = cur.fetchone()
        if not analytics:
            return {"status": "WAITING_FOR_ANALYTICS"}
        source_ref = analytics["run_id"]
        cur.execute("SELECT * FROM ems_gpt_core_ai_runs WHERE role_name='EMS_OBSERVER' AND source_ref=%s", (source_ref,))
        existing = cur.fetchone()
        if existing:
            return {"status": existing["status"], "run_id": existing["run_id"], "source_ref": source_ref, "deduplicated": True}
        cur.execute("""SELECT prompt_json FROM ems_gpt_core_ai_runs
          WHERE role_name='EMS_OBSERVER' AND status='COMPLETED'
          ORDER BY completed_at DESC LIMIT 1""")
        previous_run = cur.fetchone()
        previous_watermark = None
        if previous_run:
            try:
                previous_prompt = previous_run.get("prompt_json")
                if isinstance(previous_prompt, (bytes, bytearray)):
                    previous_prompt = previous_prompt.decode("utf-8")
                if isinstance(previous_prompt, str):
                    previous_prompt = json.loads(previous_prompt)
                previous_watermark = (previous_prompt or {}).get("analysis_scope", {}).get("history_watermark")
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                previous_watermark = None
        cur.execute("""SELECT s.slot_start,s.actual_recorded_at,s.market_window,s.price_sell_pln_kwh,
          s.forecast_pv1_kwh,s.forecast_pv2_kwh,s.forecast_pv_total_kwh,s.actual_pv1_kwh,
          s.actual_pv2_kwh,s.actual_pv_total_kwh,s.forecast_load_kwh,s.actual_load_kwh,
          s.planned_buy_kwh,s.actual_buy_kwh,s.planned_sell_kwh,s.actual_sell_kwh,
          s.planned_pv_export_kwh,s.actual_pv_export_kwh,s.forecast_heat_pump_load_kwh,
          s.heat_pump_window,s.soc_end_plan_pct,s.soc_required_pct,
          d.actual_grid_export_kwh,d.soc_start_pct,d.soc_delta_pct,d.soc_min_pct,d.coverage_pct
          FROM ems_gpt_slots s LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
          WHERE s.actual_recorded_at IS NOT NULL AND s.slot_start>=DATE_SUB(NOW(6),INTERVAL 7 DAY)
          ORDER BY s.slot_start""")
        history_rows = list(cur.fetchall())
        if previous_watermark:
            cutoff = _history_key(previous_watermark)
            new_history_rows = [row for row in history_rows
                                if (key := _history_key(row)) is not None and key > cutoff]
        else:
            new_history_rows = history_rows
        cur.execute("""SELECT slot_start,market_window,price_sell_pln_kwh,planned_sell_kwh,
          planned_pv_export_kwh,forecast_heat_pump_load_kwh,heat_pump_window,
          soc_end_plan_pct,soc_required_pct
          FROM ems_gpt_slots WHERE actual_recorded_at IS NULL AND slot_start>=NOW(6)
          ORDER BY slot_start LIMIT 96""")
        future_rows = list(cur.fetchall())
        watermark_row = max(history_rows, key=lambda row: _history_key(row) or (datetime.min, datetime.min),
                            default=None)
        watermark = None
        if watermark_row:
            watermark_key = _history_key(watermark_row)
            watermark = {"actual_recorded_at": watermark_key[0].isoformat(),
                         "slot_start": watermark_key[1].isoformat()}
        prompt = {"contract": "EMS_AI_OBSERVER_0_39_12", "mode": "SHADOW_READ_ONLY",
                  "forbidden": ["PLAN_WRITE", "PPD_WRITE", "COMMAND_WRITE", "HA_SERVICE_CALL"],
                  "analysis_scope": {"history_days": 7, "history_slots": len(history_rows),
                                     "new_history_slots": len(new_history_rows),
                                     "history_watermark": watermark,
                                     "future_slots": len(future_rows),
                                     "checks": ["plan_vs_execution", "PV_forecast_bias_by_series",
                                                "export_price_floor", "import_window", "HP_window",
                                                "end_of_day_SOC"]},
                  "analytics": {k: analytics.get(k) for k in (
                      "slots_scanned", "complete_slots", "quality_score", "pv1_wape_pct", "pv2_wape_pct",
                      "pv_wape_pct", "load_wape_pct", "import_wape_pct", "export_wape_pct",
                      "pv_bias_kwh", "load_bias_kwh", "import_bias_kwh", "export_bias_kwh",
                      "soc_mae_pct", "net_cost_variance_pln", "pv_daylight_slots",
                      "metric_confidence_pct", "import_active_mae_kwh", "export_active_mae_kwh",
                      "import_event_f1_pct", "export_event_f1_pct")}}
        prompt["analytics"].update({k: analytics.get(k) for k in (
            "suggested_pv1_scale", "suggested_pv2_scale", "suggested_load_scale")})
        suggestions = []
        def flag(metric, value, threshold, message, severity="WARNING"):
            if value is not None and abs(float(value)) > threshold:
                suggestions.append({"metric": metric, "value": float(value), "threshold": threshold,
                                    "severity": severity, "suggestion": message})
        flag("pv_wape_pct", analytics.get("pv_wape_pct"), float(options.get("observer_pv_wape_warn_pct", 30.0)), "Sprawdź profil rozdziału prognozy PV i różnice PV1/PV2.")
        flag("load_wape_pct", analytics.get("load_wape_pct"), float(options.get("observer_load_wape_warn_pct", 35.0)), "Zwiększ liczbę próbek profilu zużycia przed zmianą planera.")
        flag("soc_mae_pct", analytics.get("soc_mae_pct"), float(options.get("observer_soc_mae_warn_pct", 8.0)), "Zweryfikuj sprawności baterii oraz znak i źródło mocy baterii.")
        flag("net_cost_variance_pln", analytics.get("net_cost_variance_pln"), float(options.get("observer_cost_variance_warn_pln", 10.0)), "Przeanalizuj koszt planowany względem wykonania bez automatycznej korekty PPD.")
        quality_threshold = float(options.get("observer_min_quality_score_pct", 80.0))
        if float(analytics.get("quality_score") or 0) < quality_threshold:
            suggestions.append({"metric": "quality_score", "value": float(analytics.get("quality_score") or 0),
                                "threshold": quality_threshold, "severity": "WARNING",
                                "suggestion": "Nie używaj tego przebiegu do uczenia; popraw kompletność telemetrii."})
        operational_suggestions, operational_summary = audit_operational_rows(
            history_rows, future_rows, options=options, incident_rows=new_history_rows)
        prompt["operational_audit_summary"] = operational_summary
        suggestions.extend(operational_suggestions)
        decision = "WATCH" if suggestions else "ACCEPT"
        auto_score = max(0.0, min(100.0, float(analytics.get("quality_score") or 0)))
        result = {"contract": "EMS_AI_OBSERVER_0_39_12", "mode": "SHADOW_READ_ONLY",
                  "decision": decision, "auto_score": auto_score, "suggestions": suggestions,
                  "operational_audit": operational_summary,
                  "external_model_called": False}
        run_id = str(uuid.uuid4())
        cur.execute("""INSERT INTO ems_gpt_core_ai_runs
          (run_id,role_name,source_ref,started_at,completed_at,status,prompt_json,result_json,auto_score,decision)
          VALUES(%s,'EMS_OBSERVER',%s,NOW(6),NOW(6),'COMPLETED',%s,%s,%s,%s)""",
          (run_id, source_ref, json.dumps(prompt, ensure_ascii=False, default=str),
           json.dumps(result, ensure_ascii=False, default=str), str(round(auto_score, 2)), decision))
    active_titles = []
    todo_scope = {
        "source_analytics_run_id": source_ref,
        "history_days": 7,
        "completed_slots_analyzed": operational_summary["audited_new_history_slots"],
        "future_slots_analyzed": operational_summary["audited_future_slots"],
        "areas": ["plan i wykonanie", "prognoza PV1/PV2 i zużycia",
                  "ceny i eksport", "okna importu", "okno HP", "SOC końcowy"],
    }
    for item in suggestions:
        title = f"Obserwator: {metric_label(item['metric'])}"
        active_titles.append(title)
        error = (f"Zaobserwowano {item.get('value')}; próg ostrzeżenia: "
                 f"{item.get('threshold')}." if item.get("value") is not None else
                 "Sprawdź dowody i metryki z podanego przebiegu analityki.")
        details = format_observer_todo(
            scope=todo_scope, metric=item["metric"], severity=item["severity"],
            error=error, conclusion=item["suggestion"], recommendation=item["suggestion"],
            evidence=item.get("evidence"), run_id=run_id, source_ref=source_ref)
        create_todo("ai_observer", title, details,
                    item["severity"], run_id,
                    require_consecutive_days=item["severity"] != "CRITICAL")
    reconcile_observer_todos(active_titles)
    record_event("ai_observer_completed", "ai_observer",
                 {"run_id": run_id, "source_ref": source_ref, "decision": decision, "suggestions": len(suggestions)})
    return {"status": "COMPLETED", "run_id": run_id, "source_ref": source_ref,
            "decision": decision, "auto_score": auto_score, "suggestions": suggestions}
