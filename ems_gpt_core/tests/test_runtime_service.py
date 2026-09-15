import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_service import build_runtime


class SilentLog:
    def warning(self, *args):
        pass


class RuntimeServiceTests(unittest.TestCase):
    def test_initial_state_and_serialized_result(self):
        runtime = build_runtime("EMS-GPT Core", "test", SilentLog())
        self.assertEqual(runtime.state["status"], "STARTING")
        self.assertEqual(runtime.state["modules"]["ai_observer"], "SHADOW_READ_ONLY")
        self.assertEqual(runtime.state["module_details"]["planner"]["activity"],
                         "Oczekiwanie na pierwszy plan")
        self.assertEqual(runtime.state["recovery_contract"], "CORE_RECOVERY_0_24_2_R6")
        self.assertEqual(runtime.run_serialized("test", lambda value: value + 1, 4), 5)

    def test_runtime_instances_do_not_share_mutable_state(self):
        first = build_runtime("EMS", "one", SilentLog())
        second = build_runtime("EMS", "two", SilentLog())
        first.state["status"] = "RUNNING"
        self.assertEqual(second.state["status"], "STARTING")


if __name__ == "__main__":
    unittest.main()
