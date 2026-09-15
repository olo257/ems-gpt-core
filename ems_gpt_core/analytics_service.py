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


def _flow_metrics(rows: list[dict], forecast_key: str, actual_key: str,
                  threshold: float) -> dict:
    """Score intermittent flows without letting zero-heavy WAPE dominate."""
    pairs = [(max(0.0, float(r[forecast_key])), max(0.0, float(r[actual_key])))
             for r in rows if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    active = [(forecast, actual) for forecast, actual in pairs
              if forecast >= threshold or actual >= threshold]
    if not active:
        return {"active_slots": 0, "mae_kwh": None, "event_f1_pct": None}
    true_positive = sum(forecast >= threshold and actual >= threshold for forecast, actual in active)
    false_positive = sum(forecast >= threshold and actual < threshold for forecast, actual in active)
    false_negative = sum(forecast < threshold and actual >= threshold for forecast, actual in active)
    denominator = 2 * true_positive + false_positive + false_negative
    return {
        "active_slots": len(active),
        "mae_kwh": round(sum(abs(forecast - actual) for forecast, actual in active) / len(active), 6),
        "event_f1_pct": None if denominator == 0 else round(100 * 2 * true_positive / denominator, 2),
    }


def _suggested_scale(rows: list[dict], forecast_key: str, actual_key: str,
                     minimum_forecast_kwh: float = 1.0) -> float | None:
    """Suggest a bounded multiplier; applying it remains an operator decision."""
    pairs = [(max(0.0, float(r[forecast_key])), max(0.0, float(r[actual_key])))
             for r in rows if r.get(forecast_key) is not None and r.get(actual_key) is not None]
    forecast_total = sum(forecast for forecast, _ in pairs)
    if forecast_total < minimum_forecast_kwh:
        return None
    actual_total = sum(actual for _, actual in pairs)
    return round(min(1.5, max(0.5, actual_total / forecast_total)), 4)


def _is_core_quality_slot(row: dict) -> bool:
    """Use slots executed by Core, including explicit outage placeholders."""
    if not row.get("plan_published"):
        return False
    execution_reason = str(row.get("execution_reason") or "")
    return execution_reason.startswith("CORE_TELEMETRY_") or row.get("actual_mode") == "MISSING_OUTAGE"


def _hp_execution_metrics(rows: list[dict]) -> dict:
    """Aggregate the three physical HP modes without mixing their COP values."""
    result = {}
    total_in = total_out = 0.0
    for mode in ("heating", "dhw", "cooling"):
        consumed = sum(max(0.0, float(row.get(f"actual_{mode}_consumed_kwh") or 0)) for row in rows)
        generated = sum(max(0.0, float(row.get(f"actual_{mode}_generated_kwh") or 0)) for row in rows)
        result[f"actual_{mode}_consumed_kwh"] = round(consumed, 6)
        result[f"actual_{mode}_generated_kwh"] = round(generated, 6)
        result[f"actual_{mode}_cop"] = round(generated / consumed, 3) if consumed > .001 else None
        total_in += consumed
        total_out += generated
    result["actual_heat_pump_electric_kwh"] = round(total_in, 6)
    result["actual_heat_pump_thermal_kwh"] = round(total_out, 6)
    result["actual_heat_pump_cop"] = round(total_out / total_in, 3) if total_in > .001 else None
    result["actual_heat_pump_running_slot_count"] = sum(
        bool(row.get("actual_heat_pump_is_running")) for row in rows
    )
    return result


def _percentile(values: list[float], quantile: float) -> float | None:
    """Return a deterministic nearest-rank percentile for a small sample."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * quantile)))
    return round(ordered[index], 3)


def _target_load_kwh(row: dict) -> float | None:
    """Return unavoidable load used to reconstruct the required SOC target.

    EV is a flexible PV-surplus consumer.  DHW and heating consumption are
    intentionally retained: production telemetry currently attributes the
    heat pump's DHW mode to ``actual_dhw_kwh``, and the battery plan must bridge
    that real load when it occurs.
    """
    if row.get("actual_load_kwh") is None:
        return None
    load = max(0.0, float(row["actual_load_kwh"]))
    ev = max(0.0, float(row.get("detail_actual_ev_kwh") or 0.0))
    return round(max(0.0, load - ev), 6)


def _is_contiguous(previous: dict, current: dict, slot_minutes: int) -> bool:
    return current["slot_start"] - previous["slot_start"] == timedelta(minutes=slot_minutes)


def _find_target_relief(rows: list[dict], start_index: int, *, pv_threshold_kwh: float,
                        slot_minutes: int) -> tuple[int, str] | None:
    """Find the first confirmed future BUY or sustained actual-PV relief."""
    for index in range(start_index + 1, len(rows)):
        if not _is_contiguous(rows[index - 1], rows[index], slot_minutes):
            return None
        row = rows[index]
        if str(row.get("market_window") or "").upper() == "BUY":
            return index, "BUY"
        if float(row.get("actual_pv_total_kwh") or 0.0) < pv_threshold_kwh:
            continue
        next_index = index + 1
        if (next_index < len(rows)
                and _is_contiguous(row, rows[next_index], slot_minutes)
                and float(rows[next_index].get("actual_pv_total_kwh") or 0.0) >= pv_threshold_kwh):
            return index, "PV"
    return None


def _target_history(rows: list[dict], *, reserve_pct: float, capacity_kwh: float,
                    discharge_efficiency: float, pv_threshold_kwh: float,
                    slot_minutes: int, minimum_samples: int) -> list[dict]:
    """Reconstruct hindsight target without treating old model output as truth."""
    result = []
    for index, row in enumerate(rows):
        if row.get("soc_target_pct") is None:
            continue
        base = {
            "slot_start": row["slot_start"],
            "slot_id": row.get("slot_id"),
            "planned_target_pct": round(float(row["soc_target_pct"]), 3),
        }
        relief = _find_target_relief(
            rows, index, pv_threshold_kwh=pv_threshold_kwh, slot_minutes=slot_minutes)
        if relief is None:
            result.append({**base, "status": "INVALID", "reason": "NO_COMPLETE_RELIEF_HORIZON"})
            continue
        relief_index, relief_type = relief
        horizon = rows[index:relief_index]
        reasons = []
        if not horizon:
            reasons.append("EMPTY_HORIZON")
        for item_index, item in enumerate(horizon):
            if item_index and not _is_contiguous(horizon[item_index - 1], item, slot_minutes):
                reasons.append("NON_CONTIGUOUS_SLOTS")
                break
            if not _is_core_quality_slot(item) or item.get("actual_mode") == "MISSING_OUTAGE":
                reasons.append("NON_CORE_OR_OUTAGE_SLOT")
                break
            if int(item.get("sample_count") or 0) < minimum_samples:
                reasons.append("LOW_SAMPLE_COUNT")
                break
            if _target_load_kwh(item) is None or item.get("actual_pv_total_kwh") is None:
                reasons.append("MISSING_ACTUAL_ENERGY")
                break
        if reasons:
            result.append({**base, "relief_slot_start": rows[relief_index]["slot_start"],
                           "relief_type": relief_type, "status": "INVALID",
                           "reason": reasons[0]})
            continue
        deficit = sum(max(0.0, float(_target_load_kwh(item))
                              - max(0.0, float(item["actual_pv_total_kwh"])))
                      for item in horizon)
        required = min(100.0, reserve_pct + 100.0 * deficit
                       / (capacity_kwh * discharge_efficiency))
        error = required - float(row["soc_target_pct"])
        result.append({**base,
                       "relief_slot_start": rows[relief_index]["slot_start"],
                       "relief_type": relief_type,
                       "horizon_slots": len(horizon),
                       "required_energy_kwh": round(deficit, 6),
                       "required_target_pct": round(required, 3),
                       "target_error_pct": round(error, 3),
                       "target_shortfall_pct": round(max(0.0, error), 3),
                       "status": "VALID", "reason": "OK"})
    return result


def _target_history_metrics(samples: list[dict], *, minimum_samples: int,
                            correction_cap_pct: float) -> dict:
    valid = [sample for sample in samples if sample.get("status") == "VALID"]
    errors = [float(sample["target_error_pct"]) for sample in valid]
    shortfalls = [max(0.0, error) for error in errors]
    p80 = _percentile(shortfalls, 0.80)
    p90 = _percentile(shortfalls, 0.90)
    suggested = None if len(valid) < minimum_samples or p80 is None else min(correction_cap_pct, p80)
    evening = [sample for sample in valid
               if sample["slot_start"].hour == 18 and sample["slot_start"].minute == 30]
    evening_shortfalls = [float(sample["target_shortfall_pct"]) for sample in evening]
    return {
        "target_history_samples": len(valid),
        "target_history_invalid_samples": len(samples) - len(valid),
        "target_error_bias_pct": None if not errors else round(sum(errors) / len(errors), 3),
        "target_shortfall_p80_pct": p80,
        "target_shortfall_p90_pct": p90,
        "target_suggested_correction_pct": None if suggested is None else round(suggested, 3),
        "target_evening_1830_samples": len(evening),
        "target_evening_1830_shortfall_p90_pct": _percentile(evening_shortfalls, 0.90),
        "target_history_mode": "SHADOW_READ_ONLY",
    }


def run_analytics(*, options, db, local_now, record_event) -> dict:
    """Persist slot quality, rolling WAPE and a reproducible analysis watermark."""
    run_id = str(uuid.uuid4())
    history_days = max(7, min(90, int(options.get("target_history_days", 30))))
    cutoff = local_now().replace(tzinfo=None) - timedelta(days=history_days)
    minimum_samples = max(1, int(options.get("telemetry_min_samples_per_slot", 10)))
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO ems_gpt_core_analytics_runs(run_id,started_at,status) VALUES(%s,NOW(6),'RUNNING')", (run_id,))
        cur.execute("""SELECT s.*,d.actual_ev_kwh detail_actual_ev_kwh,
          d.actual_dhw_kwh detail_actual_dhw_kwh,
          (SELECT COUNT(*) FROM ems_gpt_telemetry_snapshots t
           WHERE t.slot_start=s.slot_start) sample_count
          FROM ems_gpt_slots s
          LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
          WHERE s.slot_start>=%s AND s.actual_recorded_at IS NOT NULL
          ORDER BY s.slot_start ASC""", (cutoff,))
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
        pv_threshold = max(0.0, float(options.get("analytics_pv_daylight_threshold_kwh", 0.02)))
        flow_threshold = max(0.0, float(options.get("technical_flow_threshold_kwh", 0.05)))
        pv_metric_rows = [row for row in metric_rows
                          if max(float(row.get("forecast_pv_total_kwh") or 0),
                                 float(row.get("actual_pv_total_kwh") or 0)) >= pv_threshold]
        import_flow = _flow_metrics(metric_rows, "planned_buy_kwh", "actual_buy_kwh", flow_threshold)
        export_flow = _flow_metrics(metric_rows, "planned_pv_export_kwh", "actual_pv_export_kwh", flow_threshold)
        metrics = {
            "pv1_wape_pct": _wape(pv_metric_rows, "forecast_pv1_kwh", "actual_pv1_kwh"),
            "pv2_wape_pct": _wape(pv_metric_rows, "forecast_pv2_kwh", "actual_pv2_kwh"),
            "pv_wape_pct": _wape(pv_metric_rows, "forecast_pv_total_kwh", "actual_pv_total_kwh"),
            "load_wape_pct": _wape(metric_rows, "forecast_load_kwh", "actual_native_load_kwh"),
            "import_wape_pct": _wape(metric_rows, "planned_buy_kwh", "actual_buy_kwh"),
            "export_wape_pct": _wape(metric_rows, "planned_pv_export_kwh", "actual_pv_export_kwh"),
            "pv_bias_kwh": _bias(pv_metric_rows, "forecast_pv_total_kwh", "actual_pv_total_kwh"),
            "load_bias_kwh": _bias(metric_rows, "forecast_load_kwh", "actual_native_load_kwh"),
            "import_bias_kwh": _bias(metric_rows, "planned_buy_kwh", "actual_buy_kwh"),
            "export_bias_kwh": _bias(metric_rows, "planned_pv_export_kwh", "actual_pv_export_kwh"),
            "soc_mae_pct": _mae(metric_rows, "soc_end_plan_pct", "soc_end_pct"),
            "import_active_mae_kwh": import_flow["mae_kwh"],
            "export_active_mae_kwh": export_flow["mae_kwh"],
            "import_event_f1_pct": import_flow["event_f1_pct"],
            "export_event_f1_pct": export_flow["event_f1_pct"],
            "suggested_pv1_scale": _suggested_scale(pv_metric_rows, "forecast_pv1_kwh", "actual_pv1_kwh"),
            "suggested_pv2_scale": _suggested_scale(pv_metric_rows, "forecast_pv2_kwh", "actual_pv2_kwh"),
            "suggested_load_scale": _suggested_scale(metric_rows, "forecast_load_kwh", "actual_native_load_kwh"),
        }
        metrics.update(_hp_execution_metrics(metric_rows))
        target_samples = _target_history(
            rows,
            reserve_pct=max(0.0, float(options.get("battery_min_soc_pct", 15.0))),
            capacity_kwh=max(0.1, float(options.get("battery_capacity_kwh", 15.0))),
            discharge_efficiency=max(0.01, float(options.get("battery_discharge_efficiency", 0.95))),
            pv_threshold_kwh=max(0.0, float(options.get("target_history_pv_relief_threshold_kwh", 0.10))),
            slot_minutes=max(1, int(options.get("slot_minutes", 15))),
            minimum_samples=minimum_samples,
        )
        target_metrics = _target_history_metrics(
            target_samples,
            minimum_samples=max(1, int(options.get("target_history_min_samples", 7))),
            correction_cap_pct=max(0.0, float(options.get("target_history_max_correction_pct", 15.0))),
        )
        metrics.update(target_metrics)
        for sample in target_samples:
            cur.execute("""INSERT INTO ems_gpt_core_target_history
              (slot_start,slot_id,relief_slot_start,relief_type,horizon_slots,
               planned_target_pct,required_target_pct,target_error_pct,target_shortfall_pct,
               required_energy_kwh,status,reason,analytics_run_id,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6))
              ON DUPLICATE KEY UPDATE slot_id=VALUES(slot_id),
               relief_slot_start=VALUES(relief_slot_start),relief_type=VALUES(relief_type),
               horizon_slots=VALUES(horizon_slots),planned_target_pct=VALUES(planned_target_pct),
               required_target_pct=VALUES(required_target_pct),target_error_pct=VALUES(target_error_pct),
               target_shortfall_pct=VALUES(target_shortfall_pct),required_energy_kwh=VALUES(required_energy_kwh),
               status=VALUES(status),reason=VALUES(reason),analytics_run_id=VALUES(analytics_run_id),
               updated_at=NOW(6)""",
              (sample["slot_start"], sample.get("slot_id"), sample.get("relief_slot_start"),
               sample.get("relief_type"), sample.get("horizon_slots"), sample["planned_target_pct"],
               sample.get("required_target_pct"), sample.get("target_error_pct"),
               sample.get("target_shortfall_pct"), sample.get("required_energy_kwh"),
               sample["status"], sample["reason"], run_id))
        planned_net = sum(float(r.get("planned_sell_kwh") or 0)*float(r.get("price_sell_pln_kwh") or 0)
                          - float(r.get("planned_buy_kwh") or 0)*float(r.get("price_buy_pln_kwh") or 0) for r in metric_rows)
        actual_net = sum((float(r.get("actual_sell_kwh") or 0)+float(r.get("actual_pv_export_kwh") or 0))*float(r.get("price_sell_pln_kwh") or 0)
                         - float(r.get("actual_buy_kwh") or 0)*float(r.get("price_buy_pln_kwh") or 0) for r in metric_rows)
        metrics["net_cost_variance_pln"] = round(actual_net-planned_net, 3)
        score = round(100 * quality_complete / quality_slots, 2) if quality_slots else 0.0
        confidence = round(score * min(1.0, len(metric_rows) / (7 * 96)), 2) if metric_rows else 0.0
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
          pv_daylight_slots=%s,metric_confidence_pct=%s,import_active_mae_kwh=%s,
          export_active_mae_kwh=%s,import_event_f1_pct=%s,export_event_f1_pct=%s,
          suggested_pv1_scale=%s,suggested_pv2_scale=%s,suggested_load_scale=%s,
          actual_heating_consumed_kwh=%s,actual_heating_generated_kwh=%s,actual_heating_cop=%s,
          actual_dhw_consumed_kwh=%s,actual_dhw_generated_kwh=%s,actual_dhw_cop=%s,
          actual_cooling_consumed_kwh=%s,actual_cooling_generated_kwh=%s,actual_cooling_cop=%s,
          actual_heat_pump_electric_kwh=%s,actual_heat_pump_thermal_kwh=%s,actual_heat_pump_cop=%s,
          actual_heat_pump_running_slot_count=%s,target_history_samples=%s,
          target_history_invalid_samples=%s,target_error_bias_pct=%s,
          target_shortfall_p80_pct=%s,target_shortfall_p90_pct=%s,
          target_suggested_correction_pct=%s,target_evening_1830_samples=%s,
          target_evening_1830_shortfall_p90_pct=%s,target_history_mode=%s,
          details_json=%s WHERE run_id=%s""",
          (len(rows), complete, metrics["pv1_wape_pct"], metrics["pv2_wape_pct"],
           metrics["pv_wape_pct"], metrics["load_wape_pct"],
           metrics["import_wape_pct"], metrics["export_wape_pct"], score,
           metrics["pv_bias_kwh"], metrics["load_bias_kwh"], metrics["import_bias_kwh"],
           metrics["export_bias_kwh"], metrics["soc_mae_pct"], metrics["net_cost_variance_pln"],
           len(pv_metric_rows), confidence, metrics["import_active_mae_kwh"], metrics["export_active_mae_kwh"],
           metrics["import_event_f1_pct"], metrics["export_event_f1_pct"],
           metrics["suggested_pv1_scale"], metrics["suggested_pv2_scale"], metrics["suggested_load_scale"],
           metrics["actual_heating_consumed_kwh"],metrics["actual_heating_generated_kwh"],metrics["actual_heating_cop"],
           metrics["actual_dhw_consumed_kwh"],metrics["actual_dhw_generated_kwh"],metrics["actual_dhw_cop"],
           metrics["actual_cooling_consumed_kwh"],metrics["actual_cooling_generated_kwh"],metrics["actual_cooling_cop"],
           metrics["actual_heat_pump_electric_kwh"],metrics["actual_heat_pump_thermal_kwh"],
           metrics["actual_heat_pump_cop"],metrics["actual_heat_pump_running_slot_count"],
           metrics["target_history_samples"],metrics["target_history_invalid_samples"],
           metrics["target_error_bias_pct"],metrics["target_shortfall_p80_pct"],
           metrics["target_shortfall_p90_pct"],metrics["target_suggested_correction_pct"],
           metrics["target_evening_1830_samples"],metrics["target_evening_1830_shortfall_p90_pct"],
           metrics["target_history_mode"],
           json.dumps({"cutoff": str(cutoff), "metrics": metrics, "metric_slots": len(metric_rows),
                       "pv_daylight_slots": len(pv_metric_rows), "metric_confidence_pct": confidence,
                       "flow_threshold_kwh": flow_threshold, "import_active_slots": import_flow["active_slots"],
                       "export_active_slots": export_flow["active_slots"], "quality_slots": quality_slots,
                       "quality_complete": quality_complete, "load_basis": "HOUSEHOLD_EXCLUDING_EV_AND_HEAT_PUMP",
                       "load_profiles": len(profiles),
                       "pv_profiles": len(pv_profiles),
                       "target_history": target_metrics}), run_id))
    result = {"run_id": run_id, "slots": len(rows), "complete": complete, "quality_score": score,
              "quality_slots": quality_slots, "quality_complete": quality_complete,
              "pv_daylight_slots": len(pv_metric_rows), "metric_confidence_pct": confidence,
              "load_profiles": len(profiles), "pv_profiles": len(pv_profiles), **metrics}
    record_event("analytics_completed", "analytics", result)
    return result
