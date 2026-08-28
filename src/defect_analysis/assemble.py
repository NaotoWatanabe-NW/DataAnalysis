"""VIN パネル組立（レイク → `data/interim/vin_panel.parquet`）。

`data/lake/` の Parquet レイクから、traceability/defect をソース別に集約・VIN で横結合し、
trend を時刻結合してパネルを作る。業務判断（除外・集約・窓幅・アンカー）はすべてここと
config に閉じる（`docs/real_data_ingest_design.md` §7 責務境界）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .naming import normalize_name, prefixed
from .raw_convert import convert_config, read_source, should_keep_float64
from .raw_sources import RawSource, discover_sources
from .vin_key import join_key, policy_from_config, split_vin_base_pass_no

logger = logging.getLogger(__name__)

DEFAULT_RAW_DIR = "data/raw"
DEFAULT_LAKE_DIR = "data/lake"
DEFAULT_PANEL_PATH = "data/interim/vin_panel.parquet"

DEFAULT_DEFECT = {
    "size_column": "不良サイズ",
    "by_size_bin": False,
    "size_bin_width": 0.1,
    "size_bin_min": 0.0,
    "size_bin_max": 2.0,
}
DEFAULT_REPAIR = {
    "time_column": "修正日時",
    "production_time_column": "PB_ON",
    "workload_column": "修正工数",
    "category_columns": {
        "大分類": True,
        "中分類": False,
        "小分類": False,
        "部品名": False,
        "修正内容": False,
        "部位": False,
    },
    "worker_column": "修正員_id",
    "max_category_columns": 30,
}
DEFAULT_TREND = {
    "enabled": True,
    "include_suffixes": ["測定値", "計算値"],
    "include_columns": [],
    "exclude_columns": [],
    "mode": "window",
    "window_minutes": 11,
    "tolerance_minutes": 5,
    "aggs": ["mean"],
    "anchor_map": {},
    "fallback_anchor_source": "ブース",
    "on_no_overlap": "warn_empty",
    "min_match_rate": 0.5,
}
DEFAULT_ASSEMBLE = {
    "date_from": None,
    "date_to": None,
    "require_sources": [],
    "max_columns_per_source": 200,
}

_VIN_FORMAT_TYPE_RE = re.compile(r"^[A-Z0-9]{5}-\d{6}$")
_VIN_FORMAT_FULL17_RE = re.compile(r"^[A-Z0-9]{17}$")
_DROP_ALWAYS = ("vin_raw", "vin_base", "vin_pass_no", "vin_is_dummy", "__source", "date")

TREND_JOIN_REPORT_COLUMNS = [
    "anchor_source", "anchor_column", "n_vin", "n_matched", "match_rate",
    "trend_min_ts", "trend_max_ts", "anchor_min_ts", "anchor_max_ts",
]
TREND_ANCHOR_MAP_COLUMNS = ["trend_token", "anchor_source", "anchor_column", "resolution"]


def _traceability_defect_read_columns(source: RawSource) -> list[str]:
    """convert 済み parquet の列から、直後に破棄される vin_raw/__source/date を除いた列名を返す（Low-1）。

    convert_file の列構成規則（VIN 原文列は vin_raw にリネームし、vin/vin_base/
    vin_pass_no/vin_is_dummy を付与する）に対応する。
    """
    base_cols = [c for c in source.columns if c != source.vin_column]
    return [*base_cols, "vin", "vin_base", "vin_pass_no", "vin_is_dummy"]


def _defect_config(cfg: Config) -> dict:
    merged = dict(DEFAULT_DEFECT)
    merged.update(cfg.get("real_ingest.defect", {}) or {})
    return merged


def _repair_config(cfg: Config) -> dict:
    merged = dict(DEFAULT_REPAIR)
    merged.update(cfg.get("real_ingest.repair", {}) or {})
    return merged


def _trend_config(cfg: Config) -> dict:
    merged = dict(DEFAULT_TREND)
    merged.update(cfg.get("real_ingest.trend", {}) or {})
    return merged


def _assemble_config(cfg: Config) -> dict:
    merged = dict(DEFAULT_ASSEMBLE)
    merged.update(cfg.get("real_ingest.assemble", {}) or {})
    return merged


def _write_report(reports_dir: Path, filename: str, df: pd.DataFrame) -> Path:
    path = reports_dir / filename
    df.to_csv(path, index=False)
    logger.info("レポートを出力: %s (%d 行)", path, len(df))
    return path


def _classify_vin_format(vin_base: object) -> str:
    if not isinstance(vin_base, str):
        return "other"
    if _VIN_FORMAT_TYPE_RE.match(vin_base):
        return "型式"
    if _VIN_FORMAT_FULL17_RE.match(vin_base):
        return "full17"
    return "other"


# ---------------------------------------------------------------------
# §8.1 ソース別フレームの構築
# ---------------------------------------------------------------------

def prepare_single_row_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """1行/VIN ソースを `{source}__{col}` にプレフィクスする。"""
    drop_cols = [c for c in _DROP_ALWAYS if c in df.columns]
    work = df.drop(columns=drop_cols)
    if work["vin"].duplicated().any():
        n_dup = int(work["vin"].duplicated().sum())
        logger.warning("[traceability/%s] vin 重複 %d 件を検出。先頭行を採用します。", source, n_dup)
        work = work.drop_duplicates(subset="vin", keep="first")
    rename_map = {c: prefixed(source, c) for c in work.columns if c != "vin"}
    return work.rename(columns=rename_map)


def prepare_multi_row_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """複数行/VIN ソースを `{source}__n_rows`（行数）1 列だけに集約する（D5・§8.1.2）。

    数値列・日時列・文字列列に対する統計量集約、設備別 pivot は一切行わない
    （2026-08-28 改定。したがって `time_column` もパネルに残らず、複数行/VIN ソースは
    trend アンカーになり得ない。§8.4(2)）。
    """
    work = df[["vin"]]
    n_rows = work.groupby("vin").size().rename(prefixed(source, "n_rows"))
    return n_rows.reset_index()


def _decimal_places(width: float) -> int:
    """`width` の小数桁数を返す（`size_bin_labels` の固定小数点表記に使う）。"""
    text = f"{width:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".")[-1])


def size_bin_labels(bin_min: float, bin_max: float, bin_width: float) -> list[str]:
    """ビン列のラベル（正規化前）を昇順で返す。データを見ずに config だけで決まる（§8.1.3-A）。

    戻り値の先頭は下限未満ビン（`{bin_min}未満`）、末尾は上限以上ビン（`{bin_max}以上`）。
    浮動小数の丸め誤差を避けるため、境界は `bin_width` の小数桁数から決めた整数インデックスで
    生成し、最後に固定小数点表記に戻す。
    """
    if bin_width <= 0:
        raise ValueError(f"size_bin_width は正の値である必要があります: {bin_width}")
    if bin_min >= bin_max:
        raise ValueError(f"size_bin_min は size_bin_max 未満である必要があります: min={bin_min}, max={bin_max}")

    decimals = _decimal_places(bin_width)
    scale = 10 ** decimals
    min_idx = round(bin_min * scale)
    max_idx = round(bin_max * scale)
    width_idx = round(bin_width * scale)

    if (max_idx - min_idx) % width_idx != 0:
        raise ValueError(
            "size_bin_width が範囲を割り切れません: "
            f"(size_bin_max - size_bin_min)={bin_max - bin_min} は "
            f"size_bin_width={bin_width} の整数倍ではありません"
        )

    labels = [f"{bin_min:.{decimals}f}未満"]
    idx = min_idx
    while idx < max_idx:
        lo = idx / scale
        hi = (idx + width_idx) / scale
        labels.append(f"{lo:.{decimals}f}-{hi:.{decimals}f}")
        idx += width_idx
    labels.append(f"{bin_max:.{decimals}f}以上")
    return labels


def prepare_defect_size_bins(
    size_numeric: pd.Series, vin: pd.Series, prefix: str, cfg_defect: dict
) -> pd.DataFrame:
    """VIN × ビンのカウント表を返す（§8.1.3-A）。

    `prepare_defect_source()` から呼び、`{P}__has` の表に `result.join(..., how="left")` で連結する。
    集計対象は非 NaN の `size_numeric` のみ。そのソースに登場する VIN については該当が無いビンにも
    0 を入れる（台帳結合後の 0 埋めとは別。§13-7 の対象外）。
    """
    bin_min = float(cfg_defect["size_bin_min"])
    bin_max = float(cfg_defect["size_bin_max"])
    bin_width = float(cfg_defect["size_bin_width"])
    labels = size_bin_labels(bin_min, bin_max, bin_width)

    decimals = _decimal_places(bin_width)
    scale = 10 ** decimals
    min_idx = round(bin_min * scale)
    max_idx = round(bin_max * scale)
    width_idx = round(bin_width * scale)

    edges: list[float] = [-np.inf, bin_min]
    idx = min_idx
    while idx < max_idx:
        idx += width_idx
        edges.append(idx / scale)
    edges.append(np.inf)

    valid_mask = size_numeric.notna()
    binned = pd.cut(size_numeric[valid_mask], bins=edges, right=False, labels=labels)
    ct = pd.crosstab(vin[valid_mask], binned, dropna=False)
    # そのソースに登場する VIN は、不良サイズが全行 NaN でもビン列を持つ（0 埋め）。
    # crosstab の index は valid_mask で絞った VIN に限られるため、全 VIN で reindex する。
    all_vins = pd.Index(vin.unique(), name="vin")
    ct = ct.reindex(index=all_vins, columns=labels, fill_value=0)
    ct.columns = [prefixed(prefix, f"size_bin__{normalize_name(label)}") for label in ct.columns]
    return ct


def prepare_defect_source(df: pd.DataFrame, source: str, cfg: Config) -> pd.DataFrame:
    """defect ソースを VIN 単位に集約する。出力列は `{P}__has` のみ（P = `defect_{source}`。D9・D11）。

    サイズ分布が必要な場合のみ `by_size_bin` で `{P}__size_bin__*` を追加する（§8.1.3-A）。
    config 検証（幅・範囲・列数上限）はデータに対する処理を始める前に行う。
    """
    defect_cfg = _defect_config(cfg)
    prefix = f"defect_{source}"

    if defect_cfg["by_size_bin"]:
        bin_min = float(defect_cfg["size_bin_min"])
        bin_max = float(defect_cfg["size_bin_max"])
        bin_width = float(defect_cfg["size_bin_width"])
        labels = size_bin_labels(bin_min, bin_max, bin_width)  # 幅・範囲の設定ミスもここで検知する
        max_columns = int(_assemble_config(cfg)["max_columns_per_source"])
        if len(labels) > max_columns:
            raise ValueError(
                f"[defect/{source}] size_bin の列数が上限を超えました: "
                f"{len(labels)} > max_columns_per_source={max_columns}"
            )

    drop_cols = [c for c in _DROP_ALWAYS if c in df.columns]
    work = df.drop(columns=drop_cols)

    grouped = work.groupby("vin")
    has = pd.Series(1, index=grouped.size().index, name=f"{prefix}__has")
    result = has.to_frame()

    if defect_cfg["by_size_bin"]:
        size_col = defect_cfg["size_column"]
        size_numeric = pd.to_numeric(work[size_col], errors="coerce")
        bins = prepare_defect_size_bins(size_numeric, work["vin"], prefix, defect_cfg)
        result = result.join(bins, how="left")

    return result.reset_index()


def prepare_repair_source(df: pd.DataFrame, source: str, cfg: Config) -> pd.DataFrame:
    """repair ソースを VIN 単位に集約する。列は必ず `repair_` で始める（R4）。

    raw 列を機械的に集約せず、下記のホワイトリスト列のみを生成する
    （docs/real_data_repair_design.md §3.3）:
        __count / __has / __first_ts / __last_ts / __{production_time_column}__min /
        __lead_time_h / __工数_sum / __工数_max / __n_{category_col} / __top_{category_col} /
        __{category_col}__{正規化値}（category_columns で true の列のみ）/
        __{worker_column}__nunique
    """
    repair_cfg = _repair_config(cfg)
    prefix = f"repair_{source}"

    time_col = repair_cfg["time_column"]
    production_col = repair_cfg["production_time_column"]
    workload_col = repair_cfg["workload_column"]
    worker_col = repair_cfg["worker_column"]
    category_columns = repair_cfg["category_columns"]
    max_category_columns = int(repair_cfg["max_category_columns"])

    grouped = df.groupby("vin")
    result = grouped.size().rename(f"{prefix}__count").to_frame()
    result[f"{prefix}__has"] = 1

    if time_col in df.columns:
        ts_stats = grouped[time_col].agg(["min", "max"])
        ts_stats.columns = [f"{prefix}__first_ts", f"{prefix}__last_ts"]
        result = result.join(ts_stats)

    if production_col in df.columns:
        prod_min = grouped[production_col].min().rename(f"{prefix}__{production_col}__min")
        result = result.join(prod_min)

    if time_col in df.columns and production_col in df.columns:
        first_ts = result[f"{prefix}__first_ts"]
        prod_ts = result[f"{prefix}__{production_col}__min"]
        both_valid = first_ts.notna() & prod_ts.notna()
        lead_time_h = (first_ts - prod_ts).dt.total_seconds() / 3600.0
        result[f"{prefix}__lead_time_h"] = lead_time_h.where(both_valid)

    if workload_col in df.columns:
        workload_numeric = pd.to_numeric(df[workload_col], errors="coerce")
        workload_stats = workload_numeric.groupby(df["vin"]).agg(["sum", "max"])
        workload_stats.columns = [f"{prefix}__工数_sum", f"{prefix}__工数_max"]
        result = result.join(workload_stats)
        if workload_numeric.fillna(0).eq(0).all():
            logger.warning(
                "[repair/%s] %s が全て 0 です（入力員が未入力のため。ユーザー確認済み・2026-07-31）。"
                "実測値ではなく未入力を表すため、この期間では特徴量として使わないこと。",
                source, workload_col,
            )

    n_category_columns = 0
    for col, enabled in category_columns.items():
        if col not in df.columns:
            continue
        result[f"{prefix}__n_{col}"] = grouped[col].nunique()
        result[f"{prefix}__top_{col}"] = grouped[col].agg(_top_value)
        if enabled:
            normalized_vals = df[col].map(lambda v: normalize_name(str(v)) if pd.notna(v) else v)
            ct = pd.crosstab(df["vin"], normalized_vals)
            ct.columns = [f"{prefix}__{col}__{v}" for v in ct.columns]
            n_category_columns += ct.shape[1]
            if n_category_columns > max_category_columns:
                raise ValueError(
                    f"[repair/{source}] カウント展開列が上限を超えました: "
                    f"{n_category_columns} > {max_category_columns}"
                )
            result = result.join(ct, how="left")
            for c in ct.columns:
                result[c] = result[c].fillna(0).astype(int)

    if worker_col in df.columns:
        result[f"{prefix}__{worker_col}__nunique"] = grouped[worker_col].nunique()

    return result.reset_index()


def _top_value(s: pd.Series) -> object:
    counts = s.value_counts()
    return counts.index[0] if len(counts) else pd.NA


# ---------------------------------------------------------------------
# §8.2 VIN 台帳
# ---------------------------------------------------------------------

def build_vin_ledger(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """全ソース（traceability + defect）の vin の和集合を台帳とする。

    `present__{source}` は traceability ソースにのみ付与する（defect 由来の列は D9 により
    必ず `defect_` で始まる必要があり、`defect_{source}__has` が同等の存在フラグを兼ねるため）。
    """
    vin_series = (
        pd.concat([df["vin"] for df in frames.values()], ignore_index=True)
        .drop_duplicates()
        .reset_index(drop=True)
    )
    ledger = pd.DataFrame({"vin": vin_series})

    parts = split_vin_base_pass_no(ledger["vin"])
    ledger["vin_base"] = parts["vin_base"]
    ledger["vin_pass_no"] = parts["vin_pass_no"]
    ledger["vin_format"] = ledger["vin_base"].map(_classify_vin_format)

    for name, frame in frames.items():
        other_cols = [c for c in frame.columns if c != "vin"]
        is_defect_frame = bool(other_cols) and all(c.startswith(f"defect_{name}__") for c in other_cols)
        if is_defect_frame:
            continue
        ledger[f"present__{name}"] = ledger["vin"].isin(frame["vin"]).astype(int)

    return ledger


def _zero_fill_after_merge(
    base: pd.DataFrame, before_cols: set[str], name: str, *, prefix: str, count_infixes: tuple[str, ...] = (),
) -> None:
    """§8.3 / docs/real_data_repair_design.md §3.3(4): `__has` / `__count` / カウント展開列を 0 埋めする。

    `__first_ts` / `__last_ts` / `__{production_time_column}__min` / `__lead_time_h` / `__工数_*`
    のような「無かった」と「0 だった」を混同してはいけない列は対象にしない
    （count_infixes に含めないことで自然に除外される）。
    """
    p = f"{prefix}_{name}"
    new_cols = [c for c in base.columns if c not in before_cols]
    for c in new_cols:
        if c in (f"{p}__has", f"{p}__count") or any(infix in c for infix in count_infixes):
            base[c] = base[c].fillna(0)


def _add_has_repair_record(base: pd.DataFrame) -> pd.DataFrame:
    """VIN のリペア記録有無フラグ `has_repair_record` を追加する。

    複数の repair ソースが存在する場合はいずれかに記録があれば 1。repair ソースが
    無ければ列を追加しない。列名が既存 `analysis.leakage_prefixes` の `has_repair` に
    一致するため、ML 特徴量からは追加設定なしで自動的に除外される。
    """
    has_cols = [c for c in base.columns if c.startswith("repair_") and c.endswith("__has")]
    if not has_cols:
        return base
    base["has_repair_record"] = base[has_cols].fillna(0).max(axis=1).astype(int)
    return base


# ---------------------------------------------------------------------
# §8.4 trend の時刻結合
# ---------------------------------------------------------------------

def build_trend_wide(
    cfg: Config,
    date_from: str | None,
    date_to: str | None,
    *,
    sources: list[RawSource] | None = None,
) -> pd.DataFrame:
    """全 trend ソースを読み、DATETIME index で横結合したワイド表を返す。

    sources を渡せば discover_sources を再実行しない（Medium-1）。未指定なら自前で discover する。
    """
    lake_dir = cfg.path("real_ingest.lake_dir", default=DEFAULT_LAKE_DIR)
    trend_cfg = _trend_config(cfg)

    include_suffixes = trend_cfg["include_suffixes"]
    include_columns = set(trend_cfg["include_columns"])
    exclude_columns = set(trend_cfg["exclude_columns"])

    if sources is None:
        raw_dir = cfg.path("real_ingest.raw_dir", default=DEFAULT_RAW_DIR)
        sources = discover_sources(raw_dir, cfg)
    trend_sources = [s for s in sources if s.kind == "trend"]

    frames: list[pd.DataFrame] = []
    for source in trend_sources:
        candidates = [c for c in source.columns if c != "DATETIME"]
        if include_columns:
            selected = [c for c in candidates if c in include_columns]
        else:
            selected = [c for c in candidates if any(c.endswith(suf) for suf in include_suffixes)]
        selected = [c for c in selected if c not in exclude_columns]
        if not selected:
            continue

        part = read_source(
            lake_dir, "trend", source.name, columns=["DATETIME", *selected],
            date_from=date_from, date_to=date_to,
        )
        if part.empty:
            continue

        part = part.rename(columns={c: f"trend__{c}" for c in selected})
        part = part.set_index("DATETIME")
        if part.index.duplicated().any():
            n_dup = int(part.index.duplicated().sum())
            logger.warning("[trend/%s] DATETIME 重複 %d 件を平均で畳み込みます。", source.name, n_dup)
            part = part.groupby(level=0).mean(numeric_only=True)
        frames.append(part)

    if not frames:
        return pd.DataFrame()

    wide = pd.concat(frames, axis=1).sort_index()
    return wide


def _resolve_anchor_source(
    token: str, source_names: list[str], anchor_map: dict[str, str], fallback: str
) -> tuple[str | None, str]:
    if token in anchor_map and anchor_map[token] in source_names:
        return anchor_map[token], "anchor_map"
    if token in source_names:
        return token, "exact"
    for src in source_names:
        if src.startswith(token) or token.startswith(src):
            return src, "prefix"
    if fallback in source_names:
        return fallback, "fallback"
    return None, "unresolved"


def resolve_trend_anchor(
    trend_column: str,
    anchor_columns: dict[str, str],
    anchor_map: dict[str, str],
    fallback: str,
) -> str | None:
    """trend 列名からアンカーとなる base 列名（traceability の通過時刻列）を解決する。"""
    name = trend_column[len("trend__"):] if trend_column.startswith("trend__") else trend_column
    token = name.split("_")[0]
    source, _method = _resolve_anchor_source(token, list(anchor_columns.keys()), anchor_map, fallback)
    return anchor_columns.get(source) if source is not None else None


def _discover_anchor_columns(
    base: pd.DataFrame, cfg: Config, *, sources: list[RawSource] | None = None
) -> dict[str, str]:
    """base の列から traceability ソース名 -> アンカー列名（通過時刻）を逆引きする。

    sources を渡せば discover_sources を再実行しない（Medium-1）。未指定なら自前で discover する。
    """
    if sources is None:
        raw_dir = cfg.path("real_ingest.raw_dir", default=DEFAULT_RAW_DIR)
        sources = discover_sources(raw_dir, cfg)
    anchor_columns: dict[str, str] = {}
    for source in sources:
        if source.kind != "traceability":
            continue
        single_col = prefixed(source.name, source.time_column)
        multi_col = prefixed(source.name, f"{source.time_column}__min")
        if single_col in base.columns:
            anchor_columns[source.name] = single_col
        elif multi_col in base.columns:
            anchor_columns[source.name] = multi_col
    return anchor_columns


def _asof_join_one_anchor(
    result: pd.DataFrame, anchor_col: str, trend_sub: pd.DataFrame, tolerance: pd.Timedelta
) -> tuple[pd.DataFrame, int, int]:
    """1 アンカー分の merge_asof。戻り値: (結合後 df, n_vin, n_matched)。"""
    new_cols = [c for c in trend_sub.columns if c != "DATETIME"]
    valid_mask = result[anchor_col].notna()
    valid = result.loc[valid_mask].copy()
    invalid = result.loc[~valid_mask].copy()

    if valid.empty:
        nan_block = pd.DataFrame(np.nan, index=result.index, columns=new_cols)
        result = pd.concat([result, nan_block], axis=1)
        return result, len(result), 0

    valid_sorted = valid.sort_values(anchor_col)
    trend_sorted = trend_sub.sort_values("DATETIME")
    merged_valid = pd.merge_asof(
        valid_sorted, trend_sorted, left_on=anchor_col, right_on="DATETIME",
        direction="nearest", tolerance=tolerance,
    )
    n_matched = int(merged_valid["DATETIME"].notna().sum())
    merged_valid = merged_valid.drop(columns=["DATETIME"])

    nan_block = pd.DataFrame(np.nan, index=invalid.index, columns=new_cols)
    invalid = pd.concat([invalid, nan_block], axis=1)

    combined = pd.concat([merged_valid, invalid], axis=0).sort_values("_row_id").reset_index(drop=True)
    return combined, len(result), n_matched


def join_trend(
    base: pd.DataFrame,
    trend_wide: pd.DataFrame,
    cfg: Config,
    *,
    sources: list[RawSource] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """trend ワイド表を rolling 窓集約したうえで、アンカー別に最近傍点参照で結合する。

    sources を渡せば discover_sources を再実行しない（Medium-1）。未指定なら自前で discover する。
    """
    reports_dir = cfg.path("paths.reports_dir", create=True)
    trend_cfg = _trend_config(cfg)
    empty_report = pd.DataFrame(columns=TREND_JOIN_REPORT_COLUMNS)

    if not trend_cfg["enabled"] or trend_wide.empty:
        logger.info("trend 結合を無効化、または結合対象の trend 列が無いためスキップします。")
        _write_report(reports_dir, "trend_anchor_map.csv", pd.DataFrame(columns=TREND_ANCHOR_MAP_COLUMNS))
        _write_report(reports_dir, "trend_join_report.csv", empty_report)
        return base, empty_report

    anchor_columns = _discover_anchor_columns(base, cfg, sources=sources)
    if not anchor_columns:
        logger.warning("trend アンカーとなる traceability ソースが base に見つかりません。trend 結合をスキップします。")
        _write_report(reports_dir, "trend_anchor_map.csv", pd.DataFrame(columns=TREND_ANCHOR_MAP_COLUMNS))
        _write_report(reports_dir, "trend_join_report.csv", empty_report)
        return base, empty_report

    anchor_map_cfg = trend_cfg["anchor_map"]
    fallback = trend_cfg["fallback_anchor_source"]

    # トークン -> アンカー解決（レポート用に一意なトークンごとに1回だけ解決する）
    anchor_map_rows = []
    col_to_anchor: dict[str, str | None] = {}
    resolved_tokens: dict[str, tuple[str | None, str]] = {}
    for col in trend_wide.columns:
        name = col[len("trend__"):] if col.startswith("trend__") else col
        token = name.split("_")[0]
        if token not in resolved_tokens:
            resolved_tokens[token] = _resolve_anchor_source(
                token, list(anchor_columns.keys()), anchor_map_cfg, fallback
            )
        source, method = resolved_tokens[token]
        anchor_col = anchor_columns.get(source) if source is not None else None
        col_to_anchor[col] = anchor_col

    for token, (source, method) in sorted(resolved_tokens.items()):
        anchor_map_rows.append({
            "trend_token": token, "anchor_source": source,
            "anchor_column": anchor_columns.get(source) if source is not None else None,
            "resolution": method,
        })
    anchor_map_df = pd.DataFrame(anchor_map_rows, columns=TREND_ANCHOR_MAP_COLUMNS)
    _write_report(reports_dir, "trend_anchor_map.csv", anchor_map_df)

    # §8.4(3): 窓集約（先に rolling）してから merge_asof で点参照する
    mode = trend_cfg["mode"]
    aggs = trend_cfg["aggs"]
    if mode == "window":
        window = int(trend_cfg["window_minutes"])
        rolled = trend_wide.rolling(window=window, center=True, min_periods=1).agg(aggs)
        if len(aggs) == 1:
            rolled.columns = [c for c, _a in rolled.columns]

            def _agg_cols(orig_col: str) -> list[str]:
                return [orig_col]
        else:
            rolled.columns = [f"{c}__{a}" for c, a in rolled.columns]

            def _agg_cols(orig_col: str) -> list[str]:
                return [f"{orig_col}__{a}" for a in aggs]
        rolled = rolled.copy()  # 列名変更後に断片化した内部ブロックをまとめ直す
    else:
        if len(aggs) > 1:
            logger.warning("trend.mode=point では trend.aggs は無視され元の値をそのまま使います。")
        rolled = trend_wide

        def _agg_cols(orig_col: str) -> list[str]:
            return [orig_col]

    anchor_to_cols: dict[str, list[str]] = {}
    for orig_col, anchor_col in col_to_anchor.items():
        if anchor_col is None:
            continue
        anchor_to_cols.setdefault(anchor_col, []).extend(_agg_cols(orig_col))

    anchor_col_to_source = {v: k for k, v in anchor_columns.items()}

    result = base.copy()
    result["_row_id"] = range(len(result))
    tolerance = pd.Timedelta(minutes=int(trend_cfg["tolerance_minutes"]))
    trend_min_ts = trend_wide.index.min()
    trend_max_ts = trend_wide.index.max()

    join_report_rows = []
    match_rates: list[float] = []
    for anchor_col, cols in anchor_to_cols.items():
        if anchor_col not in result.columns:
            continue
        trend_sub = rolled[cols].reset_index()
        anchor_min_ts = result[anchor_col].min()
        anchor_max_ts = result[anchor_col].max()
        result, n_vin, n_matched = _asof_join_one_anchor(result, anchor_col, trend_sub, tolerance)
        match_rate = (n_matched / n_vin) if n_vin else 0.0
        match_rates.append(match_rate)
        join_report_rows.append({
            "anchor_source": anchor_col_to_source.get(anchor_col), "anchor_column": anchor_col,
            "n_vin": n_vin, "n_matched": n_matched, "match_rate": match_rate,
            "trend_min_ts": trend_min_ts, "trend_max_ts": trend_max_ts,
            "anchor_min_ts": anchor_min_ts, "anchor_max_ts": anchor_max_ts,
        })

    result = result.drop(columns=["_row_id"])
    join_report_df = pd.DataFrame(join_report_rows, columns=TREND_JOIN_REPORT_COLUMNS)
    _write_report(reports_dir, "trend_join_report.csv", join_report_df)

    overall_zero = (not match_rates) or all(r == 0.0 for r in match_rates)
    on_no_overlap = trend_cfg["on_no_overlap"]
    min_match_rate = float(trend_cfg["min_match_rate"])

    if overall_zero:
        msg = (
            "trend と anchor の期間が重複しません "
            f"(trend={trend_min_ts}~{trend_max_ts})"
        )
        if on_no_overlap == "error":
            raise ValueError(msg)
        if on_no_overlap == "skip":
            logger.warning("%s trend 列を生成しません（on_no_overlap=skip）", msg)
            return base, join_report_df
        logger.warning("%s trend 列を全 NaN で生成します（on_no_overlap=warn_empty）", msg)
    else:
        for rate in match_rates:
            if 0 < rate < min_match_rate:
                logger.warning(
                    "trend マッチ率が閾値を下回りました: %.1f%% < %.1f%%",
                    rate * 100, min_match_rate * 100,
                )

    return result, join_report_df


# ---------------------------------------------------------------------
# §8.5 出力
# ---------------------------------------------------------------------

def _infer_column_source(col: str) -> str:
    if col in ("vin", "vin_base", "vin_pass_no", "vin_format"):
        return "ledger"
    if col.startswith("present__"):
        return "ledger"
    if col.startswith("trend__"):
        return "trend"
    if col.startswith("defect_"):
        rest = col[len("defect_"):]
        return f"defect_{rest.split('__')[0]}"
    if col.startswith("repair_"):
        rest = col[len("repair_"):]
        return f"repair_{rest.split('__')[0]}"
    if "__" in col:
        return col.split("__", 1)[0]
    return "other"


def _downcast_final_panel(base: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """出力直前に float64 列を float32 へ再ダウンキャストする（High-1）。

    merge（欠損 VIN による int64→float64 昇格）・trend の rolling（常に float64 を返す）・
    アンカー非重複時の全 NaN 列（既定 float64）により、convert 時点の float32 化が
    assemble の過程で無効化されるため、出力直前に一括してやり直す。
    keep_float64_suffixes に末尾一致する列（raw_convert と同じ判定）は対象外。
    """
    convert_cfg = convert_config(cfg)
    if not bool(convert_cfg["downcast_float"]):
        return base
    keep_suffixes = tuple(convert_cfg["keep_float64_suffixes"])
    target_cols = [
        c for c in base.select_dtypes(include="float64").columns
        if not should_keep_float64(c, keep_suffixes)
    ]
    for c in target_cols:
        base[c] = base[c].astype("float32")
    return base


def _build_dictionary(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in panel.columns:
        s = panel[col]
        non_null = s.dropna()
        example = non_null.iloc[0] if len(non_null) else None
        rows.append({
            "column": col,
            "dtype": str(s.dtype),
            "n_missing": int(s.isna().sum()),
            "n_unique": int(s.nunique(dropna=True)),
            "example": example,
            "source": _infer_column_source(col),
        })
    return pd.DataFrame(rows, columns=["column", "dtype", "n_missing", "n_unique", "example", "source"])


def assemble(cfg: Config, *, date_from: str | None = None, date_to: str | None = None) -> dict[str, int]:
    """レイクから VIN パネルを組み立て、`vin_panel.parquet` と品質レポート群を出力する。"""
    raw_dir = cfg.path("real_ingest.raw_dir", default=DEFAULT_RAW_DIR)
    lake_dir = cfg.path("real_ingest.lake_dir", default=DEFAULT_LAKE_DIR)
    panel_path = cfg.path("real_ingest.panel_path", default=DEFAULT_PANEL_PATH)
    reports_dir = cfg.path("paths.reports_dir", create=True)
    assemble_cfg = _assemble_config(cfg)
    vin_policy = policy_from_config(cfg)

    if not lake_dir.exists() or not any(lake_dir.rglob("*.parquet")):
        raise ValueError(f"レイクが空です。先に `convert` を実行してください: {lake_dir}")

    date_from = date_from if date_from is not None else assemble_cfg["date_from"]
    date_to = date_to if date_to is not None else assemble_cfg["date_to"]

    # discover_sources は全ファイルのヘッダを読むため、1回だけ呼んで下流（build_trend_wide /
    # join_trend）に使い回す（Medium-1）。
    sources = discover_sources(raw_dir, cfg)

    frames: dict[str, pd.DataFrame] = {}
    quality_rows: list[dict] = []
    repair_source_names: set[str] = set()
    repair_vin_sets: dict[str, set[str]] = {}
    repair_cfg = _repair_config(cfg)

    for source in sources:
        if source.kind not in ("traceability", "defect", "repair"):
            continue

        read_columns = (
            _traceability_defect_read_columns(source) if source.kind in ("traceability", "defect") else None
        )
        raw_df = read_source(
            lake_dir, source.kind, source.name,
            columns=read_columns,
            date_from=date_from, date_to=date_to,
        )
        n_rows_raw = len(raw_df)
        if raw_df.empty:
            logger.warning("[%s/%s] レイクにデータがありません。スキップします。", source.kind, source.name)
            if source.name in assemble_cfg["require_sources"]:
                raise ValueError(f"必須ソースが0行です: {source.name}")
            continue

        dummy_mask = raw_df["vin_is_dummy"].astype(bool)
        n_dummy = int(dummy_mask.sum())
        clean_df = raw_df.loc[~dummy_mask].copy()
        clean_df["vin"] = join_key(clean_df, vin_policy)

        n_parse_fail = int(clean_df[source.time_column].isna().sum()) if source.time_column in clean_df.columns else 0
        n_vin = int(clean_df["vin"].nunique())
        categories = ""
        n_vin_not_in_ledger: int | None = None
        n_size_under: int | None = None
        n_size_over: int | None = None

        if source.kind == "defect":
            prepared = prepare_defect_source(clean_df, source.name, cfg)
            n_duplicate = 0
            defect_cfg = _defect_config(cfg)
            if defect_cfg["by_size_bin"]:
                bin_min = float(defect_cfg["size_bin_min"])
                bin_max = float(defect_cfg["size_bin_max"])
                labels = size_bin_labels(bin_min, bin_max, float(defect_cfg["size_bin_width"]))
                categories = ";".join(f"size_bin:{normalize_name(label)}" for label in labels)
                size_numeric = pd.to_numeric(clean_df[defect_cfg["size_column"]], errors="coerce").dropna()
                n_size_under = int((size_numeric < bin_min).sum())
                n_size_over = int((size_numeric >= bin_max).sum())
        elif source.kind == "repair":
            prepared = prepare_repair_source(clean_df, source.name, cfg)
            n_duplicate = 0
            cat_parts: list[str] = []
            for col, enabled in repair_cfg["category_columns"].items():
                if not enabled:
                    continue
                col_prefix = f"repair_{source.name}__{col}__"
                cat_parts.extend(sorted(c[len(col_prefix):] for c in prepared.columns if c.startswith(col_prefix)))
            categories = ";".join(cat_parts)
            repair_source_names.add(source.name)
            repair_vin_sets[source.name] = set(clean_df["vin"].unique())
        else:
            is_multi = bool(clean_df["vin"].duplicated().any())
            if is_multi:
                prepared = prepare_multi_row_source(clean_df, source.name)
                n_duplicate = 0
            else:
                n_duplicate = int(clean_df["vin"].duplicated().sum())
                prepared = prepare_single_row_source(clean_df, source.name)

        frames[source.name] = prepared
        quality_rows.append({
            "kind": source.kind, "source": source.name, "n_files": len(source.files),
            "n_rows": n_rows_raw, "n_vin": n_vin, "n_dummy_excluded": n_dummy,
            "n_duplicate": n_duplicate, "n_datetime_parse_fail": n_parse_fail,
            "categories": categories, "n_vin_not_in_ledger": n_vin_not_in_ledger,
            "n_size_under": n_size_under, "n_size_over": n_size_over,
        })

    for required in assemble_cfg["require_sources"]:
        if required not in frames or frames[required].empty:
            raise ValueError(f"必須ソースが0行です: {required}")

    if not frames:
        raise ValueError("traceability/defect のいずれのソースからも VIN を取得できませんでした。")

    # repair は VIN 台帳（和集合）に加えない。既存台帳への left join のみ（R5）。
    ledger_frames = {k: v for k, v in frames.items() if k not in repair_source_names}
    if not ledger_frames:
        raise ValueError("traceability/defect のいずれのソースからも VIN を取得できませんでした。")
    ledger = build_vin_ledger(ledger_frames)

    ledger_vins = set(ledger["vin"])
    for row in quality_rows:
        if row["source"] in repair_vin_sets:
            n_not_in_ledger = len(repair_vin_sets[row["source"]] - ledger_vins)
            row["n_vin_not_in_ledger"] = n_not_in_ledger
            if n_not_in_ledger:
                logger.warning(
                    "[repair/%s] 台帳に無い VIN が %d 件あります（%s__* 列は生成されません）",
                    row["source"], n_not_in_ledger, f"repair_{row['source']}",
                )

    base = ledger
    for name, frame in frames.items():
        before_cols = set(base.columns)
        base = base.merge(frame, on="vin", how="left", validate="one_to_one")
        if name in repair_source_names:
            count_infixes = tuple(
                f"__{col}__" for col, enabled in repair_cfg["category_columns"].items() if enabled
            )
            _zero_fill_after_merge(base, before_cols, name, prefix="repair", count_infixes=count_infixes)
        # defect: レコード欠如は「未検査（unknown）」であり「不良ゼロ」と断定できないため
        # 0 埋めしない（ユーザー判断 2026-07-31。docs/real_data_ingest_design.md §13-7 参照）。
        # NaN のまま残すことで「不良ゼロ」と「未検査」を区別できるようにする。
        # traceability は present__{name} で存在有無を既に表現しているため対象外（従来から no-op）。

    base = _add_has_repair_record(base)

    _write_report(reports_dir, "ingest_quality.csv", pd.DataFrame(quality_rows))

    trend_wide = build_trend_wide(cfg, date_from, date_to, sources=sources)
    base, trend_report = join_trend(base, trend_wide, cfg, sources=sources)

    base = _downcast_final_panel(base, cfg)

    panel_path.parent.mkdir(parents=True, exist_ok=True)
    base.to_parquet(panel_path, index=False)
    logger.info("VIN パネルを出力: %s (%d 行 x %d 列)", panel_path, len(base), base.shape[1])

    dictionary = _build_dictionary(base)
    _write_report(reports_dir, "vin_panel_dictionary.csv", dictionary)

    n_trend_columns = sum(1 for c in base.columns if c.startswith("trend__"))
    if not trend_report.empty:
        total_vin = trend_report["n_vin"].sum()
        total_matched = trend_report["n_matched"].sum()
        trend_match_rate = float(total_matched / total_vin) if total_vin else 0.0
    else:
        trend_match_rate = 0.0

    result = {
        "n_vin": len(base), "n_columns": int(base.shape[1]),
        "n_trend_columns": n_trend_columns, "trend_match_rate": trend_match_rate,
    }
    logger.info("assemble 完了: %s", result)
    return result
