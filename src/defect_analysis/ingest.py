"""ステップ1: 分割保存された CSV 群を自動収集し、統一スキーマで結合する。

設備ごと・月ごとに分かれた traceability / trend、および月ごとの defect / repair を
それぞれ 1 つの統合テーブルにまとめ、interim へ保存する。
異常ファイル（パターン不一致・必須列欠落・読込失敗）はスキップしてログに残す。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from .config import Config
from .io_utils import resolve_format, save_df, table_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    subdir: str
    filename_regex: str
    required_columns: list[str]
    key_columns: list[str]
    date_columns: list[str] = field(default_factory=list)
    column_map: dict[str, str] = field(default_factory=dict)  # 生列名 -> 標準列名（config.ingest.column_maps）


SOURCES: list[SourceSpec] = [
    SourceSpec(
        name="traceability",
        subdir="traceability",
        filename_regex=r"^(?P<equipment_id>EQ-\d+)_(?P<month>\d{4}-\d{2})\.csv$",
        required_columns=[
            "vin", "equipment_id", "process_month", "in_ts", "out_ts",
            "cycle_time_sec", "judgment",
        ],
        key_columns=["vin", "equipment_id"],
        date_columns=["in_ts", "out_ts"],
    ),
    SourceSpec(
        name="trend",
        subdir="trend",
        filename_regex=r"^(?P<equipment_id>EQ-\d+)_(?P<month>\d{4}-\d{2})\.csv$",
        required_columns=["vin", "equipment_id", "process_month"],
        key_columns=["vin", "equipment_id"],
    ),
    SourceSpec(
        name="defect",
        subdir="defect",
        filename_regex=r"^defect_(?P<month>\d{4}-\d{2})\.csv$",
        required_columns=["vin", "defect_id", "defect_date", "defect_category", "severity"],
        key_columns=["defect_id"],
        date_columns=["defect_date"],
    ),
    SourceSpec(
        name="repair",
        subdir="repair",
        filename_regex=r"^repair_(?P<month>\d{4}-\d{2})\.csv$",
        required_columns=["vin", "repair_date", "repair_action"],
        key_columns=["vin"],
        date_columns=["repair_date"],
    ),
]


def _load_source(spec: SourceSpec, raw_dir: Path) -> pd.DataFrame:
    """1 データ種別のディレクトリを走査し、有効な CSV を結合して返す。"""
    directory = raw_dir / spec.subdir
    pattern = re.compile(spec.filename_regex)
    files = sorted(directory.glob("*.csv")) if directory.exists() else []

    frames: list[pd.DataFrame] = []
    skipped = 0
    for path in files:
        if not pattern.match(path.name):
            logger.warning("[%s] 命名規則に不一致のためスキップ: %s", spec.name, path.name)
            skipped += 1
            continue
        try:
            df = pd.read_csv(path)
        except Exception as exc:  # 破損ファイル等
            logger.error("[%s] 読込失敗のためスキップ: %s (%s)", spec.name, path.name, exc)
            skipped += 1
            continue
        if spec.column_map:
            df = df.rename(columns=spec.column_map)
            dup_cols = df.columns[df.columns.duplicated()].unique().tolist()
            if dup_cols:
                logger.warning("[%s] rename 後に列が重複: %s (%s)", spec.name, dup_cols, path.name)
                skipped += 1
                continue
        missing = [c for c in spec.required_columns if c not in df.columns]
        if missing:
            logger.warning("[%s] 必須列欠落のためスキップ: %s (欠落=%s)", spec.name, path.name, missing)
            skipped += 1
            continue
        frames.append(df)

    if not frames:
        logger.warning("[%s] 有効なファイルが見つかりませんでした（dir=%s）", spec.name, directory)
        return pd.DataFrame(columns=spec.required_columns)

    combined = pd.concat(frames, ignore_index=True)
    for col in spec.date_columns:
        if col in combined.columns:
            combined[col] = pd.to_datetime(combined[col], errors="coerce")

    logger.info(
        "[%s] 収集: %d ファイル採用 / %d スキップ -> %d 行", spec.name, len(frames), skipped, len(combined)
    )
    _quality_report(spec, combined)
    return combined


def _quality_report(spec: SourceSpec, df: pd.DataFrame) -> None:
    """キー重複・欠損の品質チェック結果をログに出す。"""
    if df.empty:
        return
    key = [c for c in spec.key_columns if c in df.columns]
    if key:
        dup = int(df.duplicated(subset=key).sum())
        if dup:
            logger.warning("[%s] キー重複 %d 件（key=%s）", spec.name, dup, key)
    na_counts = df.isna().sum()
    na_cols = {c: int(v) for c, v in na_counts.items() if v > 0}
    if na_cols:
        logger.info("[%s] 欠損あり列: %s", spec.name, na_cols)


def _apply_column_maps(sources: list[SourceSpec], cfg: Config) -> list[SourceSpec]:
    """config の ingest.column_maps を各 SourceSpec へ注入した spec 群を返す。"""
    maps = cfg.get("ingest.column_maps", {}) or {}
    return [replace(s, column_map=maps.get(s.name, {}) or {}) for s in sources]


def ingest(cfg: Config) -> dict[str, int]:
    """全ソースを収集・統合し interim へ保存。各ソースの行数を返す。"""
    raw_dir = cfg.path("paths.raw_dir")
    interim_dir = cfg.path("paths.interim_dir", create=True)
    fmt = resolve_format(cfg.get("storage.format", "parquet"))
    sources = _apply_column_maps(SOURCES, cfg)

    row_counts: dict[str, int] = {}
    for spec in sources:
        df = _load_source(spec, raw_dir)
        out = table_path(interim_dir, spec.name, fmt)
        save_df(df, out)
        row_counts[spec.name] = len(df)
    return row_counts
