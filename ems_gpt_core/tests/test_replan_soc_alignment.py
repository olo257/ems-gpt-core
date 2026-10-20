"""20.10 morning replay: replanning must not stop BUY at a moving ceiling."""
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner_soc_service import project_soc_to_horizon_start, validate_recoverable_soc_requirements
from planner_dispatch_service import (
    preserve_active_buy_window_target, preserve_target_contract_after_reachability,
    optimize_energy_horizon, validated_buy_deadline_targets,
    validate_physical_buy_commitments,
)


class ReplanSocAlignmentTests(unittest.TestCase):
    now = datetime(2026, 10, 20, 4, 47)
    cutoff = datetime(2026, 10, 20, 5)

    def active(self, target=33.2):
        return dict(slot_start=datetime(2026, 10, 20, 4, 45),
                    slot_end=self.cutoff, buy_window=True, sale_window=False,
                    planned_buy_kwh=1.239796, soc_charge_target_pct=target)

    def sample(self, charge=4500., discharge=0.):
        return dict(captured_at=self.now, battery_charge_power_w=charge,
                    battery_discharge_power_w=discharge,
                    pv_power_w=0., load_power_w=400.)

    def project(self, measured=16., sample=None, row=None):
        return project_soc_to_horizon_start(
            measured, self.now, self.cutoff, row or self.active(), sample,
            15., 5., .98, .98)

    def row(self, start=None, buy=True):
        start = start or self.cutoff
        return dict(slot_start=start, slot_end=start+timedelta(minutes=15),
                    buy_window=buy, sale_window=False, forecast_load_kwh=.1,
                    forecast_pv_total_kwh=0., price_buy_pln_kwh=1., price_sell_pln_kwh=0.)

    def test_remaining_charge_is_counted_once_as_dc_energy(self):
        projection = self.project(sample=self.sample())
        self.assertEqual(projection['remaining_seconds'], 780)
        self.assertAlmostEqual(projection['delta_kwh'], .975)
        self.assertAlmostEqual(projection['horizon_initial_soc_pct'], 22.5)
        self.assertEqual(projection['measured_soc_pct'], 16.)

    def test_remaining_discharge_cannot_be_omitted(self):
        projection = self.project(measured=50., sample=self.sample(0., 1500.))
        self.assertAlmostEqual(projection['horizon_initial_soc_pct'], 50.-.325/15*100)

    def test_grid_charge_projection_stops_at_active_target(self):
        self.assertEqual(self.project(measured=32., sample=self.sample())['horizon_initial_soc_pct'], 33.2)

    def test_pv_charge_is_not_limited_by_grid_target(self):
        sample = self.sample()
        sample['pv_power_w'] = 6000.
        self.assertGreater(self.project(measured=32., sample=sample)['horizon_initial_soc_pct'], 33.2)

    def test_stale_or_missing_power_never_credits_promised_charge(self):
        stale = self.sample()
        stale['captured_at'] = self.now-timedelta(minutes=3)
        for sample in (None, stale, {**self.sample(), 'battery_charge_power_w': None}):
            projection = self.project(sample=sample)
            self.assertEqual(projection['horizon_initial_soc_pct'], 16.)
            self.assertEqual(projection['source'], 'NO_FRESH_POWER_NO_CHARGE_CREDIT')

    def test_projection_uses_only_unelapsed_time_and_preserves_below_reserve_soc(self):
        projection = project_soc_to_horizon_start(
            13., self.cutoff, self.cutoff, self.active(), None, 15., 5., .98, .98)
        self.assertEqual(projection['horizon_initial_soc_pct'], 13.)
        row = self.active()
        row['planned_battery_discharge_kwh'] = .3
        self.assertLess(self.project(measured=13., row=row)['horizon_initial_soc_pct'], 13.)

    def test_measured_power_is_limited_to_battery_physical_power(self):
        projection = self.project(sample=self.sample(50000.))
        self.assertAlmostEqual(projection['delta_kwh'], 5.*.98*13/60)

    def test_morning_target_drop_is_prevented_and_full_buy_is_kept(self):
        # Original 04:45 goal = 33.2%. At 04:47 SOC was still 16%.
        # Dropping the active slot gave 05:00 a false stop threshold of 24.1%.
        rows = [self.row()]
        requested = dict(required=[100.], raw_required=[120.],
                         charge_targets=[100.], selected_buy_indices={0})
        recovery = validate_recoverable_soc_requirements(
            rows, requested['raw_required'], 16., 15., 100., capacity_kwh=15.,
            eta_c=.98, eta_d=.98, max_power_kw=5., slot_minutes=15)
        contract = preserve_target_contract_after_reachability(requested, recovery, set())
        self.assertAlmostEqual(contract['charge_targets'][0], 24.1)
        locked = preserve_active_buy_window_target(rows, self.active(), contract, 100.)
        self.assertEqual(locked['charge_targets'], [33.2])
        # Actual SOC at the 05:00 boundary was 22%. Keep charging for the
        # whole slot: the remaining physical shortfall is reported separately.
        plan = optimize_energy_horizon(
            rows, 22., 15., 15., .98, .98, .05, 0., 5., 15,
            [15.], 15., .1, 100., minimum_soc_targets=locked['charge_targets'],
            target_due_indices={0}, required_soc_pcts=[15.],
            battery_sales_enabled=False, grid_charge_caps=locked['charge_targets'],
            allow_buy_target_shortfall=True)
        self.assertEqual(plan['effective_target_pcts'], [33.2])
        self.assertAlmostEqual(plan['flows'][0]['soc_end_pct'], 30.1)
        self.assertAlmostEqual(plan['flows'][0]['grid_charge_kwh'], 1.215/.98)
        self.assertAlmostEqual(plan['buy_target_shortfalls'][0]['shortfall_pct'], 3.1)
        deadlines = validated_buy_deadline_targets(plan, [33.2], [15.], False)
        validate_physical_buy_commitments(rows, plan['flows'], {0}, deadlines, 5., 15, 15., .98)

    def test_target_is_shared_only_until_end_of_current_contiguous_buy(self):
        rows = [self.row(), self.row(self.cutoff+timedelta(minutes=15), buy=False),
                self.row(self.cutoff+timedelta(minutes=30))]
        contract = dict(charge_targets=[24.1, 17., 28.])
        locked = preserve_active_buy_window_target(rows, self.active(), contract, 100.)
        self.assertEqual(locked['charge_targets'], [33.2, 17., 28.])
        self.assertEqual(contract['charge_targets'], [24.1, 17., 28.])

    def test_no_carry_outside_buy_after_gap_or_without_selected_charge(self):
        contract = dict(charge_targets=[24.1])
        for active in (None, {**self.active(), 'buy_window': False},
                       {**self.active(), 'sale_window': True},
                       {**self.active(), 'planned_buy_kwh': 0.},
                       {**self.active(), 'slot_end': self.cutoff-timedelta(minutes=15)}):
            self.assertEqual(preserve_active_buy_window_target(
                [self.row()], active, contract, 100.)['charge_targets'], [24.1])
        self.assertEqual(preserve_active_buy_window_target(
            [self.row(buy=False)], self.active(), contract, 100.)['charge_targets'], [24.1])

    def test_increased_demand_is_allowed_and_configuration_cap_is_respected(self):
        self.assertEqual(preserve_active_buy_window_target(
            [self.row()], self.active(), dict(charge_targets=[40.]), 100.)['charge_targets'], [40.])
        self.assertEqual(preserve_active_buy_window_target(
            [self.row()], self.active(), dict(charge_targets=[24.1]), 30.)['charge_targets'], [30.])


if __name__ == '__main__':
    unittest.main()
