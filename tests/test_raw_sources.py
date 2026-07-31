"""raw CSV の発見とソース定義のテスト（raw_sources.py）。

実行:
    .venv/bin/python -m pytest tests/test_raw_sources.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.config import Config  # noqa: E402
from defect_analysis.raw_sources import discover_sources  # noqa: E402

import tempfile  # noqa: E402


def _write_csv(path: Path, columns: list[str], row: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row], columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


class DiscoverSourcesGroupingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.raw_dir = self.root / "data" / "raw"
        self.cfg = Config({}, root=self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_monthly_daily_and_suffixless_filenames_are_grouped_into_one_source(self):
        sub = self.raw_dir / "traceability"
        for name in ["シーラー炉_202607.csv", "シーラー炉_20260724.csv", "シーラー炉.csv"]:
            _write_csv(sub / name, ["VIN#", "入口 通過日時"], ["A", "2026/07/24 10:00:00"])

        sources = discover_sources(self.raw_dir, self.cfg)

        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source.kind, "traceability")
        self.assertEqual(source.name, "シーラー炉")
        self.assertEqual(
            sorted(f.name for f in source.files),
            sorted(["シーラー炉.csv", "シーラー炉_20260724.csv", "シーラー炉_202607.csv"]),
        )

    def test_nakaguro_separated_filename_normalizes_to_underscore_joined_source_name(self):
        sub = self.raw_dir / "traceability"
        _write_csv(sub / "前処理・電着_202607.csv", ["VIN#", "DATETIME"], ["A", "2026/07/24 10:00:00"])

        sources = discover_sources(self.raw_dir, self.cfg)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].name, "前処理_電着")

    def test_time_column_prefers_datetime_over_tsuukanichiji_columns(self):
        sub = self.raw_dir / "trend"
        _write_csv(sub / "何か_202607.csv", ["DATETIME", "何か 測定値"], ["2026/07/24 10:00:00", "1.0"])

        sources = discover_sources(self.raw_dir, self.cfg)

        self.assertEqual(sources[0].time_column, "DATETIME")

    def test_time_column_falls_back_to_first_tsuukanichiji_column_when_no_datetime(self):
        sub = self.raw_dir / "traceability"
        _write_csv(
            sub / "各工程滞在時間_202607.csv",
            ["VIN#", "前処理 通過日時", "電着 通過日時"],
            ["A", "2026/07/24 10:00:00", "2026/07/24 11:00:00"],
        )

        sources = discover_sources(self.raw_dir, self.cfg)

        self.assertEqual(sources[0].time_column, "前処理_通過日時")

    def test_traceability_source_without_vin_column_is_skipped_with_error_log(self):
        sub = self.raw_dir / "traceability"
        _write_csv(sub / "novin_202607.csv", ["DATETIME", "値"], ["2026/07/24 10:00:00", "1.0"])

        with self.assertLogs("defect_analysis.raw_sources", level="ERROR") as cm:
            sources = discover_sources(self.raw_dir, self.cfg)

        self.assertEqual(sources, [])
        self.assertTrue(any("VIN 列が見つからない" in msg for msg in cm.output))

    def test_unknown_subdirectory_is_skipped_with_warning(self):
        sub = self.raw_dir / "unknown_kind"
        _write_csv(sub / "x.csv", ["VIN#", "DATETIME"], ["A", "2026/07/24 10:00:00"])

        with self.assertLogs("defect_analysis.raw_sources", level="WARNING") as cm:
            sources = discover_sources(self.raw_dir, self.cfg)

        self.assertEqual(sources, [])
        self.assertTrue(any("未知のサブディレクトリ" in msg for msg in cm.output))

    def test_missing_raw_dir_returns_empty_list_without_error(self):
        sources = discover_sources(self.raw_dir / "does_not_exist", self.cfg)
        self.assertEqual(sources, [])


def _write_csv_cp932(path: Path, columns: list[str], row: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row], columns=columns).to_csv(path, index=False, encoding="cp932")


class RepairEncodingAndAliasTest(unittest.TestCase):
    """docs/real_data_repair_design.md §6.1 の tester 観点（RT1）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.raw_dir = self.root / "data" / "raw"

    def tearDown(self):
        self._tmp.cleanup()

    def test_cp932_header_is_readable_via_fallback_without_explicit_encoding_config(self):
        # kind=traceability は既定 encoding=utf-8-sig のため、fallback 無指定なら読めないはず。
        sub = self.raw_dir / "traceability"
        _write_csv_cp932(sub / "何か.csv", ["VIN#", "通過日時"], ["A", "2026/07/24 10:00:00"])
        cfg = Config(
            {"real_ingest": {"convert": {"encoding_fallbacks": ["cp932"]}}}, root=self.root
        )

        with self.assertLogs("defect_analysis.raw_sources", level="WARNING") as cm:
            sources = discover_sources(self.raw_dir, cfg)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].encoding, "cp932")
        self.assertTrue(any("cp932" in msg for msg in cm.output))

    def test_by_kind_encoding_is_used_directly_without_needing_fallback(self):
        sub = self.raw_dir / "traceability"
        _write_csv_cp932(sub / "何か.csv", ["VIN#", "通過日時"], ["A", "2026/07/24 10:00:00"])
        cfg = Config(
            {
                "real_ingest": {
                    "convert": {"by_kind": {"traceability": {"encoding": "cp932"}}}
                }
            },
            root=self.root,
        )

        with self.assertNoLogs("defect_analysis.raw_sources", level="WARNING"):
            sources = discover_sources(self.raw_dir, cfg)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].encoding, "cp932")

    def test_source_aliases_renames_the_source_and_defaults_apply_to_repair_defect(self):
        sub = self.raw_dir / "repair"
        _write_csv_cp932(sub / "defect.csv", ["VIN", "PB-ON"], ["'HE93S-122065", "20260724 100000"])
        cfg = Config({}, root=self.root)

        sources = discover_sources(self.raw_dir, cfg)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].name, "修正")
        self.assertEqual(sources[0].kind, "repair")
        self.assertEqual(sources[0].encoding, "cp932")
        self.assertEqual(sources[0].time_column, "PB_ON")

    def test_by_kind_time_column_overrides_the_rule_based_resolution(self):
        sub = self.raw_dir / "traceability"
        _write_csv_cp932(sub / "何か.csv", ["VIN#", "通過日時", "別時刻"], ["A", "2026/07/24 10:00:00", "x"])
        cfg = Config(
            {
                "real_ingest": {
                    "convert": {
                        "by_kind": {"traceability": {"encoding": "cp932", "time_column": "別時刻"}}
                    }
                }
            },
            root=self.root,
        )

        sources = discover_sources(self.raw_dir, cfg)

        self.assertEqual(sources[0].time_column, "別時刻")

    def test_by_kind_time_column_missing_from_header_skips_source_with_error_log(self):
        sub = self.raw_dir / "traceability"
        _write_csv_cp932(sub / "何か.csv", ["VIN#", "通過日時"], ["A", "2026/07/24 10:00:00"])
        cfg = Config(
            {
                "real_ingest": {
                    "convert": {
                        "by_kind": {"traceability": {"encoding": "cp932", "time_column": "存在しない列"}}
                    }
                }
            },
            root=self.root,
        )

        with self.assertLogs("defect_analysis.raw_sources", level="ERROR") as cm:
            sources = discover_sources(self.raw_dir, cfg)

        self.assertEqual(sources, [])
        self.assertTrue(any("時刻アンカー列が見つからない" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
