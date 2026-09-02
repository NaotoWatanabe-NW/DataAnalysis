"""analysis_data.build_annotation_meta / AnnotationMeta.footnote のテスト。

実行:
    .venv/bin/python -m pytest tests/test_annotation.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.analysis_data import AnnotationMeta, build_annotation_meta  # noqa: E402
from defect_analysis.config import Config  # noqa: E402


def _df_with_months() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "process_month": ["2026-01", "2026-01", "2026-02", "2026-03"],
            "value": [1, 2, 3, 4],
        }
    )


def _cfg(filters: list[dict] | None = None) -> Config:
    return Config({"analysis": {"filters": filters or []}}, root=Path("/tmp"))


class BuildAnnotationMetaTest(unittest.TestCase):
    def test_n_rows_matches_dataframe_length(self):
        meta = build_annotation_meta(_df_with_months(), _cfg())
        self.assertEqual(meta.n_rows, len(_df_with_months()))

    def test_n_rows_reflects_filtered_subset_length(self):
        filtered = _df_with_months().iloc[:2]
        meta = build_annotation_meta(filtered, _cfg())
        self.assertEqual(meta.n_rows, 2)

    def test_filters_summary_is_nashi_when_no_filters_configured(self):
        meta = build_annotation_meta(_df_with_months(), _cfg([]))
        self.assertEqual(meta.filters_summary, "なし")

    def test_filters_summary_reflects_configured_filters_and_differs_by_config(self):
        df = _df_with_months()
        meta_min = build_annotation_meta(df, _cfg([{"column": "value", "min": 1}]))
        meta_max = build_annotation_meta(df, _cfg([{"column": "value", "max": 10}]))
        # 固定文字列ではなく、設定を反映して出力が変わること
        self.assertNotEqual(meta_min.filters_summary, meta_max.filters_summary)
        self.assertIn("value≥1", meta_min.filters_summary)
        self.assertIn("value≤10", meta_max.filters_summary)


class AnnotationMetaFootnoteTest(unittest.TestCase):
    def _meta(self, *, n_rows: int = 120, filters_summary: str = "plant_code=P01") -> AnnotationMeta:
        return AnnotationMeta(n_rows=n_rows, filters_summary=filters_summary)

    def test_footnote_shows_zenkikan_and_row_count(self):
        text = self._meta(n_rows=50, filters_summary="なし").footnote(data_kind="不良率(カテゴリ別)")
        self.assertIn("全期間", text)
        self.assertIn("50", text)

    def test_footnote_defaults_to_zensetsubi_when_equipment_is_none(self):
        text = self._meta().footnote(data_kind="測定値相関", equipment=None)
        self.assertIn("全設備", text)

    def test_footnote_includes_equipment_id_and_name_when_specified(self):
        text = self._meta().footnote(data_kind="測定値相関", equipment="EQ-01 圧入")
        self.assertIn("EQ-01", text)
        self.assertIn("圧入", text)

    def test_footnote_includes_data_kind_label(self):
        text = self._meta().footnote(data_kind="ROC/PR(保持テスト)")
        self.assertIn("ROC/PR(保持テスト)", text)


if __name__ == "__main__":
    unittest.main()
