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

import numpy as np
import pandas as pd

from .config import Config

logger = logging.getLogger(__name__)

_FILTER_OPS = ("eq", "in", "not_in", "min", "max")

REPAIR_GROUP_PREFIX = "repair_group__"   # analysis.leakage_prefixes の "repair" に必ず乗せる（G3）
REPAIR_GROUP_BINARY_SUFFIX = "__bin"


@dataclass
class FeatureSpec:
    numeric: list[str] = field(default_factory=list)
    categorical: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return self.numeric + self.categorical


def load_real_panel(cfg: Config) -> pd.DataFrame:
    """実データ経路の `data/interim/vin_panel.parquet` を読み込み、analysis.filters を適用して返す。

    parquet はスキーマに datetime を保持しているため CSV 用の parse_dates は不要。
    `analysis.repair_groups`（修正なし車との対比用の群分け列）は `apply_filters` より前に
    導出する。群分け列は行ごとに独立に決まるため前後で値は変わらず、前に置くことで
    `analysis.filters` 側からも群分け列を参照できる（docs/repair_group_comparison_design.md §4）。
    """
    panel_path = cfg.path("real_ingest.panel_path", default="data/interim/vin_panel.parquet")
    df = pd.read_parquet(panel_path)
    df = build_repair_group_columns(df, cfg)
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


def filters_summary(df: pd.DataFrame, rules: list[dict]) -> str:
    """フィルタ句リストを人間可読な文字列に整形する（脚注・ログ共通）。"""
    if not rules:
        return "なし"
    return " / ".join(_describe_clause(df, clause) for clause in rules)


def clause_mask(df: pd.DataFrame, clause: dict, *, on_missing: str = "warn") -> pd.Series:
    """1句を評価し、行を残すかの bool Series（index は df と同一）を返す。

    `_apply_clause` から抽出した公開関数（`analysis.filters` / `repair_groups` 共通の DSL 評価）。
    """
    if "query" in clause and "column" not in clause:
        # 注意: 電着__本槽_極液_電導度_測定値 のように "__" や日本語を含む列名は query の識別子として
        # 扱えないため、そのような列はレンジ/集合系の句（min/max/in）で指定すること。
        expr = clause["query"]
        try:
            idx = df.query(expr, engine="python").index
        except Exception as exc:
            raise ValueError(f"不正な query 式: {expr} ({exc})") from exc
        return pd.Series(df.index.isin(idx), index=df.index)

    col = clause.get("column")
    if col is None:
        raise ValueError(f"フィルタ句に演算子がありません: {clause}")

    if col not in df.columns:
        if on_missing == "error":
            raise ValueError(f"フィルタ対象列が存在しません: {col}")
        logger.warning("[filter] 列が存在しないためスキップ: %s", col)
        return pd.Series(True, index=df.index)

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

    return mask


def _apply_clause(df: pd.DataFrame, clause: dict, on_missing: str) -> pd.DataFrame:
    """1句を評価し、絞り込んだ DataFrame を返す（`clause_mask` + DEBUG ログの薄いラッパ）。"""
    desc = _describe_clause(df, clause)
    mask = clause_mask(df, clause, on_missing=on_missing)
    out = df[mask]
    logger.debug("[filter] %s: %d 行", desc, len(out))
    return out


def apply_filter_clauses(df: pd.DataFrame, rules: list[dict], *, on_missing: str = "warn") -> pd.DataFrame:
    """句リストを順に AND 適用した DataFrame を返す（0 行でも例外にしない）。"""
    if on_missing not in ("warn", "error"):
        logger.warning("[filter] filters_on_missing_column の不正値 '%s' を warn として扱います", on_missing)
        on_missing = "warn"

    n_before = len(df)
    filtered = df
    for clause in rules:
        filtered = _apply_clause(filtered, clause, on_missing)
    n_after = len(filtered)
    logger.info("フィルタ適用: %d -> %d 行（%d 句）", n_before, n_after, len(rules))
    return filtered


def apply_filters(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """analysis.filters をリスト順に AND 適用した DataFrame を返す。"""
    rules = cfg.get("analysis.filters", []) or []
    if not rules:
        return df
    on_missing = cfg.get("analysis.filters_on_missing_column", "warn")
    filtered = apply_filter_clauses(df, rules, on_missing=on_missing)

    if len(filtered) == 0:
        summary = filters_summary(filtered, rules)
        logger.warning("適用フィルタが全行を除外しました: %s", summary)
        raise ValueError(f"適用フィルタが全行を除外しました: {summary}")
    return filtered


def _repair_group_clause_valid(df: pd.DataFrame, tag: str, label: str, clause: dict) -> bool:
    """1 群の句が評価可能かを検査する（`_apply_clause` と同じ検査。G9 の 3〜5）。"""
    if "query" in clause and "column" not in clause:
        return True
    col = clause.get("column")
    if col is None:
        logger.warning("[repair_groups/%s] 群 '%s' の句に演算子がありません: %r", tag, label, clause)
        return False
    if col not in df.columns:
        # G9: 参照列が無いときは句をスキップして全行一致にはしない（スペックごと不採用にする）。
        logger.warning("[repair_groups/%s] 参照列が存在しません: %s", tag, col)
        return False
    known_ops = set(_FILTER_OPS) & clause.keys()
    if not known_ops:
        logger.warning("[repair_groups/%s] 群 '%s' の句に演算子がありません: %r", tag, label, clause)
        return False
    return True


def _validate_repair_group_groups(
    df: pd.DataFrame, tag: str, groups: object, na_label: str | None = None,
) -> list[tuple[str, dict]] | None:
    """`groups`（形式A）を検査し `(label, clause)` のリストを返す（不合格なら None。G9 の 3〜5）。

    `na_label` は「どの群にも該当しない行」用の第3群ラベルのため、`groups` 内のいずれかの
    `label` と一致すると `s.fillna(na_label)` が未割当行をその群へ吸収してしまい、2群スペックの
    `__bin` にまで漏れ込む（対照群の汚染）。そのため `na_label` も重複チェックの対象に含める。
    """
    if not isinstance(groups, list) or not groups:
        logger.warning("[repair_groups/%s] groups が空かリストではありません: %r", tag, groups)
        return None

    labels: list[str] = []
    clauses: list[tuple[str, dict]] = []
    for g in groups:
        if not isinstance(g, dict):
            logger.warning("[repair_groups/%s] groups の要素が dict ではありません: %r", tag, g)
            return None
        label = g.get("label")
        if not label or not isinstance(label, str):
            logger.warning("[repair_groups/%s] groups の要素に label がありません: %r", tag, g)
            return None
        if label in labels:
            logger.warning("[repair_groups/%s] label が重複しています: %s", tag, label)
            return None
        if na_label and label == na_label:
            logger.warning(
                "[repair_groups/%s] na_label '%s' が群ラベル '%s' と衝突しています。"
                "na_label には groups に無いラベルを指定してください",
                tag, na_label, label,
            )
            return None
        clause = {k: v for k, v in g.items() if k != "label"}
        if not _repair_group_clause_valid(df, tag, label, clause):
            return None
        labels.append(label)
        clauses.append((label, clause))
    return clauses


def _log_repair_group_counts(tag: str, s: pd.Series, labels: list[str] | None) -> None:
    """群ごとの行数を INFO ログに出す（G10）。labels 未指定なら頻度降順で全水準を出す。"""
    if labels is not None:
        items = [(label, int((s == label).sum())) for label in labels]
    else:
        items = [(str(label), int(n)) for label, n in s.value_counts(dropna=True).items()]
    parts = [f"{label}={n:,}" for label, n in items]
    parts.append(f"未割当={int(s.isna().sum()):,}")
    logger.info("[repair_groups/%s] %s", tag, " / ".join(parts))


def _build_groups_form(
    df: pd.DataFrame, tag: str, groups: object, na_label: str | None,
) -> tuple[pd.Series, pd.Series | None] | None:
    """§3.2 の形式A（`analysis.filters` と同じ句のリスト）を評価する（不合格なら None）。

    複数の句に一致した行は「リスト順で最初に一致した群」に入れる（先勝ち。G6）。
    """
    clauses = _validate_repair_group_groups(df, tag, groups, na_label)
    if clauses is None:
        return None

    labels = [label for label, _ in clauses]
    masks = [(label, clause_mask(df, clause, on_missing="warn")) for label, clause in clauses]

    match_total = pd.Series(0, index=df.index)
    for _, m in masks:
        match_total = match_total + m.astype(int)
    overlap_rows = int((match_total >= 2).sum())

    s = pd.Series(np.nan, index=df.index, dtype=object)
    for label, m in masks:
        s = s.mask(m & s.isna(), label)

    for label in labels:
        if int((s == label).sum()) == 0:
            logger.warning("[repair_groups/%s] 群 '%s' に該当する行がありません", tag, label)
    if overlap_rows:
        logger.warning(
            "[repair_groups/%s] %d 行が複数の群に該当したため、先に宣言された群に割り当てました",
            tag, overlap_rows,
        )

    log_labels = list(labels)
    if na_label:
        s = s.fillna(na_label)
        log_labels.append(na_label)
    _log_repair_group_counts(tag, s, log_labels)

    bin_series = None
    if len(labels) == 2:
        # G8: 0 = 1番目のラベル、1 = 2番目のラベル。na_label で埋めた行は辞書に無いため NaN のまま
        # （2群比較の母集団から自動的に外れる）。
        bin_series = s.map({labels[0]: 0.0, labels[1]: 1.0}).astype("float64")
    return s, bin_series


def _build_base_column_form(
    df: pd.DataFrame, tag: str, base_column: object, na_label: str | None,
) -> tuple[pd.Series, None] | None:
    """§3.2 の形式B（既存カテゴリ列の流用）を評価する（不合格なら None）。"""
    if not base_column or not isinstance(base_column, str):
        logger.warning("[repair_groups/%s] base_column が空か非文字列です: %r", tag, base_column)
        return None
    if base_column not in df.columns:
        logger.warning("[repair_groups/%s] 参照列が存在しません: %s", tag, base_column)
        return None

    s = df[base_column].astype(object)
    if na_label:
        s = s.fillna(na_label)
    _log_repair_group_counts(tag, s, None)
    return s, None


def _apply_repair_group_spec(df: pd.DataFrame, spec: object, index: int, created_names: set[str]) -> None:
    """1 スペックを検証・評価し、合格なら df に列を追加する（不合格なら WARNING を出してスキップ。G9）。"""
    if not isinstance(spec, dict):
        logger.warning("[repair_groups/%d] スペックが dict ではありません: %r", index, spec)
        return
    name = spec.get("name")
    if not name or not isinstance(name, str):
        logger.warning("[repair_groups/%d] name が空か非文字列です: %r", index, name)
        return
    if not name.strip():
        logger.warning("[repair_groups/%d] name が空白のみです: %r", index, name)
        return
    tag = name

    groups = spec.get("groups")
    base_column = spec.get("base_column")
    if (groups is None) == (base_column is None):
        logger.warning("[repair_groups/%s] groups と base_column はどちらか一方のみ指定してください", tag)
        return

    col_name = f"{REPAIR_GROUP_PREFIX}{name}"
    bin_col_name = f"{col_name}{REPAIR_GROUP_BINARY_SUFFIX}"
    # created_names のチェックを列存在チェックより先に行う: 同一 repair_groups リスト内で name が
    # 重複した場合、後発スペックの原因は「name の重複」であって「パネルに元から同名列があった」
    # ではないため、ログの文言が実際の原因を指すよう判定順を分ける。
    if name in created_names:
        logger.warning("[repair_groups/%s] name が重複しています: %s", tag, name)
        return
    if col_name in df.columns or bin_col_name in df.columns:
        logger.warning("[repair_groups/%s] 列 '%s' は既に存在します（上書きしません）", tag, col_name)
        return

    na_label = spec.get("na_label")
    try:
        if groups is not None:
            result = _build_groups_form(df, tag, groups, na_label)
        else:
            result = _build_base_column_form(df, tag, base_column, na_label)
    except Exception:
        logger.warning("[repair_groups/%s] スペックの評価に失敗しました", tag, exc_info=True)
        return
    if result is None:
        return  # 検証エラーは各 _build_* 内で既に WARNING 済み

    series, bin_series = result
    df[col_name] = series
    created_names.add(name)
    if bin_series is not None:
        df[bin_col_name] = bin_series


def build_repair_group_columns(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """analysis.repair_groups の宣言に従い群分け列を df に追加して返す。

    df を **破壊的に更新**する（列の追加のみ。既存列は変更しない）。呼び出し元は
    load_real_panel（read_parquet 直後）のみを想定している。21,020 行 × 1,357 列
    （複製すると約 230MB）のパネル全体を複製しないための設計（G11）。
    宣言が空なら df をそのまま返す。設定誤りはスペック単位で WARNING を出してスキップし、
    例外は投げない（G9）。生成列名は必ず `repair_group__{name}`（+ 2群のとき `__bin`）になり、
    `analysis.leakage_prefixes` の "repair" に前方一致するため `resolve_predictors` が
    常に説明変数から除外する（G3）。
    """
    specs = cfg.get("analysis.repair_groups", []) or []
    if not specs:
        return df

    created_names: set[str] = set()
    for index, spec in enumerate(specs):
        _apply_repair_group_spec(df, spec, index, created_names)
    return df


@dataclass(frozen=True)
class AnnotationMeta:
    n_rows: int  # フィルタ後の行数（＝図に使われた台数）
    filters_summary: str  # 設定された filters を人間可読化（無ければ "なし"）

    def footnote(self, *, data_kind: str, equipment: str | None = None) -> str:
        eq_label = equipment if equipment else "全設備"
        range_part = f"範囲: 全期間（{self.n_rows:,}台）"
        return f"設備: {eq_label} ｜ データ種: {data_kind}\n{range_part} ｜ フィルタ: {self.filters_summary}"


def build_annotation_meta(df: pd.DataFrame, cfg: Config) -> AnnotationMeta:
    """フィルタ適用後の df と cfg から注記メタを1回だけ生成する。

    前提: df は同じ cfg で apply_filters 済みであること。件数・範囲は df 由来、
    filters_summary は cfg.analysis.filters 由来のため、両者が対応していないと
    脚注の件数とフィルタ表示が食い違う（呼び出しは load_real_panel→本関数の順で行う）。
    """
    n_rows = len(df)
    rules = cfg.get("analysis.filters", []) or []
    summary_text = filters_summary(df, rules)
    return AnnotationMeta(n_rows=n_rows, filters_summary=summary_text)


def equipment_measure_groups(df: pd.DataFrame, *, include_pass_sec: bool = False) -> dict[str, list[str]]:
    """トレンド列 '{設備}__{measure}'（例: 'ブース__Line'）を設備プレフィクスでグルーピングして返す。

    キーは設備プレフィクス、値はその設備の測定値列（既定で '__pass_sec' は除外）。決定的に列順ソート。
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
