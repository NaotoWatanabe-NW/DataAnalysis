"""カテゴリ列から統合カテゴリを生成するユーティリティ。

変換ルールは CSV の1対1マッピング表（既定 config/category_map.csv、`value,category` の2列）で
定義する。表に無い値は元の値をそのまま通し、WARNING で未一致の値と件数を報告する
（データを失わないことを優先する。docs/category_csv_and_custom_charts_design.md 参照）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_MAP_REL = Path("config") / "category_map.csv"
DEFAULT_OUTPUT_COLUMN = "統合カテゴリ"


def load_mapping(path: Path) -> dict[str, str]:
    """マッピング CSV を読み {value: category} を返す。検証エラーは ValueError。"""
    if not path.exists():
        raise FileNotFoundError(f"カテゴリ変換表が見つかりません: {path}")

    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", comment="#", keep_default_na=False)

    if list(df.columns) != ["value", "category"]:
        raise ValueError(
            f"カテゴリ変換表のヘッダは value,category である必要があります: {path} "
            f"(実際: {list(df.columns)})"
        )

    empty_mask = (df["value"] == "") | (df["category"] == "")
    if empty_mask.any():
        n_bad = int(empty_mask.sum())
        raise ValueError(f"カテゴリ変換表に空セルがある行が {n_bad} 件あります: {path}")

    dup = df["value"][df["value"].duplicated()].unique().tolist()
    if dup:
        raise ValueError(f"カテゴリ変換表の value が重複しています: {sorted(dup)}: {path}")

    return dict(zip(df["value"], df["category"]))


def apply_category_mapping(
    values: pd.Series, mapping: dict[str, str]
) -> tuple[pd.Series, dict[str, int]]:
    """写像後の Series と、未一致だった値→件数の dict を返す（ログは出さない純粋関数）。"""
    keys = values.astype("string").str.strip()
    mapped = keys.map(mapping)

    unmatched_mask = mapped.isna() & keys.notna()
    result = mapped.where(~unmatched_mask, keys)

    unmatched_counts = keys[unmatched_mask].value_counts()
    # 件数降順、値昇順で決定的に並べる。
    unmatched_counts = unmatched_counts.sort_index().sort_values(ascending=False, kind="stable")
    unmatched_values = {str(k): int(v) for k, v in unmatched_counts.items()}

    return result.astype("object"), unmatched_values


def run_category_integration(
    cfg: Config,
    input_path: str,
    output_path: str,
    *,
    source_column: str,
    output_column: str = DEFAULT_OUTPUT_COLUMN,
    map_path: str | None = None,
) -> dict:
    """入力 CSV に統合カテゴリ列を付与して出力 CSV に書き出す。"""
    root = cfg.root

    map_p = Path(map_path) if map_path else DEFAULT_MAP_REL
    if not map_p.is_absolute():
        map_p = root / map_p
    mapping = load_mapping(map_p)

    in_p = Path(input_path)
    if not in_p.is_absolute():
        in_p = root / in_p
    df = pd.read_csv(in_p)

    if source_column not in df.columns:
        raise KeyError(
            f"入力に写像元の列がありません: {source_column!r} "
            f"(実在列: {list(df.columns)})"
        )

    if output_column in df.columns:
        logger.warning("出力列 %s は既に存在するため上書きします", output_column)

    mapped, unmatched_values = apply_category_mapping(df[source_column], mapping)
    df[output_column] = mapped

    n_missing_source = int(df[source_column].isna().sum())

    out_p = Path(output_path)
    if not out_p.is_absolute():
        out_p = root / out_p
    out_p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_p, index=False)

    distribution = {str(k): int(v) for k, v in df[output_column].value_counts().items()}
    logger.info(
        "統合カテゴリ生成: %s -> %s (%d 行, %d 種)", in_p, out_p, len(df), len(distribution)
    )
    logger.info("統合カテゴリ分布: %s", distribution)
    logger.info("写像元が欠損の行: %d 行（NaN のまま出力）", n_missing_source)

    if unmatched_values:
        n_unmatched_rows = sum(unmatched_values.values())
        top = list(unmatched_values.items())[:20]
        top_text = ", ".join(f"{v}={c}" for v, c in top)
        suffix = f", 他 {len(unmatched_values) - 20} 種" if len(unmatched_values) > 20 else ""
        logger.warning(
            "マッピング表に無い値 %d 種 / %d 行を元の値のまま出力しました: %s%s",
            len(unmatched_values), n_unmatched_rows, top_text, suffix,
        )

    return {
        "n_rows": len(df),
        "output": str(out_p),
        "distribution": distribution,
        "n_unmatched": sum(unmatched_values.values()),
        "unmatched_values": unmatched_values,
    }
