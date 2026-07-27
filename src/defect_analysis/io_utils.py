"""テーブル入出力の共通層。

storage.format に parquet を指定しても、pyarrow / fastparquet が無ければ
自動的に csv へフォールバックする（一度だけ判定して以降一貫させる）。
これにより pyarrow 未導入でも end-to-end で動作し、導入後は parquet になる。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = ("parquet", "csv")


@lru_cache(maxsize=1)
def _parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401

        return True
    except ImportError:
        try:
            import fastparquet  # noqa: F401

            return True
        except ImportError:
            return False


def resolve_format(requested: str) -> str:
    """要求フォーマットを、実際に利用可能な形式へ解決する。"""
    requested = (requested or "parquet").lower()
    if requested not in SUPPORTED_FORMATS:
        raise ValueError(f"未対応の storage.format: {requested!r}（{SUPPORTED_FORMATS} のいずれか）")
    if requested == "parquet" and not _parquet_available():
        logger.warning(
            "storage.format=parquet ですが pyarrow/fastparquet が未導入のため csv にフォールバックします。"
            " parquet を使うには `uv add pyarrow` 等で導入してください。"
        )
        return "csv"
    return requested


def table_path(directory: Path, name: str, fmt: str) -> Path:
    """ディレクトリ + テーブル名 + フォーマットから出力パスを組み立てる。"""
    return directory / f"{name}.{fmt}"


def save_df(df: pd.DataFrame, path: Path) -> Path:
    """拡張子に応じて DataFrame を保存する。index は保存しない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df.to_parquet(path, index=False)
    elif suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"未対応の拡張子: {suffix}")
    logger.info("保存: %s  (%d 行 x %d 列)", path, len(df), df.shape[1])
    return path


def load_df(path: Path, parse_dates: list[str] | None = None) -> pd.DataFrame:
    """拡張子に応じて DataFrame を読み込む。

    csv では日時型が失われるため、parse_dates で列を指定すると読込後に変換する。
    parquet は dtype を保持するため parse_dates は冪等に働く。
    """
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df = pd.read_parquet(path)
    elif suffix == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError(f"未対応の拡張子: {suffix}")

    if parse_dates:
        for col in parse_dates:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
    return df
