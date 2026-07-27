"""ステップ2: VIN 軸で 4 データを結合し、分析用マート（vin_master）を作る。

設計方針:
    - トレーサビリティ（VIN 1:1/設備）を VIN 単位に集約して基準表にする
    - トレンド（VIN 1:1/設備）を設備×測定値のワイド列へ展開
    - 不良（VIN 1:N）を VIN 単位へ事前集約
    - 修正（VIN 0..1）を left join（欠損＝未修正）
"""

from __future__ import annotations

import logging

import pandas as pd

from .config import Config
from .io_utils import load_df, resolve_format, save_df, table_path

logger = logging.getLogger(__name__)

_TREND_META_COLS = {"vin", "equipment_id", "process_month"}


def _aggregate_traceability(trace: pd.DataFrame) -> pd.DataFrame:
    """設備ごとの行を VIN 単位に集約（工程数・NG数・リードタイム等）。"""
    df = trace.copy()
    df["is_ng"] = (df["judgment"].astype(str) == "NG").astype(int)
    g = df.groupby("vin")
    agg = g.agg(
        n_equipment_visited=("equipment_id", "nunique"),
        n_ng_judgment=("is_ng", "sum"),
        first_in_ts=("in_ts", "min"),
        last_out_ts=("out_ts", "max"),
        total_cycle_time_sec=("cycle_time_sec", "sum"),
        plant_code=("plant_code", "first"),
        line_code=("line_code", "first"),
        operator=("operator", "first"),
        lot_no=("lot_no", "first"),
        process_month=("process_month", "first"),
    ).reset_index()
    agg["lead_time_sec"] = (agg["last_out_ts"] - agg["first_in_ts"]).dt.total_seconds()
    return agg


def _widen_trend(trend: pd.DataFrame) -> pd.DataFrame:
    """トレンドを VIN × (設備__測定値) のワイド表へ展開する。"""
    measures = [c for c in trend.columns if c not in _TREND_META_COLS]
    if not measures:
        return trend[["vin"]].drop_duplicates()
    wide = trend.pivot_table(index="vin", columns="equipment_id", values=measures, aggfunc="mean")
    # MultiIndex (measure, equipment) -> "{equipment}__{measure}"
    wide.columns = [f"{eq}__{measure}" for measure, eq in wide.columns]
    return wide.reset_index()


def _widen_passage_time(trace: pd.DataFrame) -> pd.DataFrame:
    """設備ごとの通過時間（out_ts - in_ts）を VIN × 設備__pass_sec のワイド表にする。"""
    df = trace[["vin", "equipment_id", "in_ts", "out_ts"]].copy()
    df["pass_sec"] = (df["out_ts"] - df["in_ts"]).dt.total_seconds()
    wide = df.pivot_table(index="vin", columns="equipment_id", values="pass_sec", aggfunc="mean")
    wide.columns = [f"{eq}__pass_sec" for eq in wide.columns]
    return wide.reset_index()


def _gap_features(trace: pd.DataFrame) -> pd.DataFrame:
    """工程間の滞留/搬送時間（前工程 out → 次工程 in の差）を VIN 単位に集約する。

    通過時間の「差」= 正味加工に含まれない工程間の待ち。総和は wait_time_sec と一致し、
    ここではボトルネック検出に有効な最大・平均の滞留を返す。
    """
    df = trace.sort_values(["vin", "in_ts"]).copy()
    df["prev_out"] = df.groupby("vin")["out_ts"].shift()
    df["gap_sec"] = (df["in_ts"] - df["prev_out"]).dt.total_seconds()
    g = df.groupby("vin")["gap_sec"].agg(max_gap_sec="max", mean_gap_sec="mean").reset_index()
    return g


def _defect_count_column(category: str) -> str:
    return f"defect_cnt_{category}"


def _aggregate_defect(
    defect: pd.DataFrame, severe_level: int, categories: list[str] | None = None
) -> pd.DataFrame:
    """不良（VIN 1:N）を VIN 単位に集約する。

    総数・重大数・種類数に加え、種類別カウント列（defect_cnt_<カテゴリ>）を作る。
    categories を渡すと、その順で列を固定生成する（データに現れない種類も 0 列を作る）。
    """
    base_cols = [
        "vin", "defect_count", "severe_defect_count", "defect_type_count",
        "max_severity", "severity_sum", "first_defect_date", "last_defect_date",
        "top_defect_category", "has_defect",
    ]
    count_cols = [_defect_count_column(c) for c in (categories or [])]
    if defect.empty:
        return pd.DataFrame(columns=base_cols + count_cols)

    df = defect.copy()
    df["is_severe"] = (df["severity"] >= severe_level).astype(int)
    g = df.groupby("vin")
    agg = g.agg(
        defect_count=("defect_id", "count"),
        severe_defect_count=("is_severe", "sum"),
        defect_type_count=("defect_category", "nunique"),
        max_severity=("severity", "max"),
        severity_sum=("severity", "sum"),
        first_defect_date=("defect_date", "min"),
        last_defect_date=("defect_date", "max"),
        top_defect_category=("defect_category", lambda s: s.mode().iat[0] if not s.mode().empty else s.iat[0]),
    ).reset_index()
    agg["has_defect"] = 1

    # 種類別カウント（VIN × カテゴリ のクロス集計）
    cross = pd.crosstab(df["vin"], df["defect_category"])
    observed = list(cross.columns)
    ordered = (categories or []) + [c for c in observed if c not in (categories or [])]
    cross = cross.reindex(columns=ordered, fill_value=0)
    cross.columns = [_defect_count_column(c) for c in cross.columns]
    agg = agg.merge(cross.reset_index(), on="vin", how="left")
    return agg


def _prepare_repair(repair: pd.DataFrame) -> pd.DataFrame:
    """修正（VIN 0..1）に has_repair フラグを付ける。"""
    if repair.empty:
        return pd.DataFrame(
            columns=["vin", "repair_date", "repaired_defect_code", "repair_action", "repair_time_min", "has_repair"]
        )
    df = repair.copy()
    df["has_repair"] = 1
    # 念のため VIN 一意化（0..1 を保証）
    if df.duplicated("vin").any():
        logger.warning("repair に VIN 重複あり。最初の行を採用します。")
        df = df.drop_duplicates("vin", keep="first")
    return df


def integrate(cfg: Config) -> dict[str, int]:
    """interim の 4 テーブルを結合し vin_master を interim へ保存する。"""
    interim_dir = cfg.path("paths.interim_dir")
    fmt = resolve_format(cfg.get("storage.format", "parquet"))
    severe_level = int(cfg.get("features.severe_defect_level", 3))
    categories = list((cfg.get("features.defect_category_coarse_map") or {}).keys())

    trace = load_df(table_path(interim_dir, "traceability", fmt), parse_dates=["in_ts", "out_ts"])
    trend = load_df(table_path(interim_dir, "trend", fmt))
    defect = load_df(table_path(interim_dir, "defect", fmt), parse_dates=["defect_date"])
    repair = load_df(table_path(interim_dir, "repair", fmt), parse_dates=["repair_date"])

    if trace.empty:
        raise ValueError("traceability が空です。先に generate/ingest を実行してください。")

    base = _aggregate_traceability(trace)
    trend_wide = _widen_trend(trend)
    passage_wide = _widen_passage_time(trace)
    gap = _gap_features(trace)
    defect_agg = _aggregate_defect(defect, severe_level, categories)
    repair_prepared = _prepare_repair(repair)

    master = base.merge(trend_wide, on="vin", how="left", validate="one_to_one")
    master = master.merge(passage_wide, on="vin", how="left", validate="one_to_one")
    master = master.merge(gap, on="vin", how="left", validate="one_to_one")
    master = master.merge(defect_agg, on="vin", how="left", validate="one_to_one", indicator="_defect_merge")
    n_defect_vin = int((master["_defect_merge"] == "both").sum())
    master = master.drop(columns="_defect_merge")
    master = master.merge(repair_prepared, on="vin", how="left", validate="one_to_one", indicator="_repair_merge")
    n_repair_vin = int((master["_repair_merge"] == "both").sum())
    master = master.drop(columns="_repair_merge")

    # 欠損＝不良/修正なし として埋める（種類別カウント列も 0 埋め）
    zero_fill = ["defect_count", "severe_defect_count", "defect_type_count", "severity_sum", "max_severity", "has_defect"]
    zero_fill += [c for c in master.columns if c.startswith("defect_cnt_")]
    for col in zero_fill:
        if col in master.columns:
            master[col] = master[col].fillna(0).astype(int)
    if "top_defect_category" in master.columns:
        master["top_defect_category"] = master["top_defect_category"].fillna("なし")
    # 単一工程などで工程間滞留が定義できない場合は 0（滞留なし）
    for col in ["max_gap_sec", "mean_gap_sec"]:
        if col in master.columns:
            master[col] = master[col].fillna(0.0)
    master["has_repair"] = master.get("has_repair", 0)
    master["has_repair"] = master["has_repair"].fillna(0).astype(int)
    if "repair_action" in master.columns:
        master["repair_action"] = master["repair_action"].fillna("なし")

    interim_dir_out = cfg.path("paths.interim_dir", create=True)
    save_df(master, table_path(interim_dir_out, "vin_master", fmt))

    logger.info(
        "統合完了: VIN=%d, 列=%d, 不良ありVIN=%d, 修正ありVIN=%d",
        len(master), master.shape[1], n_defect_vin, n_repair_vin,
    )
    return {
        "n_vin": len(master),
        "n_columns": master.shape[1],
        "n_defect_vin": n_defect_vin,
        "n_repair_vin": n_repair_vin,
    }
