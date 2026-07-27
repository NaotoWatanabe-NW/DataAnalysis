"""大/中/小カテゴリから統合カテゴリを生成するユーティリティ。

変換ルールは設定ファイル（既定 config/category_map.yaml）で定義する。
rules を上から順に評価し最初に一致した割当を採用、未一致は default で生成する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yaml

from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_MAP_REL = Path("config") / "category_map.yaml"


def load_spec(path: Path) -> dict:
    """変換設定ファイルを読み、category_integration セクションを返す。"""
    if not path.exists():
        raise FileNotFoundError(f"カテゴリ変換設定が見つかりません: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    spec = data.get("category_integration")
    if not spec:
        raise ValueError(f"設定に category_integration セクションがありません: {path}")
    if "source_columns" not in spec:
        raise ValueError("category_integration.source_columns が必要です")
    return spec


def _apply_default(df: pd.DataFrame, src: dict, default: dict) -> pd.Series | str:
    method = default.get("method", "middle")
    if method == "const":
        return default.get("value", "未分類")
    if method in ("major", "middle", "minor"):
        col = src[method]
        return df[col].astype(str)
    if method == "concat":
        levels = default.get("levels", ["major", "middle"])
        sep = str(default.get("separator", "_"))
        cols = [src[lv] for lv in levels if lv in src]
        return df[cols].astype(str).agg(sep.join, axis=1)
    raise ValueError(f"未対応の default.method: {method!r}")


def integrate_categories(df: pd.DataFrame, spec: dict) -> pd.Series:
    """spec に基づき統合カテゴリの Series を返す（df は変更しない）。"""
    src = spec["source_columns"]
    missing = [c for c in src.values() if c not in df.columns]
    if missing:
        raise KeyError(f"入力に必要なカテゴリ列がありません: {missing}")

    result = pd.Series(pd.NA, index=df.index, dtype="object")
    assigned = pd.Series(False, index=df.index)

    for rule in spec.get("rules", []):
        when = rule.get("when", {})
        mask = pd.Series(True, index=df.index)
        for level, allowed in when.items():
            col = src.get(level)
            if col is None or col not in df.columns:
                mask &= False
                continue
            allowed_list = allowed if isinstance(allowed, (list, tuple)) else [allowed]
            mask &= df[col].isin(allowed_list)
        mask &= ~assigned
        result = result.mask(mask, rule["to"])
        assigned |= mask

    unset = ~assigned
    if unset.any():
        result.loc[unset] = _apply_default(df.loc[unset], src, spec.get("default", {"method": "middle"}))
        if spec.get("warn_unmatched"):
            logger.warning(
                "ルール未一致 %d 行に既定生成(%s)を適用しました。",
                int(unset.sum()),
                spec.get("default", {}).get("method", "middle"),
            )
    return result


def run_category_integration(
    cfg: Config,
    input_path: str,
    output_path: str,
    map_path: str | None = None,
) -> dict:
    """入力 CSV に統合カテゴリ列を付与して出力 CSV に書き出す。"""
    root = cfg.root

    map_p = Path(map_path) if map_path else DEFAULT_MAP_REL
    if not map_p.is_absolute():
        map_p = root / map_p
    spec = load_spec(map_p)

    in_p = Path(input_path)
    if not in_p.is_absolute():
        in_p = root / in_p
    df = pd.read_csv(in_p)

    out_col = spec.get("output_column", "統合カテゴリ")
    df[out_col] = integrate_categories(df, spec)

    out_p = Path(output_path)
    if not out_p.is_absolute():
        out_p = root / out_p
    out_p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_p, index=False)

    distribution = {str(k): int(v) for k, v in df[out_col].value_counts().items()}
    logger.info("統合カテゴリ生成: %s -> %s (%d 行, %d 種)", in_p, out_p, len(df), len(distribution))
    logger.info("統合カテゴリ分布: %s", distribution)
    return {"n_rows": len(df), "output": str(out_p), "distribution": distribution}
