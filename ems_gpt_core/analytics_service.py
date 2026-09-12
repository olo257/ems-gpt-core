"""EMS analytical materialization service."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta


def _wape(rows: list[dict], forecast_key: str, actual_key: str) -> float | None:
    pairs = [(float(r[forecast_key]), float(r[actual_key])) for r in rows
             if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    denominator = sum(abs(actual) for _, actual in pairs)
    return None if not pairs or denominator <= 1e-9 else round(100 * sum(abs(forecast-actual) for forecast, actual in pairs) / denominator, 3)


def _bias(rows: list[dict], forecast_key: str, actual_key: str) -> float | None:
    pairs = [(float(r[forecast_key]), float(r[actual_key])) for r in rows
             if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    return None if not pairs else round(sum(forecast-actual for forecast, actual in pairs), 6)


def _mae(rows: list[dict], forecast_key: str, actual_key: str) -> float | None:
    pairs = [(float(r[forecast_key]), float(r[actual_key])) for r in rows
             if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    return None if not pairs else round(sum(abs(forecast-actual) for forecast, actual in pairs)/len(pairs), 3)


def _native_load_kwh(row: dict) -> float | None:
    """Return household load without loads scheduled separately by the planner."""
    if row.get("actual_load_kwh") is None:
        return None
    total = float(row["actual_load_kwh"])
    ev = max(0.0, float(row.get("detail_actual_ev_kwh") or 0))
    heat_pump = max(0.0, float(row.get("actual_heat_pump_electric_kwh") or 0))
    return round(max(0.0, total - ev - heat_pump), 6)


def _is_core_quality_slot(row: dict) -> bool:
    """Use slots executed by Core, including explicit outage placeholders."""
    if not row.get("plan_published"):
        return False
    execution_reason = str(row.get("execution_reason") or "")
    return execution_reason.startswith("CORE_TELEMETRY_") or row.get("actual_mode") == "MISSING_OUTAGE"


def run_analytics(*, options, db, local_now, record_event) -> dict:
    """Persist slot quality, rolling WAPE and a reproducible analysis watermark."""
    run_id = str(uuid.uuid4())
    cutoff = local_now().replace(tzinfo=None) - timedelta(days=30)
    minimum_samples = max(1, int(options.get("telemetry_min_samples_per_slot", 10)))
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO ems_gpt_core_analytics_runs(run_id,started_at,status) VALUES(%s,NOW(6),'RUNNING')", (run_id,))
        cur.execute("""SELECT s.*,d.actual_ev_kwh detail_actual_ev_kwh,
          (SELECT COUNT(*) FROM ems_gpt_telemetry_snapshots t
           WHERE t.slot_start=s.slot_start) sample_count
          FROM ems_gpt_slots s
          LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
          WHERE s.slot_start>=%s AND s.actual_recorded_at IS NOT NULL
          ORDER BY s.slot_start DESC LIMIT 2880""", (cutoff,))
        rows = list(cur.fetchall())
        for row in rows:
            row["actual_native_load_kwh"] = _native_load_kwh(row)
        complete = 0
        quality_slots = 0
        quality_complete = 0
        for row in rows:
            forecast_ok = all(row.get(k) is not None for k in ("forecast_pv_total_kwh", "forecast_load_kwh"))
            actual_ok = all(row.get(k) is not None for k in ("actual_pv_total_kwh", "actual_native_load_kwh"))
            price_ok = all(row.get(k) is not None for k in ("price_buy_pln_kwh", "price_sell_pln_kwh")) and row.get("price_source") == "PSE_API"
            samples = int(row.get("sample_count") or 0)
            present = sum((forecast_ok, actual_ok, price_ok, samples >= minimum_samples))
            completeness = round(present * 25.0, 2)
            reasons = []
            if not forecast_ok: reasons.append("MISSING_FORECAST")
            if not actual_ok: reasons.append("MISSING_ACTUAL")
            if not price_ok: reasons.append("MISSING_OR_NON_PSE_PRICE")
            if samples < minimum_samples: reasons.append("LOW_SAMPLE_COUNT")
            status = "COMPLETE" if not reasons else ("PARTIAL" if present >= 2 else "INVALID")
            complete += status == "COMPLETE"
            if _is_core_quality_slot(row):
                quality_slots += 1
                quality_complete += status == "COMPLETE"
            cur.execute("""INSERT INTO ems_gpt_core_slot_quality
              (slot_start,sample_count,completeness_pct,forecast_complete,actual_complete,
               price_complete,pv_abs_error_kwh,load_abs_error_kwh,import_abs_error_kwh,
               export_abs_error_kwh,status,checked_at,reasons_json)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6),%s)
              ON DUPLICATE KEY UPDATE sample_count=VALUES(sample_count),completeness_pct=VALUES(completeness_pct),
               forecast_complete=VALUES(forecast_complete),actual_complete=VALUES(actual_complete),
               price_complete=VALUES(price_complete),pv_abs_error_kwh=VALUES(pv_abs_error_kwh),
               load_abs_error_kwh=VALUES(load_abs_error_kwh),import_abs_error_kwh=VALUES(import_abs_error_kwh),
               export_abs_error_kwh=VALUES(export_abs_error_kwh),status=VALUES(status),
               checked_at=NOW(6),reasons_json=VALUES(reasons_json)""",
              (row["slot_start"], samples, completeness, forecast_ok, actual_ok, price_ok,
               abs(float(row.get("forecast_pv_total_kwh") or 0)-float(row.get("actual_pv_total_kwh") or 0)) if forecast_ok and actual_ok else None,
               abs(float(row.get("forecast_load_kwh") or 0)-float(row.get("actual_native_load_kwh") or 0)) if forecast_ok and actual_ok else None,
               abs(float(row.get("planned_buy_kwh") or 0)-float(row.get("actual_buy_kwh") or 0)),
               abs(float(row.get("planned_pv_export_kwh") or 0)-float(row.get("actual_pv_export_kwh") or 0)),
               status, json.dumps(reasons)))
        metric_rows = [row for row in rows if _is_core_quality_slot(row)]
        metrics = {
            "pv1_wape_pct": _wape(metric_rows, "forecast_pv1_kwh", "actual_pv1_kwh"),
            "pv2_wape_pct": _wape(metric_rows, "forecast_pv2_kwh", "actual_pv2_kwh"),
            "pv_wape_pct": _wape(metric_rows, "forecast_pv_total_kwh", "actual_pv_total_kwh"),
            "load_wape_pct": _wape(metric_rows, "forecast_load_kwh", "actual_native_load_kwh"),
            "import_wape_pct": _wape(metric_rows, "planned_buy_kwh", "actual_buy_kwh"),
            "export_wape_pct": _wape(metric_rows, "planned_pv_export_kwh", "actual_pv_export_kwh"),
            "pv_bias_kwh": _bias(metric_rows, "forecast_pv_total_kwh", "actual_pv_total_kwh"),
            "load_bias_kwh": _bias(metric_rows, "forecast_load_kwh", "actual_native_load_kwh"),
            "import_bias_kwh": _bias(metric_rows, "planned_buy_kwh", "actual_buy_kwh"),
            "export_bias_kwh": _bias(metric_rows, "planned_pv_export_kwh", "actual_pv_export_kwh"),
            "soc_mae_pct": _mae(metric_rows, "soc_end_plan_pct", "soc_end_pct"),
        }
        planned_net = sum(float(r.get("planned_sell_kwh") or 0)*float(r.get("price_sell_pln_kwh") or 0)
                          - float(r.get("planned_buy_kwh") or 0)*float(r.get("price_buy_pln_kwh") or 0) for r in metric_rows)
        actual_net = sum((float(r.get("actual_sell_kwh") or 0)+float(r.get("actual_pv_export_kwh") or 0))*float(r.get("price_sell_pln_kwh") or 0)
                         - float(r.get("actual_buy_kwh") or 0)*float(r.get("price_buy_pln_kwh") or 0) for r in metric_rows)
        metrics["net_cost_variance_pln"] = round(actual_net-planned_net, 3)
        score = round(100 * quality_complete / quality_slots, 2) if quality_slots else 0.0
        profiles = {}
        pv_days = {}
        for row in rows:
            if row.get("actual_native_load_kwh") is None:
                continue
            if int(row.get("sample_count") or 0) < minimum_samples or row.get("actual_mode") == "MISSING_OUTAGE":
                continue
            key = (row["slot_start"].weekday(), row["slot_start"].hour, row["slot_start"].minute)
            profiles.setdefault(key, []).append(float(row["actual_native_load_kwh"]))
            if row.get("actual_pv_total_kwh") is not None:
                day_key = row["slot_start"].date()
                pv_days.setdefault(day_key, []).append(row)
        for (weekday, hour, minute), values in profiles.items():
            ordered = sorted(values)
            trim = max(0, int(len(ordered)*0.1))
            trimmed = ordered[trim:len(ordered)-trim] if trim and len(ordered)-2*trim else ordered
            mean = sum(ordered)/len(ordered)
            trimmed_mean = sum(trimmed)/len(trimmed)
            p80 = ordered[min(len(ordered)-1, int((len(ordered)-1)*0.8))]
            denominator = sum(abs(v) for v in ordered)
            profile_wape = None if denominator <= 1e-9 else 100*sum(abs(trimmed_mean-v) for v in ordered)/denominator
            cur.execute("""INSERT INTO ems_gpt_core_load_profiles
              (weekday_no,hour_no,minute_no,sample_count,mean_kwh,trimmed_mean_kwh,p80_kwh,wape_pct,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW(6)) ON DUPLICATE KEY UPDATE
              sample_count=VALUES(sample_count),mean_kwh=VALUES(mean_kwh),
              trimmed_mean_kwh=VALUES(trimmed_mean_kwh),p80_kwh=VALUES(p80_kwh),
              wape_pct=VALUES(wape_pct),updated_at=NOW(6)""",
              (weekday, hour, minute, len(ordered), mean, trimmed_mean, p80, profile_wape))
        pv_profiles = {}
        for day_rows in pv_days.values():
            total = sum(float(r.get("actual_pv_total_kwh") or 0) for r in day_rows)
            if total <= 0.1:
                continue
            for row in day_rows:
                key = (row["slot_start"].month, row["slot_start"].hour, row["slot_start"].minute)
                pv_profiles.setdefault(key, []).append(float(row.get("actual_pv_total_kwh") or 0)/total)
        for (month, hour, minute), shares in pv_profiles.items():
            cur.execute("""INSERT INTO ems_gpt_core_pv_profiles
              (month_no,hour_no,minute_no,sample_days,mean_share,updated_at)
              VALUES(%s,%s,%s,%s,%s,NOW(6)) ON DUPLICATE KEY UPDATE
              sample_days=VALUES(sample_days),mean_share=VALUES(mean_share),updated_at=NOW(6)""",
              (month, hour, minute, len(shares), sum(shares)/len(shares)))
        cur.execute("""UPDATE ems_gpt_core_analytics_runs SET completed_at=NOW(6),status='COMPLETED',
          slots_scanned=%s,complete_slots=%s,pv1_wape_pct=%s,pv2_wape_pct=%s,pv_wape_pct=%s,load_wape_pct=%s,
          import_wape_pct=%s,export_wape_pct=%s,quality_score=%s,pv_bias_kwh=%s,load_bias_kwh=%s,
          import_bias_kwh=%s,export_bias_kwh=%s,soc_mae_pct=%s,net_cost_variance_pln=%s,
          details_json=%s WHERE run_id=%s""",
          (len(rows), complete, metrics["pv1_wape_pct"], metrics["pv2_wape_pct"],
           metrics["pv_wape_pct"], metrics["load_wape_pct"],
           metrics["import_wape_pct"], metrics["export_wape_pct"], score,
           metrics["pv_bias_kwh"], metrics["load_bias_kwh"], metrics["import_bias_kwh"],
           metrics["export_bias_kwh"], metrics["soc_mae_pct"], metrics["net_cost_variance_pln"],
           json.dumps({"cutoff": str(cutoff), "metrics": metrics, "metric_slots": len(metric_rows),
                       "quality_slots": quality_slots,
                       "quality_complete": quality_complete, "load_basis": "HOUSEHOLD_EXCLUDING_EV_AND_HEAT_PUMP",
                       "load_profiles": len(profiles),
                       "pv_profiles": len(pv_profiles)}), run_id))
    result = {"run_id": run_id, "slots": len(rows), "complete": complete, "quality_score": score,
              "quality_slots": quality_slots, "quality_complete": quality_complete,
              "load_profiles": len(profiles), "pv_profiles": len(pv_profiles), **metrics}
    record_event("analytics_completed", "analytics", result)
    return result
