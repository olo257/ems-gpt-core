from datetime import datetime, timedelta
import unittest

from planner_service import tou_sale_safety_floors


def row(at, *, sale=False, load=0.0, pv=0.0):
    return {
        "slot_start": at,
        "local_day": at.date(),
        "sale_window": sale,
        "forecast_load_kwh": load,
        "forecast_heat_pump_load_kwh": 0.0,
        "forecast_pv_total_kwh": pv,
    }


class TouSaleSafetyFloorTest(unittest.TestCase):
    def test_sale_keeps_tou_baseline_plus_load_until_lower_program(self):
        start = datetime(2026, 9, 22, 21, 0)
        rows = [row(start, sale=True)] + [
            row(start + timedelta(minutes=15 * i), load=0.25)
            for i in range(1, 5)
        ]
        programs = ([{"program": 5, "soc": 40.0}] * 4
                    + [{"program": 6, "soc": 30.0}])
        flows = [{"soc_end_pct": 60.0, "grid_charge_kwh": 0.0} for _ in rows]
        floors = tou_sale_safety_floors(
            rows, programs, flows, 15.0, 15.0, 1.0, 0.0, 180, 15,
            [15.0] * len(rows))
        self.assertAlmostEqual(floors[0], 45.0)

    def test_program_five_can_bridge_to_real_near_buy(self):
        start = datetime(2026, 9, 22, 21, 0)
        rows = [row(start, sale=True)] + [
            row(start + timedelta(minutes=15 * i), load=0.25)
            for i in range(1, 5)
        ]
        programs = [{"program": 5, "soc": 40.0}] * len(rows)
        flows = [{"soc_end_pct": 50.0, "grid_charge_kwh": 0.0} for _ in rows]
        flows[3]["grid_charge_kwh"] = 1.0
        required = [15.0] * len(rows)
        required[-1] = 40.0
        floors = tou_sale_safety_floors(
            rows, programs, flows, 15.0, 15.0, 0.95, 1.0, 180, 15,
            required)
        self.assertEqual(floors[0], 15.0)

    def test_bridge_is_denied_when_daily_close_is_short(self):
        start = datetime(2026, 9, 22, 21, 0)
        rows = [row(start, sale=True), row(start + timedelta(minutes=15), load=0.25)]
        programs = [{"program": 5, "soc": 40.0}] * len(rows)
        flows = [
            {"soc_end_pct": 45.0, "grid_charge_kwh": 0.0},
            {"soc_end_pct": 35.0, "grid_charge_kwh": 1.0},
        ]
        floors = tou_sale_safety_floors(
            rows, programs, flows, 15.0, 15.0, 1.0, 0.0, 180, 15,
            [15.0, 40.0])
        self.assertGreaterEqual(floors[0], 40.0)


if __name__ == "__main__":
    unittest.main()
