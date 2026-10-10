import unittest

from planner_service import active_soc_target_deadlines


class ActiveSocTargetDeadlineTests(unittest.TestCase):
    def test_relaxed_buy_deadlines_are_not_revalidated_as_hard_minima(self):
        original_buy_window_ends = {36, 39}
        recovered_dispatch = {
            "effective_target_due_indices": set(),
            "effective_target_pcts": [23.8] * 40,
        }

        self.assertEqual(
            active_soc_target_deadlines(recovered_dispatch, original_buy_window_ends),
            set(),
        )

    def test_successful_dispatch_keeps_enforced_buy_deadlines(self):
        original_buy_window_ends = {36, 39}
        recovered_dispatch = {
            "effective_target_due_indices": {39},
        }

        self.assertEqual(
            active_soc_target_deadlines(recovered_dispatch, original_buy_window_ends),
            {39},
        )

    def test_legacy_results_preserve_requested_deadlines(self):
        self.assertEqual(
            active_soc_target_deadlines({}, {36}),
            {36},
        )


if __name__ == "__main__":
    unittest.main()

