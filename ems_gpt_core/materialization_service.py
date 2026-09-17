"""Slot closure, aggregates and recovery materializations."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable


def normalize_hp_mode_energy(consumed_kwh: float, generated_kwh: float,
                             activity_threshold_kwh: float = 0.02) -> tuple[float, float]:
    """Drop idle-channel noise without mixing energy between HP modes.

    HeishaMon can leave a small non-zero consumption value on an inactive
    channel (currently about 18 W on heating).  Counting that value in every
    slot creates fictitious CO energy even when the compressor never ran.
    Useful production is direct evidence of an active mode; otherwise the
    electrical energy itself must exceed the per-slot activity threshold.
    """
    consumed = max(0.0, float(consumed_kwh or 0.0))
    generated = max(0.0, float(generated_kwh or 0.0))
    active = generated > 0.001 or consumed >= activity_threshold_kwh
    return (consumed, generated) if active else (0.0, 0.0)


@dataclass(frozen=True)
class MaterializationAdapters:
    options: dict
    app_version: str
    db: Callable
    local_now: Callable
    slot_start: Callable
    record_event: Callable


def build_materializations(a: MaterializationAdapters):
    OPTIONS, APP_VERSION = a.options, a.app_version
    db, local_now, slot_start, record_event = a.db, a.local_now, a.slot_start, a.record_event

    def close_finished_slots() -> int:
        current = slot_start().replace(tzinfo=None)
        with db() as conn, conn.cursor() as cur:
            cur.execute("""SELECT slot_start,slot_end FROM ems_gpt_slots
              WHERE slot_end<=%s AND actual_recorded_at IS NULL
              ORDER BY slot_start ASC LIMIT 2688""",(current,))
            pending=list(cur.fetchall())
            closed=0
            for row in reversed(pending):
                cur.execute("""SELECT AVG(pv_power_w) pv,AVG(pv1_power_w) pv1,AVG(pv2_power_w) pv2,
                  AVG(load_power_w) load_kwh,AVG(grid_power_w) grid,
                  AVG(battery_charge_power_w) battery_charge,AVG(battery_discharge_power_w) battery_discharge,
                  AVG(ev_power_w) ev_power,AVG(dhw_power_w) dhw_power,
                  AVG(hp_outlet_temperature_c) hp_outlet,AVG(hp_inlet_temperature_c) hp_inlet,
                  AVG(hp_compressor_frequency_hz) hp_freq,AVG(hp_compressor_current_a) hp_current,
                  AVG(hp_flow_l_min) hp_flow,
                  AVG(hp_heat_consumption_w) hp_heat_cons,AVG(hp_heat_production_w) hp_heat_prod,
                  AVG(hp_dhw_production_w) hp_dhw_prod,AVG(hp_cool_consumption_w) hp_cool_cons,
                  AVG(hp_cool_production_w) hp_cool_prod,AVG(outside_temperature_c) outside_temp,
                  SUBSTRING_INDEX(GROUP_CONCAT(hp_operations_counter ORDER BY captured_at DESC),',',1) hp_ops,
                  SUBSTRING_INDEX(GROUP_CONCAT(hp_operations_hours ORDER BY captured_at DESC),',',1) hp_hours,
                  SUBSTRING_INDEX(GROUP_CONCAT(soc_pct ORDER BY captured_at ASC),',',1) soc_start,
                  MIN(soc_pct) soc_min,SUBSTRING_INDEX(GROUP_CONCAT(soc_pct ORDER BY captured_at DESC),',',1) soc,
                  SUBSTRING_INDEX(GROUP_CONCAT(dhw_temperature_c ORDER BY captured_at DESC),',',1) dhw,
                  COUNT(*) samples,SUM(source_status='COMPLETE') complete_samples,
                  MIN(captured_at) first_sample,MAX(captured_at) last_sample
                  FROM ems_gpt_telemetry_snapshots
                  WHERE captured_at>=%s AND captured_at<%s""",(row["slot_start"],row["slot_end"]))
                m=cur.fetchone(); samples=int(m["samples"] or 0)
                if samples==0:
                    cur.execute("""UPDATE ems_gpt_slots SET actual_recorded_at=NOW(6),
                      actual_mode='MISSING_OUTAGE',execution_reason='NO_TELEMETRY_AFTER_HA_OUTAGE',matched=0
                      WHERE slot_start=%s AND actual_recorded_at IS NULL""", (row["slot_start"],))
                    slot_closed=cur.rowcount
                    if slot_closed:
                        cur.execute("""INSERT INTO ems_gpt_core_execution_details
                          (slot_start,sample_count,coverage_pct,first_sample_at,last_sample_at,
                           actual_grid_export_kwh,actual_ev_kwh,actual_dhw_kwh,complete_source_samples,
                           export_attribution,soc_start_pct,soc_min_pct,soc_delta_pct,
                           recovery_status,quality_status,updated_at)
                          VALUES(%s,0,0,NULL,NULL,NULL,NULL,NULL,0,'UNRESOLVED',NULL,NULL,NULL,
                           'MISSING_OUTAGE','MISSING',NOW(6)) ON DUPLICATE KEY UPDATE
                           sample_count=0,coverage_pct=0,recovery_status='MISSING_OUTAGE',
                           quality_status='MISSING',updated_at=NOW(6)""", (row["slot_start"],))
                        closed += 1
                    continue
                pv=float(m["pv"] or 0)*.25/1000; pv1=float(m["pv1"] or 0)*.25/1000; pv2=float(m["pv2"] or 0)*.25/1000
                load=float(m["load_kwh"] or 0)*.25/1000; grid=float(m["grid"] or 0)*.25/1000
                battery_charge=float(m["battery_charge"] or 0)*.25/1000
                battery_discharge=float(m["battery_discharge"] or 0)*.25/1000
                ev_energy=float(m["ev_power"] or 0)*.25/1000
                dhw_energy=float(m["dhw_power"] or 0)*.25/1000
                hp_mode_threshold=max(0.001, float(OPTIONS.get("hp_mode_min_energy_kwh", 0.02)))
                hp_heat_cons,hp_heat_prod=normalize_hp_mode_energy(
                    float(m["hp_heat_cons"] or 0)*.25/1000,
                    float(m["hp_heat_prod"] or 0)*.25/1000,
                    hp_mode_threshold)
                dhw_energy,hp_dhw_prod=normalize_hp_mode_energy(
                    dhw_energy,float(m["hp_dhw_prod"] or 0)*.25/1000,
                    hp_mode_threshold)
                hp_cool_cons,hp_cool_prod=normalize_hp_mode_energy(
                    float(m["hp_cool_cons"] or 0)*.25/1000,
                    float(m["hp_cool_prod"] or 0)*.25/1000,
                    hp_mode_threshold)
                hp_total_cons=hp_heat_cons+dhw_energy+hp_cool_cons
                hp_total_prod=hp_heat_prod+hp_dhw_prod+hp_cool_prod
                hp_cop=hp_total_prod/hp_total_cons if hp_total_cons>.001 else None
                hp_mode="HEATING" if hp_heat_prod>.001 else "DHW" if hp_dhw_prod>.001 else "COOLING" if hp_cool_prod>.001 else "IDLE"
                grid_export=round(max(0,-grid),6)
                coverage=round(min(100.0, samples/15*100),2)
                cur.execute("""UPDATE ems_gpt_slots SET actual_recorded_at=NOW(6),
                  actual_pv_total_kwh=%s,actual_load_kwh=%s,actual_buy_kwh=%s,
                  actual_pv_export_kwh=%s,soc_end_pct=%s,actual_dhw_temperature_c=%s,
                  actual_battery_charge_kwh=%s,actual_battery_discharge_kwh=%s,
                  actual_pv1_kwh=%s,actual_pv2_kwh=%s,
                  actual_heat_pump_outlet_temperature_c=%s,actual_heat_pump_inlet_temperature_c=%s,
                  actual_heat_pump_compressor_frequency_hz=%s,actual_heat_pump_compressor_current_a=%s,
                  actual_heat_pump_flow_l_min=%s,actual_heat_pump_delta_t_c=%s,
                  actual_heat_pump_is_running=%s,actual_temperature_c=%s,
                  actual_heating_consumed_kwh=%s,actual_heating_generated_kwh=%s,actual_heating_cop=%s,
                  actual_dhw_consumed_kwh=%s,actual_dhw_generated_kwh=%s,actual_dhw_cop=%s,
                  actual_cooling_consumed_kwh=%s,actual_cooling_generated_kwh=%s,actual_cooling_cop=%s,
                  actual_heat_pump_electric_kwh=%s,actual_heat_pump_thermal_kwh=%s,actual_heat_pump_cop=%s,
                  actual_heat_pump_mode=%s,actual_heat_pump_operations_counter=%s,
                  actual_heat_pump_operations_hours=%s,actual_mode=%s,execution_reason=%s,matched=%s
                  WHERE slot_start=%s AND actual_recorded_at IS NULL""",
                  (round(pv,6),round(load,6),round(max(0,grid),6),grid_export,
                   float(m["soc"]) if m["soc"] not in (None,"") else None,
                   float(m["dhw"]) if m["dhw"] not in (None,"") else None,
                   round(battery_charge,6),round(battery_discharge,6),
                   round(pv1,6),round(pv2,6),m["hp_outlet"],m["hp_inlet"],m["hp_freq"],m["hp_current"],m["hp_flow"],
                   (float(m["hp_outlet"])-float(m["hp_inlet"])) if m["hp_outlet"] is not None and m["hp_inlet"] is not None else None,
                   1 if float(m["hp_freq"] or 0)>0 else 0,
                   m["outside_temp"],round(hp_heat_cons,6),round(hp_heat_prod,6),hp_heat_prod/hp_heat_cons if hp_heat_cons>.001 else None,
                   round(dhw_energy,6),round(hp_dhw_prod,6),hp_dhw_prod/dhw_energy if dhw_energy>.001 else None,
                   round(hp_cool_cons,6),round(hp_cool_prod,6),hp_cool_prod/hp_cool_cons if hp_cool_cons>.001 else None,
                   round(hp_total_cons,6),round(hp_total_prod,6),hp_cop,hp_mode,
                   float(m["hp_ops"]) if m["hp_ops"] not in (None,"") else None,
                   float(m["hp_hours"]) if m["hp_hours"] not in (None,"") else None,
                   "OBSERVED",f"CORE_TELEMETRY_{samples}_SAMPLES",None,row["slot_start"]))
                slot_closed=cur.rowcount
                if slot_closed:
                    cur.execute("""INSERT INTO ems_gpt_core_execution_details
                      (slot_start,sample_count,coverage_pct,first_sample_at,last_sample_at,
                       actual_grid_export_kwh,actual_ev_kwh,actual_dhw_kwh,complete_source_samples,
                       export_attribution,soc_start_pct,soc_min_pct,soc_delta_pct,
                       recovery_status,quality_status,updated_at)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'UNRESOLVED',%s,%s,%s,
                       %s,%s,NOW(6))
                      ON DUPLICATE KEY UPDATE sample_count=VALUES(sample_count),coverage_pct=VALUES(coverage_pct),
                       first_sample_at=VALUES(first_sample_at),last_sample_at=VALUES(last_sample_at),
                       actual_grid_export_kwh=VALUES(actual_grid_export_kwh),actual_ev_kwh=VALUES(actual_ev_kwh),
                       actual_dhw_kwh=VALUES(actual_dhw_kwh),complete_source_samples=VALUES(complete_source_samples),
                       soc_start_pct=VALUES(soc_start_pct),soc_min_pct=VALUES(soc_min_pct),soc_delta_pct=VALUES(soc_delta_pct),
                       recovery_status=VALUES(recovery_status),quality_status=VALUES(quality_status),updated_at=NOW(6)""",
                      (row["slot_start"],samples,coverage,m["first_sample"],m["last_sample"],grid_export,
                       round(ev_energy,6),round(dhw_energy,6),int(m["complete_samples"] or 0),
                       float(m["soc_start"]) if m["soc_start"] not in (None,"") else None,m["soc_min"],
                       float(m["soc"])-float(m["soc_start"]) if m["soc"] not in (None,"") and m["soc_start"] not in (None,"") else None,
                       "RECOVERED" if row["slot_end"] < current-timedelta(minutes=15) else "OBSERVED",
                       "ACCEPTED" if coverage>=float(OPTIONS.get("telemetry_learning_coverage_pct",80.0)) else "PARTIAL"))
                    observed_energy = {
                        # Below 50 Wh/slot the inverter flow is technical noise, not an EMS process.
                        "BATTERY_IMPORT": round(battery_charge, 6) if battery_charge >= float(OPTIONS.get("technical_flow_threshold_kwh",0.05)) else 0.0,
                        "BATTERY_EXPORT": round(battery_discharge, 6) if battery_discharge >= float(OPTIONS.get("technical_flow_threshold_kwh",0.05)) else 0.0,
                        "PV_CWU": round(dhw_energy, 6),
                        "PV_EV": round(ev_energy, 6),
                        "HP_HEAT_DHW": round(hp_heat_cons + dhw_energy, 6),
                    }
                    cur.execute("""SELECT d.process_name,d.eligible,d.decision,o.requested_state,
                      o.override_id,o.requested_by,o.reason override_reason,o.valid_until override_valid_until
                      FROM ems_gpt_core_process_decisions d
                      LEFT JOIN ems_gpt_core_process_overrides o ON o.process_name=d.process_name
                       AND o.valid_from<d.valid_until AND o.valid_until>d.slot_start
                       AND o.status IN ('ACTIVE','EXPIRED')
                      WHERE d.slot_start=%s""", (row["slot_start"],))
                    for process in cur.fetchall():
                        planned = "ON" if process["eligible"] else "OFF"
                        requested = process.get("requested_state")
                        energy_value = observed_energy.get(process["process_name"])
                        running_threshold = 0.02 if process["process_name"] == "HP_HEAT_DHW" else 0.001
                        observed = None if energy_value is None else ("RUNNING" if energy_value > running_threshold else "IDLE_OR_DISCONNECTED")
                        external_manual = requested is None and observed == "RUNNING" and planned == "OFF"
                        effective = ("ON" if requested == "FORCE_ON" else
                                     "OFF" if requested == "FORCE_OFF" else
                                     "ON" if external_manual else planned)
                        control_origin = ("MANUAL_FORCE_ON" if requested == "FORCE_ON" else
                                          "MANUAL_BLOCK" if requested == "FORCE_OFF" else
                                          "EXTERNAL_MANUAL" if external_manual else "AUTO")
                        cur.execute("""INSERT INTO ems_gpt_core_process_execution
                          (command_id,slot_start,slot_id,process_name,planned_state,effective_state,observed_state,
                           observed_energy_kwh,decision_source,reason,override_id,requested_by,override_reason,
                           override_valid_until,control_origin,recorded_at)
                          VALUES(NULL,%s,(SELECT slot_id FROM ems_gpt_core_slot_calendar WHERE slot_start_local=%s AND local_fold=0 LIMIT 1),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6))""",
                          (row["slot_start"], row["slot_start"], process["process_name"], planned, effective, observed, energy_value,
                           "OVERRIDE" if requested else "PLAN", process.get("override_reason") or process["decision"],
                           process.get("override_id"), process.get("requested_by"), process.get("override_reason"),
                           process.get("override_valid_until"), control_origin))
                closed+=slot_closed
        if closed: record_event("slots_closed","core",{"count":closed})
        return closed
    
    
    def backfill_execution_details() -> int:
        """Idempotently reconstruct recent detail rows created before schema 0.17."""
        cutoff = local_now().replace(tzinfo=None) - timedelta(hours=24)
        accepted_samples = max(1, int(15 * float(OPTIONS.get("telemetry_learning_coverage_pct", 80.0)) / 100.0 + 0.999))
        with db() as conn, conn.cursor() as cur:
            cur.execute("""INSERT IGNORE INTO ems_gpt_core_execution_details
              (slot_start,sample_count,coverage_pct,first_sample_at,last_sample_at,
               actual_grid_export_kwh,actual_ev_kwh,actual_dhw_kwh,complete_source_samples,
               export_attribution,recovery_status,quality_status,updated_at)
              SELECT s.slot_start,COUNT(t.captured_at),LEAST(100,COUNT(t.captured_at)/15*100),
               MIN(t.captured_at),MAX(t.captured_at),
               ROUND(GREATEST(0,-AVG(t.grid_power_w))*.25/1000,6),
               ROUND(COALESCE(AVG(t.ev_power_w),0)*.25/1000,6),
               ROUND(CASE WHEN ABS(COALESCE(AVG(t.dhw_power_w),0)) BETWEEN .001 AND 50
                 THEN AVG(t.dhw_power_w)*.25 ELSE COALESCE(AVG(t.dhw_power_w),0)*.25/1000 END,6),
               SUM(t.source_status='COMPLETE'),'UNRESOLVED','RECOVERED',
               CASE WHEN COUNT(t.captured_at)>=%s THEN 'ACCEPTED' ELSE 'PARTIAL' END,NOW(6)
              FROM ems_gpt_slots s JOIN ems_gpt_telemetry_snapshots t
                ON t.captured_at>=s.slot_start AND t.captured_at<s.slot_end
              LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
              WHERE s.actual_recorded_at IS NOT NULL AND s.slot_start>=%s AND d.slot_start IS NULL
              GROUP BY s.slot_start""", (accepted_samples, cutoff))
            restored = cur.rowcount
        if restored:
            record_event("execution_details_backfilled", "core", {"rows": restored})
        return restored
    
    
    def _actual_soc_bounds(cur, period_start: datetime, period_end: datetime) -> tuple[float | None, float | None]:
        """Return continuous actual SOC boundaries for an hour or day.

        The opening value is the last measured close before the boundary,
        making close(N) exactly equal to open(N+1).  Only when no earlier
        closed slot exists do we fall back to the first execution snapshot.
        """
        cur.execute("""SELECT
          (SELECT soc_end_pct FROM ems_gpt_slots
            WHERE actual_recorded_at IS NOT NULL AND soc_end_pct IS NOT NULL AND slot_start<%s
            ORDER BY slot_start DESC LIMIT 1) previous_close,
          (SELECT d.soc_start_pct FROM ems_gpt_core_execution_details d
            JOIN ems_gpt_slots s ON s.slot_start=d.slot_start
            WHERE s.actual_recorded_at IS NOT NULL AND d.soc_start_pct IS NOT NULL
              AND s.slot_start>=%s AND s.slot_start<%s
            ORDER BY s.slot_start ASC LIMIT 1) first_observed,
          (SELECT soc_end_pct FROM ems_gpt_slots
            WHERE actual_recorded_at IS NOT NULL AND soc_end_pct IS NOT NULL
              AND slot_start>=%s AND slot_start<%s
            ORDER BY slot_start DESC LIMIT 1) period_close""",
          (period_start,period_start,period_end,period_start,period_end))
        row=cur.fetchone() or {}
        opening=row.get("previous_close")
        if opening is None:
            opening=row.get("first_observed")
        closing=row.get("period_close")
        return (
            float(opening) if opening not in (None, "") else None,
            float(closing) if closing not in (None, "") else None,
        )

    def aggregate_results() -> None:
        """Maintain current hourly and daily plan-vs-actual materializations."""
        now=local_now().replace(tzinfo=None)
        day_start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        with db() as conn,conn.cursor() as cur:
            cur.execute("""SELECT DATE_FORMAT(slot_start,'%%Y-%%m-%%d %%H:00:00') hour_start,
              COUNT(*) slots,SUM(actual_recorded_at IS NOT NULL) actual_n,
              SUM(forecast_pv_total_kwh) fpv,SUM(actual_pv_total_kwh) apv,
              SUM(forecast_load_kwh) fload,SUM(actual_load_kwh) aload,
              SUM(planned_buy_kwh) pbuy,SUM(actual_buy_kwh) abuy,
              SUM(COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0)) pexport,
              SUM(COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0)) aexport,
              SUM((COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(planned_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) pnet,
              SUM((COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(actual_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) anet,
              SUBSTRING_INDEX(GROUP_CONCAT(soc_end_pct ORDER BY slot_start DESC),',',1) soc
              FROM ems_gpt_slots WHERE slot_start>=%s
              GROUP BY DATE_FORMAT(slot_start,'%%Y-%%m-%%d %%H:00:00')""",(day_start,))
            hours=cur.fetchall()
            for r in hours:
                cur.execute("""INSERT INTO ems_gpt_core_hourly
                  (hour_start,slot_count,forecast_pv_kwh,actual_pv_kwh,forecast_load_kwh,actual_load_kwh,
                   planned_import_kwh,actual_import_kwh,planned_export_kwh,actual_export_kwh,
                   planned_net_pln,actual_net_pln,soc_end_pct,updated_at,quality_status)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6),%s)
                  ON DUPLICATE KEY UPDATE slot_count=VALUES(slot_count),forecast_pv_kwh=VALUES(forecast_pv_kwh),
                   actual_pv_kwh=VALUES(actual_pv_kwh),forecast_load_kwh=VALUES(forecast_load_kwh),
                   actual_load_kwh=VALUES(actual_load_kwh),planned_import_kwh=VALUES(planned_import_kwh),
                   actual_import_kwh=VALUES(actual_import_kwh),planned_export_kwh=VALUES(planned_export_kwh),
                   actual_export_kwh=VALUES(actual_export_kwh),planned_net_pln=VALUES(planned_net_pln),
                   actual_net_pln=VALUES(actual_net_pln),soc_end_pct=VALUES(soc_end_pct),
                   updated_at=NOW(6),quality_status=VALUES(quality_status)""",
                  (r["hour_start"],r["slots"],r["fpv"],r["apv"],r["fload"],r["aload"],r["pbuy"],r["abuy"],
                   r["pexport"],r["aexport"],r["pnet"],r["anet"],float(r["soc"]) if r["soc"] not in (None,"") else None,
                   "COMPLETE" if int(r["actual_n"] or 0)==4 else "OPEN"))
                hour_start_value = r["hour_start"]
                if isinstance(hour_start_value, str):
                    hour_start_value = datetime.strptime(hour_start_value, "%Y-%m-%d %H:%M:%S")
                soc_open, soc_close = _actual_soc_bounds(cur, hour_start_value, hour_start_value+timedelta(hours=1))
                cur.execute("""UPDATE ems_gpt_core_hourly SET soc_start_pct=%s,soc_end_pct=%s
                  WHERE hour_start=%s""", (soc_open,soc_close,hour_start_value))
            cur.execute("""SELECT COUNT(*) slots,SUM(actual_recorded_at IS NOT NULL) actual_n,
              SUM(forecast_pv_total_kwh) fpv,SUM(actual_pv_total_kwh) apv,
              SUM(planned_battery_discharge_kwh) pdis,SUM(actual_battery_discharge_kwh) adis,
              SUM(planned_buy_kwh) pbuy,SUM(actual_buy_kwh) abuy,
              SUM(forecast_load_kwh) fload,SUM(actual_load_kwh) aload,
              SUM(planned_battery_charge_kwh) pcharge,SUM(actual_battery_charge_kwh) acharge,
              SUM(COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0)) pexport,
              SUM(COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0)) aexport,
              SUM((COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(planned_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) pnet,
              SUM((COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(actual_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) anet
              FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s""",(day_start,day_start+timedelta(days=1)))
            d=cur.fetchone()
            cur.execute("""INSERT INTO ems_gpt_daily(day_date,closed_at,slot_count,expected_slot_count,
              forecast_pv_kwh,actual_pv_kwh,forecast_battery_discharge_kwh,actual_battery_discharge_kwh,
              forecast_import_kwh,actual_import_kwh,forecast_load_kwh,actual_load_kwh,
              forecast_battery_charge_kwh,actual_battery_charge_kwh,forecast_export_kwh,actual_export_kwh,
              forecast_pv_export_kwh,actual_pv_export_kwh,planned_net_pln,actual_net_pln)
              VALUES(%s,NOW(6),%s,96,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON DUPLICATE KEY UPDATE closed_at=NOW(6),slot_count=VALUES(slot_count),
              forecast_pv_kwh=VALUES(forecast_pv_kwh),actual_pv_kwh=VALUES(actual_pv_kwh),
              forecast_battery_discharge_kwh=VALUES(forecast_battery_discharge_kwh),
              actual_battery_discharge_kwh=VALUES(actual_battery_discharge_kwh),
              forecast_import_kwh=VALUES(forecast_import_kwh),actual_import_kwh=VALUES(actual_import_kwh),
              forecast_load_kwh=VALUES(forecast_load_kwh),actual_load_kwh=VALUES(actual_load_kwh),
              forecast_battery_charge_kwh=VALUES(forecast_battery_charge_kwh),
              actual_battery_charge_kwh=VALUES(actual_battery_charge_kwh),
              forecast_export_kwh=VALUES(forecast_export_kwh),actual_export_kwh=VALUES(actual_export_kwh),
              forecast_pv_export_kwh=VALUES(forecast_pv_export_kwh),
              actual_pv_export_kwh=VALUES(actual_pv_export_kwh),
              planned_net_pln=VALUES(planned_net_pln),actual_net_pln=VALUES(actual_net_pln)""",
              (day_start.date(),d["slots"],d["fpv"],d["apv"],d["pdis"],d["adis"],d["pbuy"],d["abuy"],
               d["fload"],d["aload"],d["pcharge"],d["acharge"],d["pexport"],d["aexport"],
               d["pexport"],d["aexport"],d["pnet"],d["anet"]))
            soc_open, soc_close = _actual_soc_bounds(cur, day_start, day_start+timedelta(days=1))
            cur.execute("""UPDATE ems_gpt_daily SET soc_start_pct=%s,soc_end_pct=%s
              WHERE day_date=%s""", (soc_open,soc_close,day_start.date()))
    
    
    def rebuild_recovery_materializations(days: int = 7) -> dict:
        """Rebuild affected HOUR/DAILY rows after an outage without inventing actual values."""
        now = local_now().replace(tzinfo=None)
        first_day = (now-timedelta(days=max(1, days)-1)).replace(hour=0,minute=0,second=0,microsecond=0)
        with db() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DATE_FORMAT(s.slot_start,'%%Y-%%m-%%d %%H:00:00') hour_start,
              COUNT(*) expected_n,SUM(s.actual_recorded_at IS NOT NULL) terminal_n,
              SUM(s.actual_mode='MISSING_OUTAGE') missing_n,
              SUM(COALESCE(d.recovery_status,'')='RECOVERED') recovered_n,
              AVG(COALESCE(d.coverage_pct,0)) coverage,
              SUM(s.forecast_pv_total_kwh) fpv,SUM(s.actual_pv_total_kwh) apv,
              SUM(s.forecast_load_kwh) fload,SUM(s.actual_load_kwh) aload,
              SUM(s.planned_buy_kwh) pbuy,SUM(s.actual_buy_kwh) abuy,
              SUM(s.planned_pv_to_bat_kwh) pvbat,SUM(s.planned_pv_to_cwu_kwh) pvcwu,
              SUM(s.planned_pv_to_ev_kwh) pvev,SUM(s.planned_pv_export_kwh) pvexport,
              SUM(s.planned_pv_curtail_kwh) pvcurtail,
              SUM(COALESCE(s.planned_sell_kwh,0)+COALESCE(s.planned_pv_export_kwh,0)) pexport,
              SUM(COALESCE(s.actual_sell_kwh,0)+COALESCE(s.actual_pv_export_kwh,0)) aexport,
              SUM((COALESCE(s.planned_sell_kwh,0)+COALESCE(s.planned_pv_export_kwh,0))*COALESCE(s.price_sell_pln_kwh,0)-COALESCE(s.planned_buy_kwh,0)*COALESCE(s.price_buy_pln_kwh,0)) pnet,
              SUM((COALESCE(s.actual_sell_kwh,0)+COALESCE(s.actual_pv_export_kwh,0))*COALESCE(s.price_sell_pln_kwh,0)-COALESCE(s.actual_buy_kwh,0)*COALESCE(s.price_buy_pln_kwh,0)) anet,
              SUM(s.actual_heating_consumed_kwh) hp_heat_in,SUM(s.actual_heating_generated_kwh) hp_heat_out,
              SUM(s.actual_dhw_consumed_kwh) hp_dhw_in,SUM(s.actual_dhw_generated_kwh) hp_dhw_out,
              SUM(s.actual_cooling_consumed_kwh) hp_cool_in,SUM(s.actual_cooling_generated_kwh) hp_cool_out,
              SUM(COALESCE(s.actual_heat_pump_is_running,0)) hp_running_slots,
              SUBSTRING_INDEX(GROUP_CONCAT(s.soc_end_pct ORDER BY s.slot_start DESC),',',1) soc,
              MAX(s.actual_recorded_at) watermark
              FROM ems_gpt_slots s LEFT JOIN ems_gpt_core_execution_details d ON d.slot_start=s.slot_start
              WHERE s.slot_start>=%s AND s.slot_start<%s
              GROUP BY DATE_FORMAT(s.slot_start,'%%Y-%%m-%%d %%H:00:00')""", (first_day, now+timedelta(hours=1)))
            hour_rows = list(cur.fetchall())
            for r in hour_rows:
                hour_start_value = r["hour_start"]
                if isinstance(hour_start_value, str):
                    hour_start_value = datetime.strptime(hour_start_value, "%Y-%m-%d %H:%M:%S")
                expected=int(r["expected_n"] or 0); terminal=int(r["terminal_n"] or 0)
                missing=int(r["missing_n"] or 0); recovered=int(r["recovered_n"] or 0)
                ended=hour_start_value+timedelta(hours=1)<=now
                completion="CLOSED" if ended and terminal==expected else "OPEN"
                quality="ACCEPTED" if completion=="CLOSED" and missing==0 and float(r["coverage"] or 0)>=float(OPTIONS.get("telemetry_learning_coverage_pct",80.0)) else \
                        "MISSING" if completion=="CLOSED" and missing==expected else \
                        "PARTIAL" if ended else "OPEN"
                cur.execute("""INSERT INTO ems_gpt_core_hourly
                  (hour_start,slot_count,forecast_pv_kwh,actual_pv_kwh,forecast_load_kwh,actual_load_kwh,
                   planned_import_kwh,actual_import_kwh,planned_export_kwh,actual_export_kwh,
                   planned_net_pln,actual_net_pln,soc_end_pct,updated_at,quality_status,
                   expected_slot_count,terminal_slot_count,recovered_slot_count,missing_slot_count,
                   coverage_pct,completion_status,source_version,input_watermark,closed_at,learning_eligible)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(6),%s,%s,%s,%s,%s,%s,%s,
                   %s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                   slot_count=VALUES(slot_count),forecast_pv_kwh=VALUES(forecast_pv_kwh),actual_pv_kwh=VALUES(actual_pv_kwh),
                   forecast_load_kwh=VALUES(forecast_load_kwh),actual_load_kwh=VALUES(actual_load_kwh),
                   planned_import_kwh=VALUES(planned_import_kwh),actual_import_kwh=VALUES(actual_import_kwh),
                   planned_export_kwh=VALUES(planned_export_kwh),actual_export_kwh=VALUES(actual_export_kwh),
                   planned_net_pln=VALUES(planned_net_pln),actual_net_pln=VALUES(actual_net_pln),soc_end_pct=VALUES(soc_end_pct),
                   updated_at=NOW(6),quality_status=VALUES(quality_status),expected_slot_count=VALUES(expected_slot_count),
                   terminal_slot_count=VALUES(terminal_slot_count),recovered_slot_count=VALUES(recovered_slot_count),
                   missing_slot_count=VALUES(missing_slot_count),coverage_pct=VALUES(coverage_pct),
                   completion_status=VALUES(completion_status),source_version=VALUES(source_version),
                   input_watermark=VALUES(input_watermark),closed_at=CASE WHEN VALUES(completion_status)='OPEN'
                     THEN NULL ELSE COALESCE(closed_at,VALUES(closed_at)) END,
                   learning_eligible=VALUES(learning_eligible)""",
                  (hour_start_value,expected,r["fpv"],r["apv"],r["fload"],r["aload"],r["pbuy"],r["abuy"],
                   r["pexport"],r["aexport"],r["pnet"],r["anet"],float(r["soc"]) if r["soc"] not in (None,"") else None,
                   quality,expected,terminal,recovered,missing,round(float(r["coverage"] or 0),2),completion,
                   f"CORE_{APP_VERSION.replace('.', '_')}",
                   r["watermark"],now if completion=="CLOSED" else None,quality=="ACCEPTED"))
                cur.execute("""UPDATE ems_gpt_core_hourly SET planned_pv_to_bat_kwh=%s,
                  planned_pv_to_cwu_kwh=%s,planned_pv_to_ev_kwh=%s,planned_pv_export_kwh=%s,
                  planned_pv_curtail_kwh=%s,
                  actual_heating_consumed_kwh=%s,actual_heating_generated_kwh=%s,
                  actual_heating_cop=%s,actual_dhw_consumed_kwh=%s,actual_dhw_generated_kwh=%s,
                  actual_dhw_cop=%s,actual_cooling_consumed_kwh=%s,actual_cooling_generated_kwh=%s,
                  actual_cooling_cop=%s,actual_heat_pump_electric_kwh=%s,
                  actual_heat_pump_thermal_kwh=%s,actual_heat_pump_cop=%s,
                  actual_heat_pump_running_slot_count=%s WHERE hour_start=%s""",
                  (r["pvbat"] or 0,r["pvcwu"] or 0,r["pvev"] or 0,r["pvexport"] or 0,
                   r["pvcurtail"] or 0,
                   r["hp_heat_in"],r["hp_heat_out"],
                   float(r["hp_heat_out"] or 0)/float(r["hp_heat_in"]) if float(r["hp_heat_in"] or 0)>.001 else None,
                   r["hp_dhw_in"],r["hp_dhw_out"],
                   float(r["hp_dhw_out"] or 0)/float(r["hp_dhw_in"]) if float(r["hp_dhw_in"] or 0)>.001 else None,
                   r["hp_cool_in"],r["hp_cool_out"],
                   float(r["hp_cool_out"] or 0)/float(r["hp_cool_in"]) if float(r["hp_cool_in"] or 0)>.001 else None,
                   sum(float(r[k] or 0) for k in ("hp_heat_in","hp_dhw_in","hp_cool_in")),
                   sum(float(r[k] or 0) for k in ("hp_heat_out","hp_dhw_out","hp_cool_out")),
                   sum(float(r[k] or 0) for k in ("hp_heat_out","hp_dhw_out","hp_cool_out"))/
                   sum(float(r[k] or 0) for k in ("hp_heat_in","hp_dhw_in","hp_cool_in"))
                   if sum(float(r[k] or 0) for k in ("hp_heat_in","hp_dhw_in","hp_cool_in"))>.001 else None,
                   int(r["hp_running_slots"] or 0),hour_start_value))
                soc_open, soc_close = _actual_soc_bounds(cur, hour_start_value, hour_start_value+timedelta(hours=1))
                cur.execute("""UPDATE ems_gpt_core_hourly SET soc_start_pct=%s,soc_end_pct=%s
                  WHERE hour_start=%s""", (soc_open,soc_close,hour_start_value))
            for offset in range(max(1, days)):
                day_start=first_day+timedelta(days=offset); day_end=day_start+timedelta(days=1)
                cur.execute("""SELECT COUNT(*) slots,SUM(actual_recorded_at IS NOT NULL) terminal_n,
                  SUM(actual_mode='MISSING_OUTAGE') missing_n,
                  SUM(forecast_pv_total_kwh) fpv,SUM(actual_pv_total_kwh) apv,
                  SUM(planned_battery_discharge_kwh) pdis,SUM(actual_battery_discharge_kwh) adis,
                  SUM(planned_buy_kwh) pbuy,SUM(actual_buy_kwh) abuy,SUM(forecast_load_kwh) fload,SUM(actual_load_kwh) aload,
                  SUM(planned_pv_to_bat_kwh) pvbat,SUM(planned_pv_to_cwu_kwh) pvcwu,
                  SUM(planned_pv_to_ev_kwh) pvev,SUM(planned_pv_curtail_kwh) pvcurtail,
                  SUM(planned_battery_charge_kwh) pcharge,SUM(actual_battery_charge_kwh) acharge,
                  SUM(COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0)) pexport,
                  SUM(COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0)) aexport,
                  SUM((COALESCE(planned_sell_kwh,0)+COALESCE(planned_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(planned_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) pnet,
                  SUM((COALESCE(actual_sell_kwh,0)+COALESCE(actual_pv_export_kwh,0))*COALESCE(price_sell_pln_kwh,0)-COALESCE(actual_buy_kwh,0)*COALESCE(price_buy_pln_kwh,0)) anet
                  ,SUM(actual_heating_consumed_kwh) hp_heat_in,SUM(actual_heating_generated_kwh) hp_heat_out
                  ,SUM(actual_dhw_consumed_kwh) hp_dhw_in,SUM(actual_dhw_generated_kwh) hp_dhw_out
                  ,SUM(actual_cooling_consumed_kwh) hp_cool_in,SUM(actual_cooling_generated_kwh) hp_cool_out
                  ,SUM(COALESCE(actual_heat_pump_is_running,0)) hp_running_slots
                  ,MIN(CASE WHEN actual_heating_generated_kwh>0.001 THEN TIME(slot_start) END) hp_heat_start
                  ,MAX(CASE WHEN actual_heating_generated_kwh>0.001 THEN TIME(slot_end) END) hp_heat_end
                  ,MIN(CASE WHEN actual_dhw_generated_kwh>0.001 THEN TIME(slot_start) END) hp_dhw_start
                  ,MAX(CASE WHEN actual_dhw_generated_kwh>0.001 THEN TIME(slot_end) END) hp_dhw_end
                  ,MIN(CASE WHEN actual_cooling_generated_kwh>0.001 THEN TIME(slot_start) END) hp_cool_start
                  ,MAX(CASE WHEN actual_cooling_generated_kwh>0.001 THEN TIME(slot_end) END) hp_cool_end
                  FROM ems_gpt_slots WHERE slot_start>=%s AND slot_start<%s""", (day_start,day_end))
                d=cur.fetchone(); expected=int(d["slots"] or 0); terminal=int(d["terminal_n"] or 0); missing=int(d["missing_n"] or 0)
                cur.execute("""SELECT COUNT(*) n FROM ems_gpt_core_execution_details
                  WHERE slot_start>=%s AND slot_start<%s AND recovery_status='RECOVERED'""", (day_start,day_end))
                recovered=int(cur.fetchone()["n"] or 0)
                ended=day_end<=now; completion="CLOSED" if ended and expected in (92,96,100) and terminal==expected else "OPEN"
                quality="ACCEPTED" if completion=="CLOSED" and missing==0 and recovered==0 else "MISSING" if completion=="CLOSED" and missing==expected else "PARTIAL" if ended else "OPEN"
                cur.execute("""INSERT INTO ems_gpt_daily(day_date,closed_at,slot_count,expected_slot_count,
                  forecast_pv_kwh,actual_pv_kwh,forecast_battery_discharge_kwh,actual_battery_discharge_kwh,
                  forecast_import_kwh,actual_import_kwh,forecast_load_kwh,actual_load_kwh,
                  forecast_battery_charge_kwh,actual_battery_charge_kwh,forecast_export_kwh,actual_export_kwh,
                  forecast_pv_export_kwh,actual_pv_export_kwh,planned_net_pln,actual_net_pln,
                  valid_actual_slot_count,missing_actual_slot_count,recovered_slot_count,data_quality_status,
                  data_quality_reason,learning_eligible,completion_status,terminal_slot_count)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON DUPLICATE KEY UPDATE closed_at=CASE WHEN VALUES(completion_status)='OPEN'
                    THEN NULL ELSE COALESCE(closed_at,VALUES(closed_at)) END,slot_count=VALUES(slot_count),
                  expected_slot_count=VALUES(expected_slot_count),forecast_pv_kwh=VALUES(forecast_pv_kwh),
                  actual_pv_kwh=VALUES(actual_pv_kwh),forecast_battery_discharge_kwh=VALUES(forecast_battery_discharge_kwh),
                  actual_battery_discharge_kwh=VALUES(actual_battery_discharge_kwh),forecast_import_kwh=VALUES(forecast_import_kwh),
                  actual_import_kwh=VALUES(actual_import_kwh),forecast_load_kwh=VALUES(forecast_load_kwh),actual_load_kwh=VALUES(actual_load_kwh),
                  forecast_battery_charge_kwh=VALUES(forecast_battery_charge_kwh),actual_battery_charge_kwh=VALUES(actual_battery_charge_kwh),
                  forecast_export_kwh=VALUES(forecast_export_kwh),actual_export_kwh=VALUES(actual_export_kwh),
                  forecast_pv_export_kwh=VALUES(forecast_pv_export_kwh),actual_pv_export_kwh=VALUES(actual_pv_export_kwh),
                  planned_net_pln=VALUES(planned_net_pln),actual_net_pln=VALUES(actual_net_pln),
                  valid_actual_slot_count=VALUES(valid_actual_slot_count),missing_actual_slot_count=VALUES(missing_actual_slot_count),
                  recovered_slot_count=VALUES(recovered_slot_count),data_quality_status=VALUES(data_quality_status),
                  data_quality_reason=VALUES(data_quality_reason),learning_eligible=VALUES(learning_eligible),
                  completion_status=VALUES(completion_status),terminal_slot_count=VALUES(terminal_slot_count)""",
                  (day_start.date(),now if completion=="CLOSED" else None,expected,expected,d["fpv"],d["apv"],d["pdis"],d["adis"],
                   d["pbuy"],d["abuy"],d["fload"],d["aload"],d["pcharge"],d["acharge"],d["pexport"],d["aexport"],
                   d["pexport"],d["aexport"],d["pnet"],d["anet"],terminal-missing,missing,recovered,quality,
                   json.dumps({"completion":completion,"expected":expected,"terminal":terminal,"missing":missing,"recovered":recovered}),
                   quality=="ACCEPTED",completion,terminal))
                cur.execute("""UPDATE ems_gpt_daily SET planned_pv_to_bat_kwh=%s,
                  planned_pv_to_cwu_kwh=%s,planned_pv_to_ev_kwh=%s,planned_pv_curtail_kwh=%s,
                  actual_heating_consumed_kwh=%s,actual_heating_generated_kwh=%s,actual_heating_cop=%s,
                  actual_dhw_consumed_kwh=%s,actual_dhw_generated_kwh=%s,actual_dhw_cop=%s,
                  actual_cooling_consumed_kwh=%s,actual_cooling_generated_kwh=%s,actual_cooling_cop=%s,
                  actual_heat_pump_electric_kwh=%s,actual_heat_pump_thermal_kwh=%s,actual_heat_pump_cop=%s,
                  actual_heat_pump_running_slot_count=%s,
                  heating_production_start_time=%s,heating_production_end_time=%s,
                  dhw_production_start_time=%s,dhw_production_end_time=%s,
                  cooling_production_start_time=%s,cooling_production_end_time=%s
                  WHERE day_date=%s""", (d["pvbat"] or 0,d["pvcwu"] or 0,d["pvev"] or 0,
                  d["pvcurtail"] or 0,d["hp_heat_in"],d["hp_heat_out"],
                  float(d["hp_heat_out"] or 0)/float(d["hp_heat_in"]) if float(d["hp_heat_in"] or 0)>.001 else None,
                  d["hp_dhw_in"],d["hp_dhw_out"],
                  float(d["hp_dhw_out"] or 0)/float(d["hp_dhw_in"]) if float(d["hp_dhw_in"] or 0)>.001 else None,
                  d["hp_cool_in"],d["hp_cool_out"],
                  float(d["hp_cool_out"] or 0)/float(d["hp_cool_in"]) if float(d["hp_cool_in"] or 0)>.001 else None,
                  sum(float(d[k] or 0) for k in ("hp_heat_in","hp_dhw_in","hp_cool_in")),
                  sum(float(d[k] or 0) for k in ("hp_heat_out","hp_dhw_out","hp_cool_out")),
                  sum(float(d[k] or 0) for k in ("hp_heat_out","hp_dhw_out","hp_cool_out"))/
                  sum(float(d[k] or 0) for k in ("hp_heat_in","hp_dhw_in","hp_cool_in"))
                  if sum(float(d[k] or 0) for k in ("hp_heat_in","hp_dhw_in","hp_cool_in"))>.001 else None,
                  int(d["hp_running_slots"] or 0),d["hp_heat_start"],d["hp_heat_end"],
                  d["hp_dhw_start"],d["hp_dhw_end"],d["hp_cool_start"],d["hp_cool_end"],day_start.date()))
                soc_open, soc_close = _actual_soc_bounds(cur, day_start, day_end)
                cur.execute("""UPDATE ems_gpt_daily SET soc_start_pct=%s,soc_end_pct=%s
                  WHERE day_date=%s""", (soc_open,soc_close,day_start.date()))
        return {"hours":len(hour_rows),"days":max(1,days)}
    
    
    def learn_missing_load() -> int:
        updated=0; cutoff=slot_start().replace(tzinfo=None)
        with db() as conn,conn.cursor() as cur:
            cur.execute("""SELECT slot_start,forecast_load_kwh FROM ems_gpt_slots WHERE slot_start>=%s
              AND actual_recorded_at IS NULL ORDER BY slot_start""",(cutoff,))
            for row in cur.fetchall():
                s=row["slot_start"]
                cur.execute("""SELECT trimmed_mean_kwh,sample_count FROM ems_gpt_core_load_profiles
                  WHERE weekday_no=%s AND hour_no=%s AND minute_no=%s""",(s.weekday(),s.hour,s.minute))
                profile=cur.fetchone()
                if profile and int(profile["sample_count"] or 0)>=1:
                    value=float(profile["trimmed_mean_kwh"])
                    cur.execute("UPDATE ems_gpt_slots SET forecast_load_kwh=%s,load_correction=1 WHERE slot_start=%s",(round(value,6),s))
                    updated+=cur.rowcount
                    continue
                # A complete price horizon must never become an apparent
                # zero-load horizon merely because a specific weekday profile
                # has fewer than three samples. Use the recent measured base
                # load as a conservative temporary forecast until learning
                # provides the slot-specific profile.
                if row.get("forecast_load_kwh") is not None:
                    continue
                cur.execute("""SELECT AVG(actual_load_kwh) value FROM (
                  SELECT actual_load_kwh FROM ems_gpt_slots
                  WHERE actual_load_kwh IS NOT NULL AND actual_load_kwh>0
                    AND slot_start<%s ORDER BY slot_start DESC LIMIT 288
                ) recent""", (s,))
                fallback=cur.fetchone()
                value=float((fallback or {}).get("value") or 0.3125)
                cur.execute("UPDATE ems_gpt_slots SET forecast_load_kwh=%s,load_correction=1 WHERE slot_start=%s",
                            (round(max(0.05,value),6),s))
                updated+=cur.rowcount
        return updated

    return SimpleNamespace(
        close_finished_slots=close_finished_slots,
        backfill_execution_details=backfill_execution_details,
        aggregate_results=aggregate_results,
        rebuild_recovery_materializations=rebuild_recovery_materializations,
        learn_missing_load=learn_missing_load,
    )
