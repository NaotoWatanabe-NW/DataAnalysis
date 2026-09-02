"""eda.py のカスタム図レンダラ（analysis.custom_charts）のテスト。

docs/category_csv_and_custom_charts_design.md T8 のテストケース群。
`viz_style` の Agg バックエンドを使うため追加設定は不要。

実行:
    .venv/bin/python -m pytest tests/test_custom_charts.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis import eda  # noqa: E402
from defect_analysis import viz_style as vs  # noqa: E402
from defect_analysis.analysis_data import AnnotationMeta, build_repair_group_columns  # noqa: E402
from defect_analysis.config import Config  # noqa: E402


def _make_df(n: int = 30) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "num1": [float(i) for i in range(n)],
            "num2": [float((i * 3) % 11) for i in range(n)],
            "num3": [float((i * 7) % 5) for i in range(n)],
            "cat1": (["A", "B", "C"] * ((n // 3) + 1))[:n],
            "cat2": (["x", "y"] * ((n // 2) + 1))[:n],
            "cat3": (["g1"] * (n // 2) + ["g2"] * (n // 3) + ["g3"] * (n - n // 2 - n // 3)),
            "target": [i % 2 for i in range(n)],
        }
    )


def _cfg(custom_charts: list | None, root: Path, **extra_analysis) -> Config:
    analysis: dict = {}
    if custom_charts is not None:
        analysis["custom_charts"] = custom_charts
    analysis.update(extra_analysis)
    return Config({"analysis": analysis}, root=root)


def _meta(n_rows: int) -> AnnotationMeta:
    return AnnotationMeta(n_rows=n_rows, filters_summary="なし")


class CustomChartsTest(unittest.TestCase):
    def setUp(self):
        vs.apply_style()
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "eda"
        self.df = _make_df()

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_empty_list_when_custom_charts_is_absent(self):
        cfg = Config({"analysis": {}}, root=self.out_dir)
        figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(figures, [])
        self.assertFalse(self.out_dir.exists())

    def test_renders_scatter_chart_to_png(self):
        cfg = _cfg([{"type": "scatter", "x": "num1", "y": "num2"}], root=self.out_dir)
        figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(Path(figures[0]).exists())

    def test_renders_bar_chart_to_png(self):
        cfg = _cfg([{"type": "bar", "x": "cat1", "y": "target", "agg": "mean"}], root=self.out_dir)
        figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(Path(figures[0]).exists())

    def test_renders_histogram_chart_to_png(self):
        cfg = _cfg([{"type": "histogram", "x": "num1", "bins": 10}], root=self.out_dir)
        figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(Path(figures[0]).exists())

    def test_renders_box_chart_to_png(self):
        cfg = _cfg([{"type": "box", "x": "cat1", "y": "num2"}], root=self.out_dir)
        figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(Path(figures[0]).exists())

    def test_renders_heatmap_chart_to_png(self):
        cfg = _cfg([{"type": "heatmap", "columns": ["num1", "num2", "num3"]}], root=self.out_dir)
        figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(Path(figures[0]).exists())

    def test_uses_auto_generated_filename_when_output_is_omitted(self):
        cfg = _cfg([{"type": "scatter", "x": "num1", "y": "num2"}], root=self.out_dir)
        eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertTrue((self.out_dir / "custom_01_scatter.png").exists())

    def test_uses_given_output_filename(self):
        cfg = _cfg(
            [{"type": "scatter", "x": "num1", "y": "num2", "output": "my_scatter.png"}], root=self.out_dir
        )
        eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertTrue((self.out_dir / "my_scatter.png").exists())

    def test_strips_directory_from_output_and_keeps_file_under_eda_dir(self):
        cfg = _cfg(
            [{"type": "scatter", "x": "num1", "y": "num2", "output": "../escaped.png"}], root=self.out_dir
        )
        with self.assertLogs("defect_analysis.eda", level="WARNING") as cm:
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue((self.out_dir / "escaped.png").exists())
        self.assertFalse((self.out_dir.parent / "escaped.png").exists())
        self.assertTrue(any("output はファイル名のみ指定できます" in msg for msg in cm.output))

    def test_skips_chart_when_type_is_unknown(self):
        cfg = _cfg(
            [{"type": "no_such_type"}, {"type": "scatter", "x": "num1", "y": "num2"}], root=self.out_dir
        )
        with self.assertLogs("defect_analysis.eda", level="WARNING") as cm:
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(any("未知/未指定です" in msg for msg in cm.output))

    def test_skips_chart_when_required_column_is_missing(self):
        cfg = _cfg([{"type": "scatter", "x": "no_such_column", "y": "num2"}], root=self.out_dir)
        with self.assertLogs("defect_analysis.eda", level="WARNING") as cm:
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(figures, [])
        self.assertTrue(any("存在しません" in msg for msg in cm.output))

    def test_skips_scatter_when_axis_column_is_not_numeric(self):
        cfg = _cfg([{"type": "scatter", "x": "cat1", "y": "num2"}], root=self.out_dir)
        with self.assertLogs("defect_analysis.eda", level="WARNING") as cm:
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(figures, [])
        self.assertTrue(any("数値ではありません" in msg for msg in cm.output))

    def test_skips_chart_when_filters_exclude_all_rows(self):
        cfg = _cfg(
            [{"type": "histogram", "x": "num1", "filters": [{"column": "target", "eq": 99}]}],
            root=self.out_dir,
        )
        with self.assertLogs("defect_analysis.eda", level="WARNING") as cm:
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(figures, [])
        self.assertTrue(any("0 行のためスキップ" in msg for msg in cm.output))

    def test_chart_level_filter_narrows_rows_used_for_the_figure(self):
        n_filtered = int((self.df["cat2"] == "x").sum())
        self.assertGreater(n_filtered, 0)
        self.assertLess(n_filtered, len(self.df))

        captured: list[str | None] = []
        original_save = eda._save

        def spy_save(fig, path, footnote=None):
            captured.append(footnote)
            return original_save(fig, path, footnote)

        cfg_unfiltered = _cfg([{"type": "histogram", "x": "num1"}], root=self.out_dir)
        cfg_filtered = _cfg(
            [{"type": "histogram", "x": "num1", "filters": [{"column": "cat2", "eq": "x"}]}],
            root=self.out_dir,
        )

        with mock.patch.object(eda, "_save", side_effect=spy_save):
            eda._render_custom_charts(self.df, cfg_unfiltered, self.out_dir / "a", _meta(len(self.df)))
            eda._render_custom_charts(self.df, cfg_filtered, self.out_dir / "b", _meta(len(self.df)))

        self.assertEqual(len(captured), 2)
        self.assertIn(f"{len(self.df):,}", captured[0])
        self.assertIn(f"{n_filtered:,}", captured[1])
        self.assertNotIn(f"{len(self.df):,}", captured[1])

    def test_hue_levels_are_capped_and_warned_when_exceeding_limit(self):
        self.assertEqual(self.df["cat3"].nunique(), 3)
        cfg = _cfg(
            [{"type": "scatter", "x": "num1", "y": "num2", "hue": "cat3"}],
            root=self.out_dir,
            custom_chart_max_hue=2,
        )
        with self.assertLogs("defect_analysis.eda", level="WARNING") as cm:
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(Path(figures[0]).exists())
        self.assertTrue(any("水準数" in msg for msg in cm.output))

    def test_heatmap_ignores_hue_with_warning(self):
        cfg = _cfg(
            [{"type": "heatmap", "columns": ["num1", "num2", "num3"], "hue": "cat1"}], root=self.out_dir
        )
        with self.assertLogs("defect_analysis.eda", level="WARNING") as cm:
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue(Path(figures[0]).exists())
        self.assertTrue(any("hue に非対応のため無視します" in msg for msg in cm.output))

    def test_one_broken_chart_does_not_prevent_the_others(self):
        cfg = _cfg(
            [
                {"type": "scatter", "x": "no_such_column", "y": "num2"},
                {"type": "histogram", "x": "num1"},
            ],
            root=self.out_dir,
        )
        with self.assertLogs("defect_analysis.eda", level="WARNING"):
            figures = eda._render_custom_charts(self.df, cfg, self.out_dir, _meta(len(self.df)))
        self.assertEqual(len(figures), 1)
        self.assertTrue((self.out_dir / "custom_02_histogram.png").exists())
        self.assertFalse((self.out_dir / "custom_01_scatter.png").exists())


class RepairGroupBoxChartTest(unittest.TestCase):
    """docs/repair_group_comparison_design.md §10-14: 群分け列を x にした box 図。

    未割当（NaN）行が箱にならず、宣言した群のラベルだけが描画されることを検証する。
    """

    def setUp(self):
        vs.apply_style()
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "eda"

    def tearDown(self):
        self._tmp.cleanup()

    def test_box_with_group_column_draws_one_box_per_declared_group(self):
        df = pd.DataFrame(
            {
                # 0-4: 修正なし。5-7: タレ。8-9: 修正はあるがタレではない（未割当 -> NaN）。
                "has_repair_record": [0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
                "repair_修正__統合カテゴリ__タレ": [0, 0, 0, 0, 0, 1, 1, 1, 0, 0],
                "y_value": [float(i) for i in range(10)],
            }
        )
        cfg = Config(
            {
                "analysis": {
                    "repair_groups": [
                        {
                            "name": "タレ",
                            "groups": [
                                {"label": "修正なし", "column": "has_repair_record", "eq": 0},
                                {"label": "タレ", "column": "repair_修正__統合カテゴリ__タレ", "min": 1},
                            ],
                        }
                    ]
                }
            },
            root=self.out_dir,
        )
        columns_before = set(df.columns)
        df = build_repair_group_columns(df, cfg)
        generated = set(df.columns) - columns_before
        # 生成列が0本だと以降の検証が空振りで必ず緑になるため、まず1本以上あることを確認する。
        self.assertGreaterEqual(len(generated), 1)
        # ラベル文字列を持つ群列を dtype から動的に見つける（列名は直書きしない。__bin は float）。
        group_col = next(c for c in generated if df[c].dtype == object)
        self.assertEqual(df[group_col].isna().sum(), 2)  # 未割当2行の存在を前提として確認

        fig, _extra = eda._custom_box(
            df, {"x": group_col, "y": "y_value", "title": "t"}, cfg, "tag",
        )

        ax = fig.axes[0]
        labels = [t.get_text() for t in ax.get_xticklabels()]
        self.assertEqual(set(labels), {"修正なし", "タレ"})
        self.assertEqual(len(labels), 2)  # NaN 行は3本目の箱にならない


if __name__ == "__main__":
    unittest.main()
