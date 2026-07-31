"""VIN パネル組立のテスト（assemble.py）。

trend 結合は実データでは期間が重複せず検証できないため（設計書 §12 冒頭に明記）、
期間が重複する小さな自作 fixture で手計算検証する。

実行:
    .venv/bin/python -m pytest tests/test_assemble.py -q
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

from defect_analysis.assemble import (  # noqa: E402
    assemble,
    build_vin_ledger,
    join_trend,
    prepare_defect_source,
    prepare_multi_row_source,
    prepare_repair_source,
    prepare_single_row_source,
    resolve_trend_anchor,
)
from defect_analysis.config import Config  # noqa: E402
from defect_analysis.naming import normalize_name  # noqa: E402
from defect_analysis.raw_convert import convert_all  # noqa: E402


class PrepareSingleRowSourceTest(unittest.TestCase):
    def test_columns_are_prefixed_with_source_name_and_ledger_only_columns_are_dropped(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "B"],
                "vin_raw": ["A", "B"],
                "vin_base": ["A", "B"],
                "vin_pass_no": [1, 1],
                "vin_is_dummy": [False, False],
                "__source": ["シーラー炉", "シーラー炉"],
                "date": ["2026-07-24", "2026-07-24"],
                "入口_通過日時": pd.to_datetime(["2026-07-24 10:00:00", "2026-07-24 11:00:00"]),
                "値": [1.0, 2.0],
            }
        )
        out = prepare_single_row_source(df, "シーラー炉")

        self.assertEqual(
            sorted(out.columns), sorted(["vin", "シーラー炉__入口_通過日時", "シーラー炉__値"])
        )
        self.assertEqual(out.loc[out["vin"] == "A", "シーラー炉__値"].iloc[0], 1.0)


class PrepareMultiRowSourceTest(unittest.TestCase):
    def test_aggregates_three_rows_per_vin_into_mean_min_max_std_and_n_rows(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A", "A"],
                "測定値": [10.0, 20.0, 30.0],
            }
        )
        out = prepare_multi_row_source(df, "上塗ロボット", ["mean", "min", "max", "std"]).set_index("vin")

        values = [10.0, 20.0, 30.0]
        self.assertAlmostEqual(out.loc["A", "上塗ロボット__測定値__mean"], np.mean(values))
        self.assertAlmostEqual(out.loc["A", "上塗ロボット__測定値__min"], min(values))
        self.assertAlmostEqual(out.loc["A", "上塗ロボット__測定値__max"], max(values))
        self.assertAlmostEqual(out.loc["A", "上塗ロボット__測定値__std"], np.std(values, ddof=1))
        self.assertEqual(out.loc["A", "上塗ロボット__n_rows"], 3)

    def test_pivot_by_produces_source_pivotvalue_column_named_columns(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A"],
                "ロボット": ["Pi-1L", "Pi-2L"],
                "値": [1.0, 2.0],
            }
        )
        out = prepare_multi_row_source(df, "上塗ロボット", ["mean"], pivot_by=["ロボット"])

        self.assertIn("上塗ロボット__Pi_1L__値", out.columns)
        self.assertIn("上塗ロボット__Pi_2L__値", out.columns)

    def test_pivot_column_count_exceeding_max_columns_per_source_raises_value_error(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A", "A"],
                "ロボット": ["R1", "R2", "R3"],
                "m1": [1.0, 2.0, 3.0],
                "m2": [4.0, 5.0, 6.0],
            }
        )
        with self.assertRaises(ValueError):
            prepare_multi_row_source(
                df, "上塗ロボット", ["mean"], pivot_by=["ロボット"], max_columns_per_source=2
            )


class PrepareDefectSourceTest(unittest.TestCase):
    def test_kind_counts_match_manual_crosstab_and_all_columns_start_with_defect_prefix(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A", "B"],
                "不良種類": ["キズ", "ブツ", "キズ"],
                "検査部位": ["ボンネット", "ドア", "ボンネット"],
                "不良サイズ": [1.0, 2.0, 3.0],
                "入口_通過日時": pd.to_datetime(
                    ["2026-07-24 10:00:00", "2026-07-24 10:05:00", "2026-07-24 11:00:00"]
                ),
                "検査箇所": ["X-Y", "X2-Y2", "X3-Y3"],
            }
        )
        cfg = Config({}, root=Path(tempfile.gettempdir()))

        out = prepare_defect_source(df, "上塗ブツ検", cfg)

        expected = pd.crosstab(df["vin"], df["不良種類"]).reindex(["A", "B"], fill_value=0)
        actual = out.set_index("vin")
        self.assertEqual(actual["defect_上塗ブツ検__kind__キズ"].to_dict(), expected["キズ"].to_dict())
        self.assertEqual(actual["defect_上塗ブツ検__kind__ブツ"].to_dict(), expected["ブツ"].to_dict())
        self.assertNotIn("検査箇所", out.columns)
        self.assertTrue(all(c == "vin" or c.startswith("defect_") for c in out.columns))


class PrepareRepairSourceTest(unittest.TestCase):
    """docs/real_data_repair_design.md §6.3 の tester 観点（RT4）。"""

    def _cfg(self, **repair_overrides) -> Config:
        data = {"real_ingest": {"repair": repair_overrides}} if repair_overrides else {}
        return Config(data, root=Path(tempfile.gettempdir()))

    def _df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "vin": ["A", "A", "B"],
                "修正日時": pd.to_datetime(
                    ["2026-07-29 06:41:46", "2026-07-29 07:00:00", "2026-07-29 08:00:00"]
                ),
                "PB_ON": pd.to_datetime(
                    ["2026-07-24 01:09:31", "2026-07-24 01:09:31", "2026-07-24 02:00:00"]
                ),
                "修正工数": [0, 0, 0],
                "大分類": ["上塗り", "修正", "上塗り"],
                "中分類": ["キズ", "ブツ", "キズ"],
                "部位": ["ボンネット", "ドア", "ボンネット"],
                "修正員_id": ["id1", "id2", "id1"],
            }
        )

    def test_count_equals_row_count_per_vin(self):
        out = prepare_repair_source(self._df(), "修正", self._cfg()).set_index("vin")
        self.assertEqual(out.loc["A", "repair_修正__count"], 2)
        self.assertEqual(out.loc["B", "repair_修正__count"], 1)

    def test_all_generated_columns_start_with_repair_prefix(self):
        out = prepare_repair_source(self._df(), "修正", self._cfg())
        self.assertTrue(all(c == "vin" or c.startswith("repair_") for c in out.columns))

    def test_default_config_expands_only_daibunrui_and_leaves_chubunrui_and_bui_uncounted(self):
        out = prepare_repair_source(self._df(), "修正", self._cfg())
        self.assertTrue(any(c.startswith("repair_修正__大分類__") for c in out.columns))
        self.assertFalse(any(c.startswith("repair_修正__中分類__") for c in out.columns))
        self.assertFalse(any(c.startswith("repair_修正__部位__") for c in out.columns))

    def test_daibunrui_count_expansion_matches_manual_crosstab(self):
        df = self._df()
        out = prepare_repair_source(df, "修正", self._cfg()).set_index("vin")
        expected = pd.crosstab(df["vin"], df["大分類"].map(normalize_name))
        self.assertEqual(out["repair_修正__大分類__上塗り"].to_dict(), expected["上塗り"].to_dict())
        self.assertEqual(out["repair_修正__大分類__修正"].to_dict(), expected["修正"].to_dict())

    def test_exceeding_max_category_columns_raises_value_error(self):
        cfg = self._cfg(category_columns={"大分類": True, "中分類": True}, max_category_columns=2)
        with self.assertRaises(ValueError):
            prepare_repair_source(self._df(), "修正", cfg)

    def test_lead_time_h_equals_hours_between_first_correction_and_earliest_pb_on(self):
        out = prepare_repair_source(self._df(), "修正", self._cfg()).set_index("vin")
        expected_hours = (
            pd.Timestamp("2026-07-29 06:41:46") - pd.Timestamp("2026-07-24 01:09:31")
        ).total_seconds() / 3600.0
        self.assertAlmostEqual(out.loc["A", "repair_修正__lead_time_h"], expected_hours)


class BuildVinLedgerTest(unittest.TestCase):
    def test_ledger_is_union_of_vins_with_correct_present_flags_for_a_only_and_b_only_vins(self):
        frames = {
            "ソースA": pd.DataFrame({"vin": ["V1", "V2"], "ソースA__x": [1, 2]}),
            "ソースB": pd.DataFrame({"vin": ["V2", "V3"], "ソースB__y": [3, 4]}),
        }
        ledger = build_vin_ledger(frames).set_index("vin")

        self.assertEqual(sorted(ledger.index), ["V1", "V2", "V3"])
        self.assertEqual(ledger.loc["V1", "present__ソースA"], 1)
        self.assertEqual(ledger.loc["V1", "present__ソースB"], 0)
        self.assertEqual(ledger.loc["V3", "present__ソースA"], 0)
        self.assertEqual(ledger.loc["V3", "present__ソースB"], 1)
        self.assertEqual(ledger.loc["V2", "present__ソースA"], 1)
        self.assertEqual(ledger.loc["V2", "present__ソースB"], 1)


class ResolveTrendAnchorTest(unittest.TestCase):
    def setUp(self):
        self.anchor_columns = {"ブース": "ブース__通過日時", "シーラー炉": "シーラー炉__入口_通過日時"}

    def test_token_with_trailing_number_resolves_by_prefix_match(self):
        result = resolve_trend_anchor(
            "trend__ブース_1_4_結露防止_運転モード", self.anchor_columns, {}, "ブース"
        )
        self.assertEqual(result, "ブース__通過日時")

    def test_token_matching_source_name_exactly_resolves_to_that_source(self):
        result = resolve_trend_anchor(
            "trend__シーラー炉_全体_バーナー_測定値", self.anchor_columns, {}, "ブース"
        )
        self.assertEqual(result, "シーラー炉__入口_通過日時")

    def test_unresolvable_token_falls_back_to_configured_fallback_source(self):
        result = resolve_trend_anchor(
            "trend__コンベア_速度_測定値", self.anchor_columns, {}, "ブース"
        )
        self.assertEqual(result, "ブース__通過日時")

    def test_anchor_map_overrides_the_rule_based_resolution(self):
        # トークン "ブース" は本来 exact match でソース "ブース" に解決されるが、
        # anchor_map で明示的に "シーラー炉" が指定されればそちらが優先される。
        result = resolve_trend_anchor(
            "trend__ブース_xyz_測定値", self.anchor_columns, {"ブース": "シーラー炉"}, "ブース"
        )
        self.assertEqual(result, "シーラー炉__入口_通過日時")


def _make_anchor_raw_dir(tmp_path: Path, source_name: str = "ブース") -> Path:
    """trend アンカー解決に必要な traceability ソースをヘッダのみで用意する。"""
    raw_dir = tmp_path / "raw"
    sub = raw_dir / "traceability"
    sub.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"VIN#": ["A"], "通過日時": ["2026/07/24 10:00:00"]}).to_csv(
        sub / f"{source_name}.csv", index=False, encoding="utf-8-sig"
    )
    return raw_dir


def _trend_cfg(raw_dir: Path, reports_dir: Path, **trend_overrides) -> Config:
    data = {
        "real_ingest": {"raw_dir": str(raw_dir), "trend": trend_overrides},
        "paths": {"reports_dir": str(reports_dir)},
    }
    return Config(data, root=raw_dir.parent)


class JoinTrendOverlappingFixtureTest(unittest.TestCase):
    """期間が重複する自作 fixture（実データでは検証不能な経路の正当な代替）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.raw_dir = _make_anchor_raw_dir(self.tmp_path)
        self.times = pd.date_range("2026-07-24 09:50:00", periods=20, freq="1min")
        self.values = [5, 8, 3, 12, 7, 15, 9, 20, 4, 11, 6, 18, 2, 14, 10, 17, 1, 13, 16, 19]
        self.trend_wide = pd.DataFrame(
            {"trend__ブース_temp_測定値": self.values}, index=self.times
        )
        self.trend_wide.index.name = "DATETIME"

    def tearDown(self):
        self._tmp.cleanup()

    def test_window_mode_matches_hand_computed_centered_moving_average(self):
        base = pd.DataFrame({"vin": ["V1"], "ブース__通過日時": [self.times[5]]})
        cfg = _trend_cfg(
            self.raw_dir, self.tmp_path / "reports", window_minutes=3, tolerance_minutes=5, mode="window"
        )

        result, _report = join_trend(base, self.trend_wide, cfg)

        expected = (self.values[4] + self.values[5] + self.values[6]) / 3
        self.assertAlmostEqual(result.loc[0, "trend__ブース_temp_測定値"], expected)

    def test_anchor_outside_tolerance_window_results_in_nan(self):
        base = pd.DataFrame(
            {
                "vin": ["V1", "V2"],
                "ブース__通過日時": [self.times[5], self.times[10] + pd.Timedelta(minutes=30)],
            }
        )
        cfg = _trend_cfg(
            self.raw_dir, self.tmp_path / "reports", window_minutes=3, tolerance_minutes=5, mode="window"
        )

        result, _report = join_trend(base, self.trend_wide, cfg)

        self.assertTrue(pd.isna(result.loc[result["vin"] == "V2", "trend__ブース_temp_測定値"].iloc[0]))
        self.assertFalse(pd.isna(result.loc[result["vin"] == "V1", "trend__ブース_temp_測定値"].iloc[0]))

    def test_point_mode_returns_raw_nearest_value_without_window_averaging(self):
        base = pd.DataFrame({"vin": ["V1"], "ブース__通過日時": [self.times[5]]})
        cfg = _trend_cfg(
            self.raw_dir, self.tmp_path / "reports", window_minutes=3, tolerance_minutes=5, mode="point"
        )

        result, _report = join_trend(base, self.trend_wide, cfg)

        self.assertEqual(result.loc[0, "trend__ブース_temp_測定値"], self.values[5])


class JoinTrendNonOverlappingFixtureTest(unittest.TestCase):
    """期間が非重複な fixture（実データの07/24-25 vs 07/29-30 相当の状況を小さく再現）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.raw_dir = _make_anchor_raw_dir(self.tmp_path)
        times = pd.date_range("2026-07-29 00:00:00", periods=5, freq="1min")
        self.trend_wide = pd.DataFrame(
            {"trend__ブース_temp_測定値": [1, 2, 3, 4, 5]}, index=times
        )
        self.trend_wide.index.name = "DATETIME"
        self.base = pd.DataFrame({"vin": ["V1"], "ブース__通過日時": [pd.Timestamp("2026-07-24 10:00:00")]})

    def tearDown(self):
        self._tmp.cleanup()

    def test_warn_empty_generates_all_nan_trend_columns_with_warning(self):
        cfg = _trend_cfg(self.raw_dir, self.tmp_path / "reports", on_no_overlap="warn_empty")

        with self.assertLogs("defect_analysis.assemble", level="WARNING") as cm:
            result, _report = join_trend(self.base, self.trend_wide, cfg)

        self.assertIn("trend__ブース_temp_測定値", result.columns)
        self.assertTrue(result["trend__ブース_temp_測定値"].isna().all())
        self.assertTrue(any("期間が重複しません" in msg for msg in cm.output))

    def test_skip_produces_no_trend_columns(self):
        cfg = _trend_cfg(self.raw_dir, self.tmp_path / "reports", on_no_overlap="skip")

        result, _report = join_trend(self.base, self.trend_wide, cfg)

        self.assertEqual([c for c in result.columns if c.startswith("trend__")], [])

    def test_error_raises_value_error(self):
        cfg = _trend_cfg(self.raw_dir, self.tmp_path / "reports", on_no_overlap="error")

        with self.assertRaises(ValueError):
            join_trend(self.base, self.trend_wide, cfg)


class AssembleEndToEndDummyExclusionTest(unittest.TestCase):
    """ダミー行除外は assemble() 内で行われるため、小さなレイクを通した end-to-end で検証する。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self) -> Config:
        data = {
            "real_ingest": {
                "raw_dir": str(self.tmp_path / "raw"),
                "lake_dir": str(self.tmp_path / "lake"),
                "manifest_path": str(self.tmp_path / "lake" / "_manifest.json"),
                "panel_path": str(self.tmp_path / "interim" / "vin_panel.parquet"),
                "trend": {"on_no_overlap": "skip"},
            },
            "paths": {"reports_dir": str(self.tmp_path / "reports")},
        }
        return Config(data, root=self.tmp_path)

    def test_dummy_vin_rows_are_excluded_from_the_panel_and_recorded_in_quality_report(self):
        raw_dir = self.tmp_path / "raw"
        sub = raw_dir / "traceability"
        sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN#": ["A", "B", "DUMMY-YOSHIKId"],
                "通過日時": ["2026/07/24 10:00:00", "2026/07/24 11:00:00", "2026/07/24 12:00:00"],
                "値": [1.0, 2.0, 3.0],
            }
        ).to_csv(sub / "ブース_202607.csv", index=False, encoding="utf-8-sig")

        cfg = self._cfg()
        convert_all(cfg)
        result = assemble(cfg)

        self.assertEqual(result["n_vin"], 2)
        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        self.assertEqual(sorted(panel["vin"]), ["A", "B"])

        quality = pd.read_csv(self.tmp_path / "reports" / "ingest_quality.csv")
        row = quality[quality["source"] == "ブース"].iloc[0]
        self.assertEqual(row["n_dummy_excluded"], 1)


class AssembleRepairEndToEndTest(unittest.TestCase):
    """repair の台帳非算入 (R5) と 0埋め方針を convert -> assemble の end-to-end で検証する。

    traceability に A・B、repair に B・C が居る fixture（docs/real_data_repair_design.md §6.3）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self) -> Config:
        data = {
            "real_ingest": {
                "raw_dir": str(self.tmp_path / "raw"),
                "lake_dir": str(self.tmp_path / "lake"),
                "manifest_path": str(self.tmp_path / "lake" / "_manifest.json"),
                "panel_path": str(self.tmp_path / "interim" / "vin_panel.parquet"),
                "trend": {"on_no_overlap": "skip"},
            },
            "paths": {"reports_dir": str(self.tmp_path / "reports")},
        }
        return Config(data, root=self.tmp_path)

    def _write_fixture(self) -> None:
        raw_dir = self.tmp_path / "raw"
        trace_sub = raw_dir / "traceability"
        trace_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN#": ["A", "B"],
                "通過日時": ["2026/07/24 10:00:00", "2026/07/24 11:00:00"],
            }
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        repair_sub = raw_dir / "repair"
        repair_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN": ["B", "C"],
                "修正日": ["2026/07/29", "2026/07/29"],
                "修正時間": ["06:41:46", "07:00:00"],
                "PB-ON": ["20260724 010931", "20260724 020000"],
                "大分類": ["上塗り", "修正"],
                "修正工数": [0, 0],
            }
        ).to_csv(repair_sub / "defect.csv", index=False, encoding="cp932")

    def test_repair_vins_absent_from_the_ledger_do_not_increase_panel_row_count(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)

        result = assemble(cfg)

        self.assertEqual(result["n_vin"], 2)
        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        self.assertEqual(sorted(panel["vin"]), ["A", "B"])

    def test_vin_without_repair_records_has_zero_filled_counts_but_nan_timestamps_and_workload(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet").set_index("vin")

        self.assertEqual(panel.loc["A", "repair_修正__count"], 0)
        self.assertEqual(panel.loc["A", "repair_修正__has"], 0)
        self.assertEqual(panel.loc["A", "repair_修正__大分類__上塗り"], 0)
        self.assertTrue(pd.isna(panel.loc["A", "repair_修正__first_ts"]))
        self.assertTrue(pd.isna(panel.loc["A", "repair_修正__工数_sum"]))

        self.assertEqual(panel.loc["B", "repair_修正__count"], 1)
        self.assertEqual(panel.loc["B", "repair_修正__has"], 1)

    def test_lead_time_h_equals_hours_between_correction_datetime_and_pb_on(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet").set_index("vin")
        expected_hours = (
            pd.Timestamp("2026-07-29 06:41:46") - pd.Timestamp("2026-07-24 01:09:31")
        ).total_seconds() / 3600.0
        self.assertAlmostEqual(float(panel.loc["B", "repair_修正__lead_time_h"]), expected_hours, places=2)


if __name__ == "__main__":
    unittest.main()
