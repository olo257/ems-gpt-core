import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnostics_service import runtime_module_health_check


class RuntimeDiagnosticsTests(unittest.TestCase):
    def test_degraded_planner_with_old_plan_is_not_reported_healthy(self):
        result = runtime_module_health_check({
            "modules": {"planner": "DEGRADED", "ppd": "DEGRADED"},
            "planner_failure_latched": "SOC_SAFETY_BRIDGE_EXCEEDS_CAP:51:105.024",
            "ppd_failure_latched": "WAITING_FOR_VALID_PLAN",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["name"], "planner_runtime_health")

    def test_healthy_runtime_has_no_failure_alert(self):
        result = runtime_module_health_check({
            "modules": {"planner": "RUNNING", "ppd": "RUNNING"},
            "planner_failure_latched": None,
            "ppd_failure_latched": None,
        })
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
