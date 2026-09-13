import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
MODULE_SOURCES = {
    path.name: path.read_text(encoding="utf-8")
    for path in ROOT.glob("*_service.py")
}
WEBUI = (ROOT / "webui.html").read_text(encoding="utf-8")
PYTHON_SOURCE = "\n".join([APP_SOURCE, *MODULE_SOURCES.values()])
SOURCE = PYTHON_SOURCE + "\n" + WEBUI
CONFIG = (ROOT / "config.yaml").read_text(encoding="utf-8")


class OfflineContractTests(unittest.TestCase):
    def test_python_parses(self):
        ast.parse(APP_SOURCE)
        for name, source in MODULE_SOURCES.items():
            with self.subTest(module=name):
                ast.parse(source)

    def test_version_is_consistent(self):
        self.assertIn('APP_VERSION = "0.27.3"', APP_SOURCE)
        self.assertIn('version: "0.27.3"', CONFIG)

    def test_modular_runtime_boundaries(self):
        self.assertIn("from observer_service import", APP_SOURCE)
        self.assertIn("from diagnostics_service import", APP_SOURCE)
        self.assertIn("from todo_service import", APP_SOURCE)
        self.assertIn("from analytics_service import", APP_SOURCE)
        self.assertIn("from scheduler_service import", APP_SOURCE)
        self.assertIn("from api_service import", APP_SOURCE)
        self.assertIn("from executor_service import", APP_SOURCE)
        self.assertIn("from ingestion_service import", APP_SOURCE)
        self.assertIn("from telemetry_service import", APP_SOURCE)
        self.assertIn("from materialization_service import", APP_SOURCE)
        self.assertIn("from slot_calendar_service import", APP_SOURCE)
        self.assertIn("from ha_gateway_service import", APP_SOURCE)
        self.assertIn("from runtime_service import", APP_SOURCE)
        self.assertIn("from database_service import", APP_SOURCE)
        self.assertIn("from recovery_service import", APP_SOURCE)
        self.assertIn("from time_service import", APP_SOURCE)
        self.assertIn("from config_service import", APP_SOURCE)
        self.assertIn("_EXECUTOR = build_executor(ExecutorAdapters(", APP_SOURCE)
        self.assertIn("_INGESTION = build_ingestion(IngestionAdapters(", APP_SOURCE)
        self.assertIn("Handler = build_handler(ApiAdapters(", APP_SOURCE)
        self.assertNotIn("class Handler(", APP_SOURCE)
        self.assertIn("class Handler(", MODULE_SOURCES["api_service.py"])
        self.assertIn("def build_executor(", MODULE_SOURCES["executor_service.py"])
        self.assertIn("def build_ingestion(", MODULE_SOURCES["ingestion_service.py"])
        self.assertIn("def build_telemetry(", MODULE_SOURCES["telemetry_service.py"])
        self.assertIn("def build_materializations(", MODULE_SOURCES["materialization_service.py"])
        self.assertIn("_MATERIALIZATIONS = build_materializations(MaterializationAdapters(", APP_SOURCE)
        self.assertIn("_SLOT_CALENDAR = build_slot_calendar(SlotCalendarAdapters(", APP_SOURCE)
        self.assertIn("_HA_GATEWAY = build_home_assistant_gateway(HomeAssistantAdapters(", APP_SOURCE)
        self.assertIn("_RUNTIME = build_runtime(APP_NAME, APP_VERSION, LOG)", APP_SOURCE)
        self.assertIn("_DATABASE = build_database(OPTIONS, TZ)", APP_SOURCE)
        self.assertIn("_RECOVERY = build_recovery(RecoveryAdapters(", APP_SOURCE)
        self.assertIn("_TIME = build_time_service(TimeAdapters(", APP_SOURCE)
        self.assertIn("OPTIONS = load_options(OPTIONS_PATH, RUNTIME_SETTINGS_PATH)", APP_SOURCE)
        self.assertIn('with_name("webui.html")', APP_SOURCE)
        self.assertNotIn("<!doctype html>", APP_SOURCE)
        self.assertIn("<!doctype html>", WEBUI)
        self.assertIn("SHADOW_READ_ONLY", MODULE_SOURCES["observer_service.py"])

    def test_stability_scheduler_and_health_contract(self):
        self.assertIn("heavy_job_lock = threading.RLock()", SOURCE)
        self.assertIn('run_serialized("recovery_materializations"', SOURCE)
        self.assertIn("recovery_rebuild_day != clock.date()", SOURCE)
        self.assertNotIn('if start.minute == 8:', SOURCE)
        self.assertIn("if minute < 15:", SOURCE)
        self.assertIn("heartbeat_age < 180", SOURCE)
        loop = MODULE_SOURCES["scheduler_service.py"]
        self.assertEqual(loop.count("a.rebuild_recovery_materializations"), 1)
        self.assertIn('a.state["rce"] = {', loop)
        self.assertIn('"status": "ALREADY_COMPLETED"', loop)
        self.assertIn('json.loads(prior_rce.get("payload_json")', loop)
        self.assertIn('"RCE import completed: status=%s rows=%s/%s planner=%s"', loop)
        self.assertIn('"RCE import failed: target=%s"', loop)
        self.assertIn('"RCE state restored: status=ALREADY_COMPLETED rows=%s/%s target=%s"', loop)
        self.assertIn("id='rceStatus'", WEBUI)
        self.assertIn("rs.rows??'—'", WEBUI)
        self.assertIn("rce_event_keys(clock)", MODULE_SOURCES["scheduler_service.py"])

    def test_watchdog_uses_early_liveness_endpoint(self):
        api = MODULE_SOURCES["api_service.py"]
        self.assertIn("watchdog: http://[HOST]:[PORT:8099]/live", CONFIG)
        self.assertIn('path.endswith("/live")', api)
        self.assertIn('"status": "PROCESS_RUNNING"', api)
        self.assertIn("server_thread.start()", APP_SOURCE)
        main_position = APP_SOURCE.index("def main()")
        self.assertLess(APP_SOURCE.index("server_thread.start()", main_position),
                        APP_SOURCE.index("initialize()", main_position))
        self.assertIn("database_ok and status_ok", api)

    def test_hp_manual_duration_and_external_priority(self):
        self.assertIn("HP_HEAT_DHW FORCE_ON must last at least", SOURCE)
        self.assertIn("hpManualHours", SOURCE)
        self.assertIn("externally_started_hp_is_running", SOURCE)
        self.assertIn('source = "OVERRIDE" if requested else "EXTERNAL_MANUAL"', SOURCE)
        self.assertIn('requested != "FORCE_OFF"', SOURCE)
        self.assertIn('True if value.get("external_manual_on")', SOURCE)
        self.assertIn('datetime(9999, 12, 31, 23, 59, 59)', SOURCE)
        self.assertIn('BLOKADA BEZTERMINOWA', SOURCE)
        self.assertIn("hpBlocked?'danger'", SOURCE)
        observed_position = SOURCE.index('observed = None if energy_value is None')
        external_position = SOURCE.index('external_manual = requested is None and observed == "RUNNING"')
        self.assertLess(observed_position, external_position)

    def test_app_status_card_uses_backend_start_time(self):
        self.assertIn('"started_at": datetime.now(timezone.utc).isoformat()', SOURCE)
        for label in ("URUCHOMIONO", "URUCHAMIANIE", "TRYB OGRANICZONY", "BŁĄD", "BRAK POŁĄCZENIA"):
            self.assertIn(label, SOURCE)
        self.assertIn("s.started_at", SOURCE)
        self.assertIn("id='appStatus'", SOURCE)
        self.assertIn("document.getElementById('appStatus')", SOURCE)
        self.assertNotIn("id='status'", SOURCE)
        self.assertIn("uruchomiono:", SOURCE)
        self.assertNotIn("Wersja: … · heartbeat: …", SOURCE)

    def test_panel_translates_technical_connection_and_executor_states(self):
        self.assertIn("CONNECTED:'POŁĄCZONY'", WEBUI)
        self.assertIn("LIVE:'PRODUKCJA'", WEBUI)
        self.assertIn("db.textContent=displayStatus(s.database)", WEBUI)
        self.assertIn("executor.textContent=displayStatus(s.executor)", WEBUI)

    def test_hourly_source_version_tracks_runtime(self):
        self.assertNotIn("'CORE_0_22_1',%s,%s,%s) ON DUPLICATE KEY UPDATE", SOURCE)
        self.assertIn("f\"CORE_{APP_VERSION.replace('.', '_')}\"", SOURCE)

    def test_ai_observer_is_read_only_and_persisted(self):
        self.assertIn("def run_ai_observer", SOURCE)
        self.assertIn('"mode": "SHADOW_READ_ONLY"', SOURCE)
        self.assertIn('"PLAN_WRITE", "PPD_WRITE", "COMMAND_WRITE", "HA_SERVICE_CALL"', SOURCE)
        self.assertIn("INSERT INTO ems_gpt_core_ai_runs", SOURCE)
        self.assertIn("run_ai_observer(analytics_result.get(\"run_id\"))", SOURCE)

    def test_command_expiry_covers_dispatched_and_accepted(self):
        self.assertIn("def expire_stale_commands", SOURCE)
        self.assertIn("'READY_FOR_CONNECTOR','DISPATCHED','ACCEPTED'", SOURCE)
        loop = MODULE_SOURCES["scheduler_service.py"]
        self.assertIn("expire_stale_commands()", loop)

    def test_observer_todo_lifecycle(self):
        self.assertIn("require_consecutive_days=True", SOURCE)
        self.assertIn("consecutive >= 3", SOURCE)
        self.assertIn("def maintain_todo_archive", SOURCE)
        self.assertIn("def reconcile_diagnostic_todos", SOURCE)
        self.assertIn("def review_todo", SOURCE)
        self.assertIn('path.endswith("/api/todo/review")', SOURCE)

    def test_v3_analytics_metrics_are_migrated(self):
        for metric in ("pv_bias_kwh", "load_bias_kwh", "import_bias_kwh",
                       "export_bias_kwh", "soc_mae_pct", "net_cost_variance_pln"):
            self.assertIn(metric, SOURCE)
        analytics = MODULE_SOURCES["analytics_service.py"]
        self.assertIn("metric_rows = [row for row in rows if _is_core_quality_slot(row)]", analytics)
        self.assertIn('_wape(pv_metric_rows, "forecast_pv_total_kwh", "actual_pv_total_kwh")', analytics)
        self.assertIn('_wape(metric_rows, "forecast_load_kwh", "actual_native_load_kwh")', analytics)
        self.assertIn('"load_basis": "HOUSEHOLD_EXCLUDING_EV_AND_HEAT_PUMP"', analytics)
        self.assertIn('"metric_confidence_pct"', analytics)
        self.assertIn('"import_event_f1_pct"', analytics)
        self.assertIn('"export_event_f1_pct"', analytics)
        self.assertIn('for r in metric_rows)', analytics)

    def test_planner_uses_all_available_rce_slots(self):
        planner = SOURCE[SOURCE.index("def run_planner"):SOURCE.index("def _wape")]
        self.assertNotIn("ORDER BY slot_start LIMIT 96", planner)
        self.assertIn("checks[\"n\"]==len(source)", planner)

    def test_migration_audit_closure(self):
        self.assertIn('\"ai_observer\": \"SHADOW_READ_ONLY\"', SOURCE)
        self.assertIn('battery_charge >= float(OPTIONS.get("technical_flow_threshold_kwh",0.05))', SOURCE)
        self.assertIn("module_name=%s AND title=%s", SOURCE)
        self.assertIn("status IN ('WATCHING','OPEN','SUGGESTED')", SOURCE)
        self.assertIn('(command_id,slot_start,slot_id,process_name', SOURCE)

    def test_allocator_is_materialized_in_hour_and_daily(self):
        self.assertIn('core_schema_0_24_2', SOURCE)
        self.assertIn('UPDATE ems_gpt_core_hourly SET planned_pv_to_bat_kwh', SOURCE)
        self.assertIn('UPDATE ems_gpt_daily SET planned_pv_to_bat_kwh', SOURCE)

    def test_quantitative_allocator_and_independent_soc(self):
        for field in ("planned_pv_to_bat_kwh", "planned_pv_to_cwu_kwh",
                      "planned_pv_to_ev_kwh", "planned_pv_curtail_kwh"):
            self.assertIn(field, SOURCE)
        self.assertNotIn("target=min(95,max(floor,targets[i]))", SOURCE)
        self.assertIn("required_soc=max(effective_floor,target)", SOURCE)

    def test_historical_slot_relations_have_fail_closed_audit(self):
        self.assertIn("def backfill_slot_relations", SOURCE)
        self.assertIn('"ready": not any(unresolved.values())', SOURCE)
        self.assertIn('WHERE slot_start IS NOT NULL AND slot_id IS NULL', SOURCE)

    def test_canonical_time_and_dst_contract(self):
        for marker in ("ems_gpt_core_slot_calendar", "slot_id", "slot_start_utc",
                       "utc_offset_minutes", "local_fold", "slot_index_local"):
            self.assertIn(marker, SOURCE)
        self.assertIn("minute % 10 == 0", SOURCE)
        self.assertNotIn("hour==0 and 1<=minute<10", SOURCE)

    def test_rce_and_battery_import_regressions(self):
        self.assertIn('{"sell":raw,"buy":raw+margin', SOURCE)
        self.assertIn('grid_policy == "BUY_ALLOWED"', SOURCE)

    def test_operational_constants_are_panel_configurable(self):
        for setting in (
            "buy_window_tolerance_pln_kwh", "planned_flow_threshold_kwh",
            "technical_flow_threshold_kwh", "soc_floor_max_pct", "soc_target_max_pct",
            "sale_morning_start_hour", "sale_evening_end_hour",
            "hp_min_heating_hours", "hp_min_cycle_hours",
            "hp_min_cycle_break_hours", "hp_max_cycle_break_hours",
            "telemetry_min_samples_per_slot", "telemetry_learning_coverage_pct",
            "observer_pv_wape_warn_pct", "observer_load_wape_warn_pct",
            "observer_soc_mae_warn_pct", "observer_cost_variance_warn_pln",
            "observer_min_quality_score_pct",
        ):
            self.assertIn(f'"{setting}"', SOURCE)
        self.assertNotIn("CASE WHEN COUNT(t.captured_at)>=12", SOURCE)

    def test_neutral_typo_is_normalized(self):
        self.assertIn("IN ('NEURAL','NEUTRAL')", SOURCE)
        self.assertIn("replace(/^NEU\\\\s*RAL$/i,'NEUTRAL')", SOURCE)

    def test_table_formatter_preserves_letter_t(self):
        self.assertNotIn("replace('T',' ')", SOURCE)
        self.assertIn("replace(/(\\\\d)T(?=\\\\d)/,'$1 ')", SOURCE)

    def test_explicit_ppd_matrix_is_persisted_and_validated(self):
        for field in (
            "grid_buy_allowed", "grid_no_buy", "grid_neutral",
            "sell_bat_allowed", "no_sell_bat", "sell_pv_allowed", "no_sell_pv",
        ):
            self.assertIn(field, SOURCE)
        self.assertIn("grid_buy_allowed+grid_no_buy+grid_neutral<>1", SOURCE)
        self.assertIn("heat_pump_window NOT IN (0,1)", SOURCE)
        self.assertIn("CORE_0_4_1", SOURCE)

    def test_executor_is_safe_by_default(self):
        self.assertIn("executor_enabled: false", CONFIG)
        self.assertIn("executor_dry_run: true", CONFIG)
        self.assertIn('executor_activation_ack: ""', CONFIG)
        self.assertIn('EMS_CONNECTOR_ACCEPTED', SOURCE)

    def test_executor_service_adapter_and_status(self):
        self.assertIn('service_url = f"{a.ha_api}/services/{domain}/{service}"', SOURCE)
        self.assertIn('if return_response:', SOURCE)
        self.assertIn('service_url += "?return_response"', SOURCE)
        self.assertIn('return_response=True', SOURCE)
        self.assertIn('state = "DRY_RUN" if dry_run else "LIVE"', SOURCE)
        self.assertIn('STATE["executor"] = "OFF"', SOURCE)

    def test_executor_starts_live_with_complete_safe_mapping(self):
        self.assertIn("def enable_production_on_startup()", SOURCE)
        self.assertIn("startup_executor = enable_production_on_startup()", SOURCE)
        self.assertIn('"executor_enabled": not missing', SOURCE)
        self.assertIn('"executor_dry_run": bool(missing)', SOURCE)
        self.assertIn('"executor_activation_ack": "" if missing else "EMS_CONNECTOR_ACCEPTED"', SOURCE)
        self.assertIn('"missing_safe_script_mappings": missing', SOURCE)

    def test_circulation_is_outside_application_control_and_panel(self):
        self.assertNotIn("switch.sm_lite_1616r_2_pompa_cyrkulacyjna", SOURCE)
        process_contract = SOURCE[SOURCE.index("PROCESS_NAMES ="):SOURCE.index("OVERRIDE_STATES =")]
        self.assertNotIn("MANUAL_CIRCULATION", process_contract)
        self.assertIn("/api/process-status", SOURCE)
        self.assertNotIn("circulationActual", SOURCE)
        self.assertNotIn("circulationStatus", SOURCE)
        self.assertNotIn("Pompa cyrkulacyjna: DZIAŁA", SOURCE)
        self.assertIn("applicationStatus", SOURCE)
        self.assertIn("Wersja: ${s.version", SOURCE)

    def test_direct_battery_source_needs_explicit_sign(self):
        self.assertIn("direct_battery_power_mode: discharge_positive", CONFIG)
        self.assertIn("charge_positive", CONFIG)
        self.assertIn("discharge_positive", CONFIG)

    def test_no_runtime_dependency_on_v3_measurement_helpers(self):
        entities = SOURCE[SOURCE.index("ENTITIES = {"):SOURCE.index("PV_FORECAST_ENTITIES")]
        self.assertNotIn("sensor.ems_gpt_rce_pse_current", entities)
        self.assertNotIn("sensor.ems_gpt_moc_ladowania_baterii_dokladna", entities)
        self.assertNotIn("sensor.ems_gpt_moc_rozladowania_baterii_dokladna", entities)
        self.assertIn('"source": "EMS_GPT_SLOTS"', SOURCE)
        self.assertNotIn('battery_source = "LEGACY_HELPERS"', SOURCE)

    def test_five_processes_are_present(self):
        for name in ("BATTERY_IMPORT", "BATTERY_EXPORT", "PV_CWU", "PV_EV", "HP_HEAT_DHW"):
            self.assertIn(name, SOURCE)
        self.assertNotIn('"HP_DHW"', SOURCE)
        self.assertNotIn('"MANUAL_CIRCULATION"', SOURCE)

    def test_heat_dhw_uses_night_forecast_and_configured_threshold(self):
        for marker in ("night_heating_threshold_c", "night_min_by_day", "heat_dhw_allowed"):
            self.assertIn(marker, SOURCE)
        self.assertIn("night_min is None or night_min >= night_threshold", SOURCE)
        self.assertIn("i in hp_selected_indices", SOURCE)
        self.assertIn("heat_pump_window=%s", SOURCE)

    def test_hp_cost_replan_cycle_contract(self):
        for marker in ("hp_min_heating_hours", "hp_min_cycle_hours",
                       "hp_min_cycle_break_hours", "hp_max_cycle_break_hours",
                       "hp_cycle_start_penalty_pln", "optimize_hp_heating_slots",
                       "hp_minimum_heating_shortfall"):
            self.assertIn(marker, SOURCE)
        self.assertIn('"hp_min_cycle_hours": 2.0', SOURCE)
        self.assertIn('"hp_min_heating_hours": 10.0', SOURCE)

    def test_hp_load_is_coupled_into_soc_and_recharge_plan(self):
        self.assertIn("hp_load = planned_hp_kw * 0.25 if index in hp_selected_indices else 0.0", SOURCE)
        self.assertIn("hp_load=planned_hp_kw * 0.25 if i in hp_selected_indices else 0.0", SOURCE)
        self.assertIn("load = native_load + hp_load", SOURCE)
        self.assertIn("load=native_load+hp_load", SOURCE)
        self.assertIn("hp_load_kwh={hp_load:.3f}", SOURCE)
        self.assertIn("planned_hp_kwh={hp_load:.3f}", SOURCE)

    def test_hp_has_only_binary_window_decision(self):
        planner = SOURCE[SOURCE.index("def run_planner"):SOURCE.index("def _wape")]
        for obsolete in ("hp_run_preferred", "hp_run_neutral", "hp_run_avoid", "heat_pump_no_buy"):
            self.assertNotIn(obsolete, planner)
        self.assertIn('"ON" if heat_dhw_allowed else "OFF"', planner)

    def test_manual_control_origin_is_recorded(self):
        for marker in ("MANUAL_FORCE_ON", "MANUAL_BLOCK", "EXTERNAL_MANUAL",
                       "override_id", "requested_by", "override_reason", "override_valid_until"):
            self.assertIn(marker, SOURCE)

    def test_command_contract_and_ttl_are_present(self):
        for field in ("command_id", "expires_at", "plan_version", "acknowledgement_json"):
            self.assertIn(field, SOURCE)

    def test_no_soc_program_writes(self):
        forbidden = [f"inverter_program_{number}_soc" for number in range(1, 7)]
        self.assertFalse(any(value in SOURCE for value in forbidden))

    def test_tou_floor_blocks_impossible_battery_sale(self):
        for marker in ("tou_program_snapshot", "active_tou_program", "effective_floor",
                       "TOU_FLOOR_BLOCK", "TOU_FLOOR_UNAVAILABLE",
                       "battery_export_blocked_by_tou_floor"):
            self.assertIn(marker, SOURCE)
        self.assertIn('live_soc <= float(live_program["soc"]) + 0.01', SOURCE)

    def test_outside_temperature_contract(self):
        self.assertIn('"outside_temperature": "sensor.klimat_w_ogrodzie_temperature"', SOURCE)

    def test_missing_optional_sensor_is_safe(self):
        self.assertIn("if not isinstance(state, dict):", SOURCE)

    def test_outage_recovery_contract(self):
        for marker in ("MISSING_OUTAGE", "RECOVERED", "completion_status",
                       "terminal_slot_count", "missing_actual_slot_count", "learning_eligible"):
            self.assertIn(marker, SOURCE)
        self.assertIn("ORDER BY slot_start ASC LIMIT 2688", SOURCE)
        self.assertIn('a.options.get("recovery_lookback_days", 7)', SOURCE)

    def test_database_audit_is_read_only_and_non_blocking(self):
        audit_source = (ROOT / "database_audit_service.py").read_text(encoding="utf-8")
        self.assertIn("information_schema.tables", audit_source)
        self.assertIn("exact_row_count", audit_source)
        self.assertNotIn("DELETE FROM", audit_source)
        self.assertNotIn("UPDATE ", audit_source)
        self.assertNotIn("INSERT INTO", audit_source)
        self.assertNotIn("ALTER TABLE", audit_source)
        self.assertIn('LOG.exception("startup read-only database audit failed")', SOURCE)
        self.assertIn("threading.Thread(target=startup_database_audit, daemon=True).start()", SOURCE)
        self.assertIn("content_scanned\": False", audit_source)


if __name__ == "__main__":
    unittest.main()
