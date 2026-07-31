"""ステップ5: 統計手法で相関・群間差を判定する（実データパネルが対象）。

目的変数（analysis.targets。既定は classification=[has_repair_record],
regression=[defect_*__count 等]）に対して、工程系の説明変数それぞれの関連を
単変量検定で評価する。多重比較は BH-FDR で補正。

  数値 × 2群       : Welch t 検定 / Mann-Whitney U（効果量 Cohen's d / 順位二列相関）
  カテゴリ × 2群    : カイ二乗検定（効果量 Cramér's V）
  数値 × 連続       : Pearson / Spearman 相関
  カテゴリ × 連続   : 一元配置分散分析 ANOVA / Kruskal-Wallis（効果量 eta² / epsilon²）

設計方針どおり、単変量結果は「候補判断」に留め、削除の即断はしない。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import stats

from .analysis_data import drop_all_missing, get_targets, load_real_panel, resolve_predictors
from .config import Config
from .io_utils import resolve_format  # noqa: F401  (レポートは CSV 固定だが整合のため)

logger = logging.getLogger(__name__)

ALPHA = 0.05


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan")
    sa, sb = a.std(ddof=1), b.std(ddof=1)
    pooled = np.sqrt(((na - 1) * sa**2 + (nb - 1) * sb**2) / (na + nb - 2))
    if pooled == 0:
        return float("nan")
    return float((b.mean() - a.mean()) / pooled)


def _cramers_v(confusion: np.ndarray) -> float:
    chi2 = stats.chi2_contingency(confusion)[0]
    n = confusion.sum()
    r, k = confusion.shape
    denom = n * (min(r, k) - 1)
    return float(np.sqrt(chi2 / denom)) if denom > 0 else float("nan")


def _bh_fdr(pvals: pd.Series) -> pd.Series:
    """Benjamini-Hochberg 補正後 p 値を返す。"""
    p = pvals.to_numpy(dtype=float)
    mask = ~np.isnan(p)
    out = np.full_like(p, np.nan)
    idx = np.where(mask)[0]
    m = len(idx)
    if m == 0:
        return pd.Series(out, index=pvals.index)
    order = idx[np.argsort(p[idx])]
    ranked = p[order]
    adj = ranked * m / (np.arange(1, m + 1))
    adj = np.minimum.accumulate(adj[::-1])[::-1]  # 単調化
    out[order] = np.clip(adj, 0, 1)
    return pd.Series(out, index=pvals.index)


def _test_numeric_vs_binary(x: pd.Series, y: pd.Series, target: str, feature: str) -> list[dict]:
    g0 = x[y == 0].dropna().to_numpy()
    g1 = x[y == 1].dropna().to_numpy()
    if len(g0) < 2 or len(g1) < 2 or x.nunique() < 2:
        return []
    rows = []
    t_stat, t_p = stats.ttest_ind(g1, g0, equal_var=False)
    rows.append(_row(target, feature, "numeric", "welch_t", t_stat, t_p, _cohens_d(g0, g1), "cohens_d", len(g0) + len(g1)))
    u_stat, u_p = stats.mannwhitneyu(g1, g0, alternative="two-sided")
    rank_biserial = 1 - (2 * u_stat) / (len(g1) * len(g0))
    rows.append(_row(target, feature, "numeric", "mann_whitney", u_stat, u_p, rank_biserial, "rank_biserial", len(g0) + len(g1)))
    return rows


def _test_categorical_vs_binary(x: pd.Series, y: pd.Series, target: str, feature: str) -> list[dict]:
    if x.nunique() < 2:
        return []
    confusion = pd.crosstab(x, y).to_numpy()
    if confusion.shape[0] < 2 or confusion.shape[1] < 2:
        return []
    chi2, p, dof, _ = stats.chi2_contingency(confusion)
    return [_row(target, feature, "categorical", "chi2", chi2, p, _cramers_v(confusion), "cramers_v", int(confusion.sum()))]


def _test_numeric_vs_continuous(x: pd.Series, y: pd.Series, target: str, feature: str) -> list[dict]:
    d = pd.concat([x, y], axis=1).dropna()
    if len(d) < 3 or x.nunique() < 2:
        return []
    xv, yv = d.iloc[:, 0].to_numpy(), d.iloc[:, 1].to_numpy()
    rows = []
    r, rp = stats.pearsonr(xv, yv)
    rows.append(_row(target, feature, "numeric", "pearson", r, rp, r, "pearson_r", len(d)))
    rho, sp = stats.spearmanr(xv, yv)
    rows.append(_row(target, feature, "numeric", "spearman", rho, sp, rho, "spearman_rho", len(d)))
    return rows


def _test_categorical_vs_continuous(x: pd.Series, y: pd.Series, target: str, feature: str) -> list[dict]:
    if x.nunique() < 2:
        return []
    groups = [y[x == lv].dropna().to_numpy() for lv in x.dropna().unique()]
    groups = [g for g in groups if len(g) >= 2]
    if len(groups) < 2:
        return []
    rows = []
    f_stat, f_p = stats.f_oneway(*groups)
    rows.append(_row(target, feature, "categorical", "anova", f_stat, f_p, _eta_squared(groups), "eta2", sum(len(g) for g in groups)))
    h_stat, h_p = stats.kruskal(*groups)
    n = sum(len(g) for g in groups)
    epsilon2 = (h_stat - len(groups) + 1) / (n - len(groups)) if n > len(groups) else float("nan")
    rows.append(_row(target, feature, "categorical", "kruskal", h_stat, h_p, epsilon2, "epsilon2", n))
    return rows


def _eta_squared(groups: list[np.ndarray]) -> float:
    all_vals = np.concatenate(groups)
    grand = all_vals.mean()
    ss_total = ((all_vals - grand) ** 2).sum()
    ss_between = sum(len(g) * (g.mean() - grand) ** 2 for g in groups)
    return float(ss_between / ss_total) if ss_total > 0 else float("nan")


def _row(target, feature, ftype, test, stat, p, effect, effect_name, n) -> dict:
    return {
        "target": target, "feature": feature, "feature_type": ftype, "test": test,
        "statistic": float(stat), "p_value": float(p), "effect_size": float(effect),
        "effect_name": effect_name, "n": int(n),
    }


def run_stats(cfg: Config) -> dict:
    """実データパネルに対し、全目的変数×説明変数の単変量検定を実施し、CSV とサマリを出力する。"""
    df = load_real_panel(cfg)
    spec = drop_all_missing(df, resolve_predictors(df, cfg))
    targets = get_targets(cfg)
    reports_dir = cfg.path("paths.reports_dir", create=True) / "stats"
    reports_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for target in targets.get("classification", []):
        if target not in df.columns:
            continue
        y = df[target]
        for f in spec.numeric:
            rows += _test_numeric_vs_binary(df[f], y, target, f)
        for f in spec.categorical:
            rows += _test_categorical_vs_binary(df[f], y, target, f)
    for target in targets.get("regression", []):
        if target not in df.columns:
            continue
        y = df[target]
        for f in spec.numeric:
            rows += _test_numeric_vs_continuous(df[f], y, target, f)
        for f in spec.categorical:
            rows += _test_categorical_vs_continuous(df[f], y, target, f)

    result = pd.DataFrame(rows)
    if result.empty:
        logger.warning("検定対象がありませんでした。")
        return {"n_tests": 0}

    # 目的変数ごとに BH-FDR 補正
    result["p_adjusted"] = np.nan
    for target, grp in result.groupby("target"):
        result.loc[grp.index, "p_adjusted"] = _bh_fdr(grp["p_value"]).to_numpy()
    result["significant"] = result["p_adjusted"] < ALPHA
    result = result.sort_values(["target", "p_adjusted", "test"]).reset_index(drop=True)

    out_csv = reports_dir / "statistical_tests.csv"
    result.to_csv(out_csv, index=False)
    _write_summary(result, reports_dir / "statistical_summary.md")

    n_sig = int(result["significant"].sum())
    logger.info("統計検定完了: %d 検定, 有意(FDR<%.2f) %d 件 -> %s", len(result), ALPHA, n_sig, out_csv)
    return {"n_tests": len(result), "n_significant": n_sig, "output": str(out_csv)}


def _write_summary(result: pd.DataFrame, path) -> None:
    """有意な関連を目的変数別・効果量順にまとめた markdown を出力する。"""
    lines = ["# 統計検定サマリ（単変量・FDR補正）", ""]
    for target, grp in result.groupby("target"):
        sig = grp[grp["significant"]].copy()
        sig["abs_effect"] = sig["effect_size"].abs()
        # 特徴量ごとに最大効果量の検定を代表として抽出
        top = sig.sort_values("abs_effect", ascending=False).drop_duplicates("feature").head(10)
        lines.append(f"## 目的変数: {target}")
        lines.append(f"- 有意な検定: {int(grp['significant'].sum())} / {len(grp)}")
        if top.empty:
            lines.append("- 有意な説明変数なし")
        else:
            lines.append("")
            lines.append("| 説明変数 | 型 | 検定 | 効果量 | p(FDR) |")
            lines.append("|---|---|---|---|---|")
            for _, r in top.iterrows():
                lines.append(
                    f"| {r['feature']} | {r['feature_type']} | {r['test']} | "
                    f"{r['effect_name']}={r['effect_size']:.3f} | {r['p_adjusted']:.2e} |"
                )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("統計サマリを出力: %s", path)
