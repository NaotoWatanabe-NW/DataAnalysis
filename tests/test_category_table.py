"""repair 統合カテゴリの 4 キー厳密写像のテスト（category_integrate.py の追加分）。

docs/repair_integrated_category_design.md §9.1/§9.2 に対応する。
1 キー写像（CLI `category`）のテストは tests/test_transforms.py の CategoryIntegrateTest。

実行:
    .venv/bin/python -m pytest tests/test_category_table.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.category_integrate import (  # noqa: E402
    DEFAULT_CATEGORY_LABELS,
    CompositeCategoryTable,
    apply_composite_category,
    load_composite_category_table,
    summarize_unmatched_keys,
)

_HEADER = "作業工程,大分類,中分類,小分類,グラフ項目"


def _write_table_csv(path: Path, lines: list[str], header: str = _HEADER) -> None:
    text = header + "\n" + "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8-sig")


class LoadCompositeCategoryTableTest(unittest.TestCase):
    """§9.1: load_composite_category_table。"""

    def test_reads_four_key_mapping_and_derives_scope_from_first_key_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            _write_table_csv(
                path,
                [
                    "工程A,大X,中Y,小Z,値1",
                    "工程A,大X,中Y,小W,値2",
                    "工程B,大X,中Y,小Z,値3",
                ],
            )
            table = load_composite_category_table(path)

            self.assertEqual(
                table.mapping,
                {
                    ("工程A", "大X", "中Y", "小Z"): "値1",
                    ("工程A", "大X", "中Y", "小W"): "値2",
                    ("工程B", "大X", "中Y", "小Z"): "値3",
                },
            )
            self.assertEqual(table.scope_values, frozenset({"工程A", "工程B"}))
            self.assertEqual(table.table_key_columns, ("作業工程", "大分類", "中分類", "小分類"))

    def test_missing_key_column_raises_value_error_naming_actual_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            # 小分類 列が無いヘッダ。
            _write_table_csv(path, ["工程A,大X,中Y,値1"], header="作業工程,大分類,中分類,グラフ項目")

            with self.assertRaises(ValueError) as cm:
                load_composite_category_table(path)
            message = str(cm.exception)
            self.assertIn("小分類", message)
            self.assertIn("作業工程", message)  # 実在列も列挙される

    def test_empty_cell_in_value_column_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            _write_table_csv(path, ["工程A,大X,中Y,小Z,"])

            with self.assertRaises(ValueError):
                load_composite_category_table(path)

    def test_empty_cell_in_key_column_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            _write_table_csv(path, ["工程A,大X,中Y,,値1"])

            with self.assertRaises(ValueError):
                load_composite_category_table(path)

    def test_fully_duplicate_row_is_removed_and_counted_with_no_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            _write_table_csv(path, ["工程A,大X,中Y,小Z,値1", "工程A,大X,中Y,小Z,値1"])

            table = load_composite_category_table(path)

            self.assertEqual(table.n_exact_duplicates, 1)
            self.assertEqual(table.n_rows, 1)
            self.assertEqual(table.conflicts, {})
            self.assertEqual(table.mapping[("工程A", "大X", "中Y", "小Z")], "値1")

    def test_conflicting_values_for_same_key_raise_value_error_when_on_duplicate_key_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            _write_table_csv(path, ["工程A,大X,中Y,小Z,値2", "工程A,大X,中Y,小Z,値1"])

            with self.assertRaises(ValueError):
                load_composite_category_table(path, on_duplicate_key="error")

    def test_conflicting_values_pick_ascending_first_and_are_recorded_in_conflicts_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            _write_table_csv(path, ["工程A,大X,中Y,小Z,値2", "工程A,大X,中Y,小Z,値1"])

            table = load_composite_category_table(path)  # on_duplicate_key の既定値 "first"
            key = ("工程A", "大X", "中Y", "小Z")

            self.assertEqual(table.mapping[key], "値1")
            self.assertEqual(table.conflicts[key], ("値1", "値2"))

    def test_reversing_row_order_does_not_change_which_conflicting_value_is_adopted(self):
        # IC9: 行の並び順に依存しない決定的な競合解決（Excel 再エクスポート耐性の回帰テスト）。
        rows = [
            "工程A,大X,中Y,小X,値X",
            "工程A,大X,中Y,小Z,値2",
            "工程A,大X,中Y,小Z,値1",
            "工程B,大Y,中Y,小Y,値Y",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path_forward = Path(tmp) / "forward.csv"
            path_reversed = Path(tmp) / "reversed.csv"
            _write_table_csv(path_forward, rows)
            _write_table_csv(path_reversed, list(reversed(rows)))

            table_forward = load_composite_category_table(path_forward)
            table_reversed = load_composite_category_table(path_reversed)

            self.assertEqual(table_forward.mapping, table_reversed.mapping)
            self.assertEqual(table_forward.conflicts, table_reversed.conflicts)
            key = ("工程A", "大X", "中Y", "小Z")
            self.assertEqual(table_forward.mapping[key], "値1")

    def test_full_width_and_half_width_numerals_in_value_column_are_folded_by_nfkc(self):
        # IC5: 電着2次タレ（半角）/ 電着２次タレ（全角）が NFKC で同一値に畳まれる。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            _write_table_csv(
                path, ["工程A,大X,中Y,小Z,電着2次タレ", "工程A,大X,中Y,小Z,電着２次タレ"]
            )

            table = load_composite_category_table(path)

            self.assertEqual(table.n_exact_duplicates, 1)
            self.assertEqual(table.conflicts, {})
            self.assertEqual(table.mapping[("工程A", "大X", "中Y", "小Z")], "電着2次タレ")

    def test_missing_file_raises_file_not_found_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does_not_exist.csv"
            with self.assertRaises(FileNotFoundError):
                load_composite_category_table(path)


def _table(mapping: dict[tuple[str, ...], str], scope: set[str]) -> CompositeCategoryTable:
    return CompositeCategoryTable(
        mapping=dict(mapping),
        scope_values=frozenset(scope),
        table_key_columns=("作業工程", "大分類", "中分類", "小分類"),
        n_rows=len(mapping),
        n_exact_duplicates=0,
        conflicts={},
    )


_SRC_KEYS = ("入力工程", "大分類", "中分類", "小分類")


class ApplyCompositeCategoryTest(unittest.TestCase):
    """§9.2: apply_composite_category。"""

    def test_four_key_exact_match_returns_the_table_value(self):
        table = _table({("工程A", "大X", "中Y", "小Z"): "値1"}, {"工程A"})
        df = pd.DataFrame(
            {"入力工程": ["工程A"], "大分類": ["大X"], "中分類": ["中Y"], "小分類": ["小Z"]}
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertEqual(result.tolist(), ["値1"])

    def test_matching_three_keys_with_a_different_input_process_does_not_fall_back(self):
        # IC4 の核心: 大中小が一致していても入力工程が違えば一致しない（3キーへのフォールバック無し）。
        table = _table({("工程A", "大X", "中Y", "小Z"): "値1"}, {"工程A", "工程B"})
        df = pd.DataFrame(
            {
                "入力工程": ["工程A", "工程B"],
                "大分類": ["大X", "大X"],
                "中分類": ["中Y", "中Y"],
                "小分類": ["小Z", "小Z"],
            }
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertEqual(result.iloc[0], "値1")
        self.assertEqual(result.iloc[1], DEFAULT_CATEGORY_LABELS["unmatched"])
        self.assertNotEqual(result.iloc[1], "値1")

    def test_input_process_outside_scope_returns_out_of_scope_label(self):
        table = _table({("工程A", "大X", "中Y", "小Z"): "値1"}, {"工程A"})
        df = pd.DataFrame(
            {"入力工程": ["工程C"], "大分類": ["大X"], "中分類": ["中Y"], "小分類": ["小Z"]}
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertEqual(result.iloc[0], DEFAULT_CATEGORY_LABELS["out_of_scope_process"])

    def test_in_scope_process_with_unmapped_combination_returns_unmatched_label(self):
        table = _table({("工程A", "大X", "中Y", "小Z"): "値1"}, {"工程A"})
        df = pd.DataFrame(
            {"入力工程": ["工程A"], "大分類": ["大X"], "中分類": ["中Y"], "小分類": ["別の小分類"]}
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertEqual(result.iloc[0], DEFAULT_CATEGORY_LABELS["unmatched"])

    def test_table_value_that_is_an_excluded_marker_returns_excluded_label(self):
        table = _table({("工程A", "大X", "中Y", "小Z"): "-"}, {"工程A"})
        df = pd.DataFrame(
            {"入力工程": ["工程A"], "大分類": ["大X"], "中分類": ["中Y"], "小分類": ["小Z"]}
        )

        result = apply_composite_category(
            df, table, source_key_columns=_SRC_KEYS, excluded_values=("-",)
        )

        self.assertEqual(result.iloc[0], DEFAULT_CATEGORY_LABELS["excluded"])

    def test_missing_input_process_value_returns_out_of_scope_label_without_raising(self):
        table = _table({("工程A", "大X", "中Y", "小Z"): "値1"}, {"工程A"})
        df = pd.DataFrame(
            {"入力工程": [None], "大分類": ["大X"], "中分類": ["中Y"], "小分類": ["小Z"]}
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertEqual(result.iloc[0], DEFAULT_CATEGORY_LABELS["out_of_scope_process"])

    def test_result_has_no_nan_and_preserves_the_input_index(self):
        table = _table(
            {
                ("工程A", "大X", "中Y", "小Z"): "値1",
                ("工程A", "大X", "中Y", "小W"): "-",
            },
            {"工程A"},
        )
        df = pd.DataFrame(
            {
                "入力工程": ["工程A", "工程A", "工程C", None],
                "大分類": ["大X", "大X", "大X", "大X"],
                "中分類": ["中Y", "中Y", "中Y", "中Y"],
                "小分類": ["小Z", "小W", "小Z", "小Z"],
            },
            index=[5, 3, 8, 1],
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertFalse(result.isna().any())
        self.assertEqual(list(result.index), list(df.index))

    def test_leading_and_trailing_whitespace_in_key_values_is_stripped_before_matching(self):
        table = _table({("工程A", "大X", "中Y", "小Z"): "値1"}, {"工程A"})
        df = pd.DataFrame(
            {
                "入力工程": [" 工程A"],
                "大分類": ["大X "],
                "中分類": [" 中Y "],
                "小分類": ["小Z"],
            }
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertEqual(result.iloc[0], "値1")

    def test_full_width_and_half_width_key_values_do_not_match_because_keys_are_not_normalized(self):
        # IC4/IC5 が逆になっていないことの固定: キーは正規化しないので全半角ゆれは一致しない。
        table = _table({("工程A", "大X", "中Y", "小１"): "値1"}, {"工程A"})  # 全角数字キー
        df = pd.DataFrame(
            {"入力工程": ["工程A"], "大分類": ["大X"], "中分類": ["中Y"], "小分類": ["小1"]}  # 半角数字
        )

        result = apply_composite_category(df, table, source_key_columns=_SRC_KEYS)

        self.assertEqual(result.iloc[0], DEFAULT_CATEGORY_LABELS["unmatched"])


class SummarizeUnmatchedKeysTest(unittest.TestCase):
    """§9.2: summarize_unmatched_keys。"""

    def test_summary_counts_rows_and_distinct_vin_and_orders_by_category_then_rows_desc_then_key_asc(self):
        df = pd.DataFrame(
            {
                "入力工程": [
                    "A_un", "A_un", "B_un",
                    "A_out", "A_out", "B_out", "B_out",
                    "X",
                ],
                "大分類": ["大X"] * 8,
                "中分類": ["中Y"] * 8,
                "小分類": ["小1", "小1", "小2", "小3", "小3", "小4", "小4", "小5"],
                "vin": ["v1", "v2", "v3", "v4", "v4", "v5", "v6", "v7"],
            }
        )
        category = pd.Series(
            ["未分類", "未分類", "未分類", "対象外工程", "対象外工程", "対象外工程", "対象外工程", "カテゴリZ"]
        )

        result = summarize_unmatched_keys(df, category, source_key_columns=_SRC_KEYS)

        self.assertEqual(list(result.columns), ["区分", *_SRC_KEYS, "n_rows", "n_vin"])
        actual = list(zip(result["区分"], result["入力工程"], result["n_rows"], result["n_vin"]))
        expected = [
            ("未分類", "A_un", 2, 2),
            ("未分類", "B_un", 1, 1),
            ("対象外工程", "A_out", 2, 1),
            ("対象外工程", "B_out", 2, 2),
        ]
        self.assertEqual(actual, expected)

    def test_all_rows_matched_returns_an_empty_dataframe_with_the_defined_columns(self):
        df = pd.DataFrame(
            {"入力工程": ["工程A"], "大分類": ["大X"], "中分類": ["中Y"], "小分類": ["小Z"], "vin": ["v1"]}
        )
        category = pd.Series(["カテゴリZ"])

        result = summarize_unmatched_keys(df, category, source_key_columns=_SRC_KEYS)

        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), ["区分", *_SRC_KEYS, "n_rows", "n_vin"])


if __name__ == "__main__":
    unittest.main()
