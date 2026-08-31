"""analysis_data.equipment_measure_groups のテスト。

実行:
    .venv/bin/python -m pytest tests/test_equipment_groups.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.analysis_data import (  # noqa: E402
    build_repair_group_columns,
    equipment_measure_groups,
    traceability_measure_columns,
)
from defect_analysis.config import Config  # noqa: E402


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "vin": ["A", "B"],
            "EQ-02__torque": [1.0, 2.0],
            "EQ-01__pressure": [3.0, 4.0],
            "EQ-01__torque": [5.0, 6.0],
            "EQ-01__pass_sec": [10.0, 20.0],
            "EQ-02__pass_sec": [11.0, 21.0],
        }
    )


class EquipmentMeasureGroupsTest(unittest.TestCase):
    def test_groups_trend_columns_by_equipment_prefix(self):
        groups = equipment_measure_groups(_df())
        self.assertEqual(set(groups.keys()), {"EQ-01", "EQ-02"})
        self.assertEqual(groups["EQ-01"], ["EQ-01__pressure", "EQ-01__torque"])
        self.assertEqual(groups["EQ-02"], ["EQ-02__torque"])

    def test_excludes_pass_sec_columns_by_default(self):
        groups = equipment_measure_groups(_df())
        for cols in groups.values():
            self.assertTrue(all(not c.endswith("__pass_sec") for c in cols))

    def test_includes_pass_sec_columns_when_flag_enabled(self):
        groups = equipment_measure_groups(_df(), include_pass_sec=True)
        self.assertIn("EQ-01__pass_sec", groups["EQ-01"])
        self.assertIn("EQ-02__pass_sec", groups["EQ-02"])

    def test_returns_empty_dict_when_no_trend_columns_present(self):
        df = pd.DataFrame({"vin": ["A"], "operator": ["op1"]})
        groups = equipment_measure_groups(df)
        self.assertEqual(groups, {})

    def test_group_count_tracks_equipment_count_present_in_dataframe(self):
        # ハードコードではなく df のトレンド列由来。設備が1つ減れば group 数も1つ減る
        # （eda の設備ループ図はこの dict の要素数だけ生成されるため、図数の追従を担保する）。
        df_two = _df()
        df_one = df_two.drop(columns=[c for c in df_two.columns if c.startswith("EQ-02__")])
        self.assertEqual(len(equipment_measure_groups(df_two)), 2)
        self.assertEqual(len(equipment_measure_groups(df_one)), 1)


class TraceabilityMeasureColumnsRepairGroupTest(unittest.TestCase):
    """docs/repair_group_comparison_design.md §10-13: 群分け列は設備扱いされない。

    `build_repair_group_columns` が実際に生成した列（列名は直書きせず、呼び出し前後の
    差分から動的に検出する）が `traceability_measure_columns` / `equipment_measure_groups`
    の段階で設備測定値として扱われないことを検証する。
    """

    def _panel_with_generated_group_columns(self) -> tuple[pd.DataFrame, set[str]]:
        df = pd.DataFrame(
            {
                "EQ-01__pressure": [1.0, 2.0],
                "has_repair_record": [0, 1],
                "repair_修正__統合カテゴリ__タレ": [0, 1],
            }
        )
        cfg = Config(
            {
                "analysis": {
                    "repair_groups": [
                        {
                            "name": "タレ",
                            "groups": [
                                {"label": "修正なし", "column": "has_repair_record", "eq": 0},
                                {"label": "タレ", "column": "repair_修正__統合カテゴリ__タレ", "min": 1},
                            ],
                        }
                    ]
                }
            },
            root=Path("/tmp"),
        )
        columns_before = set(df.columns)
        df = build_repair_group_columns(df, cfg)
        generated = set(df.columns) - columns_before
        return df, generated

    def test_excludes_repair_group_columns_including_the_binary_variant(self):
        df, generated = self._panel_with_generated_group_columns()
        # 生成列が0本だと以降のループが空振りで必ず緑になるため、まず1本以上あることを確認する。
        self.assertGreaterEqual(len(generated), 1)

        result = traceability_measure_columns(df.columns)

        self.assertIn("EQ-01__pressure", result)
        for col in generated:
            with self.subTest(col=col):
                self.assertNotIn(col, result)

    def test_repair_group_columns_do_not_appear_as_an_equipment_group(self):
        df, generated = self._panel_with_generated_group_columns()
        self.assertGreaterEqual(len(generated), 1)

        measure_cols = traceability_measure_columns(df.columns)
        groups = equipment_measure_groups(df[measure_cols])

        self.assertEqual(set(groups.keys()), {"EQ-01"})
        for col in generated:
            with self.subTest(col=col):
                self.assertFalse(any(col in cols for cols in groups.values()))


if __name__ == "__main__":
    unittest.main()
