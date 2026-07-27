"""ステップ4: データ型別の自動 EDA グラフ生成。

分布・群差・関係性・時系列を把握するための図を reports/eda に出力する。
配色は dpviz の検証済みパレット（viz_style）を用いる。
説明変数はリーク安全な集合（analysis_data.resolve_predictors）に限定する。
トレンド測定値の分布・相関は features のトレンド列（'{EQ-xx}__{measure}'）から
動的に設備をグルーピングし、設備ごとに図を出力する（固定リストは持たない）。
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
    equipment_measure_groups,
    equipment_name_map,
    get_targets,
    load_features,
    resolve_predictors,
)
from .config import Config

logger = logging.getLogger(__name__)


def _save(fig: plt.Figure, path: Path, footnote: str | None = None) -> str:
    return vs.save_figure(fig, path, footnote, log=logger)


def _resolve_highlight(cfg: Config) -> set[str]:
    """ドライバ測定値ハイライト対象の '{eq}__{measure}' 集合を返す（無ければ空集合）。

    analysis.eda_driver_highlight が空なら synthesize.defect_drivers を参照し、
    それも無ければハイライトなし（＝図の生成有無はこの設定に依存しない）。
    """
    explicit = cfg.get("analysis.eda_driver_highlight", []) or []
    if explicit:
        return set(explicit)
    drivers = cfg.get("synthesize.defect_drivers", []) or []
    return {f"{d['equipment_id']}__{d['measure']}" for d in drivers if "equipment_id" in d and "measure" in d}


def _rate_by(df: pd.DataFrame, col: str, target: str) -> pd.Series:
    """カテゴリ別の目的変数（0/1）平均を % で返す。"""
    return df.groupby(col)[target].mean().mul(100).sort_values(ascending=False)


def _fig_rate_by_category(
    df: pd.DataFrame, cat_cols: list[str], target: str, out: Path, meta: AnnotationMeta
) -> str:
    cols = [c for c in cat_cols if df[c].nunique() > 1][:4]
    n = len(cols)
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    axes = axes.ravel()
    overall = df[target].mean() * 100
    for i, col in enumerate(cols):
        ax = axes[i]
        rates = _rate_by(df, col, target)
        ax.bar(rates.index.astype(str), rates.values, color=vs.color_for(i), width=0.62, zorder=3)
        ax.axhline(overall, color=vs.MUTED, lw=1.2, ls="--", zorder=2)
        ax.set_title(f"{col} 別 不良率")
        ax.set_ylabel("不良率 (%)")
        ax.tick_params(axis="x", rotation=30)
        for x, v in enumerate(rates.values):  # 直接ラベル
            ax.text(x, v, f"{v:.1f}", ha="center", va="bottom", fontsize=9, color=vs.INK_SECONDARY)
    for j in range(n, 4):
        axes[j].set_visible(False)
    fig.suptitle(f"カテゴリ別 不良率（破線=全体 {overall:.1f}%）", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    footnote = meta.footnote(data_kind="不良率(カテゴリ別)")
    return _save(fig, out, footnote)


def _fig_trend_boxplot_by_target(
    df: pd.DataFrame,
    target: str,
    out_dir: Path,
    meta: AnnotationMeta,
    groups: dict[str, list[str]],
    name_map: dict[str, str],
    highlight: set[str],
) -> list[str]:
    if not groups:
        logger.warning("トレンド列が見つからないため、箱ひげ図をスキップします。")
        return []

    labels = ["不良なし", "不良あり"]
    figures = []
    for eq, cols in groups.items():
        eq_label = f"{eq} {name_map.get(eq, '')}".strip()
        n = len(cols)
        n_cols_grid = 2
        n_rows_grid = max(1, -(-n // n_cols_grid))  # 測定値数に応じた行数（切り上げ）
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
            ax.set_title(f"{measure}*" if m in highlight else measure)
            ax.set_ylabel("測定値")
        for j in range(n, n_rows_grid * n_cols_grid):
            axes[j].set_visible(False)
        fig.suptitle(f"{eq_label}: 不良有無で層別したドライバ測定値の分布", fontsize=13, fontweight="bold")
        fig.tight_layout(rect=(0, 0.04, 1, 0.97))
        footnote = meta.footnote(data_kind="ドライバ測定値の分布", equipment=eq_label)
        figures.append(_save(fig, out_dir / f"02_trend_boxplot__{eq}.png", footnote))
    return figures


def _fig_corr_with_target(
    df: pd.DataFrame, numeric: list[str], target: str, out: Path, meta: AnnotationMeta, top: int = 15
) -> str:
    corr = df[numeric + [target]].corr(numeric_only=True)[target].drop(target)
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
    # 凡例代わりの直接注記
    ax.text(0.99, 0.02, "青=正の相関 / 赤=負の相関", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=9, color=vs.MUTED)
    fig.tight_layout()
    footnote = meta.footnote(data_kind="目的変数との相関")
    return _save(fig, out, footnote)


def _fig_corr_heatmap(
    df: pd.DataFrame,
    target: str,
    out_dir: Path,
    meta: AnnotationMeta,
    groups: dict[str, list[str]],
    name_map: dict[str, str],
) -> list[str]:
    if not groups:
        logger.warning("トレンド列が見つからないため、相関ヒートマップをスキップします。")
        return []

    pass_groups = equipment_measure_groups(df, include_pass_sec=True)
    figures = []
    for eq in groups:
        cols = pass_groups.get(eq, groups[eq]) + [target]
        if len(cols) < 2:  # target 含め2列未満は相関が意味を持たないためスキップ
            continue
        corr = df[cols].corr(numeric_only=True)
        fig, ax = plt.subplots(figsize=(8.5, 7))
        im = ax.imshow(corr.values, cmap=vs.diverging_cmap(), vmin=-1, vmax=1)
        ax.set_xticks(range(len(cols)), cols, rotation=45, ha="right", fontsize=9)
        ax.set_yticks(range(len(cols)), cols, fontsize=9)
        for i in range(len(cols)):
            for j in range(len(cols)):
                v = corr.values[i, j]
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7,
                        color=vs.INK_PRIMARY if abs(v) < 0.6 else "#ffffff")
        cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label("相関係数", color=vs.INK_SECONDARY)
        eq_label = f"{eq} {name_map.get(eq, '')}".strip()
        ax.set_title(f"{eq_label}: 測定値相関ヒートマップ")
        ax.grid(False)
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        footnote = meta.footnote(data_kind="測定値相関", equipment=eq_label)
        figures.append(_save(fig, out_dir / f"04_correlation_heatmap__{eq}.png", footnote))
    return figures


def _fig_monthly_trend(df: pd.DataFrame, target: str, out: Path, meta: AnnotationMeta) -> str:
    g = df.groupby("process_month")
    rate = g[target].mean().mul(100)
    n_vin = g.size()
    n_defect = g[target].sum()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True, height_ratios=[3, 2])
    ax1.plot(rate.index.astype(str), rate.values, color=vs.CATEGORICAL[0], lw=2, marker="o", ms=7, zorder=3)
    for x, v in enumerate(rate.values):
        ax1.text(x, v, f" {v:.1f}%", va="bottom", fontsize=9, color=vs.INK_SECONDARY)
    ax1.set_title("月次の不良率トレンド")
    ax1.set_ylabel("不良率 (%)")

    idx = np.arange(len(n_vin))
    w = 0.4
    ax2.bar(idx - w / 2, n_vin.values, width=w, color=vs.CATEGORICAL[2], label="生産VIN数", zorder=3)
    ax2.bar(idx + w / 2, n_defect.values, width=w, color=vs.CATEGORICAL[1], label="不良ありVIN数", zorder=3)
    ax2.set_xticks(idx, n_vin.index.astype(str))
    ax2.set_ylabel("台数")
    ax2.set_xlabel("生産月")
    ax2.legend()
    fig.suptitle("月次トレンド（不良率・台数）", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    footnote = meta.footnote(data_kind="月次トレンド")
    return _save(fig, out, footnote)


def _fig_defect_category_breakdown(df: pd.DataFrame, out: Path, meta: AnnotationMeta) -> str:
    cnt_cols = [c for c in df.columns if c.startswith("defect_cnt_")]
    totals = df[cnt_cols].sum().sort_values(ascending=False)
    totals.index = [c.replace("defect_cnt_", "") for c in totals.index]
    fig, ax = plt.subplots(figsize=(9, 5.5))
    colors = [vs.color_for(i) for i in range(len(totals))]
    ax.bar(totals.index, totals.values, color=colors, width=0.6, zorder=3)
    for x, v in enumerate(totals.values):
        ax.text(x, v, f"{int(v)}", ha="center", va="bottom", fontsize=10, color=vs.INK_SECONDARY)
    ax.set_title("不良カテゴリ別 件数")
    ax.set_ylabel("不良件数")
    fig.tight_layout()
    footnote = meta.footnote(data_kind="不良カテゴリ内訳")
    return _save(fig, out, footnote)


def run_eda(cfg: Config) -> dict:
    """EDA 図一式を生成して保存する。"""
    vs.apply_style()
    df = load_features(cfg)
    spec = resolve_predictors(df, cfg)
    target = cfg.get("analysis.eda_target", "has_defect")
    out_dir = cfg.path("paths.reports_dir", create=True) / "eda"
    meta = build_annotation_meta(df, cfg)
    groups = equipment_measure_groups(df)
    name_map = equipment_name_map(cfg)
    highlight = _resolve_highlight(cfg)

    figures = [_fig_rate_by_category(df, spec.categorical, target, out_dir / "01_defect_rate_by_category.png", meta)]
    figures += _fig_trend_boxplot_by_target(df, target, out_dir, meta, groups, name_map, highlight)
    figures.append(_fig_corr_with_target(df, spec.numeric, target, out_dir / "03_correlation_with_target.png", meta))
    figures += _fig_corr_heatmap(df, target, out_dir, meta, groups, name_map)
    figures.append(_fig_monthly_trend(df, target, out_dir / "05_monthly_defect_trend.png", meta))
    figures.append(_fig_defect_category_breakdown(df, out_dir / "06_defect_category_breakdown.png", meta))

    logger.info("EDA 完了: %d 図を %s に出力", len(figures), out_dir)
    return {"n_figures": len(figures), "output_dir": str(out_dir), "figures": figures}
