"""ステップ4: 実データパネル（`data/interim/vin_panel.parquet`）の自動 EDA グラフ生成。

分布・群差・関係性・時系列を把握するための図を reports/eda に出力する。
配色は dpviz の検証済みパレット（viz_style）を用いる。
説明変数はリーク安全な集合（analysis_data.resolve_predictors）に限定する。
目的変数は既定で `has_repair_record`（リペア記録有無。§13-7 の「未検査」判断により
`defect_*__has` は 0/1 の完全な目的変数を作れないため、0/1 が常に定義される repair 記録を使う）。
traceability 測定値の分布・相関は `{設備}__{measure}` 列から動的に設備をグルーピングし、
設備ごとに図を出力する（固定リストは持たない）。
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import viz_style as vs
from .analysis_data import (
    AnnotationMeta,
    apply_filter_clauses,
    build_annotation_meta,
    derive_production_date,
    equipment_measure_groups,
    filters_summary,
    get_targets,
    load_real_panel,
    resolve_predictors,
    traceability_measure_columns,
)
from .config import Config

logger = logging.getLogger(__name__)

TARGET_LABELS = {0: "リペア記録なし", 1: "リペア記録あり"}


def _save(fig: plt.Figure, path: Path, footnote: str | None = None) -> str:
    return vs.save_figure(fig, path, footnote, log=logger)


def _rate_by(df: pd.DataFrame, col: str, target: str) -> pd.Series:
    """カテゴリ別の目的変数（0/1）平均を % で返す。"""
    return df.groupby(col)[target].mean().mul(100).sort_values(ascending=False)


def _fig_rate_by_category(
    df: pd.DataFrame, cat_cols: list[str], target: str, out: Path, meta: AnnotationMeta, *, max_categories: int = 15,
) -> str | None:
    # ID 系の高カーディナリティ列（VIN 単位でほぼ一意など）は棒が読めなくなるため除外する
    # （id_columns で拾いきれない列名未知の高カーディナリティ列への一般的なガード）。
    cols = [c for c in cat_cols if 1 < df[c].nunique() <= max_categories][:4]
    if not cols:
        logger.warning("カテゴリ列が無いため、カテゴリ別リペア率の図をスキップします。")
        return None
    n = len(cols)
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    axes = axes.ravel()
    overall = df[target].mean() * 100
    for i, col in enumerate(cols):
        ax = axes[i]
        rates = _rate_by(df, col, target)
        ax.bar(rates.index.astype(str), rates.values, color=vs.color_for(i), width=0.62, zorder=3)
        ax.axhline(overall, color=vs.MUTED, lw=1.2, ls="--", zorder=2)
        ax.set_title(f"{col} 別 リペア率")
        ax.set_ylabel("リペア率 (%)")
        ax.tick_params(axis="x", rotation=30)
        for x, v in enumerate(rates.values):  # 直接ラベル
            ax.text(x, v, f"{v:.1f}", ha="center", va="bottom", fontsize=9, color=vs.INK_SECONDARY)
    for j in range(n, 4):
        axes[j].set_visible(False)
    fig.suptitle(f"カテゴリ別 リペア率（破線=全体 {overall:.1f}%）", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    footnote = meta.footnote(data_kind="リペア率(カテゴリ別)")
    return _save(fig, out, footnote)


def _select_histogram_columns(df: pd.DataFrame, numeric: list[str], cfg: Config) -> list[str]:
    """ヒストグラム対象列を選ぶ。明示指定（analysis.eda_histogram_columns）があればそれを使い、
    無ければ「ほぼ一定でない（nunique >= 閾値）」列に絞ったうえで欠損が少ない順に
    analysis.eda_histogram_max（既定6）本を自動選定する。

    `__n_rows` / `__nunique` / `Line__max` のような集計由来の準定数列（nunique が数個しかない）は
    ヒストグラムにしても分布が見えず有益でないため、統計的に（列名パターンではなく）除外する。
    """
    explicit = [c for c in cfg.get("analysis.eda_histogram_columns", []) or [] if c in numeric]
    if explicit:
        return explicit
    max_n = int(cfg.get("analysis.eda_histogram_max", 6))
    min_nunique = int(cfg.get("analysis.eda_histogram_min_nunique", 10))
    varied = [c for c in numeric if df[c].nunique(dropna=True) >= min_nunique]
    non_null_counts = df[varied].notna().sum().sort_values(ascending=False)
    return list(non_null_counts.index[:max_n])


def _draw_histogram_grid(df: pd.DataFrame, cols: list[str], target: str, title: str) -> plt.Figure:
    n = len(cols)
    n_cols_grid = 2
    n_rows_grid = max(1, -(-n // n_cols_grid))
    fig, axes = plt.subplots(n_rows_grid, n_cols_grid, figsize=(11, 4 * n_rows_grid))
    axes = np.atleast_1d(axes).ravel()
    for i, col in enumerate(cols):
        ax = axes[i]
        for flag_value in (0, 1):
            values = df.loc[df[target] == flag_value, col].dropna()
            if values.empty:
                continue
            ax.hist(
                values, bins=25, density=True, alpha=0.55,
                color=vs.color_for(flag_value), edgecolor=vs.color_for(flag_value),
                linewidth=1.0, zorder=3, label=f"{TARGET_LABELS[flag_value]}（n={len(values)}）",
            )
        ax.set_title(col)
        ax.set_ylabel("密度")
        ax.legend(fontsize=8)
    for j in range(n, n_rows_grid * n_cols_grid):
        axes[j].set_visible(False)
    fig.suptitle(title, fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    return fig


def _fig_histogram_by_target(
    df: pd.DataFrame, numeric: list[str], target: str, out: Path, meta: AnnotationMeta, cfg: Config
) -> str | None:
    """全設備を横断した上位列のヒストグラム（設備別の詳細は _fig_histogram_by_equipment）。"""
    cols = _select_histogram_columns(df, numeric, cfg)
    if not cols:
        logger.warning("ヒストグラム対象の数値列が無いため、ヒストグラムの図をスキップします。")
        return None
    fig = _draw_histogram_grid(df, cols, target, "目的変数別の測定値分布（全設備横断・上位列。density=True で正規化）")
    footnote = meta.footnote(data_kind="測定値分布(目的変数別・全設備横断)")
    return _save(fig, out, footnote)


def _fig_histogram_by_equipment(
    df: pd.DataFrame, target: str, out_dir: Path, meta: AnnotationMeta, groups: dict[str, list[str]], cfg: Config,
) -> list[str]:
    """設備ごとにヒストグラムを出力する（§全設備データを網羅する）。"""
    if not groups:
        logger.warning("設備の測定値列が見つからないため、設備別ヒストグラムをスキップします。")
        return []

    max_n = int(cfg.get("analysis.eda_histogram_max_per_equipment", 6))
    min_nunique = int(cfg.get("analysis.eda_histogram_min_nunique", 10))
    figures = []
    for eq, cols_for_eq in groups.items():
        varied = [c for c in cols_for_eq if df[c].nunique(dropna=True) >= min_nunique]
        if not varied:
            logger.info("[histogram/%s] 分布に広がりのある列が無いためスキップします。", eq)
            continue
        non_null_counts = df[varied].notna().sum().sort_values(ascending=False)
        cols = list(non_null_counts.index[:max_n])
        fig = _draw_histogram_grid(df, cols, target, f"{eq}: 目的変数別の測定値分布（density=True で正規化）")
        footnote = meta.footnote(data_kind="測定値分布(目的変数別)", equipment=eq)
        figures.append(_save(fig, out_dir / f"02_histogram_by_target__{eq}.png", footnote))
    return figures


def _fig_trend_boxplot_by_target(
    df: pd.DataFrame,
    target: str,
    out_dir: Path,
    meta: AnnotationMeta,
    groups: dict[str, list[str]],
) -> list[str]:
    if not groups:
        logger.warning("設備の測定値列が見つからないため、箱ひげ図をスキップします。")
        return []

    labels = [TARGET_LABELS[0], TARGET_LABELS[1]]
    figures = []
    for eq, cols in groups.items():
        n = len(cols)
        n_cols_grid = 2
        n_rows_grid = max(1, -(-n // n_cols_grid))
        fig, axes = plt.subplots(n_rows_grid, n_cols_grid, figsize=(11, 4 * n_rows_grid))
        axes = np.atleast_1d(axes).ravel()
        for i, m in enumerate(cols):
            ax = axes[i]
            g = [df.loc[df[target] == v, m].dropna().values for v in (0, 1)]
            bp = ax.boxplot(g, tick_labels=labels, patch_artist=True, widths=0.55, showfliers=False)
            for patch, c in zip(bp["boxes"], (vs.CATEGORICAL[0], vs.CATEGORICAL[7])):
                patch.set_facecolor(c)
                patch.set_alpha(0.75)
                patch.set_edgecolor(vs.INK_SECONDARY)
            for med in bp["medians"]:
                med.set_color(vs.INK_PRIMARY)
                med.set_linewidth(1.6)
            measure = m.split("__", 1)[-1]
            ax.set_title(measure)
            ax.set_ylabel("測定値")
        for j in range(n, n_rows_grid * n_cols_grid):
            axes[j].set_visible(False)
        fig.suptitle(f"{eq}: リペア有無で層別した測定値の分布", fontsize=13, fontweight="bold")
        fig.tight_layout(rect=(0, 0.04, 1, 0.97))
        footnote = meta.footnote(data_kind="測定値の分布", equipment=eq)
        figures.append(_save(fig, out_dir / f"03_trend_boxplot__{eq}.png", footnote))
    return figures


def _fig_corr_with_target(
    df: pd.DataFrame, numeric: list[str], target: str, out: Path, meta: AnnotationMeta, top: int = 15
) -> str | None:
    if not numeric:
        logger.warning("数値列が無いため、目的変数との相関の図をスキップします。")
        return None
    corr = df[numeric + [target]].corr(numeric_only=True)[target].drop(target).dropna()
    if corr.empty:
        logger.warning("相関を計算できる列が無いため、目的変数との相関の図をスキップします。")
        return None
    corr = corr.reindex(corr.abs().sort_values(ascending=False).index).head(top).iloc[::-1]
    colors = [vs.CATEGORICAL[0] if v >= 0 else vs.CATEGORICAL[7] for v in corr.values]
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.barh(corr.index, corr.values, color=colors, zorder=3)
    ax.axvline(0, color=vs.BASELINE, lw=1)
    ax.set_title(f"{target} との相関（Pearson, |上位{top}|）")
    ax.set_xlabel("相関係数")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    for y, v in enumerate(corr.values):
        ax.text(v, y, f" {v:+.2f}", va="center", ha="left" if v >= 0 else "right",
                fontsize=8, color=vs.INK_SECONDARY)
    ax.text(0.99, 0.02, "青=正の相関 / 赤=負の相関", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=9, color=vs.MUTED)
    fig.tight_layout()
    footnote = meta.footnote(data_kind="目的変数との相関")
    return _save(fig, out, footnote)


def _draw_corr_heatmap(
    df: pd.DataFrame, cols: list[str], title: str, *, figsize_base: float = 0.42, method: str = "pearson"
) -> plt.Figure:
    corr = df[cols].corr(method=method, numeric_only=True)
    side = max(8.5, figsize_base * len(cols) + 2)
    fig, ax = plt.subplots(figsize=(side, side * 0.86))
    im = ax.imshow(corr.values, cmap=vs.diverging_cmap(), vmin=-1, vmax=1)
    ax.set_xticks(range(len(cols)), cols, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(cols)), cols, fontsize=8)
    annotate = len(cols) <= 30  # 列数が多いと文字が潰れて読めないため、セル注記は小さい行列のみ
    for i in range(len(cols)):
        for j in range(len(cols)):
            v = corr.values[i, j]
            if pd.isna(v):
                continue
            if annotate:
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7,
                        color=vs.INK_PRIMARY if abs(v) < 0.6 else "#ffffff")
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("相関係数", color=vs.INK_SECONDARY)
    ax.set_title(title)
    ax.grid(False)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig


def _fig_corr_heatmap(
    df: pd.DataFrame, target: str, out_dir: Path, meta: AnnotationMeta, groups: dict[str, list[str]],
) -> list[str]:
    """設備ごとの相関ヒートマップ（設備内の測定値どうしの関係）。"""
    if not groups:
        logger.warning("設備の測定値列が見つからないため、相関ヒートマップをスキップします。")
        return []

    figures = []
    for eq, cols_for_eq in groups.items():
        cols = [*cols_for_eq, target]
        if len(cols) < 2:
            continue
        fig = _draw_corr_heatmap(df, cols, f"{eq}: 測定値相関ヒートマップ")
        footnote = meta.footnote(data_kind="測定値相関", equipment=eq)
        figures.append(_save(fig, out_dir / f"05_correlation_heatmap__{eq}.png", footnote))
    return figures


def _fig_corr_heatmap_cross_equipment(
    df: pd.DataFrame, target: str, out: Path, meta: AnnotationMeta, groups: dict[str, list[str]], cfg: Config,
) -> str | None:
    """設備間をまたいだ相関ヒートマップ（各設備から分布の広い上位列を代表として選び1枚にまとめる）。

    全列を素直に1枚にすると数百列になり O(n^2) で描画不能になるため（実測: 単設備242列で40秒超）、
    設備ごとに代表列を絞ってから横断させる。列名に設備名を含むため、設備をまたいでも判別できる。
    """
    if not groups:
        logger.warning("設備の測定値列が見つからないため、設備間ヒートマップをスキップします。")
        return None

    top_k = int(cfg.get("analysis.eda_cross_equipment_top_k", 3))
    max_total = int(cfg.get("analysis.eda_cross_equipment_max_columns", 60))
    representatives: list[str] = []
    for cols_for_eq in groups.values():
        ranked = sorted(cols_for_eq, key=lambda c: df[c].nunique(dropna=True), reverse=True)
        representatives.extend(sorted(ranked[:top_k]))

    if len(representatives) > max_total:
        logger.warning(
            "[cross-equipment heatmap] 代表列数 %d が上限 %d を超えたため、"
            "設備あたりの代表列数（eda_cross_equipment_top_k）を減らすことを検討してください。先頭 %d 列のみ使います。",
            len(representatives), max_total, max_total,
        )
        representatives = representatives[:max_total]

    cols = [*representatives, target]
    if len(cols) < 2:
        return None
    fig = _draw_corr_heatmap(
        df, cols, f"設備間 相関ヒートマップ（設備ごと代表{top_k}列。分布の広い上位列を選出）", figsize_base=0.32,
    )
    footnote = meta.footnote(data_kind="測定値相関(設備間)")
    return _save(fig, out, footnote)


def _fig_daily_trend(df: pd.DataFrame, target: str, out: Path, meta: AnnotationMeta) -> str | None:
    date = derive_production_date(df)
    n_days = date.dt.date.nunique(dropna=True)
    if n_days < 2:
        logger.info("生産日が1日以下のため、日次トレンドの図をスキップします（現在 %d 日分）。", n_days)
        return None

    g = df.groupby(date.dt.date)
    rate = g[target].mean().mul(100)
    n_vin = g.size()
    n_target = g[target].sum()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True, height_ratios=[3, 2])
    ax1.plot(rate.index.astype(str), rate.values, color=vs.CATEGORICAL[0], lw=2, marker="o", ms=7, zorder=3)
    for x, v in enumerate(rate.values):
        ax1.text(x, v, f" {v:.1f}%", va="bottom", fontsize=9, color=vs.INK_SECONDARY)
    ax1.set_title("日次のリペア率トレンド")
    ax1.set_ylabel("リペア率 (%)")

    idx = np.arange(len(n_vin))
    w = 0.4
    ax2.bar(idx - w / 2, n_vin.values, width=w, color=vs.CATEGORICAL[2], label="生産VIN数", zorder=3)
    ax2.bar(idx + w / 2, n_target.values, width=w, color=vs.CATEGORICAL[1], label="リペアありVIN数", zorder=3)
    ax2.set_xticks(idx, n_vin.index.astype(str))
    ax2.set_ylabel("台数")
    ax2.set_xlabel("生産日（全 datetime 列の行内最小値）")
    ax2.legend()
    fig.suptitle("日次トレンド（リペア率・台数）", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    footnote = meta.footnote(data_kind="日次トレンド")
    return _save(fig, out, footnote)


def _cap_group_columns(
    df: pd.DataFrame, groups: dict[str, list[str]], max_n: int, *, chart: str
) -> dict[str, list[str]]:
    """設備ごとの列数を max_n に制限する（nunique 上位を優先）。

    箱ひげ図・ヒートマップは列数に比例して図が肥大化・可読不能になり、ヒートマップは
    O(n^2) で描画コストが増える（実測: 242列の設備で単体40秒超）。ほぼ一定の列より
    分布に広がりのある列を優先するため nunique 上位で選ぶ（列名パターンには依存しない）。
    """
    capped = {}
    for eq, cols in groups.items():
        if len(cols) <= max_n:
            capped[eq] = cols
            continue
        ranked = sorted(cols, key=lambda c: df[c].nunique(dropna=True), reverse=True)
        capped[eq] = sorted(ranked[:max_n])
        logger.warning(
            "[%s/%s] 列数 %d が上限 %d を超えたため、分布の広い上位 %d 列に絞ります。",
            chart, eq, len(cols), max_n, max_n,
        )
    return capped


_CUSTOM_REQUIRED_FIELDS = {
    "scatter": ["x", "y"],
    "bar": ["x"],
    "histogram": ["x"],
    "box": ["y"],
    "heatmap": ["columns"],
}


def _custom_chart_columns(chart_type: str, spec: dict) -> list[str]:
    """列存在チェック対象の列名一覧を返す（heatmap の hue は非対応のため含めない）。"""
    cols: list[str] = []
    if chart_type == "heatmap":
        cols.extend(spec.get("columns") or [])
        return cols
    for field in ("x", "y"):
        v = spec.get(field)
        if v:
            cols.append(v)
    hue = spec.get("hue")
    if hue:
        cols.append(hue)
    return cols


def _validate_custom_spec(df: pd.DataFrame, spec: dict, chart_type: str) -> str | None:
    """設定ミスがあればエラーメッセージを返す（無ければ None）。df 自体は変更しない。"""
    required = _CUSTOM_REQUIRED_FIELDS[chart_type]
    missing_fields = [f for f in required if not spec.get(f)]
    if missing_fields:
        return f"必須フィールドが不足しています: {missing_fields}"

    if chart_type == "heatmap":
        cols = spec.get("columns")
        if not isinstance(cols, list) or len(cols) < 2:
            return f"heatmap の columns はリスト(2列以上)で指定してください: {cols!r}"

    columns = _custom_chart_columns(chart_type, spec)
    missing_cols = [c for c in columns if c not in df.columns]
    if missing_cols:
        return f"指定された列が存在しません: {missing_cols}"

    if chart_type == "heatmap":
        numeric_cols = [c for c in spec["columns"] if pd.api.types.is_numeric_dtype(df[c])]
        if len(numeric_cols) < 2:
            return f"heatmap は数値列が2本以上必要です（数値列: {numeric_cols}）"
        return None

    numeric_fields = {
        "scatter": ["x", "y"],
        "bar": ["y"] if spec.get("y") else [],
        "histogram": ["x"],
        "box": ["y"],
    }[chart_type]
    non_numeric = {f: spec[f] for f in numeric_fields if not pd.api.types.is_numeric_dtype(df[spec[f]])}
    if non_numeric:
        return f"数値であるべき列が数値ではありません: {non_numeric}"

    return None


def _auto_custom_title(chart_type: str, spec: dict) -> str:
    x, y, hue = spec.get("x"), spec.get("y"), spec.get("hue")
    if chart_type == "scatter":
        title = f"{y} と {x} の関係"
        return title + (f"（{hue} 別）" if hue else "")
    if chart_type == "bar":
        if y:
            return f"{x} 別 {y}（{spec.get('agg', 'mean')}）"
        return f"{x} 別 件数"
    if chart_type == "histogram":
        title = f"{x} の分布"
        return title + (f"（{hue} 別）" if hue else "")
    if chart_type == "box":
        return f"{x} 別 {y} の分布" if x else f"{y} の分布"
    if chart_type == "heatmap":
        cols = spec.get("columns") or []
        return f"相関ヒートマップ（{len(cols)}列・{spec.get('method', 'pearson')}）"
    return chart_type


def _custom_chart_filename(spec: dict, index: int, chart_type: str) -> str:
    output = spec.get("output")
    if not output:
        return f"custom_{index:02d}_{chart_type}.png"
    name = Path(output).name
    if not name:
        return f"custom_{index:02d}_{chart_type}.png"
    if not Path(name).suffix:
        name = f"{name}.png"
    if name != output:
        logger.warning(
            "[custom_charts/%d %s] output はファイル名のみ指定できます。%r -> %r に丸めます",
            index, chart_type, output, name,
        )
    return name


def _cap_hue_levels(sub: pd.DataFrame, hue: str, cfg: Config, tag: str) -> list[str]:
    """hue 水準を頻度降順で並べ、上限（analysis.custom_chart_max_hue）超過分を切り捨てる。"""
    max_hue = int(cfg.get("analysis.custom_chart_max_hue", 8))
    levels = [str(v) for v in sub[hue].dropna().astype(str).value_counts().index]
    if len(levels) > max_hue:
        logger.warning(
            "%s hue(%s) の水準数 %d が上限 %d を超えたため、頻度上位 %d 水準のみ描画します。",
            tag, hue, len(levels), max_hue, max_hue,
        )
        levels = levels[:max_hue]
    return levels


def _cap_categories(sub: pd.DataFrame, col: str, cfg: Config, tag: str) -> list[str]:
    """カテゴリ水準を頻度降順で並べ、上限（analysis.eda_max_categories）超過分を切り捨てる。"""
    max_n = int(cfg.get("analysis.eda_max_categories", 15))
    levels = [str(v) for v in sub[col].dropna().astype(str).value_counts().index]
    if len(levels) > max_n:
        logger.warning(
            "%s %s のカテゴリ数 %d が上限 %d を超えたため、頻度上位 %d カテゴリのみ描画します。",
            tag, col, len(levels), max_n, max_n,
        )
        levels = levels[:max_n]
    return levels


def _custom_scatter(sub: pd.DataFrame, spec: dict, cfg: Config, tag: str) -> tuple[plt.Figure, str]:
    x, y, hue = spec["x"], spec["y"], spec.get("hue")
    alpha = float(spec.get("alpha", 0.5))

    data = sub.dropna(subset=[x, y])
    if data.empty:
        raise ValueError(f"scatter: {x}/{y} が全て欠損です")

    max_points = int(cfg.get("analysis.custom_chart_max_points", 20000))
    n_total = len(data)
    extra = ""
    if n_total > max_points:
        seed = int(cfg.get("project.random_seed", 0))
        data = data.sample(n=max_points, random_state=seed)
        extra = f"（サンプリング {max_points}/{n_total} 点）"

    fig, ax = plt.subplots(figsize=(10, 6))
    if hue:
        levels = _cap_hue_levels(data, hue, cfg, tag)
        for i, level in enumerate(levels):
            sub_h = data[data[hue].astype(str) == level]
            ax.scatter(sub_h[x], sub_h[y], color=vs.color_for(i), alpha=alpha, zorder=3, label=level)
        ax.legend(fontsize=8, title=hue)
    else:
        ax.scatter(data[x], data[y], color=vs.CATEGORICAL[0], alpha=alpha, zorder=3)
    ax.set_title(spec["title"])
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig, extra


def _custom_bar(sub: pd.DataFrame, spec: dict, cfg: Config, tag: str) -> tuple[plt.Figure, str]:
    x, y, hue = spec["x"], spec.get("y"), spec.get("hue")
    agg = spec.get("agg", "mean")
    if not y and "agg" in spec:
        logger.warning("%s y が無いため agg(%s) は無視し件数を集計します", tag, agg)
    if y and agg not in ("mean", "sum", "median", "count"):
        raise ValueError(f"bar: 未対応の agg です: {agg}")

    x_levels = _cap_categories(sub, x, cfg, tag)
    data = sub[sub[x].astype(str).isin(x_levels)].copy()
    data[x] = data[x].astype(str)
    if data.empty:
        raise ValueError(f"bar: {x} が全て欠損です")

    fig, ax = plt.subplots(figsize=(min(18, 6 + 0.5 * len(x_levels)), 6))
    idx = np.arange(len(x_levels))

    if hue:
        levels = _cap_hue_levels(data, hue, cfg, tag)
        n_h = len(levels)
        w = 0.8 / max(n_h, 1)
        for i, level in enumerate(levels):
            data_h = data[data[hue].astype(str) == level]
            if y:
                values = data_h.groupby(x)[y].agg(agg).reindex(x_levels)
            else:
                values = data_h.groupby(x).size().reindex(x_levels).fillna(0)
            ax.bar(idx + (i - (n_h - 1) / 2) * w, values.values, width=w, color=vs.color_for(i), zorder=3, label=level)
        ax.set_xticks(idx, x_levels, rotation=30)
        ax.legend(fontsize=8, title=hue)
    else:
        if y:
            values = data.groupby(x)[y].agg(agg).reindex(x_levels)
        else:
            values = data.groupby(x).size().reindex(x_levels).fillna(0)
        ax.bar(idx, values.values, width=0.62, color=vs.CATEGORICAL[0], zorder=3)
        ax.set_xticks(idx, x_levels, rotation=30)
        for i, v in enumerate(values.values):
            label = f"{v:.2f}" if y else f"{int(v)}"
            ax.text(i, v, label, ha="center", va="bottom", fontsize=9, color=vs.INK_SECONDARY)

    ax.set_title(spec["title"])
    ax.set_xlabel(x)
    ax.set_ylabel(y if y else "件数")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig, ""


def _custom_histogram(sub: pd.DataFrame, spec: dict, cfg: Config, tag: str) -> tuple[plt.Figure, str]:
    x, hue = spec["x"], spec.get("hue")
    bins = int(spec.get("bins", 30))
    density = bool(spec.get("density", False))

    fig, ax = plt.subplots(figsize=(10, 6))
    if hue:
        levels = _cap_hue_levels(sub.dropna(subset=[x]), hue, cfg, tag)
        drawn = False
        for i, level in enumerate(levels):
            values = sub.loc[sub[hue].astype(str) == level, x].dropna()
            if values.empty:
                continue
            ax.hist(
                values, bins=bins, density=density, alpha=0.55, color=vs.color_for(i),
                edgecolor=vs.color_for(i), linewidth=1.0, zorder=3, label=f"{level}（n={len(values)}）",
            )
            drawn = True
        if not drawn:
            raise ValueError(f"histogram: {x} が全て欠損です")
        ax.legend(fontsize=8, title=hue)
    else:
        values = sub[x].dropna()
        if values.empty:
            raise ValueError(f"histogram: {x} が全て欠損です")
        ax.hist(values, bins=bins, density=density, color=vs.CATEGORICAL[0], edgecolor=vs.CATEGORICAL[0],
                 linewidth=1.0, zorder=3)
    ax.set_title(spec["title"])
    ax.set_xlabel(x)
    ax.set_ylabel("密度" if density else "度数")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig, ""


def _custom_box(sub: pd.DataFrame, spec: dict, cfg: Config, tag: str) -> tuple[plt.Figure, str]:
    y, x, hue = spec["y"], spec.get("x"), spec.get("hue")

    if x:
        x_levels = _cap_categories(sub, x, cfg, tag)
        data = sub[sub[x].astype(str).isin(x_levels)].copy()
        data[x] = data[x].astype(str)
    else:
        x_levels = None
        data = sub

    data = data.dropna(subset=[y])
    if data.empty:
        raise ValueError(f"box: {y} が全て欠損です")

    width = min(18, 6 + 0.5 * (len(x_levels) if x_levels else 1))
    fig, ax = plt.subplots(figsize=(width, 6))

    if x is None:
        if hue:
            levels = _cap_hue_levels(data, hue, cfg, tag)
            n_h = len(levels)
            w = 0.8 / max(n_h, 1)
            positions = [(i - (n_h - 1) / 2) * w for i in range(n_h)]
            groups = [data.loc[data[hue].astype(str) == level, y].values for level in levels]
            bp = ax.boxplot(groups, positions=positions, widths=w * 0.9, patch_artist=True, showfliers=False)
            for i, patch in enumerate(bp["boxes"]):
                patch.set_facecolor(vs.color_for(i))
                patch.set_alpha(0.75)
            ax.set_xticks(positions, levels)
            handles = [plt.Rectangle((0, 0), 1, 1, color=vs.color_for(i)) for i in range(n_h)]
            ax.legend(handles, levels, fontsize=8, title=hue)
        else:
            bp = ax.boxplot([data[y].values], tick_labels=[y], patch_artist=True, showfliers=False)
            for patch in bp["boxes"]:
                patch.set_facecolor(vs.CATEGORICAL[0])
                patch.set_alpha(0.75)
    else:
        idx = np.arange(len(x_levels))
        if hue:
            levels = _cap_hue_levels(data, hue, cfg, tag)
            n_h = len(levels)
            w = 0.8 / max(n_h, 1)
            for i, level in enumerate(levels):
                positions = idx + (i - (n_h - 1) / 2) * w
                groups = [
                    data.loc[(data[x] == cat) & (data[hue].astype(str) == level), y].values for cat in x_levels
                ]
                bp = ax.boxplot(groups, positions=positions, widths=w * 0.9, patch_artist=True, showfliers=False)
                for patch in bp["boxes"]:
                    patch.set_facecolor(vs.color_for(i))
                    patch.set_alpha(0.75)
            ax.set_xticks(idx, x_levels, rotation=30)
            handles = [plt.Rectangle((0, 0), 1, 1, color=vs.color_for(i)) for i in range(n_h)]
            ax.legend(handles, levels, fontsize=8, title=hue)
        else:
            groups = [data.loc[data[x] == cat, y].values for cat in x_levels]
            bp = ax.boxplot(groups, tick_labels=x_levels, patch_artist=True, showfliers=False)
            for patch in bp["boxes"]:
                patch.set_facecolor(vs.CATEGORICAL[0])
                patch.set_alpha(0.75)
            ax.tick_params(axis="x", rotation=30)

    ax.set_title(spec["title"])
    ax.set_ylabel(y)
    if x:
        ax.set_xlabel(x)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig, ""


def _custom_heatmap(sub: pd.DataFrame, spec: dict, cfg: Config, tag: str) -> tuple[plt.Figure, str]:
    method = spec.get("method", "pearson")
    if method not in ("pearson", "spearman"):
        raise ValueError(f"heatmap: 未対応の method です: {method}")
    if spec.get("hue"):
        logger.warning("%s heatmap は hue に非対応のため無視します: %s", tag, spec.get("hue"))
    cols = [c for c in spec["columns"] if pd.api.types.is_numeric_dtype(sub[c])]
    if len(cols) < 2:
        raise ValueError(f"heatmap: 数値列が2本未満です: {cols}")
    fig = _draw_corr_heatmap(sub, cols, spec["title"], method=method)
    return fig, ""


_CUSTOM_BUILDERS = {
    "scatter": _custom_scatter,
    "bar": _custom_bar,
    "histogram": _custom_histogram,
    "box": _custom_box,
    "heatmap": _custom_heatmap,
}


def _render_one_custom_chart(
    df: pd.DataFrame, spec: dict, index: int, cfg: Config, out_dir: Path, meta: AnnotationMeta,
) -> str | None:
    chart_type = spec.get("type")
    tag = f"[custom_charts/{index} {chart_type}]"
    builder = _CUSTOM_BUILDERS.get(chart_type)
    if builder is None:
        logger.warning("%s type が未知/未指定です: %r", tag, chart_type)
        return None

    error = _validate_custom_spec(df, spec, chart_type)
    if error:
        logger.warning("%s %s", tag, error)
        return None

    chart_rules = spec.get("filters", []) or []
    on_missing = cfg.get("analysis.filters_on_missing_column", "warn")
    sub = apply_filter_clauses(df, chart_rules, on_missing=on_missing)
    if len(sub) == 0:
        logger.warning("%s フィルタ適用後に 0 行のためスキップします", tag)
        return None

    render_spec = dict(spec)
    render_spec["title"] = spec.get("title") or _auto_custom_title(chart_type, spec)

    fig, extra_data_kind = builder(sub, render_spec, cfg, tag)

    filename = _custom_chart_filename(spec, index, chart_type)
    if chart_rules:
        combined_summary = f"{meta.filters_summary} ＋ 図単位: {filters_summary(sub, chart_rules)}"
    else:
        combined_summary = meta.filters_summary
    chart_meta = dataclasses.replace(meta, n_rows=len(sub), filters_summary=combined_summary)
    footnote = chart_meta.footnote(data_kind=f"カスタム図/{chart_type}{extra_data_kind}")
    return _save(fig, out_dir / filename, footnote)


def _render_custom_charts(df: pd.DataFrame, cfg: Config, out_dir: Path, meta: AnnotationMeta) -> list[str]:
    """analysis.custom_charts で宣言された追加 EDA 図を描画する（既存の固定図は変更しない）。"""
    specs = cfg.get("analysis.custom_charts", []) or []
    if not specs:
        return []

    figures: list[str] = []
    n_skipped = 0
    for index, spec in enumerate(specs, start=1):
        path = None
        try:
            if not isinstance(spec, dict):
                logger.warning("[custom_charts/%d] 設定が dict ではないためスキップします: %r", index, spec)
            else:
                path = _render_one_custom_chart(df, spec, index, cfg, out_dir, meta)
        except Exception:
            chart_type = spec.get("type") if isinstance(spec, dict) else None
            logger.warning(
                "[custom_charts/%d %s] 描画に失敗したためスキップします", index, chart_type, exc_info=True,
            )
            path = None
        if path:
            figures.append(path)
        else:
            n_skipped += 1
    logger.info("カスタム図: %d/%d 枚を出力（スキップ %d）", len(figures), len(specs), n_skipped)
    return figures


def run_eda(cfg: Config) -> dict:
    """実データパネルから EDA 図一式を生成して保存する。"""
    vs.apply_style()
    df = load_real_panel(cfg)
    target = cfg.get("analysis.eda_target", "has_repair_record")
    if target not in df.columns:
        raise ValueError(
            f"目的変数列が見つかりません: {target}（vin_panel.parquet に無い列です。"
            " analysis.eda_target を確認してください）"
        )
    spec = resolve_predictors(df, cfg)
    out_dir = cfg.path("paths.reports_dir", create=True) / "eda"
    meta = build_annotation_meta(df, cfg)
    trace_numeric = traceability_measure_columns(spec.numeric)
    groups = equipment_measure_groups(df[trace_numeric])
    max_boxplot = int(cfg.get("analysis.eda_max_measures_boxplot", 20))
    max_heatmap = int(cfg.get("analysis.eda_max_measures_heatmap", 15))
    boxplot_groups = _cap_group_columns(df, groups, max_boxplot, chart="boxplot")
    heatmap_groups = _cap_group_columns(df, groups, max_heatmap, chart="heatmap")

    figures = []
    max_categories = int(cfg.get("analysis.eda_max_categories", 15))
    fig = _fig_rate_by_category(
        df, spec.categorical, target, out_dir / "01_repair_rate_by_category.png", meta,
        max_categories=max_categories,
    )
    if fig:
        figures.append(fig)
    fig = _fig_histogram_by_target(df, trace_numeric, target, out_dir / "02_histogram_by_target.png", meta, cfg)
    if fig:
        figures.append(fig)
    figures += _fig_histogram_by_equipment(df, target, out_dir, meta, groups, cfg)
    figures += _fig_trend_boxplot_by_target(df, target, out_dir, meta, boxplot_groups)
    fig = _fig_corr_with_target(df, spec.numeric, target, out_dir / "04_correlation_with_target.png", meta)
    if fig:
        figures.append(fig)
    figures += _fig_corr_heatmap(df, target, out_dir, meta, heatmap_groups)
    fig = _fig_corr_heatmap_cross_equipment(
        df, target, out_dir / "05_correlation_heatmap__cross_equipment.png", meta, groups, cfg,
    )
    if fig:
        figures.append(fig)
    fig = _fig_daily_trend(df, target, out_dir / "06_daily_trend.png", meta)
    if fig:
        figures.append(fig)

    figures += _render_custom_charts(df, cfg, out_dir, meta)

    logger.info("EDA 完了: %d 図を %s に出力", len(figures), out_dir)
    return {"n_figures": len(figures), "output_dir": str(out_dir), "figures": figures}
