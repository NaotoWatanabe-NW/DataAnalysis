"""分析（EDA/統計/ML）共通のデータ読込と、リーク安全な説明変数の解決。

不良/修正の結果由来列（leakage_columns / leakage_prefixes / leakage_regex）と
目的変数・識別子を説明変数から除外し、「工程データから不良を説明/予測する」設計を
担保する。行フィルタ（analysis.filters）の適用と、図の脚注・設備グルーピングに
使う分析横断メタもここに集約する。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from .config import Config
from .io_utils import load_df, resolve_format, table_path

logger = logging.getLogger(__name__)

_DATE_COLS = ["first_in_ts", "last_out_ts", "first_defect_date", "last_defect_date", "repair_date"]
_FILTER_OPS = ("eq", "in", "not_in", "min", "max")


@dataclass
class FeatureSpec:
    numeric: list[str] = field(default_factory=list)
    categorical: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return self.numeric + self.categorical


def load_features(cfg: Config) -> pd.DataFrame:
    """processed/features を読み込み、analysis.filters を適用して返す。

    旧経路（generate/ingest/integrate/features）専用。stats/ml はこちらを使い続ける。
    """
    processed_dir = cfg.path("paths.processed_dir")
    fmt = resolve_format(cfg.get("storage.format", "parquet"))
    df = load_df(table_path(processed_dir, "features", fmt), parse_dates=_DATE_COLS)
    df = apply_filters(df, cfg)
    return df


def load_real_panel(cfg: Config) -> pd.DataFrame:
    """実データ経路の `data/interim/vin_panel.parquet` を読み込み、analysis.filters を適用して返す。

    parquet はスキーマに datetime を保持しているため CSV 用の parse_dates は不要。
    """
    panel_path = cfg.path("real_ingest.panel_path", default="data/interim/vin_panel.parquet")
    df = pd.read_parquet(panel_path)
    df = apply_filters(df, cfg)
    return df


def derive_production_date(df: pd.DataFrame) -> pd.Series:
    """VIN の生産日の代理値を返す（行内の全 datetime 列の最小値）。

    実データパネルは列名がソースごとに異なる（`process_month` のような固定列を持たない）ため、
    特定列名を仮定せず、行に存在する全 datetime 列のうち最も早い時刻を使う。
    通常は最上流工程（前処理・シーラー炉等）の入口通過時刻が採用される。
    """
    dt_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if not dt_cols:
        return pd.Series(pd.NaT, index=df.index)
    return df[dt_cols].min(axis=1)


def drop_all_missing(df: pd.DataFrame, spec: FeatureSpec) -> FeatureSpec:
    """全行が欠損の説明変数を除外する。

    実データパネルは trend（traceability と期間が重複しない）や、この期間に未使用の
    工程列（例: ブース#3）が丸ごと NaN になりうる。scikit-learn の Imputer は全欠損列だと
    中央値が NaN になり無意味な列を残すため、学習前に落とす。
    """
    numeric = [c for c in spec.numeric if df[c].notna().any()]
    categorical = [c for c in spec.categorical if df[c].notna().any()]
    dropped = (len(spec.numeric) - len(numeric)) + (len(spec.categorical) - len(categorical))
    if dropped:
        logger.info("全欠損の説明変数を %d 列除外しました。", dropped)
    return FeatureSpec(numeric=numeric, categorical=categorical)


def traceability_measure_columns(columns: Iterable[str]) -> list[str]:
    """実データパネルの列から、加工設備（traceability）由来の測定値列のみを返す。

    `defect_*` / `repair_*` / `trend__*` / `present__*` / 台帳列（vin 系・has_repair_record）は
    「加工設備」ではない別系統のため除外する（`equipment_measure_groups` に渡す前の前処理用）。
    """
    ledger_cols = {"vin", "vin_base", "vin_pass_no", "vin_format", "has_repair_record"}
    excluded_prefixes = ("defect_", "repair_", "trend__", "present__")
    return [
        c for c in columns
        if c not in ledger_cols and not c.startswith(excluded_prefixes) and "__" in c
    ]


def _describe_clause(df: pd.DataFrame, clause: dict) -> str:
    """フィルタ句を人間可読な文字列に整形する（DEBUG ログ・脚注 filters_summary 共通）。"""
    if "query" in clause and "column" not in clause:
        return f"query({clause['query']})"
    col = clause.get("column")
    if col is None:
        return str(clause)
    if col not in df.columns:
        return f"{col}(欠損)"
    parts = []
    if "eq" in clause:
        parts.append(f"{col}={clause['eq']}")
    if "in" in clause:
        parts.append(f"{col}∈{{{','.join(str(v) for v in clause['in'])}}}")
    if "not_in" in clause:
        parts.append(f"{col}∉{{{','.join(str(v) for v in clause['not_in'])}}}")
    if "min" in clause and "max" in clause:
        parts.append(f"{col}[{clause['min']},{clause['max']}]")
    elif "min" in clause:
        parts.append(f"{col}≥{clause['min']}")
    elif "max" in clause:
        parts.append(f"{col}≤{clause['max']}")
    return ", ".join(parts) if parts else str(clause)


def _filters_summary(df: pd.DataFrame, rules: list[dict]) -> str:
    if not rules:
        return "なし"
    return " / ".join(_describe_clause(df, clause) for clause in rules)


def _apply_clause(df: pd.DataFrame, clause: dict, on_missing: str) -> pd.DataFrame:
    """1句を評価し、絞り込んだ DataFrame を返す。"""
    desc = _describe_clause(df, clause)
    if "query" in clause and "column" not in clause:
        # 注意: EQ-01__pressure のようにハイフンを含む列名は query の識別子として
        # 扱えないため、EQ 列はレンジ/集合系の句（min/max/in）で指定すること。
        expr = clause["query"]
        try:
            idx = df.query(expr, engine="python").index
        except Exception as exc:
            raise ValueError(f"不正な query 式: {expr} ({exc})") from exc
        out = df.loc[idx]
        logger.debug("[filter] %s: %d 行", desc, len(out))
        return out

    col = clause.get("column")
    if col is None:
        raise ValueError(f"フィルタ句に演算子がありません: {clause}")

    if col not in df.columns:
        if on_missing == "error":
            raise ValueError(f"フィルタ対象列が存在しません: {col}")
        logger.warning("[filter] 列が存在しないためスキップ: %s", col)
        return df

    known_ops = set(_FILTER_OPS) & clause.keys()
    if not known_ops:
        raise ValueError(f"フィルタ句に演算子がありません: {clause}")
    extra_keys = set(clause.keys()) - {"column", *_FILTER_OPS}
    if extra_keys:
        logger.warning("[filter] 未知のキーを無視: %s", extra_keys)

    mask = pd.Series(True, index=df.index)
    if "eq" in clause:
        mask &= df[col] == clause["eq"]
    if "in" in clause:
        value = clause["in"]
        if not isinstance(value, list):
            raise ValueError(f"in には list を指定してください: {clause}")
        mask &= df[col].isin(value)
    if "not_in" in clause:
        value = clause["not_in"]
        if not isinstance(value, list):
            raise ValueError(f"not_in には list を指定してください: {clause}")
        mask &= ~df[col].isin(value)
    if "min" in clause or "max" in clause:
        try:
            if "min" in clause:
                mask &= df[col] >= clause["min"]
            if "max" in clause:
                mask &= df[col] <= clause["max"]
        except Exception as exc:
            raise ValueError(f"[filter] {col} の比較に失敗: {exc}") from exc

    out = df[mask]
    logger.debug("[filter] %s: %d 行", desc, len(out))
    return out


def apply_filters(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """analysis.filters をリスト順に AND 適用した DataFrame を返す。"""
    rules = cfg.get("analysis.filters", []) or []
    if not rules:
        return df
    on_missing = cfg.get("analysis.filters_on_missing_column", "warn")
    if on_missing not in ("warn", "error"):
        logger.warning("[filter] filters_on_missing_column の不正値 '%s' を warn として扱います", on_missing)
        on_missing = "warn"

    n_before = len(df)
    filtered = df
    for clause in rules:
        filtered = _apply_clause(filtered, clause, on_missing)
    n_after = len(filtered)
    logger.info("フィルタ適用: %d -> %d 行（%d 句）", n_before, n_after, len(rules))

    if n_after == 0:
        summary = _filters_summary(filtered, rules)
        logger.warning("適用フィルタが全行を除外しました: %s", summary)
        raise ValueError(f"適用フィルタが全行を除外しました: {summary}")
    return filtered


@dataclass(frozen=True)
class AnnotationMeta:
    n_rows: int  # フィルタ後の行数（＝図に使われた台数）
    month_min: str | None  # process_month の最小（無ければ None）
    month_max: str | None
    n_months: int  # process_month のユニーク数（無ければ 0）
    filters_summary: str  # 設定された filters を人間可読化（無ければ "なし"）

    def footnote(self, *, data_kind: str, equipment: str | None = None) -> str:
        eq_label = equipment if equipment else "全設備"
        if self.month_min is not None:
            range_part = f"範囲: {self.month_min}〜{self.month_max}（{self.n_months}ヶ月・{self.n_rows:,}台）"
        else:
            range_part = f"範囲: 全期間（{self.n_rows:,}台）"
        return f"設備: {eq_label} ｜ データ種: {data_kind}\n{range_part} ｜ フィルタ: {self.filters_summary}"


def build_annotation_meta(df: pd.DataFrame, cfg: Config) -> AnnotationMeta:
    """フィルタ適用後の df と cfg から注記メタを1回だけ生成する。

    前提: df は同じ cfg で apply_filters 済みであること。件数・範囲は df 由来、
    filters_summary は cfg.analysis.filters 由来のため、両者が対応していないと
    脚注の件数とフィルタ表示が食い違う（呼び出しは load_features→本関数の順で行う）。
    """
    n_rows = len(df)
    months = sorted(df["process_month"].dropna().unique().tolist()) if "process_month" in df.columns else []
    month_min = str(months[0]) if months else None
    month_max = str(months[-1]) if months else None
    n_months = len(months)
    rules = cfg.get("analysis.filters", []) or []
    filters_summary = _filters_summary(df, rules)
    return AnnotationMeta(
        n_rows=n_rows, month_min=month_min, month_max=month_max, n_months=n_months, filters_summary=filters_summary
    )


def equipment_measure_groups(df: pd.DataFrame, *, include_pass_sec: bool = False) -> dict[str, list[str]]:
    """トレンド列 '{EQ-xx}__{measure}' を設備プレフィクスでグルーピングして返す。

    キーは EQ-id、値はその設備の測定値列（既定で '__pass_sec' は除外）。決定的に列順ソート。
    """
    groups: dict[str, list[str]] = {}
    for col in df.columns:
        if "__" not in col:
            continue
        eq, _, measure = col.partition("__")
        if not include_pass_sec and measure == "pass_sec":
            continue
        groups.setdefault(eq, []).append(col)
    return {eq: sorted(cols) for eq, cols in sorted(groups.items())}


def equipment_name_map(cfg: Config) -> dict[str, str]:
    """synthesize.equipments の id->name を返す（無ければ空 dict）。表示名 'EQ-01 圧入' 用。"""
    equipments = cfg.get("synthesize.equipments", []) or []
    return {e["id"]: e["name"] for e in equipments if "id" in e and "name" in e}


def excluded_columns(cfg: Config, columns: Iterable[str] | None = None) -> set[str]:
    """説明変数から除外する列集合（目的変数・リーク・識別子）を返す。

    columns を渡すと、analysis.leakage_regex にマッチする列（接頭辞で拾えない
    結果由来列名向けのゲート）も除外集合へ追加する。
    """
    a = cfg.get("analysis", {}) or {}
    targets = a.get("targets", {}) or {}
    excl: set[str] = set()
    for group in targets.values():
        excl.update(group or [])
    excl.update(a.get("leakage_columns", []) or [])
    excl.update(a.get("id_columns", []) or [])
    if columns is not None:
        patterns = a.get("leakage_regex", []) or []
        if patterns:
            regexes = [re.compile(p) for p in patterns]
            excl.update(c for c in columns if any(rx.search(c) for rx in regexes))
    return excl


def _self_check_predictors(df: pd.DataFrame, cfg: Config, spec: FeatureSpec) -> None:
    """除外集合とターゲットの整合を検査し、除外理由の内訳を DEBUG ログへ出す（規約ドリフトの可視化）。"""
    a = cfg.get("analysis", {}) or {}
    targets = get_targets(cfg)
    all_targets = set(targets.get("classification", []) or []) | set(targets.get("regression", []) or [])
    leaked = all_targets & set(spec.all)
    if leaked:
        logger.error("目的変数が説明変数に残存しています（規約ドリフトの疑い）: %s", sorted(leaked))

    manual_excl = set(a.get("leakage_columns", []) or []) | set(a.get("id_columns", []) or []) | all_targets
    prefixes = tuple(a.get("leakage_prefixes", []) or [])
    regexes = [re.compile(p) for p in (a.get("leakage_regex", []) or [])]

    explicit, prefix_only, regex_only = set(), set(), set()
    for c in df.columns:
        if c in manual_excl:
            explicit.add(c)
        elif prefixes and c.startswith(prefixes):
            prefix_only.add(c)
        elif any(rx.search(c) for rx in regexes):
            regex_only.add(c)
    logger.debug(
        "除外列の内訳: 明示除外=%d, 接頭辞除外=%d, 正規表現除外=%d", len(explicit), len(prefix_only), len(regex_only)
    )


def resolve_predictors(df: pd.DataFrame, cfg: Config) -> FeatureSpec:
    """説明変数を数値/カテゴリに分けて返す（リーク列・識別子・目的変数を除外）。"""
    a = cfg.get("analysis", {}) or {}
    excl = excluded_columns(cfg, df.columns)
    prefixes = tuple(a.get("leakage_prefixes", []) or [])

    predictors = [
        c for c in df.columns
        if c not in excl and not (prefixes and c.startswith(prefixes))
    ]
    numeric, categorical = [], []
    for c in predictors:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            continue  # 生時刻は説明変数にしない（派生済み特徴を使う）
        if pd.api.types.is_numeric_dtype(s):
            numeric.append(c)
        else:
            categorical.append(c)
    spec = FeatureSpec(numeric=numeric, categorical=categorical)
    _self_check_predictors(df, cfg, spec)
    return spec


def get_targets(cfg: Config) -> dict:
    """{'classification': [...], 'regression': [...]} を返す。"""
    a = cfg.get("analysis", {}) or {}
    return a.get("targets", {}) or {}
