"""設定ファイル（config.yaml）の読込とパス解決。

パスはすべてプロジェクトルート（本パッケージの2階層上）からの相対で解釈する。
CWD に依存せず、どこから実行しても同じ場所を指す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# src/defect_analysis/config.py -> parents[2] = リポジトリルート
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class Config:
    """設定辞書の薄いラッパ。ドット区切りキー取得とパス解決を提供する。"""

    def __init__(self, data: dict[str, Any], root: Path = PROJECT_ROOT):
        self._data = data
        self.root = root

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        if not path.exists():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """'analysis.eda_target' のようなドット区切りで値を取得する。"""
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted_key: str, *, default: str | None = None, create: bool = False) -> Path:
        """dotted_key をルート基準の絶対パスに解決する。

        default を指定すると、キーが未設定でもエラーにせずその値を使う
        （real_ingest.* のような「config になければ既定値」なキー向け）。
        create=True なら mkdir する。
        """
        rel = self.get(dotted_key, default)
        if rel is None:
            raise KeyError(f"パス設定が存在しません: {dotted_key}")
        p = (self.root / rel).resolve()
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data(self) -> dict[str, Any]:
        return self._data
