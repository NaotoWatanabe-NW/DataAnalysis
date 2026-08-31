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
    DEFAULT_MULTI_ROW,
    DEFAULT_PRUNE,
    DEFAULT_REPAIR_CATEGORY_MAP,
    PRUNE_REPORT_COLUMNS,
    _discover_anchor_columns,
    _multi_row_config,
    _repair_category_map_config,
    _top_value,
    assemble,
    build_vin_ledger,
    is_protected_column,
    join_trend,
    plan_multi_row_aggregation,
    prepare_defect_source,
    prepare_multi_row_source,
    prepare_repair_source,
    prepare_single_row_source,
    prune_low_cardinality_columns,
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
    """複数行/VIN ソースは統計量/代表値/日時に振り分けて集約する
    （M1〜M6・`docs/panel_prune_and_multirow_agg_design.md` §5。pivot 廃止は維持）。
    """

    def _cfg(self) -> Config:
        return Config({}, root=Path(tempfile.gettempdir()))

    def test_three_rows_per_vin_are_folded_into_n_rows_and_stat_suffixed_measure_columns(self):
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
        out = prepare_multi_row_source(df, "上塗ロボット", self._cfg()).set_index("vin")

        # 2026-08-30 ユーザー判断: numeric_aggs は mean のみ（min/std/max は生成しない）。
        self.assertEqual(out.loc["A", "上塗ロボット__n_rows"], 3)
        self.assertEqual(out.loc["A", "上塗ロボット__測定値__mean"], 20.0)
        self.assertNotIn("上塗ロボット__測定値__min", out.columns)
        self.assertNotIn("上塗ロボット__測定値__max", out.columns)
        self.assertNotIn("上塗ロボット__測定値__std", out.columns)
        # 日時列の __min（datetime_aggs 由来）は変更対象外。trend アンカーとして使われるため維持する。
        self.assertEqual(
            out.loc["A", "上塗ロボット__通過日時__min"], pd.Timestamp("2026-07-24 10:00:00")
        )

    def test_no_pivot_columns_named_after_robot_values_are_generated(self):
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
        out = prepare_multi_row_source(df, "上塗ロボット", self._cfg())

        # ロボット自体は exclude_columns（既定）で集約対象外。R1/R2/R3 を軸にした pivot 列も生成されない。
        self.assertNotIn("上塗ロボット__ロボット", out.columns)
        self.assertFalse(any("R1" in c or "R2" in c or "R3" in c for c in out.columns))


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
                # 小さな fixture ではほぼ全列が定数になり、剪定が既定 on だと検証対象の列が消えるため無効化
                # （列生成の仕様と剪定の仕様を別々に検証する。docs/panel_prune_and_multirow_agg_design.md §10.1）。
                "assemble": {"prune_low_cardinality": {"enabled": False}},
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
                # 小さな fixture ではほぼ全列が定数になり、剪定が既定 on だと検証対象の列が消えるため無効化
                # （列生成の仕様と剪定の仕様を別々に検証する。docs/panel_prune_and_multirow_agg_design.md §10.1）。
                "assemble": {"prune_low_cardinality": {"enabled": False}},
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


class TopValueDeterminismTest(unittest.TestCase):
    """`_top_value` のタイブレーク決定性（docs/repair_integrated_category_design.md §6/§9.4）。"""

    def test_tied_value_counts_return_the_ascending_value_regardless_of_input_order(self):
        forward = pd.Series(["b", "a", "c"])   # 件数はすべて1のタイ
        backward = pd.Series(["c", "b", "a"])  # 同じ値集合を逆順に並べたもの

        self.assertEqual(_top_value(forward), "a")
        self.assertEqual(_top_value(backward), "a")
        self.assertEqual(_top_value(forward), _top_value(backward))


class RepairCategoryMapConfigMergeTest(unittest.TestCase):
    """`_repair_category_map_config()` のサブ辞書単位マージ（回帰: key_columns の2段マージ抜け）。

    修正前は `labels` だけが DEFAULT_REPAIR_CATEGORY_MAP と2段マージされ、`key_columns` は
    浅いマージのままだった。そのため `category_map.key_columns` を1エントリだけ書くと残り3つの
    既定キー対応が黙って消え、4キー厳密一致（IC4）が1キー一致に劣化するバグがあった。
    """

    def _cfg(self, category_map_overrides: dict) -> Config:
        data = {"real_ingest": {"repair": {"category_map": category_map_overrides}}}
        return Config(data, root=Path(tempfile.gettempdir()))

    def test_overriding_a_single_key_columns_entry_keeps_the_other_three_default_key_mappings(self):
        cfg = self._cfg({"key_columns": {"小分類": "小分類_v2"}})

        merged = _repair_category_map_config(cfg)

        self.assertEqual(merged["key_columns"]["小分類"], "小分類_v2")  # 上書きが反映される
        self.assertEqual(len(merged["key_columns"]), 4)  # 劣化バグ回帰: 他の3キーが消えない
        for table_col in ("作業工程", "大分類", "中分類"):
            self.assertEqual(
                merged["key_columns"][table_col],
                DEFAULT_REPAIR_CATEGORY_MAP["key_columns"][table_col],
            )

    def test_overriding_a_single_labels_entry_keeps_the_other_two_default_labels(self):
        cfg = self._cfg({"labels": {"unmatched": "未分類_v2"}})

        merged = _repair_category_map_config(cfg)

        self.assertEqual(merged["labels"]["unmatched"], "未分類_v2")
        self.assertEqual(
            merged["labels"]["out_of_scope_process"],
            DEFAULT_REPAIR_CATEGORY_MAP["labels"]["out_of_scope_process"],
        )
        self.assertEqual(
            merged["labels"]["excluded"], DEFAULT_REPAIR_CATEGORY_MAP["labels"]["excluded"]
        )

    def test_setting_only_a_top_level_category_map_key_keeps_key_columns_and_labels_defaults_intact(self):
        cfg = self._cfg({"enabled": False})

        merged = _repair_category_map_config(cfg)

        self.assertEqual(merged["enabled"], False)
        self.assertEqual(merged["key_columns"], DEFAULT_REPAIR_CATEGORY_MAP["key_columns"])
        self.assertEqual(merged["labels"], DEFAULT_REPAIR_CATEGORY_MAP["labels"])


class AssembleIntegratedCategoryEndToEndTest(unittest.TestCase):
    """repair 統合カテゴリ（4キー厳密写像）の assemble 組み込み（docs/repair_integrated_category_design.md §9.3）。

    traceability に A・B・C・D、repair に A(x2)・B(x2)・C(x1) が居る fixture。
    対比表は 3 行（一致2件・グラフ対象外1件）で、repair 側に「対象外工程」1行・「未分類」1行を混ぜる。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self, **repair_overrides) -> Config:
        data = {
            "real_ingest": {
                "raw_dir": str(self.tmp_path / "raw"),
                "lake_dir": str(self.tmp_path / "lake"),
                "manifest_path": str(self.tmp_path / "lake" / "_manifest.json"),
                "panel_path": str(self.tmp_path / "interim" / "vin_panel.parquet"),
                "trend": {"on_no_overlap": "skip"},
                "repair": repair_overrides,
                # 小さな fixture ではほぼ全列が定数になり、剪定が既定 on だと検証対象の列が消えるため無効化
                # （列生成の仕様と剪定の仕様を別々に検証する。docs/panel_prune_and_multirow_agg_design.md §10.1）。
                "assemble": {"prune_low_cardinality": {"enabled": False}},
            },
            "paths": {"reports_dir": str(self.tmp_path / "reports")},
        }
        return Config(data, root=self.tmp_path)

    def _write_category_table(self) -> None:
        # DEFAULT_REPAIR_CATEGORY_MAP の既定相対パス（cfg.root 基準）にそのまま置く。
        table_path = self.tmp_path / "config" / "塗装課内不良対比表_まとめ.csv"
        table_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "作業工程": ["塗装", "塗装", "塗装"],
                "大分類": ["上塗り", "修正", "上塗り"],
                "中分類": ["キズ", "ブツ", "キズ"],
                "小分類": ["治具当たり", "異物混入", "設備不良"],
                "グラフ項目": ["カテゴリA", "カテゴリB", "-"],
            }
        ).to_csv(table_path, index=False, encoding="utf-8-sig")

    def _write_fixture(self) -> None:
        raw_dir = self.tmp_path / "raw"
        trace_sub = raw_dir / "traceability"
        trace_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN#": ["A", "B", "C", "D"],
                "通過日時": [
                    "2026/07/24 10:00:00", "2026/07/24 11:00:00",
                    "2026/07/24 12:00:00", "2026/07/24 13:00:00",
                ],
            }
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        repair_sub = raw_dir / "repair"
        repair_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN": ["A", "A", "B", "B", "C"],
                "修正日": ["2026/07/29"] * 5,
                "修正時間": ["06:00:00", "06:10:00", "07:00:00", "07:10:00", "08:00:00"],
                "PB-ON": [
                    "20260724 010000", "20260724 010000",
                    "20260724 020000", "20260724 020000", "20260724 030000",
                ],
                "入力工程": ["塗装", "塗装", "塗装", "組立", "塗装"],
                "大分類": ["上塗り", "修正", "上塗り", "上塗り", "上塗り"],
                "中分類": ["キズ", "ブツ", "キズ", "キズ", "キズ"],
                "小分類": ["治具当たり", "異物混入", "設備不良", "治具当たり", "不明キー"],
                "修正工数": [0, 0, 0, 0, 0],
            }
        ).to_csv(repair_sub / "defect.csv", index=False, encoding="cp932")

    def test_category_table_expands_into_repair_prefixed_count_columns_named_after_table_values(self):
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        cat_cols = [c for c in panel.columns if c.startswith("repair_修正__統合カテゴリ__")]

        self.assertEqual(
            set(cat_cols),
            {
                f"repair_修正__統合カテゴリ__{v}"
                for v in ["カテゴリA", "カテゴリB", "グラフ対象外", "対象外工程", "未分類"]
            },
        )
        self.assertIn("repair_修正__n_統合カテゴリ", panel.columns)
        self.assertIn("repair_修正__top_統合カテゴリ", panel.columns)

    def test_all_generated_integrated_category_columns_start_with_repair_prefix(self):
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        related_cols = [c for c in panel.columns if "統合カテゴリ" in c]

        self.assertTrue(len(related_cols) > 0)
        self.assertTrue(all(c.startswith("repair_") for c in related_cols))

    def test_per_vin_sum_of_integrated_category_counts_equals_repair_count(self):
        # IC6 の不変条件: 各 VIN で Σ repair_修正__統合カテゴリ__* == repair_修正__count。
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet").set_index("vin")
        cat_cols = [c for c in panel.columns if c.startswith("repair_修正__統合カテゴリ__")]

        row_sums = panel[cat_cols].sum(axis=1)
        for vin in panel.index:
            self.assertEqual(row_sums.loc[vin], panel.loc[vin, "repair_修正__count"], f"vin={vin}")

    def test_vin_without_repair_records_gets_zero_filled_counts_and_nan_n_and_top(self):
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet").set_index("vin")

        self.assertEqual(panel.loc["D", "repair_修正__統合カテゴリ__カテゴリA"], 0)
        self.assertEqual(panel.loc["D", "repair_修正__count"], 0)
        self.assertTrue(pd.isna(panel.loc["D", "repair_修正__n_統合カテゴリ"]))
        self.assertTrue(pd.isna(panel.loc["D", "repair_修正__top_統合カテゴリ"]))

    def test_disabling_category_map_produces_no_integrated_category_columns_but_keeps_other_repair_columns(self):
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg(category_map={"enabled": False})
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")

        self.assertFalse(any("統合カテゴリ" in c for c in panel.columns))
        self.assertTrue(any(c.startswith("repair_修正__大分類__") for c in panel.columns))
        self.assertIn("repair_修正__count", panel.columns)

    def test_missing_category_table_file_logs_warning_and_assemble_completes_with_other_repair_columns_intact(self):
        # 対比表 CSV を書かないまま既定パス（存在しない）で assemble する（§4.3）。
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)

        with self.assertLogs("defect_analysis.assemble", level="WARNING") as cm:
            assemble(cfg)

        self.assertTrue(any("対比表が見つかりません" in msg for msg in cm.output))
        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        self.assertFalse(any("統合カテゴリ" in c for c in panel.columns))
        self.assertTrue(any(c.startswith("repair_修正__大分類__") for c in panel.columns))

    def test_exceeding_max_category_columns_for_integrated_category_raises_value_error(self):
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg(category_columns={"統合カテゴリ": True}, max_category_columns=1)
        convert_all(cfg)

        with self.assertRaises(ValueError):
            assemble(cfg)

    def test_unmatched_report_lists_the_unmatched_and_out_of_scope_key_combinations_with_correct_counts(self):
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        unmatched = pd.read_csv(self.tmp_path / "reports" / "repair_category_unmatched.csv")

        unmatched_rows = unmatched[unmatched["区分"] == "未分類"]
        self.assertEqual(len(unmatched_rows), 1)
        row = unmatched_rows.iloc[0]
        self.assertEqual(row["source"], "修正")
        self.assertEqual(row["入力工程"], "塗装")
        self.assertEqual(row["大分類"], "上塗り")
        self.assertEqual(row["中分類"], "キズ")
        self.assertEqual(row["小分類"], "不明キー")
        self.assertEqual(row["n_rows"], 1)
        self.assertEqual(row["n_vin"], 1)

        out_of_scope_rows = unmatched[unmatched["区分"] == "対象外工程"]
        self.assertEqual(len(out_of_scope_rows), 1)
        row2 = out_of_scope_rows.iloc[0]
        self.assertEqual(row2["入力工程"], "組立")
        self.assertEqual(row2["n_rows"], 1)
        self.assertEqual(row2["n_vin"], 1)

    def test_unmatched_report_has_a_header_with_zero_rows_when_all_keys_match(self):
        raw_dir = self.tmp_path / "raw"
        trace_sub = raw_dir / "traceability"
        trace_sub.mkdir(parents=True)
        pd.DataFrame(
            {"VIN#": ["A"], "通過日時": ["2026/07/24 10:00:00"]}
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        repair_sub = raw_dir / "repair"
        repair_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN": ["A"],
                "修正日": ["2026/07/29"],
                "修正時間": ["06:00:00"],
                "PB-ON": ["20260724 010000"],
                "入力工程": ["塗装"],
                "大分類": ["上塗り"],
                "中分類": ["キズ"],
                "小分類": ["治具当たり"],
                "修正工数": [0],
            }
        ).to_csv(repair_sub / "defect.csv", index=False, encoding="cp932")

        self._write_category_table()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)

        unmatched = pd.read_csv(self.tmp_path / "reports" / "repair_category_unmatched.csv")

        self.assertEqual(len(unmatched), 0)
        self.assertEqual(
            list(unmatched.columns),
            ["source", "区分", "入力工程", "大分類", "中分類", "小分類", "n_rows", "n_vin"],
        )

    def test_partial_key_columns_override_still_performs_strict_four_key_matching_end_to_end(self):
        # 回帰テスト: key_columns を1エントリだけ上書きしても、4キー厳密一致のまま動く必要がある。
        # 対比表に「小分類だけ共通で他3キーが異なる」2行を用意する。1キー一致に劣化していれば
        # どちらの VIN も対比表側の昇順先頭（同じ値）に丸められてしまうため、それを検出できる。
        table_path = self.tmp_path / "config" / "塗装課内不良対比表_まとめ.csv"
        table_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "作業工程": ["塗装", "塗装"],
                "大分類": ["上塗り", "修正"],
                "中分類": ["キズ", "ブツ"],
                "小分類": ["共通小分類", "共通小分類"],
                "グラフ項目": ["カテゴリA", "カテゴリB"],
            }
        ).to_csv(table_path, index=False, encoding="utf-8-sig")

        raw_dir = self.tmp_path / "raw"
        trace_sub = raw_dir / "traceability"
        trace_sub.mkdir(parents=True)
        pd.DataFrame(
            {"VIN#": ["A", "B"], "通過日時": ["2026/07/24 10:00:00", "2026/07/24 11:00:00"]}
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        repair_sub = raw_dir / "repair"
        repair_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN": ["A", "B"],
                "修正日": ["2026/07/29", "2026/07/29"],
                "修正時間": ["06:00:00", "07:00:00"],
                "PB-ON": ["20260724 010000", "20260724 020000"],
                "入力工程": ["塗装", "塗装"],
                "大分類": ["上塗り", "修正"],
                "中分類": ["キズ", "ブツ"],
                "小分類": ["共通小分類", "共通小分類"],
                "修正工数": [0, 0],
            }
        ).to_csv(repair_sub / "defect.csv", index=False, encoding="cp932")

        # key_columns を1エントリだけ上書き（既定と同じ値の再指定。劣化バグがあれば残り3キーが
        # 消え、事実上「作業工程」だけの1キー一致に落ちる）。
        cfg = self._cfg(category_map={"key_columns": {"作業工程": "入力工程"}})
        convert_all(cfg)
        assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet").set_index("vin")

        # 4キー厳密一致なら A(上塗り/キズ)=カテゴリA、B(修正/ブツ)=カテゴリB と区別される。
        # 1キー劣化なら両方とも対比表内の昇順先頭「カテゴリA」に丸められる。
        self.assertEqual(panel.loc["A", "repair_修正__統合カテゴリ__カテゴリA"], 1)
        self.assertEqual(panel.loc["B", "repair_修正__統合カテゴリ__カテゴリB"], 1)
        self.assertEqual(panel.loc["B", "repair_修正__統合カテゴリ__カテゴリA"], 0)

    def test_unmatched_report_header_uses_the_customized_key_column_names_when_all_rows_match(self):
        # 回帰テスト: 未一致0件時のヘッダが DEFAULT_REPAIR_CATEGORY_MAP 固定ではなく、
        # 解決済み config の key_columns（repair 側の実列名）から組まれる。
        table_path = self.tmp_path / "config" / "塗装課内不良対比表_まとめ.csv"
        table_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "作業工程": ["塗装"],
                "大分類": ["上塗り"],
                "中分類": ["キズ"],
                "小分類": ["治具当たり"],
                "グラフ項目": ["カテゴリA"],
            }
        ).to_csv(table_path, index=False, encoding="utf-8-sig")

        raw_dir = self.tmp_path / "raw"
        trace_sub = raw_dir / "traceability"
        trace_sub.mkdir(parents=True)
        pd.DataFrame(
            {"VIN#": ["A"], "通過日時": ["2026/07/24 10:00:00"]}
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        repair_sub = raw_dir / "repair"
        repair_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN": ["A"],
                "修正日": ["2026/07/29"],
                "修正時間": ["06:00:00"],
                "PB-ON": ["20260724 010000"],
                # repair 側のキー列名をすべてカスタマイズする。
                "工程名": ["塗装"],
                "大分類名": ["上塗り"],
                "中分類名": ["キズ"],
                "小分類名": ["治具当たり"],
                "修正工数": [0],
            }
        ).to_csv(repair_sub / "defect.csv", index=False, encoding="cp932")

        cfg = self._cfg(
            category_map={
                "key_columns": {
                    "作業工程": "工程名",
                    "大分類": "大分類名",
                    "中分類": "中分類名",
                    "小分類": "小分類名",
                }
            }
        )
        convert_all(cfg)
        assemble(cfg)

        unmatched = pd.read_csv(self.tmp_path / "reports" / "repair_category_unmatched.csv")

        self.assertEqual(len(unmatched), 0)  # fixture は全一致するため未一致0件
        self.assertEqual(
            list(unmatched.columns),
            ["source", "区分", "工程名", "大分類名", "中分類名", "小分類名", "n_rows", "n_vin"],
        )


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
                # 小さな fixture ではほぼ全列が定数になり、剪定が既定 on だと検証対象の列が消えるため無効化
                # （列生成の仕様と剪定の仕様を別々に検証する。docs/panel_prune_and_multirow_agg_design.md §10.1）。
                "assemble": {"prune_low_cardinality": {"enabled": False}},
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
                # 小さな fixture ではほぼ全列が定数になり、剪定が既定 on だと検証対象の列が消えるため無効化
                # （列生成の仕様と剪定の仕様を別々に検証する。docs/panel_prune_and_multirow_agg_design.md §10.1）。
                "assemble": {"prune_low_cardinality": {"enabled": False}},
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


class MultiRowConfigMergeTest(unittest.TestCase):
    """`_multi_row_config()` の by_source 2段マージ（回帰テスト）。

    `_repair_category_map_config` の key_columns 劣化バグと同じ趣旨: `by_source.{source}` に
    1キーだけ書いても、そのソースの他の既定キー（stat_suffixes / datetime_aggs / exclude_columns
    など）が消えてはならない（docs/panel_prune_and_multirow_agg_design.md §7 末尾）。
    """

    def _cfg(self, multi_row_overrides: dict) -> Config:
        data = {"real_ingest": {"multi_row": multi_row_overrides}}
        return Config(data, root=Path(tempfile.gettempdir()))

    def test_overriding_by_source_numeric_aggs_for_one_source_keeps_other_default_keys(self):
        cfg = self._cfg({"by_source": {"ホイ黒ロボット": {"numeric_aggs": ["mean", "std"]}}})

        resolved = _multi_row_config(cfg, "ホイ黒ロボット")

        self.assertEqual(resolved["numeric_aggs"], ["mean", "std"])  # 上書きが反映される
        self.assertEqual(resolved["stat_suffixes"], DEFAULT_MULTI_ROW["stat_suffixes"])
        self.assertEqual(resolved["datetime_aggs"], DEFAULT_MULTI_ROW["datetime_aggs"])
        self.assertEqual(resolved["exclude_columns"], DEFAULT_MULTI_ROW["exclude_columns"])

    def test_by_source_override_for_one_source_does_not_affect_other_sources(self):
        cfg = self._cfg({"by_source": {"ホイ黒ロボット": {"numeric_aggs": ["mean", "std"]}}})

        resolved_other = _multi_row_config(cfg, "上塗ロボット")
        baseline = _multi_row_config(Config({}, root=Path(tempfile.gettempdir())), "上塗ロボット")

        self.assertEqual(resolved_other, baseline)
        self.assertEqual(resolved_other["numeric_aggs"], DEFAULT_MULTI_ROW["numeric_aggs"])

    def test_setting_only_a_top_level_multi_row_key_keeps_other_top_level_defaults(self):
        cfg = self._cfg({"enabled": False})

        resolved = _multi_row_config(cfg, "上塗ロボット")

        self.assertEqual(resolved["enabled"], False)
        self.assertEqual(resolved["stat_suffixes"], DEFAULT_MULTI_ROW["stat_suffixes"])
        self.assertEqual(resolved["numeric_aggs"], DEFAULT_MULTI_ROW["numeric_aggs"])
        self.assertEqual(resolved["datetime_aggs"], DEFAULT_MULTI_ROW["datetime_aggs"])


class PlanMultiRowAggregationTest(unittest.TestCase):
    """`plan_multi_row_aggregation` は列名と dtype だけで振り分ける（M2）。"""

    def _cfg(self) -> Config:
        return Config({}, root=Path(tempfile.gettempdir()))

    def test_suffix_matching_numeric_column_becomes_stat_and_non_matching_becomes_rep(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A"],
                "塗料使用量": [1.0, 2.0],   # stat_suffixes の "使用量" に一致 -> stat
                "キャリア": ["C1", "C1"],   # 一致しない -> rep
            }
        )
        plan = plan_multi_row_aggregation(df, "上塗ロボット", self._cfg())

        self.assertEqual(plan["stat"], ["塗料使用量"])
        self.assertEqual(plan["rep"], ["キャリア"])
        self.assertEqual(plan["datetime"], [])

    def test_classification_depends_only_on_column_name_and_dtype_not_on_within_vin_variability(self):
        # M2 の核心: 同じ列名・同じ dtype なら、VIN 内で値が一定かどうかで振り分けが変わってはいけない。
        constant_within_vin = pd.DataFrame(
            {
                "vin": ["A", "A", "B", "B"],
                "高電圧_測定値": [5.0, 5.0, 5.0, 5.0],
                "Line": ["L1", "L1", "L1", "L1"],
            }
        )
        variable_within_vin = pd.DataFrame(
            {
                "vin": ["A", "A", "B", "B"],
                "高電圧_測定値": [1.0, 99.0, 2.0, 88.0],
                "Line": ["L1", "L2", "L1", "L2"],
            }
        )

        plan_constant = plan_multi_row_aggregation(constant_within_vin, "上塗ロボット", self._cfg())
        plan_variable = plan_multi_row_aggregation(variable_within_vin, "上塗ロボット", self._cfg())

        self.assertEqual(plan_constant, plan_variable)
        self.assertEqual(plan_constant["stat"], ["高電圧_測定値"])
        self.assertEqual(plan_constant["rep"], ["Line"])

    def test_excluded_columns_are_not_assigned_to_any_aggregation_bucket(self):
        df = pd.DataFrame({"vin": ["A", "A"], "ロボット": ["R1", "R2"], "キャリア": ["C1", "C1"]})

        plan = plan_multi_row_aggregation(df, "上塗ロボット", self._cfg())

        self.assertNotIn("ロボット", plan["stat"] + plan["rep"] + plan["datetime"])
        self.assertEqual(plan["rep"], ["キャリア"])

    def test_non_numeric_column_matching_a_stat_suffix_falls_back_to_representative_with_a_warning(self):
        df = pd.DataFrame({"vin": ["A", "A"], "判定_使用量": ["OK", "NG"]})  # 非数値だが "使用量" に一致

        with self.assertLogs("defect_analysis.assemble", level="WARNING") as cm:
            plan = plan_multi_row_aggregation(df, "上塗ロボット", self._cfg())

        self.assertEqual(plan["rep"], ["判定_使用量"])
        self.assertEqual(plan["stat"], [])
        self.assertTrue(any("stat_suffixes に一致しますが数値ではない" in msg for msg in cm.output))


class PrepareMultiRowSourceRepresentativeValueTest(unittest.TestCase):
    """代表値列の命名規約（M3: サフィックス無し `{source}__{col}`）と VIN 内最小値・順序非依存（M4）。

    docs/panel_prune_and_multirow_agg_design.md §10.2 の指摘: 既存 PrepareMultiRowSourceTest の
    fixture には代表値（rep）バケットの列が1本も無いため、ここで専用に検証する。
    """

    def _cfg(self) -> Config:
        return Config({}, root=Path(tempfile.gettempdir()))

    def _df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "vin": ["A", "A", "A"],
                "測定値": [10.0, 20.0, 30.0],   # stat
                "キャリア": ["C3", "C1", "C2"],  # rep（末尾一致なし）
                "通過日時": pd.to_datetime(
                    ["2026-07-24 10:02:00", "2026-07-24 10:00:00", "2026-07-24 10:01:00"]
                ),
            }
        )

    def test_representative_value_column_uses_source_col_naming_without_any_suffix(self):
        out = prepare_multi_row_source(self._df(), "上塗ロボット", self._cfg())

        self.assertIn("上塗ロボット__キャリア", out.columns)        # 代表値: サフィックス無し
        self.assertIn("上塗ロボット__測定値__mean", out.columns)     # 統計量: __{agg}
        self.assertIn("上塗ロボット__通過日時__min", out.columns)    # 日時: __min
        self.assertNotIn("上塗ロボット__キャリア__min", out.columns)
        self.assertNotIn("上塗ロボット__キャリア__mean", out.columns)

    def test_representative_value_is_the_within_vin_minimum_and_does_not_depend_on_row_order(self):
        df = self._df()
        shuffled = df.iloc[::-1].reset_index(drop=True)

        out = prepare_multi_row_source(df, "上塗ロボット", self._cfg()).set_index("vin")
        out_shuffled = prepare_multi_row_source(shuffled, "上塗ロボット", self._cfg()).set_index("vin")

        self.assertEqual(out.loc["A", "上塗ロボット__キャリア"], "C1")  # "C1" < "C2" < "C3"
        self.assertEqual(
            out.loc["A", "上塗ロボット__キャリア"], out_shuffled.loc["A", "上塗ロボット__キャリア"]
        )


class PrepareMultiRowSourceDisabledTest(unittest.TestCase):
    def test_disabling_multi_row_aggregation_outputs_only_vin_and_n_rows_columns(self):
        df = pd.DataFrame(
            {
                "vin": ["A", "A"],
                "測定値": [1.0, 2.0],
                "キャリア": ["C1", "C2"],
                "通過日時": pd.to_datetime(["2026-07-24 10:00:00", "2026-07-24 10:01:00"]),
            }
        )
        cfg = Config({"real_ingest": {"multi_row": {"enabled": False}}}, root=Path(tempfile.gettempdir()))

        out = prepare_multi_row_source(df, "上塗ロボット", cfg)

        self.assertEqual(sorted(out.columns), ["vin", "上塗ロボット__n_rows"])


class PrepareMultiRowSourceColumnCountGuardTest(unittest.TestCase):
    def test_exceeding_max_columns_per_source_raises_value_error_before_folding_data(self):
        data = {"vin": ["A", "A"]}
        for i in range(3):
            data[f"項目{i}_設定値"] = [1.0, 2.0]  # stat 列。numeric_aggs 既定 [mean] で1本ずつ
        df = pd.DataFrame(data)
        # 集約計画上の列数 = 3(stat) * 1(numeric_aggs) + 0(rep) + 0(datetime) + 1(n_rows) = 4 > 2。
        cfg = Config(
            {"real_ingest": {"assemble": {"max_columns_per_source": 2}}}, root=Path(tempfile.gettempdir())
        )

        with self.assertRaises(ValueError):
            prepare_multi_row_source(df, "上塗ロボット", cfg)


class MultiRowDatetimeAnchorResolutionTest(unittest.TestCase):
    """複数行/VIN ソースの `__{time_column}__min` が trend アンカーとして解決される（M3・§5.4）。"""

    def test_min_suffixed_datetime_column_from_a_multi_row_source_resolves_as_a_trend_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            raw_dir = _make_anchor_raw_dir(tmp_path, source_name="上塗ロボット")
            cfg = Config({"real_ingest": {"raw_dir": str(raw_dir)}}, root=tmp_path)
            base = pd.DataFrame(
                {"vin": ["A"], "上塗ロボット__通過日時__min": [pd.Timestamp("2026-07-24 10:00:00")]}
            )

            anchor_columns = _discover_anchor_columns(base, cfg)

            self.assertEqual(anchor_columns.get("上塗ロボット"), "上塗ロボット__通過日時__min")


class IsProtectedColumnTest(unittest.TestCase):
    """`is_protected_column` の保護規約3種（完全一致・前方一致・部分一致。P3）。"""

    def test_columns_in_the_exact_match_protect_list_are_protected(self):
        for col in ("vin", "has_repair_record"):
            with self.subTest(col=col):
                self.assertTrue(is_protected_column(col, DEFAULT_PRUNE))

    def test_columns_with_a_protected_prefix_are_protected(self):
        for col in ("present__ブース", "defect_上塗ブツ検__has", "repair_修正__count"):
            with self.subTest(col=col):
                self.assertTrue(is_protected_column(col, DEFAULT_PRUNE))

    def test_flag_substring_in_the_middle_of_a_column_name_is_protected_even_though_not_a_suffix(self):
        # 実データ実例: "ブース__閾値判定フラグ01" は「フラグ」の後ろに "01" が続くため
        # endswith 実装では保護漏れする（部分一致=in 実装であることの固定）。
        self.assertTrue(is_protected_column("ブース__閾値判定フラグ01", DEFAULT_PRUNE))

    def test_column_ending_with_flag_is_also_protected(self):
        self.assertTrue(is_protected_column("浮遊ゴミ__PA_ON_閾値判定フラグ", DEFAULT_PRUNE))

    def test_unrelated_column_is_not_protected(self):
        self.assertFalse(is_protected_column("ブース__値", DEFAULT_PRUNE))


class PruneLowCardinalityColumnsTest(unittest.TestCase):
    """`prune_low_cardinality_columns` の判定境界・保護・出力仕様（P1・P2・P4・P7）。"""

    def _cfg(self, prune_overrides: dict | None = None) -> Config:
        data = {"real_ingest": {"assemble": {"prune_low_cardinality": prune_overrides}}} if prune_overrides else {}
        return Config(data, root=Path(tempfile.gettempdir()))

    def _panel(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "vin": ["A", "B", "C"],
                "all_nan_col": [np.nan, np.nan, np.nan],
                "constant_col": [1, 1, 1],
                "constant_plus_nan_col": [1, np.nan, 1],
                "binary_col": [0, 1, 0],
                "present__ソースA": [1, 1, 1],
                "defect_上塗ブツ検__has": [1, 1, 1],
                "repair_修正__count": [0, 0, 0],
                "ブース__閾値判定フラグ01": [2.0, 2.0, 2.0],
            }
        )

    def test_all_nan_and_constant_columns_are_dropped_but_binary_columns_are_kept(self):
        pruned, report = prune_low_cardinality_columns(self._panel(), self._cfg())

        self.assertNotIn("all_nan_col", pruned.columns)
        self.assertNotIn("constant_col", pruned.columns)
        self.assertIn("binary_col", pruned.columns)
        self.assertNotIn("binary_col", report["column"].tolist())

    def test_a_column_with_one_value_and_nan_is_treated_as_constant_and_dropped(self):
        pruned, report = prune_low_cardinality_columns(self._panel(), self._cfg())

        self.assertNotIn("constant_plus_nan_col", pruned.columns)
        row = report.set_index("column").loc["constant_plus_nan_col"]
        self.assertEqual(row["reason"], "constant")
        self.assertEqual(row["action"], "dropped")

    def test_flag_in_the_middle_of_a_column_name_is_protected_and_recorded_as_kept_protected(self):
        pruned, report = prune_low_cardinality_columns(self._panel(), self._cfg())

        self.assertIn("ブース__閾値判定フラグ01", pruned.columns)
        row = report.set_index("column").loc["ブース__閾値判定フラグ01"]
        self.assertEqual(row["action"], "kept_protected")
        self.assertIn("protect_name_substrings", row["rule"])

    def test_present_defect_repair_prefixed_constant_columns_are_not_dropped(self):
        pruned, _report = prune_low_cardinality_columns(self._panel(), self._cfg())

        for col in ("present__ソースA", "defect_上塗ブツ検__has", "repair_修正__count"):
            with self.subTest(col=col):
                self.assertIn(col, pruned.columns)

    def test_vin_and_has_repair_record_are_not_dropped_even_when_fully_constant(self):
        panel = pd.DataFrame({"vin": ["A", "A", "A"], "has_repair_record": [0, 0, 0]})

        pruned, report = prune_low_cardinality_columns(panel, self._cfg())

        self.assertIn("vin", pruned.columns)
        self.assertIn("has_repair_record", pruned.columns)
        actions = report.set_index("column").loc[["vin", "has_repair_record"], "action"]
        self.assertTrue((actions == "kept_protected").all())

    def test_disabled_pruning_drops_nothing_and_returns_a_header_only_report(self):
        cfg = self._cfg({"enabled": False})
        panel = self._panel()

        pruned, report = prune_low_cardinality_columns(panel, cfg)

        self.assertEqual(list(pruned.columns), list(panel.columns))
        self.assertTrue(report.empty)
        self.assertEqual(list(report.columns), PRUNE_REPORT_COLUMNS)

    def test_pruned_panel_keeps_the_original_column_order(self):
        pruned, report = prune_low_cardinality_columns(self._panel(), self._cfg())

        dropped = set(report.loc[report["action"] == "dropped", "column"])
        expected_order = [c for c in self._panel().columns if c not in dropped]
        self.assertEqual(list(pruned.columns), expected_order)

    def test_prune_report_distinguishes_all_nan_from_constant_reason_for_dropped_columns(self):
        _pruned, report = prune_low_cardinality_columns(self._panel(), self._cfg())
        by_col = report.set_index("column")

        self.assertEqual(by_col.loc["all_nan_col", "reason"], "all_nan")
        self.assertEqual(by_col.loc["constant_col", "reason"], "constant")
        self.assertEqual(by_col.loc["all_nan_col", "rule"], "")
        self.assertEqual(by_col.loc["constant_col", "rule"], "")


class AssemblePruneReturnValueTest(unittest.TestCase):
    """`assemble()` の戻り値 n_columns/n_columns_pruned が剪定結果と一致する（P8）。"""

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
                # 剪定は既定 enabled のまま（このテストの検証対象そのもの）。
            },
            "paths": {"reports_dir": str(self.tmp_path / "reports")},
        }
        return Config(data, root=self.tmp_path)

    def test_return_value_matches_the_post_prune_panel_and_the_prune_report(self):
        raw_dir = self.tmp_path / "raw"
        sub = raw_dir / "traceability"
        sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN#": ["A", "B"],
                "通過日時": ["2026/07/24 10:00:00", "2026/07/24 11:00:00"],
                "値": [1.0, 1.0],   # 2 VIN とも同じ値 -> 定数列として剪定対象
            }
        ).to_csv(sub / "ブース.csv", index=False, encoding="utf-8-sig")

        cfg = self._cfg()
        convert_all(cfg)
        result = assemble(cfg)

        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        prune_report = pd.read_csv(self.tmp_path / "reports" / "panel_pruned_columns.csv")
        n_dropped_expected = int((prune_report["action"] == "dropped").sum())

        self.assertEqual(result["n_columns"], panel.shape[1])
        self.assertEqual(result["n_columns_pruned"], n_dropped_expected)
        self.assertGreater(result["n_columns_pruned"], 0)
        self.assertNotIn("ブース__値", panel.columns)  # 定数列は実際に落ちている


class PruneAndMultiRowInteractionEndToEndTest(unittest.TestCase):
    """剪定と複数行/VIN 集約・repair 統合カテゴリの相互作用（X1〜X3）。"""

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
                # 剪定・複数行集約とも既定 enabled のまま（相互作用そのものが検証対象）。
            },
            "paths": {"reports_dir": str(self.tmp_path / "reports")},
        }
        return Config(data, root=self.tmp_path)

    def _write_category_table(self) -> None:
        table_path = self.tmp_path / "config" / "塗装課内不良対比表_まとめ.csv"
        table_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "作業工程": ["塗装", "塗装"],
                "大分類": ["上塗り", "修正"],
                "中分類": ["キズ", "ブツ"],
                "小分類": ["治具当たり", "異物混入"],
                "グラフ項目": ["カテゴリA", "カテゴリB"],
            }
        ).to_csv(table_path, index=False, encoding="utf-8-sig")

    def _write_fixture(self) -> None:
        raw_dir = self.tmp_path / "raw"
        trace_sub = raw_dir / "traceability"
        trace_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN#": ["A", "B", "C"],
                "通過日時": [
                    "2026/07/24 10:00:00", "2026/07/24 11:00:00", "2026/07/24 12:00:00",
                ],
            }
        ).to_csv(trace_sub / "ブース.csv", index=False, encoding="utf-8-sig")

        # 複数行/VIN ソース。各 VIN に2行。測定値・閾値判定フラグはどちらも全域定数になるように作る。
        pd.DataFrame(
            {
                "VIN#": ["A", "A", "B", "B", "C", "C"],
                "通過日時": [
                    "2026/07/24 09:50:00", "2026/07/24 09:51:00",
                    "2026/07/24 10:50:00", "2026/07/24 10:51:00",
                    "2026/07/24 11:50:00", "2026/07/24 11:51:00",
                ],
                "高電圧_測定値": [5.0, 5.0, 5.0, 5.0, 5.0, 5.0],  # 集約後も全域定数 -> 剪定対象（X1）
                "閾値判定フラグ": [1, 1, 1, 1, 1, 1],              # 集約後も全域定数だが保護される（X2）
                "ロボット": ["R1", "R2", "R1", "R2", "R1", "R2"],
            }
        ).to_csv(trace_sub / "上塗ロボット.csv", index=False, encoding="utf-8-sig")

        repair_sub = raw_dir / "repair"
        repair_sub.mkdir(parents=True)
        pd.DataFrame(
            {
                "VIN": ["A", "A", "B", "C"],
                "修正日": ["2026/07/29"] * 4,
                "修正時間": ["06:00:00", "06:10:00", "07:00:00", "08:00:00"],
                "PB-ON": [
                    "20260724 010000", "20260724 010000",
                    "20260724 020000", "20260724 030000",
                ],
                "入力工程": ["塗装", "塗装", "塗装", "塗装"],
                "大分類": ["上塗り", "修正", "上塗り", "上塗り"],
                "中分類": ["キズ", "ブツ", "キズ", "キズ"],
                "小分類": ["治具当たり", "異物混入", "治具当たり", "治具当たり"],
                "修正工数": [0, 0, 0, 0],
            }
        ).to_csv(repair_sub / "defect.csv", index=False, encoding="cp932")

    def _run(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        self._write_category_table()
        self._write_fixture()
        cfg = self._cfg()
        convert_all(cfg)
        assemble(cfg)
        panel = pd.read_parquet(self.tmp_path / "interim" / "vin_panel.parquet")
        prune_report = pd.read_csv(self.tmp_path / "reports" / "panel_pruned_columns.csv")
        return panel, prune_report

    def test_multi_row_source_column_that_becomes_constant_after_aggregation_is_pruned(self):
        # X1: 「集約 -> 結合 -> trend -> 剪定」の順序どおり、復活列にも剪定が効く。
        panel, prune_report = self._run()

        self.assertNotIn("上塗ロボット__高電圧_測定値__mean", panel.columns)
        row = prune_report.set_index("column").loc["上塗ロボット__高電圧_測定値__mean"]
        self.assertEqual(row["action"], "dropped")
        self.assertEqual(row["reason"], "constant")

    def test_multi_row_source_flag_column_stays_constant_but_is_protected(self):
        # X2: 閾値判定フラグは全域定数でも保護されて残る。
        panel, prune_report = self._run()

        self.assertIn("上塗ロボット__閾値判定フラグ", panel.columns)
        row = prune_report.set_index("column").loc["上塗ロボット__閾値判定フラグ"]
        self.assertEqual(row["action"], "kept_protected")
        self.assertIn("protect_name_substrings", row["rule"])

    def test_pruning_preserves_the_integrated_category_count_invariant(self):
        # X3: repair_ 保護により、剪定後も Σ 統合カテゴリ列 == repair_修正__count が成り立つ。
        panel, _prune_report = self._run()
        panel = panel.set_index("vin")

        cat_cols = [c for c in panel.columns if c.startswith("repair_修正__統合カテゴリ__")]
        self.assertGreater(len(cat_cols), 0)
        row_sums = panel[cat_cols].sum(axis=1)
        for vin in panel.index:
            self.assertEqual(row_sums.loc[vin], panel.loc[vin, "repair_修正__count"], f"vin={vin}")


if __name__ == "__main__":
    unittest.main()
