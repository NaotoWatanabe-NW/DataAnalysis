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

from defect_analysis.analysis_data import excluded_columns, resolve_predictors  # noqa: E402
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
