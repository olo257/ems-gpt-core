import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
FUNCTION = next(node for node in TREE.body
                if isinstance(node, ast.FunctionDef) and node.name == "optimize_hp_heating_slots")
NAMESPACE = {}
exec(compile(ast.Module(body=[FUNCTION], type_ignores=[]), "app.py", "exec"), NAMESPACE)
OPTIMIZE = NAMESPACE["optimize_hp_heating_slots"]


def rows(prices):
    return [{
        "forecast_pv_total_kwh": 0.0,
        "forecast_load_kwh": 0.0,
        "price_buy_pln_kwh": price,
        "price_sell_pln_kwh": 0.0,
    } for price in prices]


def runs(selected, length):
    values = [index in selected for index in range(length)]
    result = []
    start = None
    for index, value in enumerate(values + [False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            result.append((start, index))
            start = None
    return result


class HeatPumpOptimizerTests(unittest.TestCase):
    def test_uniform_cost_prefers_one_ten_hour_cycle(self):
        data = rows([1.0] * 64)
        selected = OPTIMIZE(data, [], 40, 8, 4, 12, 2.5, 0.25)
        self.assertEqual(40, len(selected))
        self.assertEqual(1, len(runs(selected, len(data))))

    def test_split_cycles_respect_minimum_run_and_gap(self):
        data = rows([0.1] * 8 + [5.0] * 4 + [0.1] * 8)
        selected = OPTIMIZE(data, [], 16, 8, 4, 12, 2.5, 0.25)
        blocks = runs(selected, len(data))
        self.assertEqual(16, len(selected))
        self.assertEqual(2, len(blocks))
        self.assertTrue(all(end - start >= 8 for start, end in blocks))
        self.assertTrue(all(4 <= blocks[i][0] - blocks[i - 1][1] <= 12 for i in range(1, len(blocks))))

    def test_manual_force_on_counts_toward_daily_minimum(self):
        selected = OPTIMIZE(rows([1.0] * 32), [True] * 8, 40, 8, 4, 12, 2.5, 0.25)
        self.assertEqual(32, len(selected))


if __name__ == "__main__":
    unittest.main()
