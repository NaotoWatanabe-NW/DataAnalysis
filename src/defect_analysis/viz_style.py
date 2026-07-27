"""可視化スタイル（検証済みパレット + 日本語フォント）。

dataviz スキルの reference palette（light surface）を matplotlib に適用する。
カテゴリは固定順で使い（循環させない）、相関などの発散表現は blue↔red + グレー中点。
"""

from __future__ import annotations

import logging

import matplotlib

matplotlib.use("Agg")  # 非GUI環境で PNG 出力
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.font_manager import fontManager

logger = logging.getLogger(__name__)

# カテゴリ配色（固定順・循環禁止）: blue, orange, aqua, yellow, magenta, green, violet, red
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
# クローム/インク
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
# ステータス
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

_CJK_CANDIDATES = ["Noto Sans CJK JP", "IPAGothic", "TakaoGothic", "VL Gothic", "Noto Sans JP"]


def _resolve_cjk_font() -> str | None:
    available = {f.name for f in fontManager.ttflist}
    for name in _CJK_CANDIDATES:
        if name in available:
            return name
    return None


def apply_style() -> None:
    """matplotlib のグローバルスタイルを適用する（recessive なグリッド・軸）。"""
    font = _resolve_cjk_font()
    if font is None:
        logger.warning("日本語フォントが見つかりません。ラベルが文字化けする可能性があります。")
    plt.rcParams.update(
        {
            "font.family": font or "sans-serif",
            "axes.unicode_minus": False,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "figure.dpi": 110,
            "savefig.dpi": 130,
            "savefig.bbox": "tight",
            "text.color": INK_PRIMARY,
            "axes.labelcolor": INK_SECONDARY,
            "axes.edgecolor": BASELINE,
            "axes.titlecolor": INK_PRIMARY,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": INK_SECONDARY,
            "ytick.labelcolor": INK_SECONDARY,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": False,
            "figure.autolayout": False,
        }
    )


def stamp_footnote(fig, text: str) -> None:
    """図の左下端に脚注を焼き込む。tight_layout 実行後・savefig 前に呼ぶ。"""
    fig.text(0.005, 0.005, text, ha="left", va="bottom", fontsize=7, color=MUTED, linespacing=1.3)


def save_figure(fig, path, footnote: str | None = None, *, log: logging.Logger | None = None) -> str:
    """図保存の共通手続き: 親dir作成 →（脚注スタンプ）→ savefig → close → ログ。

    log を渡すと呼び出し元モジュールのロガーで「図を保存」を記録する（未指定は本モジュール）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if footnote:
        stamp_footnote(fig, footnote)
    fig.savefig(path)
    plt.close(fig)
    (log or logger).info("図を保存: %s", path)
    return str(path)


def diverging_cmap() -> LinearSegmentedColormap:
    """相関ヒートマップ用: blue ↔ gray ↔ red の発散カラーマップ。"""
    return LinearSegmentedColormap.from_list("blue_gray_red", ["#184f95", "#f0efec", "#b3312f"])


def sequential_cmap() -> LinearSegmentedColormap:
    """連続量用: 単一 blue の light→dark ランプ。"""
    return LinearSegmentedColormap.from_list("blue_seq", ["#e8f1fc", "#86b6ef", "#2a78d6", "#104281"])


def color_for(index: int) -> str:
    """カテゴリ色を固定順で返す（8を超えたら循環せず末尾色を使う）。"""
    return CATEGORICAL[index] if index < len(CATEGORICAL) else CATEGORICAL[-1]
