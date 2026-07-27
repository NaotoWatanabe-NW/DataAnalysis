"""viz_style.stamp_footnote / save_figure のテスト。

実行:
    .venv/bin/python -m pytest tests/test_viz.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import matplotlib.pyplot as plt

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from defect_analysis import viz_style as vs  # noqa: E402


class StampFootnoteTest(unittest.TestCase):
    def test_stamp_adds_exactly_one_text_artist_with_given_content(self):
        fig, _ax = plt.subplots()
        try:
            before = len(fig.texts)
            text = "設備: EQ-01 圧入 ｜ データ種: テスト"
            vs.stamp_footnote(fig, text)
            self.assertEqual(len(fig.texts), before + 1)
            self.assertTrue(any(t.get_text() == text for t in fig.texts))
        finally:
            plt.close(fig)

    def test_stamping_twice_adds_two_distinct_artists(self):
        fig, _ax = plt.subplots()
        try:
            vs.stamp_footnote(fig, "1回目")
            vs.stamp_footnote(fig, "2回目")
            self.assertEqual(len(fig.texts), 2)
            self.assertEqual({t.get_text() for t in fig.texts}, {"1回目", "2回目"})
        finally:
            plt.close(fig)


class SaveFigureTest(unittest.TestCase):
    def test_save_figure_writes_png_file_and_closes_the_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sub" / "fig.png"
            baseline = len(plt.get_fignums())
            fig, ax = plt.subplots()
            ax.plot([0, 1], [0, 1])
            self.assertEqual(len(plt.get_fignums()), baseline + 1)

            result = vs.save_figure(fig, out)

            self.assertTrue(out.exists())
            self.assertEqual(result, str(out))
            # savefig 後は plt.close されて描画中の figure 数が呼び出し前の水準に戻る
            self.assertEqual(len(plt.get_fignums()), baseline)

    def test_save_figure_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a" / "b" / "c.png"
            fig, _ax = plt.subplots()
            vs.save_figure(fig, out)
            self.assertTrue(out.parent.is_dir())
            self.assertTrue(out.exists())

    def test_save_figure_stamps_footnote_before_saving_when_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fig.png"
            fig, _ax = plt.subplots()
            footnote = "設備: 全設備 ｜ データ種: 単体テスト"
            vs.save_figure(fig, out, footnote=footnote)
            self.assertTrue(any(t.get_text() == footnote for t in fig.texts))
            self.assertTrue(out.exists())

    def test_save_figure_without_footnote_adds_no_text_artist(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fig.png"
            fig, _ax = plt.subplots()
            before = len(fig.texts)
            vs.save_figure(fig, out)
            self.assertEqual(len(fig.texts), before)


if __name__ == "__main__":
    unittest.main()
