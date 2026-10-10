import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from energy_balance_service import assess_published_plan, calculate_slot_energy_balance


class EnergyBalanceServiceTests(unittest.TestCase):
    def test_status_balance_includes_heat_pump_and_reports_residual_grid_load(self):
        row = {
            "forecast_pv_total_kwh": 0.034637,
            "forecast_load_kwh": 0.163584,
            "forecast_heat_pump_load_kwh": 0.1267,
            "planned_battery_discharge_kwh": 0.24,
            "planned_buy_kwh": 0,
            "planned_battery_charge_kwh": 0,
            "planned_sell_kwh": 0,
            "planned_pv_to_bat_kwh": 0,
        }
        result = calculate_slot_energy_balance(row, 0.9, 0.98)
        self.assertAlmostEqual(result["total_load_kwh"], 0.290284, places=6)
        self.assertGreater(result["grid_load_kwh"], 0.0)
        self.assertAlmostEqual(result["difference_kwh"], 0.0, places=6)

    def test_published_plan_fails_on_unbalanced_energy_or_soc_requirement(self):
        balanced = {
            "forecast_pv_total_kwh": 0.2, "forecast_load_kwh": 0.2,
            "forecast_heat_pump_load_kwh": 0, "planned_battery_discharge_kwh": 0,
            "planned_buy_kwh": 0, "planned_battery_charge_kwh": 0,
            "planned_sell_kwh": 0, "planned_pv_to_bat_kwh": 0,
            "soc_end_plan_pct": 30, "soc_required_pct": 25,
        }
        self.assertTrue(assess_published_plan([balanced], 0.9, 0.95)["ok"])
        broken = {**balanced, "forecast_heat_pump_load_kwh": 0.3}
        self.assertFalse(assess_published_plan([broken], 0.9, 0.95)["ok"])
        broken_soc = {**balanced, "soc_end_plan_pct": 20}
        self.assertFalse(assess_published_plan([broken_soc], 0.9, 0.95)["ok"])


if __name__ == "__main__":
    unittest.main()
