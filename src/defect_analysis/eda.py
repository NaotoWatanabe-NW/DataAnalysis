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

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import viz_style as vs
from .analysis_data import (
    AnnotationMeta,
    build_annotation_meta,
    derive_production_date,
    equipment_measure_groups,
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


def _draw_corr_heatmap(df: pd.DataFrame, cols: list[str], title: str, *, figsize_base: float = 0.42) -> plt.Figure:
    corr = df[cols].corr(numeric_only=True)
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


def _defect_kind_groups(df: pd.DataFrame) -> dict[str, list[str]]:
    """`defect_{source}__kind__{種類}` 列を検査工程（source）別にグルーピングする。"""
    groups: dict[str, list[str]] = {}
    for col in df.columns:
        if "__kind__" not in col or not col.startswith("defect_"):
            continue
        source = col[len("defect_"):].split("__kind__")[0]
        groups.setdefault(source, []).append(col)
    return {k: sorted(v) for k, v in sorted(groups.items())}


def _fig_defect_category_breakdown(df: pd.DataFrame, out: Path, meta: AnnotationMeta) -> str | None:
    groups = _defect_kind_groups(df)
    if not groups:
        logger.warning("不良種類別カウント列が見つからないため、不良カテゴリ内訳の図をスキップします。")
        return None

    n = len(groups)
    fig, axes = plt.subplots(1, n, figsize=(6 * n, 5.5))
    axes = np.atleast_1d(axes)
    for ax, (source, cols) in zip(axes, groups.items()):
        totals = df[cols].sum().sort_values(ascending=False)
        totals.index = [c.split("__kind__", 1)[-1] for c in totals.index]
        colors = [vs.color_for(i) for i in range(len(totals))]
        ax.bar(totals.index, totals.values, color=colors, width=0.6, zorder=3)
        for x, v in enumerate(totals.values):
            ax.text(x, v, f"{int(v)}", ha="center", va="bottom", fontsize=10, color=vs.INK_SECONDARY)
        ax.set_title(f"{source}")
        ax.set_ylabel("不良件数")
    fig.suptitle("不良カテゴリ別 件数（検査工程別。未検査 VIN は集計対象外）", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    footnote = meta.footnote(data_kind="不良カテゴリ内訳")
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
    fig = _fig_defect_category_breakdown(df, out_dir / "07_defect_category_breakdown.png", meta)
    if fig:
        figures.append(fig)

    logger.info("EDA 完了: %d 図を %s に出力", len(figures), out_dir)
    return {"n_figures": len(figures), "output_dir": str(out_dir), "figures": figures}
