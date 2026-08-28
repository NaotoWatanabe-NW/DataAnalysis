"""ステップ6: 機械学習で予測性能を判定する（主軸: LightGBM。実データパネルが対象）。

工程系の説明変数のみ（リーク列除外）から、analysis.targets（既定は
classification=[has_repair_record], regression=[repair_修正__count 等]）を予測する。
ベースライン→木モデル→LightGBM の順で交差検証比較し、LightGBM で保持テストの評価図と
特徴量重要度（gain / permutation）を出力する。p ≫ n（説明変数が数百〜千に対し行数は
千数百）であるため、過学習・不安定な重要度に注意（README 参照）。
"""

from __future__ import annotations

import logging

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import auc, precision_recall_curve, roc_curve
from sklearn.model_selection import KFold, StratifiedKFold, cross_validate, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from . import viz_style as vs
from .analysis_data import (
    AnnotationMeta,
    FeatureSpec,
    build_annotation_meta,
    drop_all_missing,
    get_targets,
    load_real_panel,
    resolve_predictors,
)
from .config import Config

logger = logging.getLogger(__name__)

_CLF_SCORING = ["roc_auc", "average_precision", "f1", "recall", "accuracy"]
_REG_SCORING = ["neg_mean_absolute_error", "neg_root_mean_squared_error", "r2"]
# 表示用の指標名と符号（neg_* は反転）
_METRIC_LABEL = {
    "roc_auc": ("ROC-AUC", 1), "average_precision": ("PR-AUC", 1), "f1": ("F1", 1),
    "recall": ("Recall", 1), "accuracy": ("Accuracy", 1),
    "neg_mean_absolute_error": ("MAE", -1), "neg_root_mean_squared_error": ("RMSE", -1), "r2": ("R2", 1),
}


def _build_preprocessor(spec: FeatureSpec) -> ColumnTransformer:
    """欠損を補完してからスケーリング/エンコードする。

    実データは構造的に欠損が多い（ある VIN がその設備を通過していなければ、
    その設備の測定値列は全て NaN）。scikit-learn の StandardScaler/OneHotEncoder は
    NaN を扱えないため、Imputer を先に挟む（LightGBM は NaN をネイティブに扱えるが、
    パイプラインは baseline/RandomForest とも共有するため全モデル向けに補完する）。
    """
    numeric_pipe = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
    ])
    categorical_pipe = Pipeline([
        ("impute", SimpleImputer(strategy="constant", fill_value="missing")),
        ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
    ])
    return ColumnTransformer(
        transformers=[
            ("num", numeric_pipe, spec.numeric),
            ("cat", categorical_pipe, spec.categorical),
        ],
        remainder="drop",
    )


def _models(task: str, seed: int) -> dict[str, object]:
    if task == "classification":
        return {
            "logistic(baseline)": LogisticRegression(max_iter=1000, class_weight="balanced"),
            "random_forest": RandomForestClassifier(n_estimators=300, class_weight="balanced", random_state=seed, n_jobs=-1),
            "lightgbm": LGBMClassifier(n_estimators=400, class_weight="balanced", random_state=seed, verbose=-1),
        }
    return {
        "linear(baseline)": LinearRegression(),
        "random_forest": RandomForestRegressor(n_estimators=300, random_state=seed, n_jobs=-1),
        "lightgbm": LGBMRegressor(n_estimators=400, random_state=seed, verbose=-1),
    }


def _cross_validate(models, pre, X, y, task, cv) -> pd.DataFrame:
    scoring = _CLF_SCORING if task == "classification" else _REG_SCORING
    rows = []
    for name, model in models.items():
        pipe = Pipeline([("pre", pre), ("model", model)])
        cvres = cross_validate(pipe, X, y, scoring=scoring, cv=cv, n_jobs=-1)
        for score in scoring:
            label, sign = _METRIC_LABEL[score]
            vals = cvres[f"test_{score}"] * sign
            rows.append({"model": name, "metric": label, "cv_mean": float(np.mean(vals)), "cv_std": float(np.std(vals))})
    return pd.DataFrame(rows)


def _fig_model_comparison(perf: pd.DataFrame, target: str, primary: str, out, meta: AnnotationMeta) -> str:
    sub = perf[perf["metric"] == primary].set_index("model")
    fig, ax = plt.subplots(figsize=(8, 4.8))
    colors = [vs.color_for(i) for i in range(len(sub))]
    ax.bar(sub.index, sub["cv_mean"], yerr=sub["cv_std"], color=colors, width=0.55, zorder=3,
           error_kw={"ecolor": vs.MUTED, "capsize": 4})
    for x, (m, s) in enumerate(zip(sub["cv_mean"], sub["cv_std"])):
        ax.text(x, m, f"{m:.3f}", ha="center", va="bottom", fontsize=9, color=vs.INK_SECONDARY)
    ax.set_title(f"{target}: モデル別 {primary}（{int(perf.attrs.get('cv_folds', 5))}分割CV平均±SD）")
    ax.set_ylabel(primary)
    fig.tight_layout()
    footnote = meta.footnote(data_kind="モデル比較(CV)")
    return _save(fig, out, footnote)


def _fig_roc_pr(y_true, y_score, target: str, out, meta: AnnotationMeta) -> str:
    fpr, tpr, _ = roc_curve(y_true, y_score)
    roc_auc = auc(fpr, tpr)
    prec, rec, _ = precision_recall_curve(y_true, y_score)
    pr_auc = auc(rec, prec)
    base = float(np.mean(y_true))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 5))
    ax1.plot(fpr, tpr, color=vs.CATEGORICAL[0], lw=2, label=f"ROC (AUC={roc_auc:.3f})")
    ax1.plot([0, 1], [0, 1], color=vs.MUTED, lw=1, ls="--")
    ax1.set_title("ROC 曲線")
    ax1.set_xlabel("偽陽性率")
    ax1.set_ylabel("真陽性率")
    ax1.legend(loc="lower right")
    ax2.plot(rec, prec, color=vs.CATEGORICAL[1], lw=2, label=f"PR (AUC={pr_auc:.3f})")
    ax2.axhline(base, color=vs.MUTED, lw=1, ls="--", label=f"ベースライン={base:.3f}")
    ax2.set_title("Precision-Recall 曲線")
    ax2.set_xlabel("Recall")
    ax2.set_ylabel("Precision")
    ax2.legend(loc="upper right")
    fig.suptitle(f"{target}: LightGBM 保持テスト評価", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.96))
    footnote = meta.footnote(data_kind="ROC/PR(保持テスト)")
    return _save(fig, out, footnote)


def _fig_pred_vs_actual(y_true, y_pred, target: str, out, meta: AnnotationMeta) -> str:
    fig, ax = plt.subplots(figsize=(6.5, 6))
    ax.scatter(y_true, y_pred, s=18, alpha=0.4, color=vs.CATEGORICAL[0], edgecolor="none", zorder=3)
    lo, hi = float(min(y_true.min(), y_pred.min())), float(max(y_true.max(), y_pred.max()))
    ax.plot([lo, hi], [lo, hi], color=vs.MUTED, lw=1.2, ls="--")
    ax.set_title(f"{target}: 予測 vs 実測（LightGBM 保持テスト）")
    ax.set_xlabel("実測値")
    ax.set_ylabel("予測値")
    fig.tight_layout()
    footnote = meta.footnote(data_kind="予測vs実測")
    return _save(fig, out, footnote)


def _fig_importance(imp: pd.DataFrame, target: str, out, meta: AnnotationMeta, top: int = 15) -> str:
    d = imp.sort_values("gain_importance", ascending=False).head(top).iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.barh(d["feature"], d["gain_importance"], color=vs.CATEGORICAL[0], zorder=3)
    ax.set_title(f"{target}: LightGBM 特徴量重要度（gain, 上位{top}）")
    ax.set_xlabel("gain importance")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    footnote = meta.footnote(data_kind="特徴量重要度")
    return _save(fig, out, footnote)


def _feature_importance(model, Xt_test, y_test, names, task: str, seed: int) -> pd.DataFrame:
    """変換後特徴量レベルで gain と permutation の重要度を揃えて返す。"""
    gain = model.feature_importances_
    scoring = "roc_auc" if task == "classification" else "r2"
    perm = permutation_importance(model, Xt_test, y_test, scoring=scoring, n_repeats=10, random_state=seed, n_jobs=-1)
    return pd.DataFrame(
        {
            "feature": [n.split("__", 1)[-1] for n in names],
            "gain_importance": gain,
            "permutation_importance": perm.importances_mean,
        }
    ).sort_values("gain_importance", ascending=False).reset_index(drop=True)


def _save(fig: plt.Figure, path, footnote: str | None = None) -> str:
    return vs.save_figure(fig, path, footnote, log=logger)


def _run_one_target(target: str, task: str, df: pd.DataFrame, spec: FeatureSpec, cfg: Config,
                    out_dir, seed: int, folds: int, test_size: float, meta: AnnotationMeta) -> pd.DataFrame:
    X = df[spec.all]
    y = df[target]
    pre = _build_preprocessor(spec)
    models = _models(task, seed)

    if task == "classification":
        cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
        primary = "ROC-AUC"
        strat = y
    else:
        cv = KFold(n_splits=folds, shuffle=True, random_state=seed)
        primary = "R2"
        strat = None

    perf = _cross_validate(models, pre, X, y, task, cv)
    perf.insert(0, "target", target)
    perf.insert(1, "task", task)
    perf.attrs["cv_folds"] = folds
    _fig_model_comparison(perf, target, primary, out_dir / f"{target}_model_comparison.png", meta)

    # LightGBM を保持テストで評価し、図と重要度を出力（前処理を明示適用し
    # gain と permutation を同じ変換後特徴量レベルで揃える）
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=test_size, random_state=seed, stratify=strat)
    pre.fit(X_tr)
    Xt_tr, Xt_te = pre.transform(X_tr), pre.transform(X_te)
    names = pre.get_feature_names_out()
    main = _models(task, seed)["lightgbm"]
    main.fit(Xt_tr, y_tr)
    if task == "classification":
        y_score = main.predict_proba(Xt_te)[:, 1]
        _fig_roc_pr(y_te.to_numpy(), y_score, target, out_dir / f"{target}_roc_pr.png", meta)
    else:
        _fig_pred_vs_actual(
            y_te, pd.Series(main.predict(Xt_te), index=y_te.index), target, out_dir / f"{target}_pred_vs_actual.png", meta
        )

    imp = _feature_importance(main, Xt_te, y_te, names, task, seed)
    imp.insert(0, "target", target)
    imp.to_csv(out_dir / f"feature_importance_{target}.csv", index=False)
    _fig_importance(imp, target, out_dir / f"{target}_feature_importance.png", meta)
    return perf


def run_ml(cfg: Config) -> dict:
    """実データパネルに対し、全目的変数について交差検証比較・保持テスト評価・重要度を出力する。"""
    vs.apply_style()
    df = load_real_panel(cfg)
    spec = drop_all_missing(df, resolve_predictors(df, cfg))
    targets = get_targets(cfg)
    a = cfg.get("analysis", {}) or {}
    seed = int(a.get("random_state", 42))
    folds = int(a.get("cv_folds", 5))
    test_size = float(a.get("test_size", 0.25))
    out_dir = cfg.path("paths.reports_dir", create=True) / "ml"
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("説明変数: 数値%d + カテゴリ%d = %d 列（リーク列除外済）",
                len(spec.numeric), len(spec.categorical), len(spec.all))

    all_perf = []
    tasks = [("classification", t) for t in targets.get("classification", [])]
    tasks += [("regression", t) for t in targets.get("regression", [])]
    for task, target in tasks:
        if target not in df.columns:
            continue
        # 目的変数が NaN の行（例: defect_*__has は「未検査」で NaN）は学習対象から除外する。
        # 0 埋めしていない設計（docs/real_data_ingest_design.md §13-7）なので、ここで落とさないと
        # scikit-learn が「y に NaN がある」で例外を投げる。
        target_df = df[df[target].notna()]
        n_dropped = len(df) - len(target_df)
        if n_dropped:
            logger.info("[%s] 目的変数が欠損（未検査等）の %d 行を学習対象から除外しました。", target, n_dropped)
        if task == "classification" and target_df[target].nunique() < 2:
            logger.warning("[%s] 目的変数が単一クラスのため学習をスキップします。", target)
            continue
        target_meta = build_annotation_meta(target_df, cfg)
        logger.info("=== モデル学習: %s (%s) ===", target, task)
        all_perf.append(
            _run_one_target(target, task, target_df, spec, cfg, out_dir, seed, folds, test_size, target_meta)
        )

    perf = pd.concat(all_perf, ignore_index=True)
    perf_path = out_dir / "model_performance.csv"
    perf.to_csv(perf_path, index=False)
    logger.info("機械学習完了: 性能レポート -> %s", perf_path)
    return {"n_targets": len(tasks), "output": str(perf_path), "output_dir": str(out_dir)}
