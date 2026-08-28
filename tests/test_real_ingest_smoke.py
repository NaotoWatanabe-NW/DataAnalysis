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

from defect_analysis.assemble import assemble  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
