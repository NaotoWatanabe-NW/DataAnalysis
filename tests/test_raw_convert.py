"""CSV → Parquet レイク変換・読取のテスト（raw_convert.py）。

実行:
    .venv/bin/python -m pytest tests/test_raw_convert.py -q
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.config import Config  # noqa: E402
from defect_analysis.raw_convert import (  # noqa: E402
    DEFAULT_CONVERT_BY_KIND_PREPROCESS,
    convert_file,
    read_source,
)
from defect_analysis.raw_sources import RawSource  # noqa: E402


def _make_source(
    files: list[Path], columns: list[str], time_column: str = "通過日時", *, kind: str = "traceability",
) -> RawSource:
    return RawSource(
        kind=kind,
        name="テスト",
        files=files,
        vin_column="VIN",
        time_column=time_column,
        columns=columns,
        rename_map={},
    )


def _make_cfg(root: Path, lake_name: str = "lake", **convert_overrides) -> Config:
    data = {
        "real_ingest": {
            "manifest_path": f"{lake_name}/_manifest.json",
            "convert": convert_overrides,
        }
    }
    return Config(data, root=root)


class ConvertFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.raw_csv = self.root / "in.csv"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_csv(self, df: pd.DataFrame) -> None:
        df.to_csv(self.raw_csv, index=False, encoding="utf-8-sig")

    def test_single_file_crossing_a_day_boundary_is_split_into_two_date_partitions(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN#": ["A", "B"],
                    "通過日時": ["2026/07/24 06:00:00", "2026/07/25 02:00:00"],
                    "値": [1.5, 2.5],
                }
            )
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        result = convert_file(self.raw_csv, source, lake_dir, cfg)

        self.assertEqual(result.n_partitions, 2)
        date_dirs = sorted(p.name for p in (lake_dir / "traceability" / "テスト").glob("date=*"))
        self.assertEqual(date_dirs, ["date=2026-07-24", "date=2026-07-25"])

    def test_chunksize_one_produces_the_same_data_as_a_single_large_chunk(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN#": ["A", "B", "C"],
                    "通過日時": [
                        "2026/07/24 06:00:00",
                        "2026/07/24 07:00:00",
                        "2026/07/25 02:00:00",
                    ],
                    "値": [1.5, 2.5, 3.5],
                }
            )
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])

        cfg_default = _make_cfg(self.root, lake_name="lake_default", chunksize=200_000)
        cfg_single = _make_cfg(self.root, lake_name="lake_chunk1", chunksize=1)
        lake_default = self.root / "lake_default"
        lake_single = self.root / "lake_chunk1"

        convert_file(self.raw_csv, source, lake_default, cfg_default)
        convert_file(self.raw_csv, source, lake_single, cfg_single)

        df_default = read_source(lake_default, "traceability", "テスト").sort_values("vin_raw").reset_index(drop=True)
        df_single = read_source(lake_single, "traceability", "テスト").sort_values("vin_raw").reset_index(drop=True)

        pd.testing.assert_frame_equal(df_default, df_single)

    def test_second_run_without_changes_is_skipped(self):
        self._write_csv(
            pd.DataFrame({"VIN#": ["A"], "通過日時": ["2026/07/24 06:00:00"], "値": [1.0]})
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        first = convert_file(self.raw_csv, source, lake_dir, cfg)
        second = convert_file(self.raw_csv, source, lake_dir, cfg)

        self.assertFalse(first.skipped)
        self.assertTrue(second.skipped)
        self.assertEqual(second.reason, "unchanged")

    def test_mtime_change_triggers_reconversion_even_without_force(self):
        self._write_csv(
            pd.DataFrame({"VIN#": ["A"], "通過日時": ["2026/07/24 06:00:00"], "値": [1.0]})
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        future = time.time() + 100
        os.utime(self.raw_csv, (future, future))
        result = convert_file(self.raw_csv, source, lake_dir, cfg)

        self.assertFalse(result.skipped)

    def test_force_true_always_reconverts_even_when_unchanged(self):
        self._write_csv(
            pd.DataFrame({"VIN#": ["A"], "通過日時": ["2026/07/24 06:00:00"], "値": [1.0]})
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        result = convert_file(self.raw_csv, source, lake_dir, cfg, force=True)

        self.assertFalse(result.skipped)

    def test_reconverting_with_fewer_rows_removes_stale_part_files_for_dropped_dates(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN#": ["A", "B"],
                    "通過日時": ["2026/07/24 06:00:00", "2026/07/25 02:00:00"],
                    "値": [1.0, 2.0],
                }
            )
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        source_dir = lake_dir / "traceability" / "テスト"
        self.assertEqual(len(list(source_dir.rglob("part-*.parquet"))), 2)

        # 行数を減らして再変換（date=2026-07-25 のデータが無くなる）
        self._write_csv(
            pd.DataFrame({"VIN#": ["A"], "通過日時": ["2026/07/24 06:00:00"], "値": [9.0]})
        )
        convert_file(self.raw_csv, source, lake_dir, cfg, force=True)

        remaining_parts = list(source_dir.rglob("part-*.parquet"))
        self.assertEqual(len(remaining_parts), 1)
        self.assertIn("date=2026-07-24", str(remaining_parts[0]))
        df = read_source(lake_dir, "traceability", "テスト")
        self.assertEqual(len(df), 1)

    def test_column_ending_in_sekisanchi_stays_float64_while_other_floats_are_downcast(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN#": ["A"],
                    "通過日時": ["2026/07/24 06:00:00"],
                    "値": [1.123456789],
                    "電力量 積算値": [100.123456789],
                }
            )
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値", "電力量_積算値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "traceability", "テスト")

        self.assertEqual(str(df["値"].dtype), "float32")
        self.assertEqual(str(df["電力量_積算値"].dtype), "float64")

    def test_unparseable_datetime_row_goes_to_date_unknown_partition_with_warning(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN#": ["A", "B"],
                    "通過日時": ["2026/07/24 06:00:00", "not-a-date"],
                    "値": [1.0, 2.0],
                }
            )
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        with self.assertLogs("defect_analysis.raw_convert", level="WARNING") as cm:
            convert_file(self.raw_csv, source, lake_dir, cfg)

        unknown_dir = lake_dir / "traceability" / "テスト" / "date=unknown"
        self.assertTrue(unknown_dir.exists())
        self.assertTrue(any(unknown_dir.glob("*.parquet")))
        self.assertTrue(any("date=unknown" in msg for msg in cm.output))

    def test_datetime_without_seconds_is_parsed_via_fallback_format_without_warning(self):
        # 実データでは通過イベント系（秒あり）とセンサー/PLC系（秒なし）が混在する
        # （docs/real_data_facts.md 参照）。既定の datetime_format_fallbacks（コード側 DEFAULT_CONVERT）
        # が秒なし側を吸収し、失敗率の WARN を出さないことを確認する。
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN#": ["A", "B"],
                    "通過日時": ["2026/1/2 16:35", "2026/07/24 06:00:00"],
                    "値": [1.0, 2.0],
                }
            )
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        with self.assertNoLogs("defect_analysis.raw_convert", level="WARNING"):
            convert_file(self.raw_csv, source, lake_dir, cfg)

        result = read_source(lake_dir, "traceability", "テスト")
        parsed = result.set_index("vin")["通過日時"]
        self.assertEqual(parsed.loc["A"], pd.Timestamp("2026-01-02 16:35:00"))
        self.assertEqual(parsed.loc["B"], pd.Timestamp("2026-07-24 06:00:00"))
        # 秒あり行・秒なし行（片方は主フォーマットが全滅せず一部成功、他方は全滅からの
        # フォールバック）が混在しても、書き出し後の dtype 解像度が揃っていること
        # （揃わないと後段の merge_asof が例外を投げる。実装レビューで発見した回帰）。
        self.assertEqual(str(result["通過日時"].dtype), "datetime64[us]")

    def test_datetime_fully_without_seconds_falls_back_for_the_whole_column(self):
        # 主フォーマットが「全行」失敗するケース（ブース・trend 等の実データで実際に起きる）。
        # 全滅時 pandas は解像度を datetime64[s] と推定するため、フォールバック結果への
        # 昇格が正しく行われるかを別途確認する。
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN#": ["A", "B"],
                    "通過日時": ["2026/1/2 16:35", "2026/1/4 14:41"],
                    "値": [1.0, 2.0],
                }
            )
        )
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        with self.assertNoLogs("defect_analysis.raw_convert", level="WARNING"):
            convert_file(self.raw_csv, source, lake_dir, cfg)

        result = read_source(lake_dir, "traceability", "テスト")
        self.assertEqual(str(result["通過日時"].dtype), "datetime64[us]")
        self.assertFalse(result["通過日時"].isna().any())


def _repair_by_kind(**pii_overrides) -> dict:
    """DEFAULT_CONVERT_BY_KIND_PREPROCESS["repair"] を土台に pii だけ上書きする。

    convert_config() は real_ingest.convert 配下をトップレベルで丸ごとマージするため、
    by_kind を渡すと datetime_columns 等の既定が消えて PB_ON が未パースのまま date 計算に
    使われて壊れる。pii 以外は既定を引き継いで安全に上書きする。
    """
    base = dict(DEFAULT_CONVERT_BY_KIND_PREPROCESS["repair"])
    base["pii"] = {**base["pii"], **pii_overrides}
    return {"repair": base}


class RepairPreprocessingTest(unittest.TestCase):
    """repair kind 限定の convert 前処理のテスト（docs/real_data_repair_design.md §6.2）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.raw_csv = self.root / "defect.csv"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_csv(self, df: pd.DataFrame) -> None:
        df.to_csv(self.raw_csv, index=False, encoding="utf-8-sig")

    def _repair_source(self, columns: list[str]) -> RawSource:
        return _make_source([self.raw_csv], columns, time_column="PB_ON", kind="repair")

    def test_leading_apostrophe_is_stripped_but_embedded_apostrophes_are_kept(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["'HE93S-122065"],
                    "不良No": ["'ab'c"],
                    "修正日": ["2026/07/29"],
                    "修正時間": ["06:41:46"],
                    "PB-ON": ["20260724 010931"],
                }
            )
        )
        source = self._repair_source(["VIN", "不良No", "修正日", "修正時間", "PB_ON"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "repair", "テスト")

        self.assertEqual(df.loc[0, "不良No"], "ab'c")
        self.assertEqual(df.loc[0, "vin"], "HE93S-122065")

    def test_strip_apostrophe_does_not_affect_kinds_without_the_by_kind_config(self):
        self._write_csv(pd.DataFrame({"VIN#": ["'A"], "通過日時": ["2026/07/24 06:00:00"], "値": [1.0]}))
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値"])  # kind=traceability（既定・by_kind 無し）
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "traceability", "テスト")

        self.assertEqual(df.loc[0, "vin_raw"], "'A")

    def test_full_sentinel_column_becomes_nat_without_a_parse_failure_warning(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A", "B"],
                    "修正日": ["2026/07/29", "2026/07/29"],
                    "修正時間": ["06:41:46", "06:46:40"],
                    "PB-ON": ["20260724 010931", "20260724 013337"],
                    "WB-ON": ["00000000 000000", "00000000 000000"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON", "WB_ON"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        with self.assertNoLogs("defect_analysis.raw_convert", level="WARNING"):
            convert_file(self.raw_csv, source, lake_dir, cfg)

        df = read_source(lake_dir, "repair", "テスト")
        self.assertTrue(df["WB_ON"].isna().all())

    def test_correction_date_and_time_are_combined_into_a_new_datetime_column(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A", "B"],
                    "修正日": ["2026/07/29", "2026/07/29"],
                    "修正時間": ["06:41:46", None],
                    "PB-ON": ["20260724 010931", "20260724 013337"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "repair", "テスト").sort_values("vin").reset_index(drop=True)

        self.assertEqual(df.loc[0, "修正日時"], pd.Timestamp("2026-07-29 06:41:46"))
        self.assertTrue(pd.isna(df.loc[1, "修正日時"]))

    def test_pb_on_style_columns_are_parsed_into_datetime_dtype(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A"],
                    "修正日": ["2026/07/29"],
                    "修正時間": ["06:41:46"],
                    "PB-ON": ["20260724 010931"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "repair", "テスト")

        self.assertTrue(pd.api.types.is_datetime64_any_dtype(df["PB_ON"]))
        self.assertEqual(df.loc[0, "PB_ON"], pd.Timestamp("2026-07-24 01:09:31"))

    def test_pii_hash_mode_drops_the_original_column_and_adds_a_deterministic_id_column(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A", "B"],
                    "修正日": ["2026/07/29", "2026/07/29"],
                    "修正時間": ["06:41:46", "06:46:40"],
                    "PB-ON": ["20260724 010931", "20260724 013337"],
                    "修正員": ["山田太郎", "山田太郎"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON", "修正員"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "repair", "テスト")

        self.assertNotIn("修正員", df.columns)
        self.assertIn("修正員_id", df.columns)
        self.assertEqual(df.loc[0, "修正員_id"], df.loc[1, "修正員_id"])

    def test_pii_hash_id_changes_when_a_different_salt_is_used(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A"],
                    "修正日": ["2026/07/29"],
                    "修正時間": ["06:41:46"],
                    "PB-ON": ["20260724 010931"],
                    "修正員": ["山田太郎"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON", "修正員"])
        cfg_a = _make_cfg(self.root, lake_name="lake_a", by_kind=_repair_by_kind(salt="salt-a"))
        cfg_b = _make_cfg(self.root, lake_name="lake_b", by_kind=_repair_by_kind(salt="salt-b"))

        convert_file(self.raw_csv, source, self.root / "lake_a", cfg_a)
        convert_file(self.raw_csv, source, self.root / "lake_b", cfg_b)

        id_a = read_source(self.root / "lake_a", "repair", "テスト").loc[0, "修正員_id"]
        id_b = read_source(self.root / "lake_b", "repair", "テスト").loc[0, "修正員_id"]
        self.assertNotEqual(id_a, id_b)

    def test_pii_drop_mode_removes_the_column_entirely(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A"],
                    "修正日": ["2026/07/29"],
                    "修正時間": ["06:41:46"],
                    "PB-ON": ["20260724 010931"],
                    "修正員": ["山田太郎"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON", "修正員"])
        cfg = _make_cfg(self.root, by_kind=_repair_by_kind(mode="drop"))
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "repair", "テスト")

        self.assertNotIn("修正員", df.columns)
        self.assertNotIn("修正員_id", df.columns)

    def test_pii_keep_mode_preserves_the_column_and_logs_a_warning(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A"],
                    "修正日": ["2026/07/29"],
                    "修正時間": ["06:41:46"],
                    "PB-ON": ["20260724 010931"],
                    "修正員": ["山田太郎"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON", "修正員"])
        cfg = _make_cfg(self.root, by_kind=_repair_by_kind(mode="keep"))
        lake_dir = self.root / "lake"

        with self.assertLogs("defect_analysis.raw_convert", level="WARNING") as cm:
            convert_file(self.raw_csv, source, lake_dir, cfg)
        df = read_source(lake_dir, "repair", "テスト")

        self.assertIn("修正員", df.columns)
        self.assertTrue(any("個人情報列をそのまま保存します" in msg for msg in cm.output))

    def test_date_partition_is_derived_from_pb_on_not_from_correction_date(self):
        self._write_csv(
            pd.DataFrame(
                {
                    "VIN": ["A"],
                    "修正日": ["2026/07/29"],
                    "修正時間": ["06:41:46"],
                    "PB-ON": ["20260724 010931"],
                }
            )
        )
        source = self._repair_source(["VIN", "修正日", "修正時間", "PB_ON"])
        cfg = _make_cfg(self.root)
        lake_dir = self.root / "lake"

        convert_file(self.raw_csv, source, lake_dir, cfg)

        date_dirs = sorted(p.name for p in (lake_dir / "repair" / "テスト").glob("date=*"))
        self.assertEqual(date_dirs, ["date=2026-07-24"])  # PB_ON の日付。修正日(07/29) ではない


class ReadSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.raw_csv = self.root / "in.csv"
        pd.DataFrame(
            {
                "VIN#": ["A", "B"],
                "通過日時": ["2026/07/24 06:00:00", "2026/07/24 07:00:00"],
                "値": [1.0, 2.0],
                "備考": ["x", "y"],
            }
        ).to_csv(self.raw_csv, index=False, encoding="utf-8-sig")
        source = _make_source([self.raw_csv], ["VIN", "通過日時", "値", "備考"])
        cfg = _make_cfg(self.root)
        self.lake_dir = self.root / "lake"
        convert_file(self.raw_csv, source, self.lake_dir, cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_columns_argument_returns_only_the_requested_columns(self):
        df = read_source(self.lake_dir, "traceability", "テスト", columns=["vin", "値"])
        self.assertEqual(list(df.columns), ["vin", "値"])

    def test_nonexistent_source_returns_empty_dataframe_without_error(self):
        df = read_source(self.lake_dir, "traceability", "存在しないソース")
        self.assertTrue(df.empty)


if __name__ == "__main__":
    unittest.main()
