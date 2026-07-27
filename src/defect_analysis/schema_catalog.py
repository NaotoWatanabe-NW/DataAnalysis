"""CSV データのスキーマを YAML カタログに記録するユーティリティ。

各 CSV ファイルについて、列名・データ型（論理型/pandas型）・元ファイル名・
欠損数・ユニーク数・値の例を収集し、1 つの YAML にまとめて出力する。
実データ受領時の項目定義の突合や、パイプライン入力仕様の可視化に使う。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .config import Config

logger = logging.getLogger(__name__)

_WILDCARDS = set("*?[]")


def _to_native(value: Any) -> Any:
    """numpy / pandas スカラを YAML 直列化可能な素の Python 値へ変換する。"""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy スカラ
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def _logical_type(s: pd.Series, datetime_ratio: float) -> str:
    """pandas dtype と中身から、扱いやすい論理型を推定する。"""
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_float_dtype(s):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    non_null = s.dropna()
    if not non_null.empty:
        parsed = pd.to_datetime(non_null, errors="coerce", format="mixed")
        if parsed.notna().mean() >= datetime_ratio:
            return "datetime"
    return "string"


def profile_csv(path: Path, root: Path, datetime_ratio: float = 0.9) -> dict:
    """1 つの CSV ファイルの列スキーマを辞書化する。"""
    df = pd.read_csv(path)
    try:
        display_path = str(path.relative_to(root))
    except ValueError:
        display_path = str(path)

    columns = []
    for col in df.columns:
        s = df[col]
        non_null = s.dropna()
        columns.append(
            {
                "name": str(col),
                "logical_type": _logical_type(s, datetime_ratio),
                "pandas_dtype": str(s.dtype),
                "n_missing": int(s.isna().sum()),
                "n_unique": int(s.nunique(dropna=True)),
                "example": _to_native(non_null.iloc[0]) if not non_null.empty else None,
            }
        )
    return {
        "file": display_path,
        "source_name": path.parent.name,  # 元データの区分（親ディレクトリ名）
        "n_rows": int(len(df)),
        "n_columns": int(df.shape[1]),
        "columns": columns,
    }


def _resolve_files(root: Path, pattern: str) -> list[Path]:
    """glob / ディレクトリ / 単一ファイルのいずれの指定でも CSV 一覧に解決する。"""
    p = Path(pattern)
    if p.is_absolute():
        if p.is_file():
            return [p]
        if p.is_dir():
            return sorted(p.rglob("*.csv"))
        anchor = Path(p.anchor)
        return sorted(anchor.glob(str(p.relative_to(anchor))))
    if _WILDCARDS & set(pattern):
        return sorted(root.glob(pattern))
    candidate = root / pattern
    if candidate.is_file():
        return [candidate]
    if candidate.is_dir():
        return sorted(candidate.rglob("*.csv"))
    return sorted(root.glob(pattern))


def build_catalog(cfg: Config, input_glob: str | None = None, output_path: str | None = None) -> dict:
    """設定または引数に基づき CSV 群を走査し、スキーマカタログ YAML を出力する。"""
    root = cfg.root
    pattern = input_glob or cfg.get("catalog.input_glob", "data/raw/**/*.csv")
    datetime_ratio = float(cfg.get("catalog.datetime_detect_ratio", 0.9))

    out_str = output_path or cfg.get("catalog.output_path", "reports/data_catalog.yaml")
    out = Path(out_str)
    if not out.is_absolute():
        out = root / out

    files = [f for f in _resolve_files(root, pattern) if f.is_file()]
    if not files:
        logger.warning("カタログ対象の CSV が見つかりません（pattern=%s）", pattern)

    entries = []
    for f in files:
        try:
            entries.append(profile_csv(f, root, datetime_ratio))
        except Exception as exc:
            logger.error("プロファイル失敗のためスキップ: %s (%s)", f, exc)

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_glob": pattern,
        "n_files": len(entries),
        "files": entries,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(catalog, fh, allow_unicode=True, sort_keys=False)

    logger.info("データカタログ出力: %s (%d ファイル)", out, len(entries))
    return {"n_files": len(entries), "output": str(out)}
