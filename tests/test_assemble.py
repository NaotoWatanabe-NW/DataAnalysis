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
    size_bin_labels,
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
    """複数行/VIN ソースは `{source}__n_rows` のみを返す（D5・§8.1.2・§12.5）。

    数値列・日時列・文字列列に対する統計量集約・pivot は 2026-08-28 の設計変更で廃止された。
    """

    def test_three_rows_per_vin_produce_only_vin_and_n_rows_columns_with_value_three(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A", "A"],
                "測定値": [10.0, 20.0, 30.0],
                "通過日時": pd.to_datetime(
                    ["2026-07-24 10:00:00", "2026-07-24 10:01:00", "2026-07-24 10:02:00"]
                ),
                "ロボット": ["R1", "R2", "R3"],
            }
        )
        out = prepare_multi_row_source(df, "上塗ロボット")

        self.assertEqual(sorted(out.columns), ["vin", "上塗ロボット__n_rows"])
        self.assertEqual(out.set_index("vin").loc["A", "上塗ロボット__n_rows"], 3)

    def test_no_aggregate_or_pivot_suffixed_columns_are_generated(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A", "A"],
                "測定値": [10.0, 20.0, 30.0],
                "通過日時": pd.to_datetime(
                    ["2026-07-24 10:00:00", "2026-07-24 10:01:00", "2026-07-24 10:02:00"]
                ),
                "ロボット": ["R1", "R2", "R3"],
            }
        )
        out = prepare_multi_row_source(df, "上塗ロボット")

        forbidden_suffixes = ("__mean", "__min", "__max", "__std", "__nunique", "__first")
        self.assertFalse(any(c.endswith(forbidden_suffixes) for c in out.columns))
        self.assertNotIn("上塗ロボット__通過日時", out.columns)
        self.assertNotIn("上塗ロボット__通過日時__min", out.columns)


class PrepareDefectSourceTest(unittest.TestCase):
    """defect ソースの出力は `{P}__has` のみ（＋ `by_size_bin` 有効時は `__size_bin__*`）（D11・§12.5）。

    count / size 統計 / 種類別・部位別カウント列は 2026-08-08 の設計変更で廃止された。
    """

    def test_default_config_produces_only_has_column_with_value_one_for_every_vin_with_a_row(self):
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

        self.assertEqual(sorted(out.columns), ["defect_上塗ブツ検__has", "vin"])
        actual = out.set_index("vin")
        self.assertEqual(actual.loc["A", "defect_上塗ブツ検__has"], 1)
        self.assertEqual(actual.loc["B", "defect_上塗ブツ検__has"], 1)

    def test_no_count_size_stat_or_kind_part_columns_are_generated(self):
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

        forbidden_substrings = (
            "__count", "__size_mean", "__size_max", "__size_sum", "__first_ts", "__last_ts",
            "__n_kind", "__n_part", "__top_kind", "__kind__", "__part__",
        )
        self.assertFalse(any(sub in c for c in out.columns for sub in forbidden_substrings))
        self.assertTrue(all(c == "vin" or c.startswith("defect_") for c in out.columns))


class SizeBinLabelsTest(unittest.TestCase):
    """`size_bin_labels()` は config だけでラベルを決める純関数（§8.1.3-A・§12.5）。"""

    def test_default_range_generates_label_count_matching_round_range_over_width_plus_two(self):
        labels = size_bin_labels(0.0, 2.0, 0.1)
        self.assertEqual(len(labels), round((2.0 - 0.0) / 0.1) + 2)

    def test_labels_start_with_under_bin_and_end_with_over_bin(self):
        labels = size_bin_labels(0.0, 2.0, 0.1)
        self.assertEqual(labels[0], "0.0未満")
        self.assertEqual(labels[-1], "2.0以上")
        self.assertEqual(labels[1], "0.0-0.1")
        self.assertEqual(labels[-2], "1.9-2.0")

    def test_labels_contain_no_floating_point_rounding_artifacts(self):
        labels = size_bin_labels(0.0, 2.0, 0.1)
        self.assertNotIn("0.7000000000000001", " ".join(labels))
        for label in labels[1:-1]:
            lo, hi = label.split("-")
            self.assertRegex(lo, r"^\d+\.\d$")
            self.assertRegex(hi, r"^\d+\.\d$")

    def test_non_positive_width_raises_value_error(self):
        for width in (0.0, -0.1):
            with self.subTest(width=width):
                with self.assertRaises(ValueError):
                    size_bin_labels(0.0, 2.0, width)

    def test_min_greater_or_equal_to_max_raises_value_error(self):
        with self.assertRaises(ValueError):
            size_bin_labels(2.0, 2.0, 0.1)
        with self.assertRaises(ValueError):
            size_bin_labels(3.0, 2.0, 0.1)


class PrepareDefectSourceSizeBinTest(unittest.TestCase):
    """`prepare_defect_source(by_size_bin=True)` のビン化ロジック（§8.1.3-A・§12.5）。"""

    def _cfg(self, *, max_columns_per_source: int | None = None, **defect_overrides) -> Config:
        defect_cfg = {"by_size_bin": True, **defect_overrides}
        data: dict = {"real_ingest": {"defect": defect_cfg}}
        if max_columns_per_source is not None:
            data["real_ingest"]["assemble"] = {"max_columns_per_source": max_columns_per_source}
        return Config(data, root=Path(tempfile.gettempdir()))

    def _boundary_df(self) -> pd.DataFrame:
        # 設計書 §12.5 が指定する境界値一式 + to_numeric で NaN になる非数値を 1 行混ぜる。
        return pd.DataFrame(
            {
                "vin": ["A"] * 8,
                "不良サイズ": [-0.1, 0.0, 0.09, 0.1, 1.9, 2.0, 1462978.45, "不明"],
            }
        )

    def test_generated_column_count_is_independent_of_the_data_value_range(self):
        cfg = self._cfg()
        small_range_df = pd.DataFrame({"vin": ["A", "A"], "不良サイズ": [0.05, 1.95]})
        huge_range_df = pd.DataFrame({"vin": ["A", "A"], "不良サイズ": [-1000.0, 500000.0]})

        out_small = prepare_defect_source(small_range_df, "上塗ブツ検", cfg)
        out_huge = prepare_defect_source(huge_range_df, "上塗ブツ検", cfg)

        bin_cols_small = sorted(c for c in out_small.columns if "__size_bin__" in c)
        bin_cols_huge = sorted(c for c in out_huge.columns if "__size_bin__" in c)
        self.assertEqual(bin_cols_small, bin_cols_huge)
        self.assertEqual(len(bin_cols_small), round((2.0 - 0.0) / 0.1) + 2)

    def test_boundary_values_are_assigned_to_correct_half_open_bins(self):
        out = prepare_defect_source(self._boundary_df(), "上塗ブツ検", self._cfg()).set_index("vin")
        prefix = "defect_上塗ブツ検__size_bin__"

        self.assertEqual(out.loc["A", f"{prefix}0_0未満"], 1)  # -0.1
        self.assertEqual(out.loc["A", f"{prefix}0_0_0_1"], 2)  # 0.0, 0.09
        self.assertEqual(out.loc["A", f"{prefix}0_1_0_2"], 1)  # 0.1（下のビンに入らない）
        self.assertEqual(out.loc["A", f"{prefix}1_9_2_0"], 1)  # 1.9
        self.assertEqual(out.loc["A", f"{prefix}2_0以上"], 2)  # 2.0, 1462978.45

    def test_bin_counts_for_one_vin_sum_to_its_non_nan_size_row_count(self):
        out = prepare_defect_source(self._boundary_df(), "上塗ブツ検", self._cfg()).set_index("vin")
        bin_cols = [c for c in out.columns if "__size_bin__" in c]

        # 8 行中 1 行（"不明"）は to_numeric で NaN になりどのビンにも入らない。
        self.assertEqual(int(out.loc["A", bin_cols].sum()), 7)

    def test_exceeding_max_columns_per_source_raises_value_error(self):
        cfg = self._cfg(max_columns_per_source=5)  # 既定範囲は 22 列
        df = pd.DataFrame({"vin": ["A"], "不良サイズ": [1.0]})

        with self.assertRaises(ValueError):
            prepare_defect_source(df, "上塗ブツ検", cfg)

    def test_size_bin_width_zero_raises_value_error(self):
        cfg = self._cfg(size_bin_width=0.0)
        df = pd.DataFrame({"vin": ["A"], "不良サイズ": [1.0]})

        with self.assertRaises(ValueError):
            prepare_defect_source(df, "上塗ブツ検", cfg)

    def test_size_bin_min_greater_or_equal_to_max_raises_value_error(self):
        cfg = self._cfg(size_bin_min=2.0, size_bin_max=2.0)
        df = pd.DataFrame({"vin": ["A"], "不良サイズ": [1.0]})

        with self.assertRaises(ValueError):
            prepare_defect_source(df, "上塗ブツ検", cfg)


class PrepareDefectSourceSizeBinAllNonNumericVinTest(unittest.TestCase):
    """defect ソースに登場する（has=1）が不良サイズが全行非数値の VIN は、size_bin も 0 埋めされる。

    `pd.crosstab` を `size_numeric.notna()` で絞った VIN だけに対して作ると、この VIN が
    crosstab の index から丸ごと欠落し `join` 後に NaN になってしまう回帰バグの防止。
    """

    def test_vin_with_all_non_numeric_sizes_gets_zero_filled_bins_not_nan(self):
        df = pd.DataFrame({"vin": ["A", "A"], "不良サイズ": ["不明", "N/A"]})
        cfg = Config({"real_ingest": {"defect": {"by_size_bin": True}}}, root=Path(tempfile.gettempdir()))

        out = prepare_defect_source(df, "上塗ブツ検", cfg).set_index("vin")
        bin_cols = [c for c in out.columns if "__size_bin__" in c]

        self.assertEqual(out.loc["A", "defect_上塗ブツ検__has"], 1)
        self.assertFalse(any(pd.isna(out.loc["A", c]) for c in bin_cols))
        self.assertEqual(int(out.loc["A", bin_cols].sum()), 0)


class SizeBinLabelsNonDivisibleRangeTest(unittest.TestCase):
    """`size_bin_width` が範囲を割り切れない config は、境界とラベルが食い違う前に ValueError で拒否する。"""

    def test_range_not_a_multiple_of_width_raises_value_error(self):
        with self.assertRaises(ValueError):
            size_bin_labels(bin_min=0.0, bin_max=1.0, bin_width=0.3)

    def test_range_that_is_a_multiple_of_width_does_not_raise(self):
        labels = size_bin_labels(bin_min=0.0, bin_max=1.0, bin_width=0.25)
        self.assertEqual(len(labels), round(1.0 / 0.25) + 2)


class DefectSizeBinAbsentVinTest(unittest.TestCase):
    """size_bin 列も `__has` と同様、defect ソースに登場しない VIN では 0 埋めしない（§13-7・§12.5）。"""

    def test_size_bin_columns_remain_nan_for_vin_absent_from_defect_source(self):
        defect_df = pd.DataFrame({"vin": ["A", "A"], "不良サイズ": [0.05, 1.95]})
        cfg = Config({"real_ingest": {"defect": {"by_size_bin": True}}}, root=Path(tempfile.gettempdir()))
        defect_frame = prepare_defect_source(defect_df, "上塗ブツ検", cfg)

        traceability_frame = pd.DataFrame({"vin": ["A", "B"], "ブース__値": [1, 2]})
        ledger = build_vin_ledger({"ブース": traceability_frame, "上塗ブツ検": defect_frame})
        merged = ledger.merge(defect_frame, on="vin", how="left").set_index("vin")

        size_bin_cols = [c for c in merged.columns if "__size_bin__" in c]
        self.assertTrue(pd.isna(merged.loc["B", "defect_上塗ブツ検__has"]))
        self.assertTrue(all(pd.isna(merged.loc["B", c]) for c in size_bin_cols))
        # A は defect に登場するため has=1、該当ビンが無ければ 0（NaN ではない）。
        self.assertEqual(merged.loc["A", "defect_上塗ブツ検__has"], 1)
        self.assertFalse(any(pd.isna(merged.loc["A", c]) for c in size_bin_cols))


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


class AssembleDefectNotInspectedAndHasRepairRecordTest(unittest.TestCase):
    """defect レコード欠如は「未検査」として NaN のまま残し、`has_repair_record` を検証する。

    ユーザー判断（2026-07-31）: ブツ検にレコードが無い VIN は「不良ゼロ」と断定できないため
    0 埋めしない（`docs/real_data_ingest_design.md` §13-7 の未確定事項が確定した）。
    traceability に A・B・C、defect に A のみ、repair に A のみが居る fixture。
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
                "VIN#": ["A", "B", "C"],
                "通過日時": ["2026/07/24 10:00:00", "2026/07/24 11:00:00", "2026/07/24 12:00:00"],
            }
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        defect_sub = raw_dir / "defect"
        defect_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN#": ["A", "A"],
                "入口 通過日時": ["2026/07/24 10:05:00", "2026/07/24 10:06:00"],
                "不良ｻｲｽﾞ": ["3.2", "4.1"],
                "検査部位": ["左前ﾌｪﾝﾀﾞｰ", "右後ﾄﾞｱ"],
                "不良種類": ["凸不良", "平面不良"],
            }
        ).to_csv(defect_sub / "上塗ブツ検.csv", index=False, encoding="utf-8-sig")

        repair_sub = raw_dir / "repair"
        repair_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN": ["A"],
                "修正日": ["2026/07/29"],
                "修正時間": ["06:41:46"],
                "PB-ON": ["20260724 010931"],
                "大分類": ["上塗り"],
                "修正工数": [0],
            }
        ).to_csv(repair_sub / "defect.csv", index=False, encoding="cp932")

    def test_defect_absence_leaves_has_as_nan_not_zero(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet").set_index("vin")

        # A は検査記録あり
        self.assertEqual(panel.loc["A", "defect_上塗ブツ検__has"], 1)

        # B・C は defect データに一切登場しない = 「未検査」であり「不良ゼロ」ではないため NaN
        self.assertTrue(pd.isna(panel.loc["B", "defect_上塗ブツ検__has"]))
        self.assertTrue(pd.isna(panel.loc["C", "defect_上塗ブツ検__has"]))

    def test_defect_kind_and_count_columns_are_no_longer_generated_in_the_panel(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")

        self.assertFalse(any(c.startswith("defect_上塗ブツ検__kind__") for c in panel.columns))
        self.assertNotIn("defect_上塗ブツ検__count", panel.columns)

    def test_has_repair_record_is_one_only_for_vins_with_a_repair_row(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet").set_index("vin")

        self.assertEqual(panel.loc["A", "has_repair_record"], 1)
        self.assertEqual(panel.loc["B", "has_repair_record"], 0)
        self.assertEqual(panel.loc["C", "has_repair_record"], 0)
        self.assertEqual(panel["has_repair_record"].dtype, np.int64)


class AssembleDefectSizeBinEndToEndTest(unittest.TestCase):
    """`by_size_bin: true` の `ingest_quality.csv` 出力（categories / n_size_under / n_size_over。§12.5）。"""

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
                "defect": {"by_size_bin": True},
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
                "VIN#": ["A"],
                "通過日時": ["2026/07/24 10:00:00"],
            }
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        defect_sub = raw_dir / "defect"
        defect_sub.mkdir(parents=True)
        # 既定範囲 [0.0, 2.0) に対し、下限未満 1 件（-1.0）・上限以上 2 件（3.0 / 5.0）を混ぜる。
        pd.DataFrame(
            {
                "VIN#": ["A", "A", "A", "A"],
                "入口 通過日時": [
                    "2026/07/24 10:05:00", "2026/07/24 10:06:00",
                    "2026/07/24 10:07:00", "2026/07/24 10:08:00",
                ],
                "不良ｻｲｽﾞ": ["-1.0", "0.5", "3.0", "5.0"],
                "検査部位": ["左前ﾌｪﾝﾀﾞｰ", "右後ﾄﾞｱ", "左前ﾌｪﾝﾀﾞｰ", "右後ﾄﾞｱ"],
                "不良種類": ["凸不良", "平面不良", "凸不良", "平面不良"],
            }
        ).to_csv(defect_sub / "上塗ブツ検.csv", index=False, encoding="utf-8-sig")

    def test_ingest_quality_reports_size_bin_categories_and_under_over_counts(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        quality = pd.read_csv(self.tmp_path / "reports" / "ingest_quality.csv")
        row = quality[quality["source"] == "上塗ブツ検"].iloc[0]

        self.assertEqual(row["n_size_under"], 1)
        self.assertEqual(row["n_size_over"], 2)
        categories = row["categories"].split(";")
        self.assertEqual(len(categories), round((2.0 - 0.0) / 0.1) + 2)
        self.assertEqual(categories[0], "size_bin:0_0未満")
        self.assertEqual(categories[-1], "size_bin:2_0以上")

    def test_panel_contains_22_size_bin_columns_for_the_default_range_and_width(self):
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        size_bin_cols = [c for c in panel.columns if c.startswith("defect_上塗ブツ検__size_bin__")]

        self.assertEqual(len(size_bin_cols), round((2.0 - 0.0) / 0.1) + 2)


if __name__ == "__main__":
    unittest.main()
