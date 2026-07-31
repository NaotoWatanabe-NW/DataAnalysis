"""VIN 正規化のテスト（vin_key.py）。

内部空白を含むサフィックス VIN の分解が本モジュールの核心的な検証観点。
`str.strip()` の実装では末尾サフィックス前の空白が残るため、その回帰を検出するテストを含む。

実行:
    .venv/bin/python -m pytest tests/test_vin_key.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.vin_key import VinPolicy, join_key, normalize_vin  # noqa: E402


class NormalizeVinTest(unittest.TestCase):
    def test_internal_whitespace_before_suffix_is_fully_removed_not_just_stripped(self):
        # "HE93S-122023     a" は空白が文字列の内部（base と suffix の間）にある。
        # str.strip() では取り除けない位置であり、全空白除去のみが正しく分解できる。
        s = pd.Series(["HE93S-122023     a"])
        out = normalize_vin(s)
        self.assertEqual(out.loc[0, "vin"], "HE93S-122023a")
        self.assertEqual(out.loc[0, "vin_base"], "HE93S-122023")
        self.assertEqual(out.loc[0, "vin_pass_no"], 2)
        # strip() のみの誤実装であれば ("HE93S-122023     a" の先頭以外に空白が残る) こうなるはず:
        stripped_only = "HE93S-122023     a".strip()
        self.assertNotEqual(out.loc[0, "vin"], stripped_only)

    def test_full17_vin_without_suffix_has_pass_no_one(self):
        s = pd.Series(["JS3JB74V8V5106401 "])
        out = normalize_vin(s)
        self.assertEqual(out.loc[0, "vin"], "JS3JB74V8V5106401")
        self.assertEqual(out.loc[0, "vin_pass_no"], 1)

    def test_suffix_a_b_c_map_to_pass_no_2_3_4(self):
        s = pd.Series(["HE93S-122023a", "HE93S-122023b", "HE93S-122023c"])
        out = normalize_vin(s)
        self.assertEqual(out["vin_pass_no"].tolist(), [2, 3, 4])
        self.assertEqual(out["vin_base"].tolist(), ["HE93S-122023"] * 3)

    def test_dummy_and_empty_values_are_flagged_is_dummy(self):
        s = pd.Series(["DUMMY-YOSHIKId", "EMPTY", ""])
        out = normalize_vin(s)
        self.assertTrue(out["vin_is_dummy"].all())

    def test_real_vin_values_are_not_flagged_dummy(self):
        s = pd.Series(["HE93S-122023a", "JS3JB74V8V5106401"])
        out = normalize_vin(s)
        self.assertFalse(out["vin_is_dummy"].any())

    def test_suffix_letter_is_not_uppercased(self):
        s = pd.Series(["HE93S-122023a"])
        out = normalize_vin(s)
        self.assertEqual(out.loc[0, "vin"], "HE93S-122023a")
        self.assertNotEqual(out.loc[0, "vin"], "HE93S-122023A")


class JoinKeyTest(unittest.TestCase):
    def test_keep_policy_uses_vin_including_suffix_as_join_key(self):
        s = pd.Series(["HE93S-122023a", "HE93S-122023b"])
        df = normalize_vin(s)
        keys = join_key(df, VinPolicy(suffix_policy="keep"))
        self.assertEqual(keys.tolist(), ["HE93S-122023a", "HE93S-122023b"])

    def test_merge_policy_collapses_suffixed_vins_to_shared_vin_base(self):
        s = pd.Series(["HE93S-122023a", "HE93S-122023b", "HE93S-122023"])
        df = normalize_vin(s)
        keys = join_key(df, VinPolicy(suffix_policy="merge"))
        self.assertEqual(keys.nunique(), 1)
        self.assertEqual(keys.iloc[0], "HE93S-122023")


if __name__ == "__main__":
    unittest.main()
