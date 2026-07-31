"""リペア記録有無で色分けしたトレーサビリティ測定値のヒストグラム（試作）。

`data/interim/vin_panel.parquet`（assemble 済みの実データパネル）が入力。
`has_repair_record`（VIN 単位のリペア記録有無フラグ）で2群に分け、
上塗ロボットの測定値分布を比較する。群のサイズが大きく異なるため density=True で正規化する。

実行:
    .venv/bin/python scripts/repair_flag_histogram.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from defect_analysis import viz_style as vs  # noqa: E402

PANEL_PATH = _ROOT / "data" / "interim" / "vin_panel.parquet"
OUT_PATH = _ROOT / "reports" / "repair_flag_histogram.png"

# 比較する測定値（列, 表示ラベル）。両方とも上塗ロボットの多数回測定を VIN 単位で平均した列で、
# 欠損が少なく分布に広がりがある（vin_panel_dictionary.csv で確認済み）。
MEASURES = [
    ("上塗ロボット__塗料使用量__mean", "上塗ロボット 塗料使用量（平均）"),
    ("上塗ロボット__サイクルタイム__mean", "上塗ロボット サイクルタイム（平均）"),
]
FLAG_COLUMN = "has_repair_record"
GROUP_LABELS = {0: "リペア記録なし", 1: "リペア記録あり"}


def build_group_series(panel: pd.DataFrame, measure: str) -> dict[int, pd.Series]:
    """flag 値ごとに、欠損を除いた measure 列の値を返す。"""
    groups: dict[int, pd.Series] = {}
    for flag_value in (0, 1):
        mask = panel[FLAG_COLUMN] == flag_value
        groups[flag_value] = panel.loc[mask, measure].dropna()
    return groups


def plot_histograms(panel: pd.DataFrame, measures: list[tuple[str, str]]) -> plt.Figure:
    vs.apply_style()
    fig, axes = plt.subplots(1, len(measures), figsize=(6.4 * len(measures), 4.6))
    if len(measures) == 1:
        axes = [axes]

    for ax, (col, label) in zip(axes, measures):
        groups = build_group_series(panel, col)
        for flag_value, color_index in ((0, 0), (1, 1)):
            values = groups[flag_value]
            n = len(values)
            ax.hist(
                values, bins=30, density=True, alpha=0.55,
                color=vs.color_for(color_index), edgecolor=vs.color_for(color_index),
                linewidth=1.2, zorder=3,
                label=f"{GROUP_LABELS[flag_value]}（n={n}）",
            )
        ax.set_title(label)
        ax.set_xlabel(label)
        ax.set_ylabel("密度（density=True）")
        ax.legend()

    fig.suptitle("リペア記録有無別の上塗ロボット測定値分布（試作）", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return fig


def main() -> None:
    panel = pd.read_parquet(PANEL_PATH, columns=[FLAG_COLUMN, *[c for c, _ in MEASURES]])
    fig = plot_histograms(panel, MEASURES)
    vs.save_figure(
        fig, OUT_PATH,
        footnote=f"出所: {PANEL_PATH.relative_to(_ROOT)}（has_repair_record で層別、density 正規化）",
    )


if __name__ == "__main__":
    main()
