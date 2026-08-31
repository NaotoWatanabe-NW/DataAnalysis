"""analysis_data.apply_filters のテスト（標準ライブラリ unittest、追加依存なし）。

実行:
    .venv/bin/python -m pytest tests/test_filters.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.analysis_data import apply_filters, clause_mask  # noqa: E402
from defect_analysis.config import Config  # noqa: E402


def _cfg(filters: list[dict], on_missing: str = "warn") -> Config:
    return Config(
        {"analysis": {"filters": filters, "filters_on_missing_column": on_missing}},
        root=Path("/tmp"),
    )


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "process_month": ["2026-01", "2026-01", "2026-02", "2026-03"],
            "plant_code": ["P01", "P01", "P01", "P02"],
            "operator": ["op_ito", "op_sato", "op_sato", "op_sato"],
            "is_weekend": [0, 1, 0, 0],
            "lead_time_sec": [50, 100, 200, 4000],
            "ng_rate": [0.1, 0.6, 0.3, 0.9],
        }
    )


class ApplyFiltersOperatorTest(unittest.TestCase):
    def test_eq_keeps_only_matching_rows(self):
        out = apply_filters(_df(), _cfg([{"column": "plant_code", "eq": "P01"}]))
        self.assertEqual(len(out), 3)
        self.assertTrue((out["plant_code"] == "P01").all())

    def test_in_operator_keeps_only_listed_values(self):
        out = apply_filters(_df(), _cfg([{"column": "process_month", "in": ["2026-01", "2026-02"]}]))
        self.assertEqual(len(out), 3)
        self.assertTrue(out["process_month"].isin(["2026-01", "2026-02"]).all())

    def test_not_in_operator_excludes_listed_values(self):
        out = apply_filters(_df(), _cfg([{"column": "operator", "not_in": ["op_ito"]}]))
        self.assertEqual(len(out), 3)
        self.assertFalse((out["operator"] == "op_ito").any())

    def test_min_only_filters_lower_bound_inclusive(self):
        out = apply_filters(_df(), _cfg([{"column": "lead_time_sec", "min": 100}]))
        # 境界値 100 を含めて 100,200,4000 の3行
        self.assertEqual(sorted(out["lead_time_sec"].tolist()), [100, 200, 4000])

    def test_max_only_filters_upper_bound_inclusive(self):
        out = apply_filters(_df(), _cfg([{"column": "lead_time_sec", "max": 200}]))
        # 境界値 200 を含めて 50,100,200 の3行
        self.assertEqual(sorted(out["lead_time_sec"].tolist()), [50, 100, 200])

    def test_min_and_max_combined_filters_range_inclusive(self):
        out = apply_filters(_df(), _cfg([{"column": "lead_time_sec", "min": 100, "max": 200}]))
        self.assertEqual(sorted(out["lead_time_sec"].tolist()), [100, 200])

    def test_query_expression_filters_rows(self):
        out = apply_filters(_df(), _cfg([{"query": "ng_rate < 0.5"}]))
        self.assertEqual(sorted(out["ng_rate"].tolist()), [0.1, 0.3])

    def test_invalid_query_expression_raises_value_error(self):
        with self.assertRaises(ValueError):
            apply_filters(_df(), _cfg([{"query": "ng_rate @@ 0.5"}]))

    def test_not_in_with_non_list_value_raises_value_error(self):
        with self.assertRaises(ValueError):
            apply_filters(_df(), _cfg([{"column": "operator", "not_in": "op_ito"}]))

    def test_in_with_non_list_value_raises_value_error(self):
        with self.assertRaises(ValueError):
            apply_filters(_df(), _cfg([{"column": "operator", "in": "op_ito"}]))

    def test_clause_without_recognized_operator_raises_value_error(self):
        with self.assertRaises(ValueError):
            apply_filters(_df(), _cfg([{"column": "plant_code"}]))


class ApplyFiltersCombinationTest(unittest.TestCase):
    def test_multiple_clauses_are_and_combined_regardless_of_order(self):
        rules_a = [{"column": "plant_code", "eq": "P01"}, {"column": "is_weekend", "eq": 0}]
        rules_b = [{"column": "is_weekend", "eq": 0}, {"column": "plant_code", "eq": "P01"}]
        out_a = apply_filters(_df(), _cfg(rules_a))
        out_b = apply_filters(_df(), _cfg(rules_b))
        self.assertEqual(sorted(out_a.index.tolist()), sorted(out_b.index.tolist()))
        self.assertEqual(sorted(out_a.index.tolist()), [0, 2])  # plant_code=P01 かつ is_weekend=0 は行0,2

    def test_empty_filters_returns_input_unchanged(self):
        df = _df()
        out = apply_filters(df, _cfg([]))
        pd.testing.assert_frame_equal(out, df)

    def test_process_month_in_operator_matches_string_values(self):
        out = apply_filters(_df(), _cfg([{"column": "process_month", "in": ["2026-03"]}]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["process_month"], "2026-03")

    def test_process_month_min_max_matches_lexicographic_calendar_order(self):
        out = apply_filters(_df(), _cfg([{"column": "process_month", "min": "2026-02", "max": "2026-03"}]))
        self.assertEqual(sorted(out["process_month"].tolist()), ["2026-02", "2026-03"])

    def test_unspecified_filters_key_returns_input_unchanged(self):
        # analysis.filters キー自体が config に無い場合も、空リスト指定と同様に無変更
        df = _df()
        cfg = Config({"analysis": {}}, root=Path("/tmp"))
        out = apply_filters(df, cfg)
        pd.testing.assert_frame_equal(out, df)


class ApplyFiltersMissingColumnTest(unittest.TestCase):
    def test_missing_column_with_warn_skips_clause_and_keeps_row_count(self):
        cfg = _cfg([{"column": "plant_line", "eq": "X"}], on_missing="warn")
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING") as cm:
            out = apply_filters(_df(), cfg)
        self.assertEqual(len(out), len(_df()))  # 句がスキップされ行数不変
        self.assertTrue(any("列が存在しないためスキップ" in msg for msg in cm.output))

    def test_missing_column_with_error_raises_value_error(self):
        cfg = _cfg([{"column": "plant_line", "eq": "X"}], on_missing="error")
        with self.assertRaises(ValueError):
            apply_filters(_df(), cfg)


class ApplyFiltersAllExcludedTest(unittest.TestCase):
    def test_all_rows_excluded_raises_value_error_with_summary(self):
        cfg = _cfg([{"column": "plant_code", "eq": "NO_SUCH_PLANT"}])
        with self.assertRaises(ValueError) as cm:
            apply_filters(_df(), cfg)
        self.assertIn("plant_code", str(cm.exception))
        self.assertIn("全行を除外", str(cm.exception))


class ApplyFiltersLoggingTest(unittest.TestCase):
    def test_before_and_after_row_counts_are_logged_at_info_level(self):
        cfg = _cfg([{"column": "plant_code", "eq": "P01"}])
        with self.assertLogs("defect_analysis.analysis_data", level="INFO") as cm:
            apply_filters(_df(), cfg)
        self.assertTrue(any("4 -> 3" in msg for msg in cm.output))


class ClauseMaskTest(unittest.TestCase):
    """`clause_mask`（`_apply_clause` から切り出した公開関数。docs/repair_group_comparison_design.md §10-15）。

    `analysis.repair_groups` からも再利用される DSL 評価そのもの。ここでは `apply_filters` を
    経由せず直接呼び出し、index 整合の bool Series を返すことを固定する。
    """

    def test_returns_boolean_series_aligned_with_a_non_default_index(self):
        df = _df()
        df.index = [10, 20, 30, 40]

        mask = clause_mask(df, {"column": "plant_code", "eq": "P01"})

        self.assertEqual(mask.dtype, bool)
        self.assertEqual(list(mask.index), list(df.index))
        self.assertEqual(mask.tolist(), [True, True, True, False])

    def test_query_clause_returns_a_mask_matching_apply_filters_result(self):
        df = _df()
        mask = clause_mask(df, {"query": "ng_rate < 0.5"})

        self.assertEqual(sorted(df.loc[mask, "ng_rate"].tolist()), [0.1, 0.3])

    def test_missing_column_with_warn_returns_all_true_without_raising(self):
        df = _df()
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            mask = clause_mask(df, {"column": "plant_line", "eq": "X"}, on_missing="warn")

        self.assertTrue(mask.all())

    def test_missing_column_with_error_raises_value_error(self):
        df = _df()
        with self.assertRaises(ValueError):
            clause_mask(df, {"column": "plant_line", "eq": "X"}, on_missing="error")


if __name__ == "__main__":
    unittest.main()
