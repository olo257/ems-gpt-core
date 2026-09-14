"""External PV, weather and RCE data ingestion."""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlencode


def derive_price_windows(prices: list[dict], eta_c: float, eta_d: float,
                         degradation: float, min_margin: float,
                         buy_tolerance: float) -> list[tuple[bool, bool]]:
    """Derive economic sell/buy candidates without clock-based sessions."""
    windows = []
    for index, current in enumerate(prices):
        later = prices[index + 1:]
        later_sell = [float(row["sell"]) for row in later]
        later_buy = [float(row["buy"]) for row in later]
        replacement = min(later_buy) if later_buy else None
        future_peak = max(later_sell) if later_sell else None
        required_sell = (replacement / (eta_c * eta_d) + degradation + min_margin
                         if replacement is not None else None)
        economically_ready = required_sell is not None and float(current["sell"]) >= required_sell
        peak_ready = future_peak is None or float(current["sell"]) >= future_peak - min_margin
        sale_window = bool(economically_ready and peak_ready)
        buy_window = bool(
            later_sell
            and float(current["sell"]) <= min(later_sell) + buy_tolerance
            and max(later_sell) * eta_d - float(current["buy"]) / eta_c - degradation >= min_margin
        )
        windows.append((sale_window, buy_window))
    return windows


@dataclass(frozen=True)
class IngestionAdapters:
    options: dict
    timezone: Any
    pv_forecast_entities: dict
    db: Callable
    local_now: Callable
    slot_start: Callable
    canonical_slots_for_day: Callable
    number: Callable
    ha_state: Callable
    ha_service_response: Callable
    record_event: Callable


def build_ingestion(a: IngestionAdapters):
    OPTIONS, TZ, PV_FORECAST_ENTITIES = a.options, a.timezone, a.pv_forecast_entities
    db, local_now, slot_start = a.db, a.local_now, a.slot_start
    canonical_slots_for_day, number, ha_state = a.canonical_slots_for_day, a.number, a.ha_state
    ha_service_response, record_event = a.ha_service_response, a.record_event

    def refresh_pv_forecast() -> dict:
        """Disaggregate Open-Meteo daily PV energy using an actual-production profile."""
        today = local_now().date()
        results = {}
        with db() as conn, conn.cursor() as cur:
            for label, target in (("today", today), ("tomorrow", today+timedelta(days=1))):
                pv1 = number(ha_state(PV_FORECAST_ENTITIES[label][0]))
                pv2 = number(ha_state(PV_FORECAST_ENTITIES[label][1]))
                if pv1 is None or pv2 is None:
                    results[label] = {"status": "WAITING_SOURCE"}
                    continue
                lower = max(slot_start().replace(tzinfo=None), datetime.combine(target, datetime.min.time())) if label == "today" else datetime.combine(target, datetime.min.time())
                upper = datetime.combine(target+timedelta(days=1), datetime.min.time())
                cur.execute("""SELECT s.slot_start,p.mean_share FROM ems_gpt_slots s
                  LEFT JOIN ems_gpt_core_pv_profiles p ON p.month_no=MONTH(s.slot_start)
                   AND p.hour_no=HOUR(s.slot_start) AND p.minute_no=MINUTE(s.slot_start)
                  WHERE s.slot_start>=%s AND s.slot_start<%s AND s.actual_recorded_at IS NULL
                  ORDER BY s.slot_start""", (lower, upper))
                slots = list(cur.fetchall())
                weight_sum = sum(float(r.get("mean_share") or 0) for r in slots)
                if not slots or weight_sum <= 0:
                    results[label] = {"status": "WAITING_PROFILE", "slots": len(slots)}
                    continue
                for row in slots:
                    share = float(row.get("mean_share") or 0)/weight_sum
                    a, b = pv1*share, pv2*share
                    cur.execute("""UPDATE ems_gpt_slots SET forecast_pv1_kwh=%s,forecast_pv2_kwh=%s,
                      forecast_pv_total_kwh=%s,forecast_pv_source='OPEN_METEO_PROFILE_V1',
                      pv_correction=1 WHERE slot_start=%s AND actual_recorded_at IS NULL""",
                      (round(a, 6), round(b, 6), round(a+b, 6), row["slot_start"]))
                results[label] = {"status": "OK", "slots": len(slots), "pv1_kwh": pv1, "pv2_kwh": pv2}
        record_event("pv_forecast_refreshed", "analytics", results)
        return results
    
    
    def refresh_weather_forecast() -> dict:
        response = ha_service_response("weather", "get_forecasts", {"entity_id": "weather.dom", "type": "hourly"}, return_response=True) or {}
        service_response = response.get("service_response", response)
        forecast = (service_response.get("weather.dom") or {}).get("forecast", [])
        updated = 0
        with db() as conn, conn.cursor() as cur:
            for item in forecast:
                try:
                    hour = datetime.fromisoformat(str(item["datetime"]).replace("Z", "+00:00")).astimezone(TZ).replace(tzinfo=None)
                    cur.execute("""UPDATE ems_gpt_slots SET forecast_temperature_c=%s,
                      forecast_cloud_coverage_pct=%s,forecast_precipitation_mm=%s
                      WHERE slot_start>=%s AND slot_start<%s AND actual_recorded_at IS NULL""",
                      (item.get("temperature"), item.get("cloud_coverage"), item.get("precipitation"),
                       hour, hour+timedelta(hours=1)))
                    updated += cur.rowcount
                except (KeyError, TypeError, ValueError):
                    continue
        result = {"status": "OK" if forecast else "WAITING_SOURCE", "hours": len(forecast), "slots_updated": updated, "source": "OPEN_METEO"}
        record_event("weather_forecast_refreshed", "analytics", result, "INFO" if forecast else "WARNING")
        return result
    
    
    def refresh_rce(day=None) -> dict:
        target = day or (local_now().date()+timedelta(days=1))
        calendar = canonical_slots_for_day(target)
        calendar_by_local = {r["slot_start_local"]: r for r in calendar if not r["local_fold"]}
        expected = len(calendar)
        margin=max(0.0,float(OPTIONS.get("purchase_margin_pln_kwh", 0.59)))
        params={"$select":"dtime,period,rce_pln,business_date,publication_ts",
                "$filter":f"business_date eq '{target:%Y-%m-%d}'","$first":200}
        req=urllib.request.Request("https://api.raporty.pse.pl/api/rce-pln?"+urlencode(params),
                                   headers={"Accept":"application/json","User-Agent":"EMS-GPT-Core/0.4"})
        with urllib.request.urlopen(req,timeout=30) as response: payload=json.load(response)
        unique={}
        for item in payload.get("value",[]):
            try:
                end=datetime.fromisoformat(str(item["dtime"]).replace("Z","+00:00"))
                if end.tzinfo is None: end=end.replace(tzinfo=TZ)
                start=end.astimezone(TZ)-timedelta(minutes=15)
                if start.date()!=target: continue
                raw=float(item["rce_pln"])/1000
                pub=item.get("publication_ts")
                unique[start.replace(tzinfo=None)]={"sell":raw,"buy":raw+margin,"publication":pub}
            except Exception:
                continue
        ordered=sorted(unique)
        eta_c=max(.01,min(1.0,float(OPTIONS.get("battery_charge_efficiency",.90))))
        eta_d=max(.01,min(1.0,float(OPTIONS.get("battery_discharge_efficiency",.95))))
        degradation=max(0,float(OPTIONS.get("battery_degradation_cost_pln_kwh",.08)))
        min_margin=max(0,float(OPTIONS.get("minimum_arbitrage_margin_pln_kwh",.05)))
        buy_tolerance=max(0.0,float(OPTIONS.get("buy_window_tolerance_pln_kwh",.05)))
        price_windows=derive_price_windows(
            [unique[s] for s in ordered], eta_c, eta_d, degradation, min_margin, buy_tolerance)
        for s,(sale_window,buy_window) in zip(ordered,price_windows):
            unique[s]["sale_window"]=sale_window
            unique[s]["buy_window"]=buy_window
        with db() as conn,conn.cursor() as cur:
            for s,v in unique.items():
                canonical=calendar_by_local.get(s)
                cur.execute("""INSERT INTO ems_gpt_slots(slot_start,slot_end,price_sell_pln_kwh,price_buy_pln_kwh,
                  sale_window,buy_window,price_source,price_fetched_at,price_publication_at,
                  slot_id,slot_start_utc,slot_start_local,utc_offset_minutes,local_fold,local_day,slot_index_local)
                  VALUES(%s,%s,%s,%s,%s,%s,'PSE_API',NOW(6),%s,%s,%s,%s,%s,%s,%s,%s)
                  ON DUPLICATE KEY UPDATE price_sell_pln_kwh=IF(actual_recorded_at IS NULL,VALUES(price_sell_pln_kwh),price_sell_pln_kwh),
                  price_buy_pln_kwh=IF(actual_recorded_at IS NULL,VALUES(price_buy_pln_kwh),price_buy_pln_kwh),
                  sale_window=IF(actual_recorded_at IS NULL,VALUES(sale_window),sale_window),
                  buy_window=IF(actual_recorded_at IS NULL,VALUES(buy_window),buy_window),
                  price_fetched_at=IF(actual_recorded_at IS NULL,NOW(6),price_fetched_at),
                  price_publication_at=IF(actual_recorded_at IS NULL,VALUES(price_publication_at),price_publication_at),
                  price_source=IF(actual_recorded_at IS NULL,'PSE_API',price_source),
                  slot_id=COALESCE(slot_id,VALUES(slot_id)),slot_start_utc=COALESCE(slot_start_utc,VALUES(slot_start_utc)),
                  slot_start_local=COALESCE(slot_start_local,VALUES(slot_start_local)),
                  utc_offset_minutes=COALESCE(utc_offset_minutes,VALUES(utc_offset_minutes)),
                  local_day=COALESCE(local_day,VALUES(local_day)),slot_index_local=COALESCE(slot_index_local,VALUES(slot_index_local))""",
                  (s,s+timedelta(minutes=15),v["sell"],v["buy"],v["sale_window"],v["buy_window"],v["publication"],
                   canonical["slot_id"] if canonical else None,canonical["slot_start_utc"] if canonical else None,s,
                   canonical["utc_offset_minutes"] if canonical else None,canonical["local_fold"] if canonical else 0,
                   target,canonical["slot_index_local"] if canonical else None))
        result={"day":str(target),"rows":len(unique),"expected":expected,
                "status":"OK" if len(unique)==expected else "PARTIAL","margin":margin}
        record_event("rce_refreshed","core",result)
        return result

    return SimpleNamespace(
        refresh_pv_forecast=refresh_pv_forecast,
        refresh_weather_forecast=refresh_weather_forecast,
        refresh_rce=refresh_rce,
    )
