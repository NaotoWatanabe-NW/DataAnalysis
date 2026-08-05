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
