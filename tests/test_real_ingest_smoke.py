"""実データを用いた convert -> assemble の統合 smoke テスト（§12.6）。

`data/raw/` が存在する場合のみ実行する。実データ（41MB）を読むのはこの1本のみとし、
他のテストはすべて小さな自作 fixture を用いる。出力先は tmp_path 配下にして
リポジトリの `data/lake` / `data/interim` / `reports` は変更しない。

実行:
    .venv/bin/python -m pytest tests/test_real_ingest_smoke.py -q -m slow
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.assemble import _prune_config, assemble, is_protected_column  # noqa: E402
from defect_analysis.config import PROJECT_ROOT, Config  # noqa: E402
from defect_analysis.raw_convert import convert_all  # noqa: E402

pytestmark = pytest.mark.slow

_REAL_RAW_DIR = PROJECT_ROOT / "data" / "raw"


@unittest.skipUnless(
    _REAL_RAW_DIR.exists() and any(_REAL_RAW_DIR.rglob("*.csv")),
    "data/raw/ が存在しないため実データ smoke テストをスキップします",
)
class RealDataConvertAssembleSmokeTest(unittest.TestCase):
    def test_convert_then_assemble_completes_and_produces_a_usable_panel_with_all_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = Config(
                {
                    "real_ingest": {
                        "raw_dir": str(_REAL_RAW_DIR),
                        "lake_dir": str(tmp_path / "lake"),
                        "manifest_path": str(tmp_path / "lake" / "_manifest.json"),
                        "panel_path": str(tmp_path / "interim" / "vin_panel.parquet"),
                        # smoke は root=tmp_path のため category_map.path の既定相対パスでは
                        # リポジトリの対比表を見つけられない。絶対パスで明示する
                        # （docs/repair_integrated_category_design.md §9.5）。
                        "repair": {
                            "category_map": {
                                "path": str(PROJECT_ROOT / "config" / "塗装課内不良対比表_まとめ.csv"),
                            },
                        },
                    },
                    "paths": {"reports_dir": str(tmp_path / "reports")},
                },
                root=tmp_path,
            )

            convert_result = convert_all(cfg)
            self.assertGreater(convert_result["n_files"], 0)

            assemble_result = assemble(cfg)

            self.assertGreater(assemble_result["n_vin"], 1000)
            self.assertGreater(assemble_result["n_trend_columns"], 0)
            # 2026-08-28 時点の data/raw/ は trend と traceability の期間が重なる1か月分データに
            # 差し替わっており、実際に trend 結合が成立する（旧サンプルは期間非重複で必ず 0.0 だった。
            # D7 / §12.6 参照）。ここでは「結合の仕組み自体が壊れていないこと」だけを見る。
            self.assertGreater(assemble_result["trend_match_rate"], 0.0)

            panel_path = tmp_path / "interim" / "vin_panel.parquet"
            self.assertTrue(panel_path.exists())
            panel = pd.read_parquet(panel_path)
            self.assertGreater(len(panel), 1000)
            self.assertTrue(any(c.startswith("trend__") for c in panel.columns))

            reports_dir = tmp_path / "reports"
            expected_reports = [
                "column_name_mapping.csv",
                "ingest_quality.csv",
                "trend_anchor_map.csv",
                "trend_join_report.csv",
                "vin_panel_dictionary.csv",
            ]
            for name in expected_reports:
                self.assertTrue((reports_dir / name).exists(), f"レポートが生成されていません: {name}")

            # docs/real_data_repair_design.md §6.5: repair の既定値がコード側 DEFAULT_* に無いと
            # ここが落ちる（config/config.yaml を読まないため）。
            self.assertTrue(
                any(c.startswith("repair_") for c in panel.columns),
                "パネルに repair_ 列が1つもありません",
            )
            self.assertNotIn("修正員", panel.columns, "パネルに個人情報列（修正員）が生の氏名のまま残っています")

            quality = pd.read_csv(reports_dir / "ingest_quality.csv")
            self.assertIn("repair", set(quality["kind"]))

            # docs/repair_integrated_category_design.md §9.5: repair 統合カテゴリ（4キー厳密写像）。
            cat_cols = [c for c in panel.columns if c.startswith("repair_修正__統合カテゴリ__")]
            self.assertGreaterEqual(len(cat_cols), 30, "統合カテゴリのカウント列が30列未満です")

            # IC6 の不変条件: 各 VIN で Σ 統合カテゴリ列 == repair_修正__count。
            row_sums = panel[cat_cols].sum(axis=1)
            self.assertTrue((row_sums == panel["repair_修正__count"]).all())

            self.assertIn(
                "repair_修正__統合カテゴリ__未分類", panel.columns,
                "未一致行のラベル化（未分類）列が生成されていません",
            )

            self.assertTrue(
                (reports_dir / "repair_category_unmatched.csv").exists(),
                "reports/repair_category_unmatched.csv が生成されていません",
            )

            # docs/panel_prune_and_multirow_agg_design.md §10.3: 列剪定と複数行/VIN 集約。
            # 数値の直書きはしない（データ差し替えで壊れるため。§10.3 末尾に明記）。
            self.assertTrue(
                (reports_dir / "panel_pruned_columns.csv").exists(),
                "reports/panel_pruned_columns.csv が生成されていません",
            )
            self.assertGreater(assemble_result["n_columns_pruned"], 0, "列剪定が1列も効いていません")
            self.assertLess(
                panel.shape[1],
                assemble_result["n_columns"] + assemble_result["n_columns_pruned"],
                "剪定後の列数が剪定前より少なくなっていません",
            )

            # 剪定後に nunique(dropna=True) <= 1 の列が残っているなら、それはすべて保護規約
            # （is_protected_column）に一致するはず（保護以外の理由で残る低カーディナリティ列は無い）。
            prune_cfg = _prune_config(cfg)
            low_cardinality_cols = [c for c in panel.columns if panel[c].nunique(dropna=True) <= 1]
            unprotected = [c for c in low_cardinality_cols if not is_protected_column(c, prune_cfg)]
            self.assertEqual(
                unprotected, [], f"保護規約に一致しない低カーディナリティ列が残っています: {unprotected}"
            )

            # defect_*__has は定数（nunique<=1）でも repair_/defect_ 接頭辞保護で必ず残る。
            defect_has_cols = [c for c in panel.columns if c.startswith("defect_") and c.endswith("__has")]
            self.assertGreater(len(defect_has_cols), 0, "defect_*__has 列が1つも残っていません")

            # 複数行/VIN ソース（上塗/下塗/ホイ黒ロボット）の集約復活（M1〜M6）。
            multi_row_stat_cols = [
                c for c in panel.columns
                if any(c.startswith(f"{s}__") for s in ("上塗ロボット", "下塗ロボット", "ホイ黒ロボット"))
                and "__mean" in c
            ]
            self.assertGreater(
                len(multi_row_stat_cols), 0, "複数行ソース由来の統計量列（__mean）が1つもありません"
            )
            multi_row_n_rows_cols = [
                c for c in panel.columns
                if c in ("上塗ロボット__n_rows", "下塗ロボット__n_rows", "ホイ黒ロボット__n_rows")
            ]
            self.assertGreater(len(multi_row_n_rows_cols), 0, "複数行ソースの __n_rows 列が維持されていません")


if __name__ == "__main__":
    unittest.main()
