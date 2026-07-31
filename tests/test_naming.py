"""列名・ソース名の機械的正規化のテスト（naming.py）。

実行:
    .venv/bin/python -m pytest tests/test_naming.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.naming import (  # noqa: E402
    normalize_columns,
    normalize_name,
    prefixed,
    source_key,
)


class NormalizeNameTest(unittest.TestCase):
    def test_halfwidth_kana_is_folded_to_fullwidth_with_space_and_hash_as_underscore(self):
        self.assertEqual(normalize_name("ｿﾞｰﾝ#1 通過日時"), "ゾーン_1_通過日時")

    def test_trailing_hash_only_column_is_dropped(self):
        self.assertEqual(normalize_name("VIN#"), "VIN")
        self.assertEqual(normalize_name("ﾛﾎﾞｯﾄ#"), "ロボット")

    def test_parenthesized_kanji_and_pa_on_hyphen_are_replaced(self):
        self.assertEqual(normalize_name("PA-ON 粒子数(大)"), "PA_ON_粒子数_大")

    def test_ampersand_and_repeated_special_chars_are_folded_to_single_underscore(self):
        self.assertEqual(
            normalize_name("中上炉 ゾーン#1&5 制御盤 電力量 積算値"),
            "中上炉_ゾーン_1_5_制御盤_電力量_積算値",
        )

    def test_multiple_tokens_collapse_consecutive_underscores_and_strip_edges(self):
        self.assertEqual(normalize_name("ﾌﾞｰｽ#1 4 結露防止 運転ﾓｰﾄﾞ"), "ブース_1_4_結露防止_運転モード")

    def test_column_without_special_chars_is_left_unchanged(self):
        self.assertEqual(normalize_name("判定結果_3Bit"), "判定結果_3Bit")

    def test_empty_string_after_normalization_becomes_col(self):
        self.assertEqual(normalize_name("###"), "col")
        self.assertEqual(normalize_name(""), "col")

    def test_normalize_name_is_idempotent(self):
        for raw in ["ｿﾞｰﾝ#1 通過日時", "PA-ON 粒子数(大)", "中上炉 ゾーン#1&5 制御盤 電力量 積算値", "VIN#"]:
            once = normalize_name(raw)
            twice = normalize_name(once)
            self.assertEqual(once, twice)


class NormalizeColumnsTest(unittest.TestCase):
    def test_colliding_columns_get_suffix_and_both_original_names_are_kept_in_rename_map(self):
        cols, rename_map = normalize_columns(["VIN#", "VIN ", "値"])
        self.assertEqual(cols, ["VIN", "VIN__2", "値"])
        self.assertEqual(rename_map["VIN"], "VIN#")
        self.assertEqual(rename_map["VIN__2"], "VIN ")
        self.assertEqual(rename_map["値"], "値")

    def test_three_way_collision_increments_suffix_for_each_extra_column(self):
        cols, rename_map = normalize_columns(["A#", "A ", "A&"])
        self.assertEqual(cols, ["A", "A__2", "A__3"])
        self.assertEqual(set(rename_map.values()), {"A#", "A ", "A&"})

    def test_no_collision_when_columns_are_already_distinct(self):
        cols, rename_map = normalize_columns(["VIN#", "ｿﾞｰﾝ#1 通過日時"])
        self.assertEqual(cols, ["VIN", "ゾーン_1_通過日時"])


class PrefixedTest(unittest.TestCase):
    def test_joins_source_and_column_with_double_underscore(self):
        self.assertEqual(prefixed("シーラー炉", "入口_通過日時"), "シーラー炉__入口_通過日時")


class SourceKeyTest(unittest.TestCase):
    def test_strips_monthly_date_suffix(self):
        self.assertEqual(source_key("シーラー炉_202607"), "シーラー炉")

    def test_strips_daily_date_suffix(self):
        self.assertEqual(source_key("ブース_20260724"), "ブース")

    def test_no_date_suffix_is_left_as_is_after_normalization(self):
        self.assertEqual(source_key("ブース"), "ブース")

    def test_nakaguro_in_filename_is_normalized_to_underscore(self):
        self.assertEqual(source_key("前処理・電着_202607"), "前処理_電着")


if __name__ == "__main__":
    unittest.main()
