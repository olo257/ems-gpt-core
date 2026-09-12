import pathlib
import sys
import unittest
from datetime import datetime


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ha_gateway_service import HomeAssistantAdapters, build_home_assistant_gateway


class SilentLog:
    def warning(self, *args):
        pass


class HomeAssistantGatewayTests(unittest.TestCase):
    def setUp(self):
        self.gateway = build_home_assistant_gateway(HomeAssistantAdapters(
            supervisor_token=None, ha_api="http://supervisor/core/api", log=SilentLog()
        ))

    def test_missing_token_fails_closed(self):
        self.assertIsNone(self.gateway.ha_state("sensor.example"))
        self.assertIsNone(self.gateway.ha_service_response("script", "turn_on", {}))

    def test_number_rejects_unavailable_values(self):
        self.assertEqual(self.gateway.number({"state": "12.5"}), 12.5)
        self.assertIsNone(self.gateway.number({"state": "unavailable"}))
        self.assertIsNone(self.gateway.number(None))

    def test_active_tou_wraps_before_first_boundary(self):
        programs = [
            {"program": 1, "minute": 360, "soc": 30.0},
            {"program": 2, "minute": 1200, "soc": 60.0},
        ]
        self.assertEqual(
            self.gateway.active_tou_program(datetime(2026, 9, 12, 5, 0), programs)["program"],
            2,
        )
        self.assertEqual(
            self.gateway.active_tou_program(datetime(2026, 9, 12, 8, 0), programs)["program"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
