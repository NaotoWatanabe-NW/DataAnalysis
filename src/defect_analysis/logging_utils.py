"""ロギング設定。コンソールとファイルの両方に出力する。"""

from __future__ import annotations

import logging
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(logs_dir: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """ルートロガーを設定して返す。既存ハンドラは張り替える（多重実行対策）。"""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    return root
