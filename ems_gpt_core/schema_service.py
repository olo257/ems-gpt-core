from __future__ import annotations

import json
from datetime import timedelta

_SLOT_ID_BACKFILL_KEY = "slot_id_backfill_batched_0_38_8"
_SLOT_ID_TABLES = (
    "ems_gpt_core_events",
    "ems_gpt_core_module_runs",
    "ems_gpt_core_slot_quality",
    "ems_gpt_core_process_decisions",
    "ems_gpt_core_commands",
    "ems_gpt_core_process_execution",
    "ems_gpt_core_execution_details",
)


def _index_exists(cur, table: str, index_name: str) -> bool:
    cur.execute("""SELECT COUNT(*) n FROM information_schema.statistics
      WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s""",
                (table, index_name))
    return bool(int(cur.fetchone()["n"]))


def _ensure_slot_id_backfill_indexes(cur) -> None:
    if not _index_exists(cur, "ems_gpt_core_slot_calendar", "ix_calendar_local_start_fold"):
        cur.execute("""ALTER TABLE ems_gpt_core_slot_calendar
          ADD INDEX ix_calendar_local_start_fold(slot_start_local,local_fold)""")
    for table in _SLOT_ID_TABLES:
        index_name = f"ix_{table.removeprefix('ems_gpt_core_')}_slot_id_start"
        if not _index_exists(cur, table, index_name):
            cur.execute(
                f"ALTER TABLE {table} ADD INDEX {index_name}(slot_id,slot_start)"
            )


def _backfill_slot_ids_batched(conn, cur) -> dict:
    cur.execute("SELECT COUNT(*) n FROM ems_gpt_core_migrations WHERE migration_key=%s",
                (_SLOT_ID_BACKFILL_KEY,))
    if int(cur.fetchone()["n"]):
        return {"status": "ALREADY_APPLIED", "rows": 0, "batches": 0}

    _ensure_slot_id_backfill_indexes(cur)
    conn.commit()
    total_rows = 0
    batches = 0
    try:
        for table in _SLOT_ID_TABLES:
            cur.execute(
                f"""SELECT DATE(MIN(slot_start)) first_day,
                           DATE(MAX(slot_start)) last_day
                    FROM {table} WHERE slot_start IS NOT NULL AND slot_id IS NULL"""
            )
            bounds = cur.fetchone()
            day = bounds["first_day"]
            last_day = bounds["last_day"]
            while day is not None and last_day is not None and day <= last_day:
                next_day = day + timedelta(days=1)
                cur.execute(
                    f"""UPDATE {table} t
                      JOIN ems_gpt_core_slot_calendar c
                        ON c.slot_start_local=t.slot_start AND c.local_fold=0
                      SET t.slot_id=c.slot_id
                      WHERE t.slot_id IS NULL
                        AND t.slot_start >= %s AND t.slot_start < %s""",
                    (day, next_day),
                )
                total_rows += max(0, int(cur.rowcount))
                batches += 1
                conn.commit()
                day = next_day
        cur.execute(
            """INSERT INTO ems_gpt_core_migrations
              (migration_key,applied_at,details_json) VALUES(%s,NOW(6),%s)""",
            (_SLOT_ID_BACKFILL_KEY, json.dumps({
                "scope": "batched_slot_id_backfill",
                "rows": total_rows,
                "batches": batches,
            })),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": "APPLIED", "rows": total_rows, "batches": batches}


def ensure_runtime_schema(*, db, app_version: str) -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_migrations (
          migration_key VARCHAR(100) PRIMARY KEY, applied_at DATETIME(6) NOT NULL,
          details_json LONGTEXT NULL) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_events (
          id BIGINT AUTO_INCREMENT PRIMARY KEY, created_at DATETIME(6) NOT NULL,
          severity VARCHAR(12) NOT NULL, event_type VARCHAR(64) NOT NULL,
          module_name VARCHAR(32) NOT NULL, slot_start DATETIME(6) NULL,
          payload_json LONGTEXT NOT NULL, INDEX ix_core_events_time(created_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_module_runs (
          run_id VARCHAR(36) PRIMARY KEY, module_name VARCHAR(32) NOT NULL,
          run_type VARCHAR(32) NOT NULL, status VARCHAR(24) NOT NULL,
          started_at DATETIME(6) NOT NULL, completed_at DATETIME(6) NULL,
          slot_start DATETIME(6) NULL, input_watermark VARCHAR(80) NULL,
          output_version VARCHAR(80) NULL, reason TEXT NULL,
          INDEX ix_module_runs(module_name,started_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_telemetry_snapshots (
          captured_at DATETIME(6) PRIMARY KEY, slot_start DATETIME(6) NOT NULL,
          soc_pct DOUBLE NULL, pv_power_w DOUBLE NULL, load_power_w DOUBLE NULL,
          grid_power_w DOUBLE NULL, rce_sell_pln_kwh DOUBLE NULL,
          rce_buy_pln_kwh DOUBLE NULL, dhw_temperature_c DOUBLE NULL,
          battery_charge_power_w DOUBLE NULL, battery_discharge_power_w DOUBLE NULL,
          ev_power_w DOUBLE NULL, dhw_power_w DOUBLE NULL, pv_cwu_on TINYINT NULL,
          source_status VARCHAR(24) NOT NULL, payload_json LONGTEXT NOT NULL,
          INDEX ix_telemetry_slot(slot_start,captured_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_hourly (
          hour_start DATETIME(6) PRIMARY KEY, slot_count INT NOT NULL,
          forecast_pv_kwh DOUBLE NULL, actual_pv_kwh DOUBLE NULL,
          forecast_load_kwh DOUBLE NULL, actual_load_kwh DOUBLE NULL,
          planned_import_kwh DOUBLE NULL, actual_import_kwh DOUBLE NULL,
          planned_export_kwh DOUBLE NULL, actual_export_kwh DOUBLE NULL,
          planned_net_pln DOUBLE NULL, actual_net_pln DOUBLE NULL,
          soc_end_pct DOUBLE NULL, updated_at DATETIME(6) NOT NULL,
          quality_status VARCHAR(24) NOT NULL) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_slot_calendar (
          slot_id VARCHAR(32) PRIMARY KEY, slot_start_utc DATETIME(6) NOT NULL,
          slot_end_utc DATETIME(6) NOT NULL, slot_start_local DATETIME(6) NOT NULL,
          utc_offset_minutes SMALLINT NOT NULL, local_fold TINYINT NOT NULL,
          local_day DATE NOT NULL, slot_index_local SMALLINT NOT NULL,
          UNIQUE KEY uq_calendar_utc(slot_start_utc),
          INDEX ix_calendar_local_day(local_day,slot_index_local)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_analytics_runs (
          run_id VARCHAR(36) PRIMARY KEY, started_at DATETIME(6) NOT NULL,
          completed_at DATETIME(6) NULL, status VARCHAR(24) NOT NULL,
          slots_scanned INT NOT NULL DEFAULT 0, complete_slots INT NOT NULL DEFAULT 0,
          pv_wape_pct DOUBLE NULL, load_wape_pct DOUBLE NULL,
          import_wape_pct DOUBLE NULL, export_wape_pct DOUBLE NULL,
          quality_score DOUBLE NULL, details_json LONGTEXT NULL,
          INDEX ix_analytics_runs_time(started_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_slot_quality (
          slot_start DATETIME(6) PRIMARY KEY, sample_count INT NOT NULL,
          completeness_pct DOUBLE NOT NULL, forecast_complete TINYINT(1) NOT NULL,
          actual_complete TINYINT(1) NOT NULL, price_complete TINYINT(1) NOT NULL,
          pv_abs_error_kwh DOUBLE NULL, load_abs_error_kwh DOUBLE NULL,
          import_abs_error_kwh DOUBLE NULL, export_abs_error_kwh DOUBLE NULL,
          status VARCHAR(24) NOT NULL, checked_at DATETIME(6) NOT NULL,
          reasons_json LONGTEXT NOT NULL) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_diagnostic_reports (
          report_id VARCHAR(36) PRIMARY KEY, created_at DATETIME(6) NOT NULL,
          trigger_name VARCHAR(32) NOT NULL, status VARCHAR(24) NOT NULL,
          alert_count INT NOT NULL, summary VARCHAR(500) NOT NULL,
          checks_json LONGTEXT NOT NULL, INDEX ix_diag_time(created_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_load_profiles (
          weekday_no TINYINT NOT NULL, hour_no TINYINT NOT NULL, minute_no TINYINT NOT NULL,
          sample_count INT NOT NULL, mean_kwh DOUBLE NOT NULL, trimmed_mean_kwh DOUBLE NOT NULL,
          p80_kwh DOUBLE NOT NULL, wape_pct DOUBLE NULL, updated_at DATETIME(6) NOT NULL,
          PRIMARY KEY(weekday_no,hour_no,minute_no)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_pv_profiles (
          month_no TINYINT NOT NULL, hour_no TINYINT NOT NULL, minute_no TINYINT NOT NULL,
          sample_days INT NOT NULL, mean_share DOUBLE NOT NULL, updated_at DATETIME(6) NOT NULL,
          PRIMARY KEY(month_no,hour_no,minute_no)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_target_history (
          slot_start DATETIME(6) PRIMARY KEY, slot_id VARCHAR(32) NULL,
          relief_slot_start DATETIME(6) NULL, relief_type VARCHAR(8) NULL,
          horizon_slots INT NULL, planned_target_pct DOUBLE NOT NULL,
          required_target_pct DOUBLE NULL, target_error_pct DOUBLE NULL,
          target_shortfall_pct DOUBLE NULL, required_energy_kwh DOUBLE NULL,
          status VARCHAR(24) NOT NULL, reason VARCHAR(64) NOT NULL,
          analytics_run_id VARCHAR(36) NOT NULL, updated_at DATETIME(6) NOT NULL,
          INDEX ix_target_history_status_time(status,slot_start),
          INDEX ix_target_history_run(analytics_run_id)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_process_decisions (
          slot_start DATETIME(6) NOT NULL, process_name VARCHAR(32) NOT NULL,
          decision VARCHAR(24) NOT NULL, eligible TINYINT(1) NOT NULL,
          reason VARCHAR(1000) NOT NULL, plan_run_id VARCHAR(36) NOT NULL,
          valid_until DATETIME(6) NOT NULL, connector_required TINYINT(1) NOT NULL DEFAULT 1,
          published_at DATETIME(6) NOT NULL, PRIMARY KEY(slot_start,process_name),
          INDEX ix_process_decisions_run(plan_run_id)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_process_overrides (
          override_id VARCHAR(36) PRIMARY KEY, process_name VARCHAR(32) NOT NULL,
          requested_state VARCHAR(16) NOT NULL, requested_at DATETIME(6) NOT NULL,
          valid_from DATETIME(6) NOT NULL, valid_until DATETIME(6) NOT NULL,
          requested_by VARCHAR(100) NOT NULL, reason VARCHAR(1000) NOT NULL,
          status VARCHAR(24) NOT NULL, cancelled_at DATETIME(6) NULL,
          INDEX ix_process_override_active(process_name,status,valid_from,valid_until)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_commands (
          command_id VARCHAR(36) PRIMARY KEY, slot_start DATETIME(6) NOT NULL,
          process_name VARCHAR(32) NOT NULL, decision VARCHAR(16) NOT NULL,
          plan_version VARCHAR(80) NOT NULL, created_at DATETIME(6) NOT NULL,
          expires_at DATETIME(6) NOT NULL, source VARCHAR(24) NOT NULL,
          status VARCHAR(24) NOT NULL, safety_json LONGTEXT NOT NULL,
          dispatched_at DATETIME(6) NULL, acknowledged_at DATETIME(6) NULL,
          executed_at DATETIME(6) NULL, acknowledgement_json LONGTEXT NULL,
          UNIQUE KEY uq_command_intent(slot_start,process_name,decision,plan_version),
          INDEX ix_commands_status(status,expires_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_process_execution (
          id BIGINT AUTO_INCREMENT PRIMARY KEY, command_id VARCHAR(36) NULL,
          slot_start DATETIME(6) NOT NULL, process_name VARCHAR(32) NOT NULL,
          planned_state VARCHAR(16) NOT NULL, effective_state VARCHAR(16) NOT NULL,
          observed_state VARCHAR(24) NULL, observed_energy_kwh DOUBLE NULL,
          decision_source VARCHAR(24) NOT NULL, reason VARCHAR(1000) NOT NULL,
          recorded_at DATETIME(6) NOT NULL,
          INDEX ix_process_execution_slot(slot_start,process_name)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_todo (
          todo_id VARCHAR(36) PRIMARY KEY, created_at DATETIME(6) NOT NULL,
          local_day DATE NOT NULL, severity VARCHAR(12) NOT NULL,
          module_name VARCHAR(32) NOT NULL, title VARCHAR(300) NOT NULL,
          details LONGTEXT NOT NULL, status VARCHAR(24) NOT NULL DEFAULT 'OPEN',
          source_ref VARCHAR(100) NULL, resolved_at DATETIME(6) NULL,
          INDEX ix_todo_day(local_day,status,severity)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_ai_runs (
          run_id VARCHAR(36) PRIMARY KEY, role_name VARCHAR(32) NOT NULL,
          source_ref VARCHAR(100) NOT NULL, started_at DATETIME(6) NOT NULL,
          completed_at DATETIME(6) NULL, status VARCHAR(24) NOT NULL,
          prompt_json LONGTEXT NOT NULL, result_json LONGTEXT NULL,
          auto_score VARCHAR(24) NULL, decision VARCHAR(24) NULL,
          UNIQUE KEY uq_ai_role_source(role_name,source_ref),
          INDEX ix_ai_runs_time(started_at)) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_execution_details (
          slot_start DATETIME(6) PRIMARY KEY, sample_count INT NOT NULL,
          coverage_pct DOUBLE NOT NULL, first_sample_at DATETIME(6) NULL,
          last_sample_at DATETIME(6) NULL, actual_grid_export_kwh DOUBLE NULL,
          actual_ev_kwh DOUBLE NULL, actual_dhw_kwh DOUBLE NULL,
          complete_source_samples INT NOT NULL DEFAULT 0,
          export_attribution VARCHAR(32) NOT NULL DEFAULT 'UNRESOLVED',
          updated_at DATETIME(6) NOT NULL) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS ems_gpt_core_appliance_daily (
          local_day DATE NOT NULL, appliance_key VARCHAR(40) NOT NULL,
          appliance_name VARCHAR(100) NOT NULL, counter_type VARCHAR(16) NOT NULL,
          energy_entity VARCHAR(255) NULL, power_entity VARCHAR(255) NULL,
          water_entity VARCHAR(255) NULL, state_entity VARCHAR(255) NULL,
          first_energy_kwh DOUBLE NULL, last_energy_kwh DOUBLE NULL, daily_energy_kwh DOUBLE NULL,
          first_water_l DOUBLE NULL, last_water_l DOUBLE NULL, daily_water_l DOUBLE NULL,
          current_power_w DOUBLE NULL, max_power_w DOUBLE NULL, is_active TINYINT(1) NOT NULL DEFAULT 0,
          cycle_count INT NOT NULL DEFAULT 0, state_value VARCHAR(100) NULL,
          quality_status VARCHAR(24) NOT NULL, updated_at DATETIME(6) NOT NULL,
          PRIMARY KEY(local_day,appliance_key), INDEX ix_appliance_key_day(appliance_key,local_day)
        ) ENGINE=InnoDB""",
    ]
    with db(read_timeout=120, write_timeout=120) as conn, conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
        for column in (
            "battery_charge_power_w DOUBLE NULL", "battery_discharge_power_w DOUBLE NULL",
            "ev_power_w DOUBLE NULL", "dhw_power_w DOUBLE NULL", "pv_cwu_on TINYINT NULL",
            "pv1_power_w DOUBLE NULL", "pv2_power_w DOUBLE NULL",
            "hp_outlet_temperature_c DOUBLE NULL", "hp_inlet_temperature_c DOUBLE NULL",
            "hp_compressor_frequency_hz DOUBLE NULL", "hp_compressor_current_a DOUBLE NULL",
            "hp_flow_l_min DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_telemetry_snapshots ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "hp_heat_consumption_w DOUBLE NULL", "hp_heat_production_w DOUBLE NULL",
            "hp_dhw_production_w DOUBLE NULL", "hp_cool_consumption_w DOUBLE NULL",
            "hp_cool_production_w DOUBLE NULL", "outside_temperature_c DOUBLE NULL",
            "hp_operations_counter DOUBLE NULL", "hp_operations_hours DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_telemetry_snapshots ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "soc_start_pct DOUBLE NULL", "soc_min_pct DOUBLE NULL", "soc_delta_pct DOUBLE NULL",
            "recovery_status VARCHAR(24) NOT NULL DEFAULT 'OBSERVED'",
            "quality_status VARCHAR(24) NOT NULL DEFAULT 'OPEN'",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_execution_details ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "override_id VARCHAR(36) NULL", "requested_by VARCHAR(100) NULL",
            "override_reason VARCHAR(1000) NULL", "override_valid_until DATETIME(6) NULL",
            "control_origin VARCHAR(32) NOT NULL DEFAULT 'AUTO'",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_process_execution ADD COLUMN IF NOT EXISTS {column}")
        for column in ("pv1_wape_pct DOUBLE NULL", "pv2_wape_pct DOUBLE NULL"):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "pv_bias_kwh DOUBLE NULL", "load_bias_kwh DOUBLE NULL",
            "import_bias_kwh DOUBLE NULL", "export_bias_kwh DOUBLE NULL",
            "battery_charge_bias_kwh DOUBLE NULL", "battery_discharge_bias_kwh DOUBLE NULL",
            "soc_mae_pct DOUBLE NULL", "net_cost_variance_pln DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "pv_daylight_slots INT NOT NULL DEFAULT 0", "metric_confidence_pct DOUBLE NULL",
            "import_active_mae_kwh DOUBLE NULL", "export_active_mae_kwh DOUBLE NULL",
            "import_event_f1_pct DOUBLE NULL", "export_event_f1_pct DOUBLE NULL",
            "battery_charge_wape_pct DOUBLE NULL", "battery_discharge_wape_pct DOUBLE NULL",
            "battery_charge_active_mae_kwh DOUBLE NULL", "battery_discharge_active_mae_kwh DOUBLE NULL",
            "battery_charge_event_f1_pct DOUBLE NULL", "battery_discharge_event_f1_pct DOUBLE NULL",
            "suggested_pv1_scale DOUBLE NULL", "suggested_pv2_scale DOUBLE NULL",
            "suggested_load_scale DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "target_history_samples INT NOT NULL DEFAULT 0",
            "target_history_invalid_samples INT NOT NULL DEFAULT 0",
            "target_error_bias_pct DOUBLE NULL", "target_shortfall_p80_pct DOUBLE NULL",
            "target_shortfall_p90_pct DOUBLE NULL", "target_suggested_correction_pct DOUBLE NULL",
            "target_evening_1830_samples INT NOT NULL DEFAULT 0",
            "target_evening_1830_shortfall_p90_pct DOUBLE NULL",
            "target_history_mode VARCHAR(24) NOT NULL DEFAULT 'SHADOW_READ_ONLY'",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "first_seen_day DATE NULL", "last_seen_day DATE NULL",
            "occurrence_count INT NOT NULL DEFAULT 1",
            "consecutive_days INT NOT NULL DEFAULT 1",
            "reviewed_at DATETIME(6) NULL", "reviewed_by VARCHAR(100) NULL",
            "review_note VARCHAR(1000) NULL", "archived_at DATETIME(6) NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_todo ADD COLUMN IF NOT EXISTS {column}")
        cur.execute("""UPDATE ems_gpt_core_todo SET first_seen_day=COALESCE(first_seen_day,local_day),
          last_seen_day=COALESCE(last_seen_day,local_day)""")
        for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows"):
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS market_window VARCHAR(8) NULL")
            for column in (
                "soc_start_plan_pct DOUBLE NULL", "soc_end_plan_pct DOUBLE NULL",
                "soc_reserve_pct DOUBLE NULL", "soc_required_pct DOUBLE NULL",
                "soc_charge_target_pct DOUBLE NULL", "soc_sale_floor_pct DOUBLE NULL",
                "heat_pump_window TINYINT(1) NOT NULL DEFAULT 0",
                "forecast_heat_pump_load_kwh DOUBLE NULL",
                "forecast_heat_pump_dhw_load_kwh DOUBLE NULL",
                "planned_pv_to_bat_kwh DOUBLE NOT NULL DEFAULT 0",
                "planned_pv_to_cwu_kwh DOUBLE NOT NULL DEFAULT 0",
                "planned_pv_to_ev_kwh DOUBLE NOT NULL DEFAULT 0",
                "planned_pv_curtail_kwh DOUBLE NOT NULL DEFAULT 0",
            ):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
        cur.execute("""UPDATE ems_gpt_slots SET market_window=CASE
          WHEN COALESCE(sale_window,0)=1 THEN 'SELL'
          WHEN COALESCE(buy_window,0)=1 THEN 'BUY' ELSE 'NEUTRAL' END
          WHERE market_window IS NULL""")
        legacy_flags = (
            "grid_buy_allowed", "grid_no_buy", "grid_neutral",
            "sell_bat_allowed", "no_sell_bat", "sell_pv_allowed", "no_sell_pv",
            "pv_to_bat_planned", "pv_to_cwu_planned", "pv_to_ev_planned",
            "pv_export_planned", "pv_curtail_planned",
            "potential_sell_pv", "potential_no_export", "no_export_active", "surplus_enabled",
        )
        cur.execute("SELECT COUNT(*) n FROM ems_gpt_core_migrations WHERE migration_key=%s",
                    ("slot_legacy_flags_removed_0_33_6",))
        if not int(cur.fetchone()["n"]):
            selected = ",".join(("slot_start",) + legacy_flags)
            cur.execute(f"""CREATE TABLE IF NOT EXISTS ems_gpt_slot_legacy_flags_0335
              AS SELECT {selected} FROM ems_gpt_slots""")
            for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows"):
                for column in legacy_flags:
                    cur.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
            cur.execute("""INSERT INTO ems_gpt_core_migrations(migration_key,applied_at,details_json)
              VALUES(%s,NOW(6),%s)""", ("slot_legacy_flags_removed_0_33_6",
              json.dumps({"archive": "ems_gpt_slot_legacy_flags_0335", "columns": legacy_flags})))
        for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows", "ems_gpt_telemetry_snapshots"):
            for column in (
                "slot_id VARCHAR(32) NULL", "slot_start_utc DATETIME(6) NULL",
                "slot_start_local DATETIME(6) NULL", "utc_offset_minutes SMALLINT NULL",
                "local_fold TINYINT NOT NULL DEFAULT 0", "local_day DATE NULL",
                "slot_index_local SMALLINT NULL",
            ):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "expected_slot_count INT NOT NULL DEFAULT 4", "terminal_slot_count INT NOT NULL DEFAULT 0",
            "recovered_slot_count INT NOT NULL DEFAULT 0", "missing_slot_count INT NOT NULL DEFAULT 0",
            "coverage_pct DOUBLE NOT NULL DEFAULT 0", "completion_status VARCHAR(24) NOT NULL DEFAULT 'OPEN'",
            "source_version VARCHAR(32) NULL", "input_watermark DATETIME(6) NULL",
            "closed_at DATETIME(6) NULL", "learning_eligible TINYINT(1) NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_hourly ADD COLUMN IF NOT EXISTS {column}")
        for table in ("ems_gpt_core_hourly", "ems_gpt_daily"):
            for column in ("soc_start_pct DOUBLE NULL", "soc_end_pct DOUBLE NULL"):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
        cur.execute("""ALTER TABLE ems_gpt_core_process_decisions
          ADD COLUMN IF NOT EXISTS ppd_run_id VARCHAR(36) NULL""")
        cur.execute("""CREATE INDEX IF NOT EXISTS ix_process_decisions_ppd_run
          ON ems_gpt_core_process_decisions(ppd_run_id)""")
        for column in (
            "planned_pv_to_bat_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_to_cwu_kwh DOUBLE NOT NULL DEFAULT 0",
            "planned_pv_to_ev_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_export_kwh DOUBLE NOT NULL DEFAULT 0",
            "planned_pv_curtail_kwh DOUBLE NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_hourly ADD COLUMN IF NOT EXISTS {column}")
        hp_energy_columns = (
            "actual_heating_consumed_kwh DOUBLE NULL", "actual_heating_generated_kwh DOUBLE NULL",
            "actual_heating_cop DOUBLE NULL", "actual_dhw_consumed_kwh DOUBLE NULL",
            "actual_dhw_generated_kwh DOUBLE NULL", "actual_dhw_cop DOUBLE NULL",
            "actual_cooling_consumed_kwh DOUBLE NULL", "actual_cooling_generated_kwh DOUBLE NULL",
            "actual_cooling_cop DOUBLE NULL", "actual_heat_pump_electric_kwh DOUBLE NULL",
            "actual_heat_pump_thermal_kwh DOUBLE NULL", "actual_heat_pump_cop DOUBLE NULL",
            "actual_heat_pump_running_slot_count INT NOT NULL DEFAULT 0",
        )
        for table in ("ems_gpt_core_hourly", "ems_gpt_daily"):
            for column in hp_energy_columns:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "pv_production_start_time TIME NULL", "pv_production_end_time TIME NULL",
            "heating_production_start_time TIME NULL", "heating_production_end_time TIME NULL",
            "dhw_production_start_time TIME NULL", "dhw_production_end_time TIME NULL",
            "cooling_production_start_time TIME NULL", "cooling_production_end_time TIME NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_daily ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "pv_production_start_time", "pv_production_end_time",
            "heating_production_start_time", "heating_production_end_time",
            "dhw_production_start_time", "dhw_production_end_time",
            "cooling_production_start_time", "cooling_production_end_time",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_daily MODIFY COLUMN {column} TIME NULL")
        for column in (
            "actual_heating_consumed_kwh DOUBLE NULL", "actual_heating_generated_kwh DOUBLE NULL",
            "actual_heating_cop DOUBLE NULL", "actual_dhw_consumed_kwh DOUBLE NULL",
            "actual_dhw_generated_kwh DOUBLE NULL", "actual_dhw_cop DOUBLE NULL",
            "actual_cooling_consumed_kwh DOUBLE NULL", "actual_cooling_generated_kwh DOUBLE NULL",
            "actual_cooling_cop DOUBLE NULL", "actual_heat_pump_electric_kwh DOUBLE NULL",
            "actual_heat_pump_thermal_kwh DOUBLE NULL", "actual_heat_pump_cop DOUBLE NULL",
            "actual_heat_pump_running_slot_count INT NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_core_analytics_runs ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "completion_status VARCHAR(24) NOT NULL DEFAULT 'OPEN'",
            "terminal_slot_count INT NOT NULL DEFAULT 0",
            "soc_mean_7d_pct DOUBLE NULL", "soc_mean_14d_pct DOUBLE NULL",
            "soc_mean_28d_pct DOUBLE NULL", "soc_samples_7d INT NOT NULL DEFAULT 0",
            "soc_samples_14d INT NOT NULL DEFAULT 0", "soc_samples_28d INT NOT NULL DEFAULT 0",
            "soc_terminal_forecast_pct DOUBLE NULL",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_daily ADD COLUMN IF NOT EXISTS {column}")
        for column in (
            "planned_pv_to_bat_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_to_cwu_kwh DOUBLE NOT NULL DEFAULT 0",
            "planned_pv_to_ev_kwh DOUBLE NOT NULL DEFAULT 0", "planned_pv_curtail_kwh DOUBLE NOT NULL DEFAULT 0",
        ):
            cur.execute(f"ALTER TABLE ems_gpt_daily ADD COLUMN IF NOT EXISTS {column}")
        cur.execute("ALTER TABLE ems_gpt_daily MODIFY COLUMN closed_at DATETIME(6) NULL")
        # 0.25.11 intentionally removes the short-lived legacy HP decision
        # matrix and retired process history. Telemetry and DHW analytics stay.
        for table in ("ems_gpt_slots", "ems_gpt_plan_stage_rows"):
            for column in ("heat_pump_no_buy", "hp_run_preferred", "hp_run_neutral", "hp_run_avoid"):
                cur.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
        for table in ("ems_gpt_core_process_decisions", "ems_gpt_core_process_overrides",
                      "ems_gpt_core_commands", "ems_gpt_core_process_execution"):
            cur.execute(f"DELETE FROM {table} WHERE process_name IN ('MANUAL_CIRCULATION','HP_DHW')")
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_7_0", json.dumps({"version": app_version})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_21_0", json.dumps({"version": app_version, "scope": "overrides_commands_process_execution_todo"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_22_0", json.dumps({"version": app_version, "scope": "explicit_ppd_matrix"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_22_1", json.dumps({"version": app_version, "scope": "outage_recovery_hour_daily_quality"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_23_0", json.dumps({"version": app_version, "scope": "canonical_utc_dst_slot_calendar_rce_schedule"})))
        for table in _SLOT_ID_TABLES:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS slot_id VARCHAR(32) NULL")
        _backfill_slot_ids_batched(conn, cur)
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_24_0", json.dumps({"version": app_version, "scope": "independent_soc_quantitative_allocator_slot_id_relations"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_24_1", json.dumps({"version": app_version, "scope": "continuous_slot_relations_diagnostics_todo_threshold"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_24_2", json.dumps({"version": app_version, "scope": "quantitative_pv_allocator_hour_daily"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_25_0", json.dumps({"version": app_version, "scope": "v3_analytics_ai_observer_multi_slot_rce_horizon"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_25_11", json.dumps({"version": app_version, "scope": "binary_hp_window_cost_replan_manual_origin_cleanup"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_25_20", json.dumps({"version": app_version, "scope": "command_expiry_observer_todo_lifecycle"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_27_0", json.dumps({"version": app_version, "scope": "analytics_confidence_daylight_intermittent_flows"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_29_0", json.dumps({"version": app_version, "scope": "configurable_appliance_daily_metering"})))
        cur.execute("INSERT IGNORE INTO ems_gpt_core_migrations VALUES (%s,NOW(6),%s)",
                    ("core_schema_0_35_7", json.dumps({"version": app_version, "scope": "shadow_historical_soc_target_analysis"})))
        # Normalize the historical/UI typo before the 0.24 slot-id cutover.
        for table, column in (
            ("ems_gpt_slots", "grid_policy_planned"),
            ("ems_gpt_slots", "export_policy_planned"),
            ("ems_gpt_plan_stage_rows", "grid_policy_planned"),
            ("ems_gpt_plan_stage_rows", "export_policy_planned"),
            ("ems_gpt_core_process_decisions", "decision"),
        ):
            cur.execute(f"""UPDATE {table} SET {column}='NEUTRAL'
              WHERE UPPER(REPLACE({column},' ','')) IN ('NEURAL','NEUTRAL')
                AND {column}<>'NEUTRAL'""")

