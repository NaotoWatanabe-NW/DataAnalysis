"""scripts/repair_flag_histogram.py のデータ整形ロジックのテスト。

図の描画結果そのものは意味のあるアサーションができないため、群分け・欠損除外という
実際にバグりうるロジック（build_group_series）だけを検証する。

実行:
    .venv/bin/python -m pytest tests/test_repair_flag_histogram.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from repair_flag_histogram import build_group_series  # noqa: E402


class BuildGroupSeriesTest(unittest.TestCase):
    def test_splits_measure_values_by_repair_flag(self):
        panel = pd.DataFrame(
            {
                "has_repair_record": [0, 0, 1, 1, 1],
                "measure": [10.0, 20.0, 30.0, 40.0, 50.0],
            }
        )
        groups = build_group_series(panel, "measure")
        self.assertEqual(sorted(groups[0].tolist()), [10.0, 20.0])
        self.assertEqual(sorted(groups[1].tolist()), [30.0, 40.0, 50.0])

    def test_drops_missing_measure_values_without_affecting_the_other_group(self):
        panel = pd.DataFrame(
            {
                "has_repair_record": [0, 0, 1, 1],
                "measure": [10.0, np.nan, np.nan, 40.0],
            }
        )
        groups = build_group_series(panel, "measure")
        self.assertEqual(groups[0].tolist(), [10.0])
        self.assertEqual(groups[1].tolist(), [40.0])

    def test_returns_empty_series_when_a_flag_value_has_no_rows(self):
        panel = pd.DataFrame({"has_repair_record": [0, 0], "measure": [1.0, 2.0]})
        groups = build_group_series(panel, "measure")
        self.assertEqual(len(groups[1]), 0)


if __name__ == "__main__":
    unittest.main()
