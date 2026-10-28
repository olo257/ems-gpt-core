import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ingestion_service import extend_night_buy_capacity, build_ingestion, IngestionAdapters
from planner_dispatch_service import (
    deterministic_soc_target_contract, preserve_target_contract_after_reachability,
    optimize_energy_horizon, morning_energy_coverage,
)
from planner_soc_service import validate_recoverable_soc_requirements


class NightBuyCapacityTests(unittest.TestCase):
    def rows(self):
        start = datetime(2026, 10, 20, 4)
        prices = [1.33709,1.33858,1.34513,1.30343,1.25363,1.311,1.38715,1.43094]
        rows = []
        for i in range(24):
            clock = start+timedelta(minutes=15*i)
            buy = i in (3,4,23)
            rows.append(dict(slot_start=clock, slot_end=clock+timedelta(minutes=15),
                             buy_window=buy, sale_window=False,
                             market_window='BUY' if buy else 'NEUTRAL',
                             price_buy_pln_kwh=prices[i] if i < 8 else 1.7,
                             forecast_load_kwh=.1 if i < 8 else .25,
                             forecast_pv_total_kwh=0., forecast_heat_pump_load_kwh=0.))
        return rows

    def expand(self, rows):
        return extend_night_buy_capacity(rows, 15., 15., 2., .98, .98,
                                         5., 15, 1., .05, 0.)

    def test_real_prices_extend_cheap_window_instead_of_target_only(self):
        rows = self.rows()
        result = self.expand(rows)
        self.assertEqual(result['changed_indices'], [2,5])
        self.assertEqual([i for i,r in enumerate(result['rows']) if r['buy_window']], [2,3,4,5,23])
        report = result['reports'][0]
        self.assertEqual(report['window_end'], '2026-10-20 05:30:00')
        self.assertEqual(report['status'], 'CAPACITY_READY')
        self.assertGreater(report['required_soc_after_window_pct'], 40.)
        self.assertAlmostEqual(report['available_charge_kwh'], 4.86)
        self.assertEqual(report['window_capacity_shortfall_kwh'], 0.)
        self.assertFalse(rows[2]['buy_window'])

    def test_low_morning_load_does_not_expand_night(self):
        rows = self.rows()
        for row in rows:
            row['forecast_load_kwh'] = .02
        self.assertEqual(self.expand(rows)['changed_indices'], [])

    def test_useful_pv_reduces_required_night_capacity(self):
        rows = self.rows()
        for row in rows[12:23]:
            row['forecast_pv_total_kwh'] = .5
        self.assertEqual(self.expand(rows)['changed_indices'], [])

    def test_sell_is_never_overwritten_and_expensive_neighbours_are_rejected(self):
        rows = self.rows()
        rows[2].update(sale_window=True, market_window='SELL')
        rows[5]['price_buy_pln_kwh'] = 2.
        result = self.expand(rows)
        self.assertEqual(result['changed_indices'], [])
        self.assertTrue(result['rows'][2]['sale_window'])
        self.assertFalse(result['rows'][2]['buy_window'])
        self.assertEqual(result['reports'][0]['status'], 'ECONOMIC_OR_NIGHT_CAPACITY_LIMIT')
        self.assertGreater(result['reports'][0]['window_capacity_shortfall_kwh'], 0.)

    def test_missing_forecasts_never_create_new_permissions(self):
        rows = self.rows()
        rows[15]['forecast_load_kwh'] = None
        result = self.expand(rows)
        self.assertEqual(result['changed_indices'], [])
        self.assertEqual(result['reports'][0]['status'], 'WAITING_COMPLETE_FORECAST')

    def test_battery_capacity_limit_is_reported_instead_of_coverage_success(self):
        rows = self.rows()
        for row in rows[8:23]:
            row['forecast_load_kwh'] = 1.5
        result = self.expand(rows)
        self.assertGreater(result['reports'][0]['battery_capacity_shortfall_pct'], 0.)
        self.assertNotEqual(result['reports'][0]['status'], 'CAPACITY_READY')
        self.assertTrue(all(result['rows'][i]['slot_start'].hour < 6 for i in result['changed_indices']))

    def test_remaining_horizon_cannot_expand_already_finished_night(self):
        result = self.expand(self.rows()[8:])
        self.assertEqual(result['changed_indices'], [])
        self.assertEqual(result['reports'], [])

    def test_daytime_buy_expands_before_expensive_sell_and_never_overwrites_sell(self):
        from ingestion_service import extend_buy_capacity_before_sell
        start = datetime(2026, 10, 20, 6)
        rows = []
        for i in range(40):
            clock = start + timedelta(minutes=15*i)
            buy = i == 14
            sell = i >= 28
            rows.append(dict(
                slot_start=clock, slot_end=clock+timedelta(minutes=15),
                buy_window=buy, sale_window=sell,
                market_window='SELL' if sell else 'BUY' if buy else 'NEUTRAL',
                price_buy_pln_kwh=.3 + i*.01, forecast_load_kwh=.25,
                forecast_pv_total_kwh=0., forecast_heat_pump_load_kwh=0.))
        result = extend_buy_capacity_before_sell(
            rows, 15., 15., 2., .98, .98, 5., 15, 1.)
        self.assertTrue(result['changed_indices'])
        self.assertTrue(all(i < 28 for i in result['changed_indices']))
        self.assertTrue(all(not row['buy_window'] for row in result['rows'][28:]))

    def test_daytime_expansion_never_reopens_completed_buy_windows(self):
        from ingestion_service import extend_buy_capacity_before_sell
        start = datetime(2026, 10, 20, 6)
        rows = []
        for i in range(40):
            clock = start + timedelta(minutes=15*i)
            buy = i in (14, 24)
            sell = i >= 28
            rows.append(dict(
                slot_start=clock, slot_end=clock+timedelta(minutes=15),
                buy_window=buy, sale_window=sell,
                market_window='SELL' if sell else 'BUY' if buy else 'NEUTRAL',
                price_buy_pln_kwh=.3 + i*.01, forecast_load_kwh=.25,
                forecast_pv_total_kwh=0., forecast_heat_pump_load_kwh=0.,
                actual_recorded_at=clock if i < 16 else None))
        result = extend_buy_capacity_before_sell(
            rows, 15., 15., 2., .98, .98, 5., 15, 1.,
            earliest_slot_start=start + timedelta(minutes=15*16))
        self.assertTrue(result['changed_indices'])
        self.assertTrue(all(i >= 16 for i in result['changed_indices']))
        self.assertTrue(all(result['rows'][i]['actual_recorded_at'] is None
                            for i in result['changed_indices']))

    def dispatch(self, rows):
        requested = deterministic_soc_target_contract(
            rows, [17.]*len(rows), [17.]*len(rows), 15., 15., .98, .98, 5., 15, 1.)
        recovery = validate_recoverable_soc_requirements(
            rows, requested['raw_required'], 15., 15., 100., capacity_kwh=15.,
            eta_c=.98, eta_d=.98, max_power_kw=5.)
        contract = preserve_target_contract_after_reachability(requested, recovery, set())
        return optimize_energy_horizon(
            rows, 15., 15., 15., .98, .98, .05, 0., 5., 15,
            [15.]*len(rows), 17., .1, 100., minimum_soc_targets=contract['charge_targets'],
            target_due_indices=contract['buy_due_indices'], required_soc_pcts=contract['required'],
            battery_sales_enabled=False, grid_charge_caps=contract['charge_targets'],
            allow_buy_target_shortfall=True)

    def test_expansion_covers_morning_instead_of_merely_retaining_low_target(self):
        rows = self.rows()
        original = self.dispatch(rows)
        original_report = morning_energy_coverage(
            rows, original['flows'], 15., 15., 2., .98, .98, 5., 15, 1.)
        self.assertEqual(original_report[0]['status'], 'SHORTFALL')
        expanded = self.expand(rows)['rows']
        corrected = self.dispatch(expanded)
        corrected_report = morning_energy_coverage(
            expanded, corrected['flows'], 15., 15., 2., .98, .98, 5., 15, 1.)
        self.assertEqual(corrected_report[0]['status'], 'COVERED')
        self.assertEqual(corrected_report[0]['morning_grid_load_kwh'], 0.)

    def test_unavoidable_morning_grid_is_reported_as_shortfall(self):
        rows = self.rows()[8:23]
        flows = [dict(soc_start_pct=15., grid_charge_kwh=0., grid_load_kwh=.25) for _ in rows]
        report = morning_energy_coverage(rows, flows, 15., 15., 2., .98, .98, 5., 15, 1.)
        self.assertEqual(report[0]['status'], 'SHORTFALL')
        self.assertGreater(report[0]['missing_internal_kwh'], 3.)
        self.assertAlmostEqual(report[0]['morning_grid_load_kwh'], 3.75)

    def test_importer_uses_callers_transaction_and_rejects_permission_conflict(self):
        def forbidden_db():
            raise AssertionError('Night permissions must use the planner transaction')

        service = build_ingestion(IngestionAdapters(
            options=dict(battery_capacity_kwh=15., battery_min_soc_pct=15.,
                         battery_charge_efficiency=.98, battery_discharge_efficiency=.98,
                         battery_max_power_kw=5., battery_degradation_cost_pln_kwh=.05,
                         minimum_arbitrage_margin_pln_kwh=0.),
            timezone=None, pv_forecast_entities={}, db=forbidden_db,
            local_now=lambda: datetime(2026,10,20), slot_start=lambda: datetime(2026,10,20),
            canonical_slots_for_day=lambda _: [], number=lambda v: v, ha_state=lambda _: None,
            ha_service_response=lambda *args: None, record_event=lambda *args: None))

        class Cursor:
            def __init__(self, rowcount):
                self.rowcount, self.writes = rowcount, []
            def execute(self, sql, params):
                self.writes.append(params[0])

        cursor = Cursor(1)
        result = service.ensure_night_buy_capacity(self.rows(), cursor)
        self.assertEqual(cursor.writes, [self.rows()[i]['slot_start'] for i in result['changed_indices']])
        with self.assertRaisesRegex(RuntimeError, 'NIGHT_BUY_PERMISSION_CHANGED'):
            service.ensure_night_buy_capacity(self.rows(), Cursor(0))


if __name__ == '__main__':
    unittest.main()
