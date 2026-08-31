"""analysis_data.excluded_columns / resolve_predictors のテスト（leakage_regex・接頭辞規約）。

既存 tests/test_transforms.py の AnalysisDataTest は簡易設定での基本挙動を検証済み。
本ファイルは config/config.yaml と同等の現実的な leakage_prefixes / leakage_regex を用いて、
新規に増えた結果列が明示リストに無くても規約だけで自動除外されることを検証する。

実行:
    .venv/bin/python -m pytest tests/test_predictors.py -q
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
    excluded_columns,
    resolve_predictors,
)
from defect_analysis.config import Config  # noqa: E402


def _cfg() -> Config:
    # config/config.yaml の analysis セクションと同等の現実的な値。
    return Config(
        {
            "analysis": {
                "targets": {"classification": ["has_defect", "has_severe_defect"], "regression": ["defect_count"]},
                "leakage_columns": ["first_defect_date", "last_defect_date"],
                "leakage_prefixes": [
                    "defect", "repair", "severe", "severity", "top_defect",
                    "max_severity", "time_to_repair", "has_defect", "has_severe", "has_repair",
                ],
                "leakage_regex": ["_defect_", "defect_date$"],
                "id_columns": ["vin", "lot_no", "first_in_ts", "last_out_ts"],
            }
        },
        root=Path("/tmp"),
    )


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "vin": ["A", "B"],
            "lot_no": ["L1", "L2"],
            "first_in_ts": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "last_out_ts": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "EQ-01__pressure": [10.0, 11.0],
            "EQ-02__torque": [40.0, 41.0],
            "lead_time_sec": [100.0, 200.0],
            "operator": ["op1", "op2"],
            "production_shift": ["1直", "2直"],
            "is_weekend": [0, 1],
            "process_month": ["2026-01", "2026-01"],
            "has_defect": [0, 1],
            "has_severe_defect": [0, 1],
            "defect_count": [0, 2],
            "defect_foo": [0, 1],  # 新規結果列（明示リストに無いが接頭辞 "defect" で除外されるはず）
            "first_defect_date_note": ["x", "y"],  # 接頭辞では拾えないが leakage_regex "_defect_" にマッチ
            "repair_修正__count": [0, 1],  # repair 由来の結果列（接頭辞 "repair" で除外されるはず）
        }
    )


class ResolvePredictorsLeakageTest(unittest.TestCase):
    def test_output_excludes_targets_and_result_named_columns_entirely(self):
        spec = resolve_predictors(_df(), _cfg())
        for col in [
            "has_defect", "has_severe_defect", "defect_count", "defect_foo",
            "first_defect_date_note", "vin", "lot_no", "first_in_ts", "last_out_ts",
        ]:
            self.assertNotIn(col, spec.all)

    def test_new_defect_prefixed_column_is_auto_excluded_without_explicit_listing(self):
        cfg = _cfg()
        self.assertNotIn("defect_foo", cfg.get("analysis.leakage_columns"))  # 明示リストには無い
        spec = resolve_predictors(_df(), cfg)
        self.assertNotIn("defect_foo", spec.all)

    def test_repair_prefixed_column_is_auto_excluded_without_explicit_listing(self):
        # docs/real_data_repair_design.md §6.4: repair_* はリーク列なので説明変数に1列も残らない。
        cfg = _cfg()
        self.assertNotIn("repair_修正__count", cfg.get("analysis.leakage_columns"))  # 明示リストには無い
        spec = resolve_predictors(_df(), cfg)
        self.assertNotIn("repair_修正__count", spec.all)

    def test_composite_repair_category_column_is_excluded_without_explicit_listing(self):
        # docs/repair_integrated_category_design.md IC11: repair_ 始まりの統合カテゴリ列は
        # leakage_prefixes の "repair" に自動的に乗り、説明変数には1列も残らない。
        cfg = _cfg()
        df = _df().copy()
        df["repair_修正__統合カテゴリ__上塗ブツ"] = [3, 0]

        self.assertNotIn("repair_修正__統合カテゴリ__上塗ブツ", cfg.get("analysis.leakage_columns"))
        spec = resolve_predictors(df, cfg)
        self.assertNotIn("repair_修正__統合カテゴリ__上塗ブツ", spec.all)

    def test_repair_group_columns_generated_by_build_repair_group_columns_are_excluded_from_predictors(self):
        # docs/repair_group_comparison_design.md G3: build_repair_group_columns が実際に生成した
        # 列（列名は直書きしない。呼び出し前後の差分から動的に検出する）が、接頭辞 "repair" により
        # resolve_predictors の説明変数（数値・カテゴリ双方）から必ず除外されることを検証する。
        # これにより REPAIR_GROUP_PREFIX 自体が別の接頭辞に変わる変異でも、生成列名がそれに追従して
        # 検出されるため（本テストは常に実装の出力を追跡する）、リーク安全性の破壊を検出できる。
        cfg = Config(
            {
                "analysis": {
                    **_cfg().data["analysis"],
                    "repair_groups": [
                        {
                            "name": "タレ",
                            "groups": [
                                {"label": "修正なし", "column": "has_repair_record", "eq": 0},
                                {"label": "タレ", "column": "repair_修正__統合カテゴリ__タレ", "min": 1},
                            ],
                        }
                    ],
                }
            },
            root=Path("/tmp"),
        )
        df = _df().copy()
        df["has_repair_record"] = [0, 1]
        df["repair_修正__統合カテゴリ__タレ"] = [0, 1]

        columns_before = set(df.columns)
        df = build_repair_group_columns(df, cfg)
        generated_columns = set(df.columns) - columns_before

        # 生成列が0本だと以降のループが空振りで必ず緑になるため、まず1本以上あることを確認する。
        self.assertGreaterEqual(len(generated_columns), 1)

        spec = resolve_predictors(df, cfg)
        for col in generated_columns:
            with self.subTest(col=col):
                self.assertNotIn(col, spec.all)
                self.assertNotIn(col, spec.numeric)
                self.assertNotIn(col, spec.categorical)

    def test_regex_only_leakage_column_is_excluded_via_leakage_regex(self):
        # "first_defect_date_note" は prefix "defect"/"has_defect" 等のいずれにも startswith しないが、
        # leakage_regex "_defect_" にマッチするため除外されるべき。
        spec = resolve_predictors(_df(), _cfg())
        self.assertFalse(any(c.startswith(("defect", "has_defect")) for c in ["first_defect_date_note"]))
        self.assertNotIn("first_defect_date_note", spec.all)

    def test_valid_process_predictors_are_retained(self):
        spec = resolve_predictors(_df(), _cfg())
        self.assertIn("EQ-01__pressure", spec.numeric)
        self.assertIn("EQ-02__torque", spec.numeric)
        self.assertIn("lead_time_sec", spec.numeric)
        self.assertIn("is_weekend", spec.numeric)
        self.assertIn("operator", spec.categorical)
        self.assertIn("production_shift", spec.categorical)
        self.assertIn("process_month", spec.categorical)

    def test_excluded_columns_adds_regex_matches_only_when_columns_argument_given(self):
        cfg = _cfg()
        without_columns = excluded_columns(cfg)
        with_columns = excluded_columns(cfg, _df().columns)
        self.assertNotIn("first_defect_date_note", without_columns)
        self.assertIn("first_defect_date_note", with_columns)


if __name__ == "__main__":
    unittest.main()
