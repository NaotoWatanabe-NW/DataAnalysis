"""カテゴリ列から統合カテゴリを生成するユーティリティ。

変換ルールは CSV の1対1マッピング表（既定 config/category_map.csv、`value,category` の2列）で
定義する。表に無い値は元の値をそのまま通し、WARNING で未一致の値と件数を報告する
（データを失わないことを優先する。docs/category_csv_and_custom_charts_design.md 参照）。

本モジュールには 1 キー写像（CLI `category` サブコマンド。上記）と、4 キー厳密一致による
複合キー写像（repair の統合カテゴリ。`docs/repair_integrated_category_design.md`）の 2 系統がある。
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


# ---------------------------------------------------------------------
# 4 キー厳密一致による複合キー写像（repair の統合カテゴリ）
# docs/repair_integrated_category_design.md §2/§3.1
# ---------------------------------------------------------------------

DEFAULT_CATEGORY_TABLE_REL = Path("config") / "塗装課内不良対比表_まとめ.csv"
DEFAULT_CATEGORY_KEY_COLUMNS: dict[str, str] = {   # 表側の列 -> repair 側の列
    "作業工程": "入力工程", "大分類": "大分類", "中分類": "中分類", "小分類": "小分類",
}
DEFAULT_CATEGORY_VALUE_COLUMN = "グラフ項目"
DEFAULT_CATEGORY_EXCLUDED_VALUES = ("-",)
DEFAULT_CATEGORY_LABELS = {
    "out_of_scope_process": "対象外工程",
    "unmatched": "未分類",
    "excluded": "グラフ対象外",
}

_KEY_SEP = "\x1f"


@dataclass(frozen=True)
class CompositeCategoryTable:
    """4 キー → 統合カテゴリの写像表（読み込み済み・検証済み）。"""

    mapping: dict[tuple[str, ...], str]   # キー tuple（table_key_columns の順）-> 値（NFKC 済み）
    scope_values: frozenset[str]          # 第1キー（作業工程）の値集合
    table_key_columns: tuple[str, ...]    # 表側のキー列名（順序固定）
    n_rows: int                           # 重複除去後の行数
    n_exact_duplicates: int               # 除去した完全重複行数
    conflicts: dict[tuple[str, ...], tuple[str, ...]]   # 競合キー -> 候補値（昇順・採用値が先頭）


def load_composite_category_table(
    path: Path,
    *,
    key_columns: Sequence[str] = tuple(DEFAULT_CATEGORY_KEY_COLUMNS),
    value_column: str = DEFAULT_CATEGORY_VALUE_COLUMN,
    on_duplicate_key: str = "first",
) -> CompositeCategoryTable:
    """対比表 CSV を読み検証する。§2.1/§2.2。ログは出さない（結果は戻り値から判定できる）。"""
    if not path.exists():
        raise FileNotFoundError(f"対比表が見つかりません: {path}")

    key_columns = tuple(key_columns)
    if on_duplicate_key not in ("first", "error"):
        raise ValueError(f"on_duplicate_key は 'first' か 'error' のみ対応: {on_duplicate_key!r}")

    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)

    missing_columns = [c for c in (*key_columns, value_column) if c not in df.columns]
    if missing_columns:
        raise ValueError(
            f"対比表に指定した列がありません: {missing_columns} "
            f"(path={path}, 実在列: {list(df.columns)})"
        )

    work = df[[*key_columns, value_column]].copy()
    empty_mask = (work == "").any(axis=1)
    if empty_mask.any():
        n_bad = int(empty_mask.sum())
        raise ValueError(f"対比表に空セルがある行が {n_bad} 件あります: {path}")

    # IC4: キーは str 化 + 前後 strip のみ（全半角・大小文字の正規化はしない）。
    for col in key_columns:
        work[col] = work[col].astype(str).str.strip()
    # IC5: 値（グラフ項目）だけ NFKC 正規化する。
    work[value_column] = work[value_column].map(lambda v: unicodedata.normalize("NFKC", str(v)))

    n_before = len(work)
    deduped = work.drop_duplicates().reset_index(drop=True)
    n_exact_duplicates = n_before - len(deduped)

    deduped = deduped.assign(_key=list(zip(*(deduped[c] for c in key_columns))))
    grouped = deduped.groupby("_key")[value_column].apply(lambda s: tuple(sorted(set(s))))

    conflicts: dict[tuple[str, ...], tuple[str, ...]] = {
        key: values for key, values in grouped.items() if len(values) > 1
    }

    if conflicts and on_duplicate_key == "error":
        detail = "; ".join(f"{key} -> {values}" for key, values in sorted(conflicts.items()))
        raise ValueError(
            f"対比表の4キーが競合しています（on_duplicate_key=error）: {detail} (path={path})"
        )

    mapping: dict[tuple[str, ...], str] = {key: values[0] for key, values in grouped.items()}
    scope_values = frozenset(deduped[key_columns[0]].unique())

    return CompositeCategoryTable(
        mapping=mapping,
        scope_values=scope_values,
        table_key_columns=key_columns,
        n_rows=len(deduped),
        n_exact_duplicates=n_exact_duplicates,
        conflicts=conflicts,
    )


def apply_composite_category(
    df: pd.DataFrame,
    table: CompositeCategoryTable,
    *,
    source_key_columns: Sequence[str],          # repair 側の列名（table_key_columns と同順）
    excluded_values: Sequence[str] = DEFAULT_CATEGORY_EXCLUDED_VALUES,
    labels: Mapping[str, str] = DEFAULT_CATEGORY_LABELS,
) -> pd.Series:
    """§2.3 の規則で統合カテゴリ Series（df と同じ index / 全行非 NaN / dtype=object）を返す純粋関数。"""
    source_key_columns = tuple(source_key_columns)

    # IC4: キーは str 化 + 前後 strip のみ。欠損は突合前にマスクし "nan" 文字列を作らない。
    keys_str = [df[c].astype("string").str.strip() for c in source_key_columns]

    composite = keys_str[0]
    for s in keys_str[1:]:
        composite = composite.str.cat(s, sep=_KEY_SEP)

    key_lookup = {_KEY_SEP.join(k): v for k, v in table.mapping.items()}
    mapped = composite.map(key_lookup)

    scope_series = keys_str[0]
    in_scope = scope_series.notna() & scope_series.isin(table.scope_values)

    excluded_set = set(excluded_values)

    result = pd.Series(labels["unmatched"], index=df.index, dtype="object")
    result[~in_scope] = labels["out_of_scope_process"]

    matched_mask = in_scope & mapped.notna()
    result[matched_mask] = mapped[matched_mask]

    excluded_mask = matched_mask & mapped.isin(excluded_set)
    result[excluded_mask] = labels["excluded"]

    return result.astype("object")


def summarize_unmatched_keys(
    df: pd.DataFrame,
    category: pd.Series,
    *,
    source_key_columns: Sequence[str],
    labels: Mapping[str, str] = DEFAULT_CATEGORY_LABELS,
    vin_column: str | None = "vin",
) -> pd.DataFrame:
    """未一致（`未分類` / `対象外工程`）の 4 キー組合せ別サマリを返す純粋関数。

    列: 区分, {source_key_columns...}, n_rows, n_vin（vin_column が df に無ければ n_vin を出さない）。
    並び: 区分（未分類 → 対象外工程）→ n_rows 降順 → キー昇順（決定的）。
    """
    source_key_columns = tuple(source_key_columns)
    unmatched_label = labels["unmatched"]
    out_of_scope_label = labels["out_of_scope_process"]
    mask = category.isin((unmatched_label, out_of_scope_label))

    has_vin = vin_column is not None and vin_column in df.columns
    columns = ["区分", *source_key_columns, "n_rows"] + (["n_vin"] if has_vin else [])

    if not mask.any():
        return pd.DataFrame(columns=columns)

    sub = df.loc[mask, list(source_key_columns)].copy()
    sub["区分"] = category[mask].to_numpy()
    group_cols = ["区分", *source_key_columns]

    counts = sub.groupby(group_cols, dropna=False).size().rename("n_rows")
    if has_vin:
        sub["vin"] = df.loc[mask, vin_column].to_numpy()
        n_vin = sub.groupby(group_cols, dropna=False)["vin"].nunique().rename("n_vin")
        agg = pd.concat([counts, n_vin], axis=1).reset_index()
    else:
        agg = counts.reset_index()

    order_priority = {unmatched_label: 0, out_of_scope_label: 1}
    agg["_order"] = agg["区分"].map(order_priority)
    agg = agg.sort_values(
        by=["_order", "n_rows", *source_key_columns],
        ascending=[True, False, *([True] * len(source_key_columns))],
        kind="stable",
    ).drop(columns="_order").reset_index(drop=True)

    return agg[columns]
