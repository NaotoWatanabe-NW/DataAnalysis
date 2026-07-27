"""コア変換ロジックのテスト（標準ライブラリ unittest、追加依存なし）。

実行:
    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.config import Config  # noqa: E402
from defect_analysis.features import (  # noqa: E402
    _add_production_time_features,
    _consolidate_rare,
    _shift_of_hour,
    build_features,
)
from defect_analysis.ingest import SourceSpec, _load_source, ingest  # noqa: E402
from defect_analysis.integrate import (  # noqa: E402
    _aggregate_defect,
    _aggregate_traceability,
    _gap_features,
    _prepare_repair,
    _widen_passage_time,
    _widen_trend,
    integrate,
)
from defect_analysis.io_utils import load_df, resolve_format, save_df  # noqa: E402
from defect_analysis.category_integrate import (  # noqa: E402
    integrate_categories,
    load_spec,
    run_category_integration,
)
from defect_analysis.schema_catalog import (  # noqa: E402
    _logical_type,
    _resolve_files,
    build_catalog,
    profile_csv,
)
from defect_analysis.analysis_data import excluded_columns, resolve_predictors  # noqa: E402
from defect_analysis.stats_tests import (  # noqa: E402
    _bh_fdr,
    _cohens_d,
    _cramers_v,
    _test_numeric_vs_binary,
)
from defect_analysis.ml import _build_preprocessor, _models  # noqa: E402


class AggregateTraceabilityTest(unittest.TestCase):
    def test_computes_visit_count_ng_count_and_lead_time(self):
        trace = pd.DataFrame(
            {
                "vin": ["A", "A", "B"],
                "equipment_id": ["EQ-01", "EQ-02", "EQ-01"],
                "in_ts": pd.to_datetime(["2026-01-01 10:00:00", "2026-01-01 10:01:00", "2026-01-01 09:00:00"]),
                "out_ts": pd.to_datetime(["2026-01-01 10:00:30", "2026-01-01 10:02:00", "2026-01-01 09:00:45"]),
                "cycle_time_sec": [30.0, 60.0, 45.0],
                "judgment": ["OK", "NG", "OK"],
                "plant_code": ["P01", "P01", "P01"],
                "line_code": ["L-A", "L-A", "L-B"],
                "operator": ["op1", "op1", "op2"],
                "lot_no": ["LOT1", "LOT1", "LOT2"],
                "process_month": ["2026-01", "2026-01", "2026-01"],
            }
        )
        agg = _aggregate_traceability(trace).set_index("vin")
        self.assertEqual(agg.loc["A", "n_equipment_visited"], 2)
        self.assertEqual(agg.loc["A", "n_ng_judgment"], 1)
        self.assertEqual(agg.loc["A", "total_cycle_time_sec"], 90.0)
        # first_in=10:00:00, last_out=10:02:00 -> 120秒
        self.assertEqual(agg.loc["A", "lead_time_sec"], 120.0)
        self.assertEqual(agg.loc["B", "n_ng_judgment"], 0)


class WidenTrendTest(unittest.TestCase):
    def test_pivots_measures_into_equipment_prefixed_columns(self):
        trend = pd.DataFrame(
            {
                "vin": ["A", "A", "B", "B"],
                "equipment_id": ["EQ-01", "EQ-02", "EQ-01", "EQ-02"],
                "process_month": ["2026-01"] * 4,
                "torque": [1.0, 2.0, 3.0, 4.0],
                "vibration": [0.1, 0.2, 0.3, 0.4],
            }
        )
        wide = _widen_trend(trend).set_index("vin")
        self.assertEqual(wide.loc["A", "EQ-01__torque"], 1.0)
        self.assertEqual(wide.loc["A", "EQ-02__torque"], 2.0)
        self.assertEqual(wide.loc["B", "EQ-02__vibration"], 0.4)
        # 各設備×測定値の列が揃っている
        self.assertIn("EQ-01__vibration", wide.columns)


class AggregateDefectTest(unittest.TestCase):
    def test_counts_severity_and_top_category_with_threshold(self):
        defect = pd.DataFrame(
            {
                "vin": ["A", "A", "A", "B"],
                "defect_id": ["d1", "d2", "d3", "d4"],
                "defect_date": pd.to_datetime(["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]),
                "defect_category": ["外観不良", "外観不良", "機能不良", "寸法不良"],
                "severity": [1, 2, 3, 1],
            }
        )
        agg = _aggregate_defect(defect, severe_level=3).set_index("vin")
        self.assertEqual(agg.loc["A", "defect_count"], 3)
        self.assertEqual(agg.loc["A", "severe_defect_count"], 1)  # severity>=3 は1件
        self.assertEqual(agg.loc["A", "max_severity"], 3)
        self.assertEqual(agg.loc["A", "severity_sum"], 6)
        self.assertEqual(agg.loc["A", "defect_type_count"], 2)
        self.assertEqual(agg.loc["A", "top_defect_category"], "外観不良")  # 最頻
        self.assertEqual(agg.loc["A", "has_defect"], 1)

    def test_per_category_counts_with_fixed_columns(self):
        defect = pd.DataFrame(
            {
                "vin": ["A", "A", "B"],
                "defect_id": ["d1", "d2", "d3"],
                "defect_date": pd.to_datetime(["2026-01-02", "2026-01-03", "2026-01-04"]),
                "defect_category": ["外観不良", "外観不良", "寸法不良"],
                "severity": [1, 2, 1],
            }
        )
        # データに現れない「機能不良」も 0 列として固定生成される
        agg = _aggregate_defect(defect, severe_level=3, categories=["寸法不良", "外観不良", "機能不良"]).set_index("vin")
        self.assertEqual(agg.loc["A", "defect_cnt_外観不良"], 2)
        self.assertEqual(agg.loc["A", "defect_cnt_寸法不良"], 0)
        self.assertEqual(agg.loc["B", "defect_cnt_寸法不良"], 1)
        self.assertIn("defect_cnt_機能不良", agg.columns)  # 未出現でも列は存在

    def test_empty_defect_returns_empty_frame_with_schema(self):
        agg = _aggregate_defect(pd.DataFrame(), severe_level=3, categories=["寸法不良"])
        self.assertTrue(agg.empty)
        self.assertIn("has_defect", agg.columns)
        self.assertIn("defect_cnt_寸法不良", agg.columns)


class PassageAndGapTest(unittest.TestCase):
    def _trace(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "vin": ["A", "A"],
                "equipment_id": ["EQ-01", "EQ-02"],
                "in_ts": pd.to_datetime(["2026-01-01 10:00:00", "2026-01-01 10:01:00"]),
                "out_ts": pd.to_datetime(["2026-01-01 10:00:30", "2026-01-01 10:02:00"]),
            }
        )

    def test_passage_time_per_equipment(self):
        wide = _widen_passage_time(self._trace()).set_index("vin")
        self.assertEqual(wide.loc["A", "EQ-01__pass_sec"], 30.0)  # 10:00:00->10:00:30
        self.assertEqual(wide.loc["A", "EQ-02__pass_sec"], 60.0)  # 10:01:00->10:02:00

    def test_inter_process_gap(self):
        gap = _gap_features(self._trace()).set_index("vin")
        # EQ-01 out 10:00:30 -> EQ-02 in 10:01:00 = 30秒の滞留
        self.assertEqual(gap.loc["A", "max_gap_sec"], 30.0)
        self.assertEqual(gap.loc["A", "mean_gap_sec"], 30.0)


class ProductionTimeTest(unittest.TestCase):
    def test_shift_boundaries(self):
        self.assertEqual(_shift_of_hour(6), "1直")
        self.assertEqual(_shift_of_hour(13), "1直")
        self.assertEqual(_shift_of_hour(14), "2直")
        self.assertEqual(_shift_of_hour(21), "2直")
        self.assertEqual(_shift_of_hour(22), "3直")
        self.assertEqual(_shift_of_hour(3), "3直")

    def test_production_features_from_timestamp(self):
        df = pd.DataFrame({"first_in_ts": pd.to_datetime(["2026-01-01 10:30:00", "2026-01-03 23:00:00"])})
        _add_production_time_features(df)
        self.assertEqual(df.loc[0, "production_hour"], 10)
        self.assertEqual(df.loc[0, "production_shift"], "1直")
        self.assertEqual(df.loc[0, "production_dayofweek"], 3)  # 2026-01-01 は木曜
        self.assertEqual(df.loc[0, "is_weekend"], 0)
        # 2026-01-03 は土曜, 23時 -> 3直, 週末
        self.assertEqual(df.loc[1, "production_shift"], "3直")
        self.assertEqual(df.loc[1, "is_weekend"], 1)


class PrepareRepairTest(unittest.TestCase):
    def test_deduplicates_vin_and_sets_flag(self):
        repair = pd.DataFrame(
            {
                "vin": ["A", "A"],
                "repair_date": pd.to_datetime(["2026-01-06", "2026-01-07"]),
                "repair_action": ["再塗装", "部品交換"],
            }
        )
        prepared = _prepare_repair(repair)
        self.assertEqual(len(prepared), 1)  # VIN 0..1 を保証
        self.assertEqual(prepared.iloc[0]["has_repair"], 1)


class ConsolidateRareTest(unittest.TestCase):
    def test_rare_categories_are_collapsed_to_other(self):
        s = pd.Series(["a"] * 98 + ["b"] * 1 + ["c"] * 1)
        out = _consolidate_rare(s, threshold=0.02)  # 2%未満(=b,c)をOTHERへ
        self.assertEqual(set(out.unique()), {"a", "OTHER"})
        self.assertEqual((out == "OTHER").sum(), 2)

    def test_no_rare_category_returns_unchanged(self):
        s = pd.Series(["a", "b", "a", "b"])
        out = _consolidate_rare(s, threshold=0.1)
        self.assertEqual(list(out), list(s))


class LoadSourceTest(unittest.TestCase):
    def _spec(self) -> SourceSpec:
        return SourceSpec(
            name="traceability",
            subdir="traceability",
            filename_regex=r"^(?P<equipment_id>EQ-\d+)_(?P<month>\d{4}-\d{2})\.csv$",
            required_columns=["vin", "equipment_id", "in_ts"],
            key_columns=["vin", "equipment_id"],
            date_columns=["in_ts"],
        )

    def test_skips_bad_filename_and_missing_columns_and_concatenates_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            sub = raw / "traceability"
            sub.mkdir()
            # 有効ファイル
            pd.DataFrame({"vin": ["A"], "equipment_id": ["EQ-01"], "in_ts": ["2026-01-01 10:00:00"]}).to_csv(
                sub / "EQ-01_2026-01.csv", index=False
            )
            # 命名規則違反（無視される）
            pd.DataFrame({"vin": ["B"], "equipment_id": ["EQ-02"], "in_ts": ["2026-01-01 11:00:00"]}).to_csv(
                sub / "invalid_name.csv", index=False
            )
            # 必須列欠落（無視される）
            pd.DataFrame({"vin": ["C"]}).to_csv(sub / "EQ-03_2026-01.csv", index=False)

            out = _load_source(self._spec(), raw)
            self.assertEqual(len(out), 1)
            self.assertEqual(out.iloc[0]["vin"], "A")
            self.assertTrue(pd.api.types.is_datetime64_any_dtype(out["in_ts"]))


class IoUtilsTest(unittest.TestCase):
    def test_resolve_format_csv_stays_csv(self):
        self.assertEqual(resolve_format("csv"), "csv")

    def test_resolve_format_rejects_unknown(self):
        with self.assertRaises(ValueError):
            resolve_format("orc")

    def test_save_load_csv_roundtrip_with_date_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.csv"
            df = pd.DataFrame({"vin": ["A"], "d": pd.to_datetime(["2026-01-01"])})
            save_df(df, path)
            back = load_df(path, parse_dates=["d"])
            self.assertEqual(back.iloc[0]["vin"], "A")
            self.assertTrue(pd.api.types.is_datetime64_any_dtype(back["d"]))


class ConfigTest(unittest.TestCase):
    def test_dotted_get_and_default(self):
        cfg = Config({"a": {"b": 5}}, root=Path("/tmp"))
        self.assertEqual(cfg.get("a.b"), 5)
        self.assertIsNone(cfg.get("a.x"))
        self.assertEqual(cfg.get("a.x", 9), 9)

    def test_path_resolves_relative_to_root(self):
        cfg = Config({"paths": {"raw_dir": "data/raw"}}, root=Path("/proj"))
        self.assertEqual(cfg.path("paths.raw_dir"), Path("/proj/data/raw"))


class IntegratePipelineTest(unittest.TestCase):
    """interim テーブルから vin_master までの結合・基数・欠損埋めを検証する。"""

    def _make_config(self, tmp: Path) -> Config:
        cfg = Config(
            {
                "storage": {"format": "csv"},
                "features": {
                    "severe_defect_level": 3,
                    "defect_category_coarse_map": {
                        "寸法不良": "寸法系", "組付不良": "組立系", "外観不良": "外観系",
                        "機能不良": "機能系", "その他": "その他",
                    },
                },
                "paths": {"interim_dir": "data/interim", "processed_dir": "data/processed", "reports_dir": "reports"},
            },
            root=tmp,
        )
        return cfg

    def _seed_interim(self, cfg: Config) -> None:
        interim = cfg.path("paths.interim_dir", create=True)
        # VIN A: 不良+修正あり / VIN B: 不良・修正なし
        trace = pd.DataFrame(
            {
                "vin": ["A", "A", "B", "B"],
                "equipment_id": ["EQ-01", "EQ-02", "EQ-01", "EQ-02"],
                "in_ts": pd.to_datetime(
                    ["2026-01-01 10:00:00", "2026-01-01 10:01:00", "2026-01-01 09:00:00", "2026-01-01 09:01:00"]
                ),
                "out_ts": pd.to_datetime(
                    ["2026-01-01 10:00:30", "2026-01-01 10:02:00", "2026-01-01 09:00:30", "2026-01-01 09:02:00"]
                ),
                "cycle_time_sec": [30.0, 60.0, 30.0, 60.0],
                "judgment": ["OK", "NG", "OK", "OK"],
                "plant_code": "P01",
                "line_code": ["L-A", "L-A", "L-B", "L-B"],
                "operator": ["op1", "op1", "op2", "op2"],
                "lot_no": ["LOT1", "LOT1", "LOT2", "LOT2"],
                "process_month": "2026-01",
            }
        )
        trend = pd.DataFrame(
            {
                "vin": ["A", "A", "B", "B"],
                "equipment_id": ["EQ-01", "EQ-02", "EQ-01", "EQ-02"],
                "process_month": "2026-01",
                "torque": [10.0, 20.0, 11.0, 21.0],
                "vibration": [1.0, 2.0, 1.1, 2.1],
            }
        )
        defect = pd.DataFrame(
            {
                "vin": ["A"],
                "defect_id": ["d1"],
                "defect_date": pd.to_datetime(["2026-01-02 08:00:00"]),
                "defect_category": ["機能不良"],
                "severity": [3],
            }
        )
        repair = pd.DataFrame(
            {
                "vin": ["A"],
                "repair_date": pd.to_datetime(["2026-01-04 08:00:00"]),
                "repaired_defect_code": ["D004"],
                "repair_action": ["部品交換"],
                "repair_time_min": [90.0],
            }
        )
        for name, dframe in {"traceability": trace, "trend": trend, "defect": defect, "repair": repair}.items():
            save_df(dframe, interim / f"{name}.csv")

    def test_master_cardinality_and_missing_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_config(Path(tmp))
            self._seed_interim(cfg)
            result = integrate(cfg)

            self.assertEqual(result["n_vin"], 2)
            self.assertEqual(result["n_defect_vin"], 1)
            self.assertEqual(result["n_repair_vin"], 1)

            master = load_df(cfg.path("paths.interim_dir") / "vin_master.csv").set_index("vin")
            # 不良ありVIN
            self.assertEqual(master.loc["A", "has_defect"], 1)
            self.assertEqual(master.loc["A", "has_repair"], 1)
            self.assertEqual(master.loc["A", "severe_defect_count"], 1)
            # 不良なしVINは 0 埋め / カテゴリは「なし」
            self.assertEqual(master.loc["B", "has_defect"], 0)
            self.assertEqual(master.loc["B", "defect_count"], 0)
            self.assertEqual(master.loc["B", "has_repair"], 0)
            self.assertEqual(master.loc["B", "top_defect_category"], "なし")
            # トレンドがワイド展開されている
            self.assertEqual(master.loc["A", "EQ-02__torque"], 20.0)
            # 不良の種類別カウント（未修正Bは 0 埋め）
            self.assertEqual(master.loc["A", "defect_cnt_機能不良"], 1)
            self.assertEqual(master.loc["B", "defect_cnt_機能不良"], 0)
            # 設備別通過時間と工程間滞留
            self.assertEqual(master.loc["A", "EQ-01__pass_sec"], 30.0)
            self.assertEqual(master.loc["A", "EQ-02__pass_sec"], 60.0)
            self.assertEqual(master.loc["A", "max_gap_sec"], 30.0)

    def test_features_derivations(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_config(Path(tmp))
            cfg.data["features"]["rare_category_threshold"] = 0.0  # 集約無効
            cfg.data["features"]["outlier_clip_quantiles"] = None
            cfg.data["features"]["defect_category_coarse_map"] = {"機能不良": "機能系"}
            self._seed_interim(cfg)
            integrate(cfg)
            build_features(cfg)

            feat = load_df(
                cfg.path("paths.processed_dir") / "features.csv",
                parse_dates=["repair_date", "last_defect_date"],
            ).set_index("vin")
            # wait_time = lead(120) - cycle(90) = 30
            self.assertAlmostEqual(feat.loc["A", "wait_time_sec"], 30.0, places=3)
            # ng_rate = NG1/訪問2 = 0.5
            self.assertAlmostEqual(feat.loc["A", "ng_rate"], 0.5, places=3)
            self.assertEqual(feat.loc["A", "has_severe_defect"], 1)
            self.assertEqual(feat.loc["B", "has_severe_defect"], 0)
            # 修正までの日数 = 01-04 - 01-02 = 2日、未修正Bは NaN
            self.assertAlmostEqual(feat.loc["A", "time_to_repair_days"], 2.0, places=3)
            self.assertTrue(pd.isna(feat.loc["B", "time_to_repair_days"]))
            # 大分類マップ適用
            self.assertEqual(feat.loc["A", "top_defect_category_coarse"], "機能系")
            # 生産時間帯（A は first_in 10:00 -> 10時, 1直）
            self.assertEqual(feat.loc["A", "production_hour"], 10)
            self.assertEqual(feat.loc["A", "production_shift"], "1直")


class IngestPipelineTest(unittest.TestCase):
    def test_ingest_writes_all_interim_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(
                {"storage": {"format": "csv"}, "paths": {"raw_dir": "data/raw", "interim_dir": "data/interim"}},
                root=root,
            )
            raw = cfg.path("paths.raw_dir", create=True)
            (raw / "traceability").mkdir(parents=True)
            (raw / "trend").mkdir(parents=True)
            (raw / "defect").mkdir(parents=True)
            (raw / "repair").mkdir(parents=True)
            pd.DataFrame(
                {
                    "vin": ["A"], "equipment_id": ["EQ-01"], "process_month": ["2026-01"],
                    "in_ts": ["2026-01-01 10:00:00"], "out_ts": ["2026-01-01 10:00:30"],
                    "cycle_time_sec": [30.0], "judgment": ["OK"],
                }
            ).to_csv(raw / "traceability" / "EQ-01_2026-01.csv", index=False)
            pd.DataFrame(
                {"vin": ["A"], "equipment_id": ["EQ-01"], "process_month": ["2026-01"], "torque": [10.0]}
            ).to_csv(raw / "trend" / "EQ-01_2026-01.csv", index=False)
            pd.DataFrame(
                {
                    "vin": ["A"], "defect_id": ["d1"], "defect_date": ["2026-01-02 08:00:00"],
                    "defect_category": ["機能不良"], "severity": [3],
                }
            ).to_csv(raw / "defect" / "defect_2026-01.csv", index=False)
            pd.DataFrame(
                {"vin": ["A"], "repair_date": ["2026-01-04 08:00:00"], "repair_action": ["部品交換"]}
            ).to_csv(raw / "repair" / "repair_2026-01.csv", index=False)

            counts = ingest(cfg)
            self.assertEqual(counts["traceability"], 1)
            self.assertEqual(counts["defect"], 1)
            interim = cfg.path("paths.interim_dir")
            for name in ["traceability", "trend", "defect", "repair"]:
                self.assertTrue((interim / f"{name}.csv").exists())


class SchemaCatalogTest(unittest.TestCase):
    def test_logical_type_inference(self):
        self.assertEqual(_logical_type(pd.Series([1, 2, 3]), 0.9), "integer")
        self.assertEqual(_logical_type(pd.Series([1.0, 2.5]), 0.9), "float")
        self.assertEqual(_logical_type(pd.Series(["x", "y"]), 0.9), "string")
        self.assertEqual(
            _logical_type(pd.Series(["2026-01-01 10:00:00", "2026-01-02 11:00:00"]), 0.9), "datetime"
        )

    def test_profile_csv_records_columns_and_native_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sub" / "t.csv"
            path.parent.mkdir()
            pd.DataFrame({"vin": ["A", "B"], "n": [1, 2], "d": ["2026-01-01", "2026-01-02"]}).to_csv(
                path, index=False
            )
            prof = profile_csv(path, root)
            self.assertEqual(prof["file"], "sub/t.csv")
            self.assertEqual(prof["source_name"], "sub")
            self.assertEqual(prof["n_rows"], 2)
            by_name = {c["name"]: c for c in prof["columns"]}
            self.assertEqual(by_name["n"]["logical_type"], "integer")
            self.assertEqual(by_name["d"]["logical_type"], "datetime")
            # 例は素の Python 型（numpy でない）
            self.assertIsInstance(by_name["n"]["example"], int)

    def test_build_catalog_writes_yaml_for_all_csv(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config({"catalog": {"input_glob": "raw/**/*.csv", "output_path": "out/catalog.yaml"}}, root=root)
            (root / "raw").mkdir()
            pd.DataFrame({"vin": ["A"], "n": [1]}).to_csv(root / "raw" / "a.csv", index=False)
            pd.DataFrame({"vin": ["B"], "m": [2.0]}).to_csv(root / "raw" / "b.csv", index=False)

            result = build_catalog(cfg)
            self.assertEqual(result["n_files"], 2)
            out = root / "out" / "catalog.yaml"
            self.assertTrue(out.exists())
            loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
            self.assertEqual(loaded["n_files"], 2)
            self.assertEqual(len(loaded["files"]), 2)

    def test_resolve_files_accepts_dir_glob_and_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "d").mkdir()
            (root / "d" / "x.csv").write_text("a\n1\n")
            self.assertEqual(len(_resolve_files(root, "d")), 1)          # ディレクトリ
            self.assertEqual(len(_resolve_files(root, "d/*.csv")), 1)    # glob
            self.assertEqual(len(_resolve_files(root, "d/x.csv")), 1)    # 単一ファイル


class CategoryIntegrateTest(unittest.TestCase):
    def _spec(self) -> dict:
        return {
            "source_columns": {"major": "大", "middle": "中", "minor": "小"},
            "output_column": "統合",
            "rules": [
                {"when": {"middle": ["締結"]}, "to": "締結不良"},
                {"when": {"major": ["外観"]}, "to": "外観系"},
            ],
            "default": {"method": "concat", "levels": ["major", "middle"], "separator": "＞"},
            "warn_unmatched": False,
        }

    def _df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "大": ["機能", "外観", "寸法"],
                "中": ["締結", "塗装", "切削"],
                "小": ["ボルト緩み", "色差", "面粗さ"],
            }
        )

    def test_rules_priority_and_default_concat(self):
        out = integrate_categories(self._df(), self._spec())
        self.assertEqual(out.iloc[0], "締結不良")     # middle=締結 が最優先で一致
        self.assertEqual(out.iloc[1], "外観系")       # major=外観 で一致
        self.assertEqual(out.iloc[2], "寸法＞切削")   # 未一致 -> default concat

    def test_first_matching_rule_wins(self):
        # major=外観 かつ middle=締結 の行は、先に評価される締結ルールが勝つ
        df = pd.DataFrame({"大": ["外観"], "中": ["締結"], "小": ["x"]})
        out = integrate_categories(df, self._spec())
        self.assertEqual(out.iloc[0], "締結不良")

    def test_missing_source_column_raises(self):
        df = pd.DataFrame({"大": ["機能"]})  # 中/小 が無い
        with self.assertRaises(KeyError):
            integrate_categories(df, self._spec())

    def test_load_spec_requires_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("foo: bar\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_spec(path)

    def test_run_end_to_end_writes_output_with_new_column(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config({}, root=root)
            map_path = root / "map.yaml"
            map_path.write_text(
                yaml.safe_dump({"category_integration": self._spec()}, allow_unicode=True), encoding="utf-8"
            )
            in_path = root / "in.csv"
            self._df().to_csv(in_path, index=False)
            out_path = root / "out.csv"

            result = run_category_integration(cfg, str(in_path), str(out_path), map_path=str(map_path))
            self.assertEqual(result["n_rows"], 3)
            written = pd.read_csv(out_path)
            self.assertIn("統合", written.columns)
            self.assertEqual(written.loc[0, "統合"], "締結不良")


class AnalysisDataTest(unittest.TestCase):
    def _cfg(self) -> Config:
        return Config(
            {
                "analysis": {
                    "targets": {"classification": ["has_defect"], "regression": ["defect_count"]},
                    "leakage_columns": ["has_defect", "defect_count", "severity_sum", "repair_action"],
                    "leakage_prefixes": ["defect_cnt_"],
                    "id_columns": ["vin", "lot_no", "first_in_ts"],
                },
            },
            root=Path("/tmp"),
        )

    def _df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "vin": ["A", "B"],
                "lot_no": ["L1", "L2"],
                "first_in_ts": pd.to_datetime(["2026-01-01", "2026-01-02"]),  # 生時刻→説明変数外
                "EQ-04__vibration": [3.0, 3.5],  # 工程数値 → 採用
                "operator": ["op1", "op2"],       # 工程カテゴリ → 採用
                "severity_sum": [0, 2],           # リーク → 除外
                "defect_cnt_機能不良": [0, 1],     # 接頭辞リーク → 除外
                "has_defect": [0, 1],             # 目的変数 → 除外
                "defect_count": [0, 1],           # 目的変数 → 除外
            }
        )

    def test_excluded_columns_covers_targets_leakage_ids(self):
        excl = excluded_columns(self._cfg())
        for c in ["has_defect", "defect_count", "severity_sum", "repair_action", "vin", "lot_no", "first_in_ts"]:
            self.assertIn(c, excl)

    def test_resolve_predictors_excludes_leakage_and_keeps_process_features(self):
        spec = resolve_predictors(self._df(), self._cfg())
        self.assertIn("EQ-04__vibration", spec.numeric)
        self.assertIn("operator", spec.categorical)
        # リーク・目的変数・識別子・生時刻・接頭辞は説明変数に入らない
        for leaked in ["has_defect", "defect_count", "severity_sum", "defect_cnt_機能不良", "vin", "lot_no", "first_in_ts"]:
            self.assertNotIn(leaked, spec.all)


class StatsHelpersTest(unittest.TestCase):
    def test_bh_fdr_is_monotone_and_bounded(self):
        raw = pd.Series([0.01, 0.02, 0.03, 0.04])
        adj = _bh_fdr(raw)
        self.assertTrue((adj <= 1).all() and (adj >= 0).all())
        self.assertTrue((adj.values >= raw.values - 1e-12).all())  # 補正後 >= 生
        # 昇順 p に対し補正後も非減少
        self.assertTrue(all(adj.values[i] <= adj.values[i + 1] + 1e-12 for i in range(len(adj) - 1)))

    def test_cohens_d_sign_and_zero(self):
        rng = np.random.default_rng(0)
        a = rng.normal(0, 1, 200)
        b = rng.normal(1, 1, 200)
        self.assertGreater(_cohens_d(a, b), 0)          # b の平均が大 → 正
        # 同一分布同士の差は 0 近傍
        self.assertAlmostEqual(_cohens_d(rng.normal(0, 1, 500), rng.normal(0, 1, 500)), 0.0, delta=0.2)

    def test_cramers_v_high_for_perfect_association(self):
        table = np.array([[50, 0], [0, 50]])
        self.assertGreater(_cramers_v(table), 0.9)

    def test_numeric_vs_binary_detects_separation(self):
        rng = np.random.default_rng(0)
        y = pd.Series([0] * 100 + [1] * 100)
        x = pd.Series(np.concatenate([rng.normal(0, 1, 100), rng.normal(3, 1, 100)]))
        rows = _test_numeric_vs_binary(x, y, "target", "feat")
        tests = {r["test"] for r in rows}
        self.assertEqual(tests, {"welch_t", "mann_whitney"})
        self.assertTrue(all(r["p_value"] < 0.001 for r in rows))  # 明確な分離は有意


class MlBuildersTest(unittest.TestCase):
    def test_models_factory_returns_three_including_lightgbm(self):
        clf = _models("classification", seed=0)
        reg = _models("regression", seed=0)
        self.assertEqual(len(clf), 3)
        self.assertIn("lightgbm", clf)
        self.assertIn("lightgbm", reg)

    def test_preprocessor_onehot_expands_categoricals(self):
        spec = FeatureSpecStub(numeric=["x"], categorical=["c"])
        df = pd.DataFrame({"x": [1.0, 2.0, 3.0], "c": ["a", "b", "a"]})
        pre = _build_preprocessor(spec)
        out = pre.fit_transform(df)
        names = pre.get_feature_names_out()
        # 数値1 + カテゴリ(a,b)2 = 3 列
        self.assertEqual(out.shape[1], 3)
        self.assertEqual(len(names), 3)


class FeatureSpecStub:
    """_build_preprocessor は .numeric/.categorical だけ参照するため軽量スタブで足りる。"""

    def __init__(self, numeric, categorical):
        self.numeric = numeric
        self.categorical = categorical


if __name__ == "__main__":
    unittest.main()
