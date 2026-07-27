"""ステップ3: vin_master から分析/予測に有効な特徴量を作る。

- 時間特徴: リードタイム、加工時間合計、滞留時間、滞留比
- 品質特徴: NG率、重大不良フラグ、修正までの日数
- カテゴリ統合: 細分類→大分類、低頻度カテゴリの OTHER 集約
- 欠損/外れ値処理: トレンド欠損の中央値補完、分位クリップ
成果物: processed/features と reports/feature_dictionary。
"""

from __future__ import annotations

import logging

import pandas as pd

from .config import Config
from .io_utils import load_df, resolve_format, save_df, table_path

logger = logging.getLogger(__name__)

# vin_master 側の日時列（fmt=csv 経由でも再パースする）
_DATE_COLS = ["first_in_ts", "last_out_ts", "first_defect_date", "last_defect_date", "repair_date"]
# 非特徴（識別子・生日時）として辞書上マークする列
_ID_COLS = {"vin", "lot_no"}


def _consolidate_rare(s: pd.Series, threshold: float, other: str = "OTHER") -> pd.Series:
    """出現率が threshold 未満のカテゴリを other に集約する。"""
    freq = s.value_counts(normalize=True)
    rare = set(freq[freq < threshold].index)
    if not rare:
        return s
    return s.where(~s.isin(rare), other)


def _trend_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if "__" in c]


def _shift_of_hour(hour: int) -> str:
    """稼働シフト区分（3直制）。1直 6-14 / 2直 14-22 / 3直 22-6。"""
    if 6 <= hour < 14:
        return "1直"
    if 14 <= hour < 22:
        return "2直"
    return "3直"


def _add_production_time_features(df: pd.DataFrame) -> None:
    """工程開始時刻から生産時間帯の特徴量を付与する（df をその場で更新）。"""
    ts = df["first_in_ts"]
    df["production_hour"] = ts.dt.hour
    df["production_shift"] = df["production_hour"].map(_shift_of_hour).astype("object")
    df["production_dayofweek"] = ts.dt.dayofweek  # 0=月 .. 6=日
    df["is_weekend"] = (df["production_dayofweek"] >= 5).astype(int)


def build_features(cfg: Config) -> dict[str, int]:
    """特徴量テーブルとデータ辞書を生成する。"""
    interim_dir = cfg.path("paths.interim_dir")
    processed_dir = cfg.path("paths.processed_dir", create=True)
    reports_dir = cfg.path("paths.reports_dir", create=True)
    fmt = resolve_format(cfg.get("storage.format", "parquet"))

    severe_level = int(cfg.get("features.severe_defect_level", 3))
    rare_threshold = float(cfg.get("features.rare_category_threshold", 0.01))
    clip_q = cfg.get("features.outlier_clip_quantiles")
    coarse_map = cfg.get("features.defect_category_coarse_map", {}) or {}

    df = load_df(table_path(interim_dir, "vin_master", fmt), parse_dates=_DATE_COLS)

    # ---- 時間特徴 -----------------------------------------------------
    df["wait_time_sec"] = (df["lead_time_sec"] - df["total_cycle_time_sec"]).clip(lower=0)
    df["ng_rate"] = df["n_ng_judgment"] / df["n_equipment_visited"].replace(0, pd.NA)
    df["ng_rate"] = df["ng_rate"].fillna(0.0)

    # ---- 生産時間帯（工程開始時刻 first_in_ts 由来）-------------------
    _add_production_time_features(df)

    # ---- 外れ値クリップ（連続量のみ）----------------------------------
    clip_cols = _trend_columns(df) + [
        "lead_time_sec", "total_cycle_time_sec", "wait_time_sec", "max_gap_sec", "mean_gap_sec",
    ]
    clip_cols = [c for c in clip_cols if c in df.columns]
    if clip_q:
        low, high = float(clip_q[0]), float(clip_q[1])
        for col in clip_cols:
            lo, hi = df[col].quantile(low), df[col].quantile(high)
            df[col] = df[col].clip(lo, hi)

    # ---- クリップ後に派生（分・比率）----------------------------------
    df["lead_time_min"] = df["lead_time_sec"] / 60.0
    df["total_cycle_time_min"] = df["total_cycle_time_sec"] / 60.0
    df["wait_ratio"] = (df["wait_time_sec"] / df["lead_time_sec"].replace(0, pd.NA)).fillna(0.0)

    # ---- 品質特徴 -----------------------------------------------------
    df["has_severe_defect"] = (df["severe_defect_count"] > 0).astype(int)
    # 修正までの日数（修正ありのみ有効、他は NaN のまま）
    ttr = (df["repair_date"] - df["last_defect_date"]).dt.total_seconds() / 86400.0
    df["time_to_repair_days"] = ttr.where(df["has_repair"] == 1)

    # ---- カテゴリ統合 -------------------------------------------------
    df["top_defect_category_coarse"] = (
        df["top_defect_category"].map(coarse_map).fillna(df["top_defect_category"])
    )
    for col in ["operator", "top_defect_category", "top_defect_category_coarse", "repair_action"]:
        if col in df.columns:
            df[col] = _consolidate_rare(df[col].astype("object"), rare_threshold)

    # ---- 欠損処理: トレンド未取得は中央値補完 --------------------------
    for col in _trend_columns(df):
        if df[col].isna().any():
            df[col] = df[col].fillna(df[col].median())

    save_df(df, table_path(processed_dir, "features", fmt))
    _write_data_dictionary(df, reports_dir, severe_level)

    n_features = df.shape[1] - len(_ID_COLS & set(df.columns))
    logger.info("特徴量テーブル生成: %d 行 x %d 列（識別子除く特徴 ≒ %d）", len(df), df.shape[1], n_features)
    return {"n_vin": len(df), "n_columns": df.shape[1]}


def _write_data_dictionary(df: pd.DataFrame, reports_dir, severe_level: int) -> None:
    """各列の型・欠損・ユニーク数・例をまとめたデータ辞書と数値サマリを出力する。"""
    rows = []
    for col in df.columns:
        s = df[col]
        non_null = s.dropna()
        example = non_null.iloc[0] if not non_null.empty else ""
        role = "id" if col in _ID_COLS else ("datetime" if pd.api.types.is_datetime64_any_dtype(s) else "feature")
        rows.append(
            {
                "column": col,
                "dtype": str(s.dtype),
                "role": role,
                "n_missing": int(s.isna().sum()),
                "n_unique": int(s.nunique(dropna=True)),
                "example": example,
            }
        )
    dictionary = pd.DataFrame(rows)
    dictionary.to_csv(reports_dir / "feature_dictionary.csv", index=False)

    numeric = df.select_dtypes("number")
    if not numeric.empty:
        numeric.describe().T.to_csv(reports_dir / "feature_summary.csv")
    logger.info("データ辞書を出力: %s", reports_dir / "feature_dictionary.csv")
