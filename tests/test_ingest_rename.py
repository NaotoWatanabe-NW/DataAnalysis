"""ingest.SourceSpec.column_map による列名リネームのテスト（_load_source / _apply_column_maps）。

実行:
    .venv/bin/python -m pytest tests/test_ingest_rename.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis.config import Config  # noqa: E402
from defect_analysis.ingest import SourceSpec, _apply_column_maps, _load_source  # noqa: E402


def _spec(column_map: dict | None = None) -> SourceSpec:
    return SourceSpec(
        name="traceability",
        subdir="traceability",
        filename_regex=r"^(?P<equipment_id>EQ-\d+)_(?P<month>\d{4}-\d{2})\.csv$",
        required_columns=["vin", "equipment_id", "process_month", "in_ts", "out_ts"],
        key_columns=["vin", "equipment_id"],
        date_columns=["in_ts", "out_ts"],
        column_map=column_map or {},
    )


class LoadSourceColumnMapTest(unittest.TestCase):
    def test_rename_via_column_map_lets_raw_column_names_satisfy_required_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            sub = raw / "traceability"
            sub.mkdir()
            pd.DataFrame(
                {
                    "車台番号": ["A"],
                    "設備ID": ["EQ-01"],
                    "process_month": ["2026-01"],
                    "投入時刻": ["2026-01-01 10:00:00"],
                    "払出時刻": ["2026-01-01 10:00:30"],
                }
            ).to_csv(sub / "EQ-01_2026-01.csv", index=False)

            spec = _spec({"車台番号": "vin", "設備ID": "equipment_id", "投入時刻": "in_ts", "払出時刻": "out_ts"})
            out = _load_source(spec, raw)

            self.assertEqual(len(out), 1)
            self.assertEqual(out.iloc[0]["vin"], "A")
            self.assertEqual(out.iloc[0]["equipment_id"], "EQ-01")
            self.assertTrue(pd.api.types.is_datetime64_any_dtype(out["in_ts"]))

    def test_non_standard_column_names_without_map_are_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            sub = raw / "traceability"
            sub.mkdir()
            pd.DataFrame(
                {
                    "車台番号": ["A"],
                    "設備ID": ["EQ-01"],
                    "process_month": ["2026-01"],
                    "投入時刻": ["2026-01-01 10:00:00"],
                    "払出時刻": ["2026-01-01 10:00:30"],
                }
            ).to_csv(sub / "EQ-01_2026-01.csv", index=False)

            spec = _spec()  # column_map 無し = 生列名のまま必須列に届かない
            with self.assertLogs("defect_analysis.ingest", level="WARNING") as cm:
                out = _load_source(spec, raw)

            self.assertEqual(len(out), 0)  # 従来どおり必須列欠落でスキップ
            self.assertTrue(any("必須列欠落" in msg for msg in cm.output))

    def test_column_map_collision_produces_warning_and_skips_file_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            sub = raw / "traceability"
            sub.mkdir()
            pd.DataFrame(
                {
                    "vin_old": ["A"],
                    "vin_new": ["A2"],
                    "equipment_id": ["EQ-01"],
                    "process_month": ["2026-01"],
                    "in_ts": ["2026-01-01 10:00:00"],
                    "out_ts": ["2026-01-01 10:00:30"],
                }
            ).to_csv(sub / "EQ-01_2026-01.csv", index=False)

            # vin_old / vin_new が両方とも canonical "vin" へマップされ、rename後に列が重複する
            spec = _spec({"vin_old": "vin", "vin_new": "vin"})
            with self.assertLogs("defect_analysis.ingest", level="WARNING") as cm:
                out = _load_source(spec, raw)

            self.assertEqual(len(out), 0)  # 安全側でスキップされ、採用されない
            self.assertTrue(any("重複" in msg for msg in cm.output))


class ApplyColumnMapsTest(unittest.TestCase):
    def test_injects_column_map_from_config_matching_source_name(self):
        cfg = Config({"ingest": {"column_maps": {"traceability": {"車台番号": "vin"}}}}, root=Path("/tmp"))
        sources = [
            SourceSpec(
                name="traceability", subdir="traceability", filename_regex=r".*",
                required_columns=[], key_columns=[],
            ),
            SourceSpec(
                name="trend", subdir="trend", filename_regex=r".*",
                required_columns=[], key_columns=[],
            ),
        ]
        mapped = _apply_column_maps(sources, cfg)
        by_name = {s.name: s for s in mapped}
        self.assertEqual(by_name["traceability"].column_map, {"車台番号": "vin"})
        self.assertEqual(by_name["trend"].column_map, {})  # 未指定ソースは空マップのまま


if __name__ == "__main__":
    unittest.main()
