"""analysis_data.build_repair_group_columns のテスト（修正なし車との対比用の群分け列）。

docs/repair_group_comparison_design.md §10 のテストケース群（G1〜G12）。
`Config` は tests/test_filters.py と同じ流儀（`Config({...}, root=Path("/tmp"))`）で組む。

実行:
    .venv/bin/python -m pytest tests/test_repair_groups.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.analysis_data import (  # noqa: E402
    REPAIR_GROUP_BINARY_SUFFIX,
    REPAIR_GROUP_PREFIX,
    build_repair_group_columns,
)
from defect_analysis.config import Config  # noqa: E402


def _cfg(repair_groups: list | None) -> Config:
    analysis: dict = {}
    if repair_groups is not None:
        analysis["repair_groups"] = repair_groups
    return Config({"analysis": analysis}, root=Path("/tmp"))


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            # 0,1: 修正なし。2: タレのみ。3: 色ブツのみ。4: タレ+色ブツ両方（重複該当。G6）。
            # 5: 修正はあるがタレ/色ブツどちらでもない（G7 の未割当検証用）。
            "has_repair_record": [0, 0, 1, 1, 1, 1],
            "repair_修正__統合カテゴリ__タレ": [0, 0, 1, 0, 1, 0],
            "repair_修正__統合カテゴリ__色ブツ": [0, 0, 0, 1, 1, 0],
            "repair_修正__top_統合カテゴリ": [np.nan, np.nan, "タレ", "色ブツ", "タレ", "マスキング不良"],
        }
    )


_TARE_SPEC = {
    "name": "タレ",
    "groups": [
        {"label": "修正なし", "column": "has_repair_record", "eq": 0},
        {"label": "タレ", "column": "repair_修正__統合カテゴリ__タレ", "min": 1},
    ],
}


class BuildRepairGroupColumnsTwoGroupFormTest(unittest.TestCase):
    """形式A（`groups` による句のリスト。§3.1〜§3.2）。"""

    def test_assigns_no_repair_and_category_labels_from_count_column(self):
        df = _df()
        out = build_repair_group_columns(df, _cfg([_TARE_SPEC]))

        col = out[f"{REPAIR_GROUP_PREFIX}タレ"]
        self.assertEqual(col.iloc[0], "修正なし")
        self.assertEqual(col.iloc[1], "修正なし")
        self.assertEqual(col.iloc[2], "タレ")

    def test_assigns_nan_to_cars_repaired_only_in_other_categories(self):
        # row3: has_repair_record=1・タレのカウントは0（色ブツのみ）-> 2群のどちらにも入らない。
        df = _df()
        out = build_repair_group_columns(df, _cfg([_TARE_SPEC]))

        self.assertTrue(pd.isna(out[f"{REPAIR_GROUP_PREFIX}タレ"].iloc[3]))
        self.assertTrue(pd.isna(out[f"{REPAIR_GROUP_PREFIX}タレ"].iloc[5]))


class BuildRepairGroupColumnsOverlapTest(unittest.TestCase):
    """複数の群に該当する行の解決（先勝ち。G6）。"""

    def _overlap_spec(self, first_label: str, first_column: str, second_label: str, second_column: str) -> dict:
        return {
            "name": "重複",
            "groups": [
                {"label": first_label, "column": first_column, "min": 1},
                {"label": second_label, "column": second_column, "min": 1},
            ],
        }

    def test_first_matching_group_wins_when_row_matches_multiple_clauses(self):
        # row4 はタレ・色ブツ両方のカウントが1（重複該当）。先に宣言した "タレ" に割り当てる。
        df = _df()
        spec = self._overlap_spec(
            "タレ", "repair_修正__統合カテゴリ__タレ", "色ブツ", "repair_修正__統合カテゴリ__色ブツ"
        )
        out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(out[f"{REPAIR_GROUP_PREFIX}重複"].iloc[4], "タレ")

    def test_declaration_order_determines_the_winner_for_overlapping_rows(self):
        # 宣言順を入れ替えると同じ行の割当が変わることを固定する（先勝ちが値ではなく順序で
        # 決まっていることの回帰テスト）。
        df = _df()
        spec = self._overlap_spec(
            "色ブツ", "repair_修正__統合カテゴリ__色ブツ", "タレ", "repair_修正__統合カテゴリ__タレ"
        )
        out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(out[f"{REPAIR_GROUP_PREFIX}重複"].iloc[4], "色ブツ")

    def test_overlapping_rows_emit_a_warning_with_the_overlap_count(self):
        df = _df()
        spec = self._overlap_spec(
            "タレ", "repair_修正__統合カテゴリ__タレ", "色ブツ", "repair_修正__統合カテゴリ__色ブツ"
        )
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING") as cm:
            build_repair_group_columns(df, _cfg([spec]))

        self.assertTrue(any("複数の群に該当した" in msg for msg in cm.output))


class BuildRepairGroupColumnsNaLabelTest(unittest.TestCase):
    """`na_label`（G7）。"""

    def test_labels_unmatched_rows_when_na_label_is_declared(self):
        df = _df()
        spec = {**_TARE_SPEC, "na_label": "その他修正"}
        out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(out[f"{REPAIR_GROUP_PREFIX}タレ"].iloc[3], "その他修正")
        self.assertEqual(out[f"{REPAIR_GROUP_PREFIX}タレ"].iloc[5], "その他修正")
        # 元々割り当て済みの行は変わらない。
        self.assertEqual(out[f"{REPAIR_GROUP_PREFIX}タレ"].iloc[0], "修正なし")
        self.assertEqual(out[f"{REPAIR_GROUP_PREFIX}タレ"].iloc[2], "タレ")

        # G8 の保証（設計書 §3.2 step 6）: na_label で埋めた第3群は __bin では NaN のまま
        # （対照群の 0/1 を汚染しない）。行ごとの対応を文字列列と突き合わせて固定する。
        bin_col = out[f"{REPAIR_GROUP_PREFIX}タレ{REPAIR_GROUP_BINARY_SUFFIX}"]
        self.assertEqual(bin_col.iloc[0], 0.0)      # 修正なし -> 0.0
        self.assertEqual(bin_col.iloc[2], 1.0)      # タレ -> 1.0
        self.assertTrue(pd.isna(bin_col.iloc[3]))   # その他修正（na_label） -> NaN
        self.assertTrue(pd.isna(bin_col.iloc[5]))   # その他修正（na_label） -> NaN

    def test_without_na_label_unmatched_rows_stay_nan(self):
        df = _df()
        out = build_repair_group_columns(df, _cfg([_TARE_SPEC]))

        self.assertTrue(pd.isna(out[f"{REPAIR_GROUP_PREFIX}タレ"].iloc[3]))


class BuildRepairGroupColumnsNaLabelCollisionTest(unittest.TestCase):
    """`na_label` が群ラベルと衝突するスペックを弾く（対照群汚染バグの回帰。修正1）。

    修正前は `s.fillna(na_label)` が未割当行を群ラベルと同じ na_label に吸収してしまい、
    修正があった車が「修正なし」群へ混入していた（§3.2 step 6 の保証が破られていた）。
    """

    def test_na_label_colliding_with_a_group_label_is_skipped_with_a_warning(self):
        df = _df()
        n_cols_before = len(df.columns)
        spec = {**_TARE_SPEC, "na_label": "修正なし"}  # 群ラベルと同じ文字列
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING") as cm:
            out = build_repair_group_columns(df, _cfg([spec]))

        # 衝突したスペックは列を1本も作らない（未割当行が静かに「修正なし」へ吸収されない）。
        self.assertEqual(len(out.columns), n_cols_before)
        self.assertFalse(any(c.startswith(REPAIR_GROUP_PREFIX) for c in out.columns))
        self.assertTrue(any("衝突しています" in msg for msg in cm.output))

    def test_other_specs_still_run_when_one_spec_has_a_colliding_na_label(self):
        df = _df()
        bad_spec = {**_TARE_SPEC, "name": "衝突", "na_label": "修正なし"}
        good_spec = {**_TARE_SPEC, "name": "正常"}
        out = build_repair_group_columns(df, _cfg([bad_spec, good_spec]))

        self.assertNotIn(f"{REPAIR_GROUP_PREFIX}衝突", out.columns)
        self.assertIn(f"{REPAIR_GROUP_PREFIX}正常", out.columns)


class BuildRepairGroupColumnsDuplicateNameLoggingTest(unittest.TestCase):
    """同一リスト内の name 重複は「name が重複しています」を出す（修正2）。

    パネルに元から同名列がある場合（BuildRepairGroupColumnsInvalidSpecTest の
    test_skips_spec_when_name_collides_with_existing_column）とはログの原因が異なることを固定する。
    """

    def test_duplicate_name_within_the_same_list_skips_the_second_spec_with_a_duplicate_name_warning(self):
        df = _df()
        spec_a = dict(_TARE_SPEC)
        spec_b = dict(_TARE_SPEC)  # 同じ name "タレ" をもう一度宣言

        with self.assertLogs("defect_analysis.analysis_data", level="WARNING") as cm:
            out = build_repair_group_columns(df, _cfg([spec_a, spec_b]))

        self.assertIn(f"{REPAIR_GROUP_PREFIX}タレ", out.columns)  # 1つ目は正常に作られる
        self.assertTrue(any("name が重複しています" in msg for msg in cm.output))
        self.assertFalse(any("既に存在します" in msg for msg in cm.output))


class BuildRepairGroupColumnsWhitespaceNameTest(unittest.TestCase):
    """空白のみの `name` を弾く（修正3）。"""

    def test_whitespace_only_name_is_skipped_without_creating_a_column(self):
        df = _df()
        n_cols_before = len(df.columns)
        spec = {**_TARE_SPEC, "name": "   "}

        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(len(out.columns), n_cols_before)
        self.assertFalse(any(c.startswith(REPAIR_GROUP_PREFIX) for c in out.columns))


class BuildRepairGroupColumnsBaseColumnFormTest(unittest.TestCase):
    """形式B（`base_column` による既存カテゴリ列の流用。G5）。"""

    def test_base_column_form_fills_missing_category_with_na_label(self):
        df = _df()
        spec = {
            "name": "最頻カテゴリ",
            "base_column": "repair_修正__top_統合カテゴリ",
            "na_label": "修正なし",
        }
        out = build_repair_group_columns(df, _cfg([spec]))

        col = out[f"{REPAIR_GROUP_PREFIX}最頻カテゴリ"]
        self.assertEqual(col.iloc[0], "修正なし")  # 元は NaN -> na_label
        self.assertEqual(col.iloc[1], "修正なし")
        self.assertEqual(col.iloc[2], "タレ")       # 既存の値は書き換わらない
        self.assertEqual(col.iloc[3], "色ブツ")
        self.assertEqual(col.iloc[5], "マスキング不良")


class BuildRepairGroupColumnsBinaryColumnTest(unittest.TestCase):
    """2群スペックの `__bin` 列（G8）。"""

    def test_emits_binary_column_mapping_first_label_to_zero_and_second_to_one(self):
        df = _df()
        out = build_repair_group_columns(df, _cfg([_TARE_SPEC]))

        str_col = out[f"{REPAIR_GROUP_PREFIX}タレ"]
        bin_col = out[f"{REPAIR_GROUP_PREFIX}タレ{REPAIR_GROUP_BINARY_SUFFIX}"]

        self.assertEqual(bin_col.dtype, np.float64)
        for i in range(len(out)):
            if str_col.iloc[i] == "修正なし":
                self.assertEqual(bin_col.iloc[i], 0.0)
            elif str_col.iloc[i] == "タレ":
                self.assertEqual(bin_col.iloc[i], 1.0)
            else:
                self.assertTrue(pd.isna(str_col.iloc[i]))
                self.assertTrue(pd.isna(bin_col.iloc[i]))

    def test_does_not_emit_binary_column_for_three_group_spec(self):
        df = _df()
        spec = {
            "name": "三群",
            "groups": [
                {"label": "修正なし", "column": "has_repair_record", "eq": 0},
                {"label": "タレ", "column": "repair_修正__統合カテゴリ__タレ", "min": 1},
                {"label": "色ブツ", "column": "repair_修正__統合カテゴリ__色ブツ", "min": 1},
            ],
        }
        out = build_repair_group_columns(df, _cfg([spec]))

        self.assertIn(f"{REPAIR_GROUP_PREFIX}三群", out.columns)
        self.assertNotIn(f"{REPAIR_GROUP_PREFIX}三群{REPAIR_GROUP_BINARY_SUFFIX}", out.columns)


class BuildRepairGroupColumnsInvalidSpecTest(unittest.TestCase):
    """設定誤りは WARNING を出してそのスペックだけスキップする（G9）。"""

    def test_skips_spec_without_creating_columns_when_referenced_column_is_missing(self):
        df = _df()
        n_cols_before = len(df.columns)
        spec = {
            "name": "存在しない列",
            "groups": [
                {"label": "修正なし", "column": "has_repair_record", "eq": 0},
                {"label": "対象", "column": "repair_修正__統合カテゴリ__存在しないカテゴリ", "min": 1},
            ],
        }
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            out = build_repair_group_columns(df, _cfg([spec]))

        # 参照列が無いスペックは列を1本も作らない（全行一致にフォールバックしない。G9）。
        self.assertEqual(len(out.columns), n_cols_before)
        self.assertFalse(any(c.startswith(REPAIR_GROUP_PREFIX) for c in out.columns))

    def test_skips_spec_when_groups_and_base_column_are_both_declared(self):
        df = _df()
        n_cols_before = len(df.columns)
        spec = {
            "name": "併用",
            "groups": _TARE_SPEC["groups"],
            "base_column": "repair_修正__top_統合カテゴリ",
        }
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(len(out.columns), n_cols_before)

    def test_skips_spec_when_neither_groups_nor_base_column_is_declared(self):
        df = _df()
        n_cols_before = len(df.columns)
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            out = build_repair_group_columns(df, _cfg([{"name": "空スペック"}]))

        self.assertEqual(len(out.columns), n_cols_before)

    def test_skips_spec_when_name_collides_with_existing_column(self):
        df = _df()
        df[f"{REPAIR_GROUP_PREFIX}タレ"] = "PRESET"  # パネルに既に同名列がある想定

        with self.assertLogs("defect_analysis.analysis_data", level="WARNING") as cm:
            out = build_repair_group_columns(df, _cfg([_TARE_SPEC]))

        # 既存列は上書きされない。
        self.assertTrue((out[f"{REPAIR_GROUP_PREFIX}タレ"] == "PRESET").all())
        self.assertTrue(any("既に存在します" in msg for msg in cm.output))

    def test_skips_spec_when_name_is_empty(self):
        df = _df()
        n_cols_before = len(df.columns)
        spec = {"name": "", "groups": _TARE_SPEC["groups"]}
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(len(out.columns), n_cols_before)

    def test_skips_spec_when_a_group_label_is_missing(self):
        df = _df()
        n_cols_before = len(df.columns)
        spec = {
            "name": "ラベル欠落",
            "groups": [
                {"column": "has_repair_record", "eq": 0},
                {"label": "タレ", "column": "repair_修正__統合カテゴリ__タレ", "min": 1},
            ],
        }
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(len(out.columns), n_cols_before)

    def test_skips_spec_when_group_labels_are_duplicated(self):
        df = _df()
        n_cols_before = len(df.columns)
        spec = {
            "name": "ラベル重複",
            "groups": [
                {"label": "対象", "column": "has_repair_record", "eq": 0},
                {"label": "対象", "column": "repair_修正__統合カテゴリ__タレ", "min": 1},
            ],
        }
        with self.assertLogs("defect_analysis.analysis_data", level="WARNING"):
            out = build_repair_group_columns(df, _cfg([spec]))

        self.assertEqual(len(out.columns), n_cols_before)

    def test_other_specs_still_run_when_one_spec_is_invalid(self):
        # 1 スペックの設定誤りで他のスペックが止まらないこと（G9）。
        df = _df()
        bad_spec = {"name": "壊れている", "groups": [{"column": "存在しない列", "min": 1}]}
        out = build_repair_group_columns(df, _cfg([bad_spec, _TARE_SPEC]))

        self.assertNotIn(f"{REPAIR_GROUP_PREFIX}壊れている", out.columns)
        self.assertIn(f"{REPAIR_GROUP_PREFIX}タレ", out.columns)


class BuildRepairGroupColumnsNoSpecTest(unittest.TestCase):
    def test_returns_dataframe_unchanged_when_repair_groups_is_absent(self):
        df = _df()
        n_cols_before = len(df.columns)
        out = build_repair_group_columns(df, _cfg(None))

        self.assertEqual(len(out.columns), n_cols_before)

    def test_returns_dataframe_unchanged_when_repair_groups_is_an_empty_list(self):
        df = _df()
        n_cols_before = len(df.columns)
        out = build_repair_group_columns(df, _cfg([]))

        self.assertEqual(len(out.columns), n_cols_before)


class BuildRepairGroupColumnsInPlaceTest(unittest.TestCase):
    """in-place 更新で同一オブジェクトを返す（G11）。"""

    def test_returns_the_same_dataframe_object_it_was_given(self):
        df = _df()
        out = build_repair_group_columns(df, _cfg([_TARE_SPEC]))

        self.assertIs(out, df)

    def test_mutates_the_input_dataframe_in_place(self):
        df = _df()
        build_repair_group_columns(df, _cfg([_TARE_SPEC]))

        # 戻り値を使わなくても、渡した df 自体に列が追加されている。
        self.assertIn(f"{REPAIR_GROUP_PREFIX}タレ", df.columns)


if __name__ == "__main__":
    unittest.main()
