"""raw CSV の発見とソース定義（`RawSource`）の構築。

`data/raw/{kind}/*.csv` を走査し、ファイル名の日付サフィックス（月次/日次/無し）を
除去して同一ソースへ束ね、VIN 列・時刻アンカー列を規則で決定する。
CSV 本体は読まない（`pd.read_csv(..., nrows=0)` でヘッダのみ取得する）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import Config
from .naming import normalize_columns, source_key

logger = logging.getLogger(__name__)

DEFAULT_ENCODING = "utf-8-sig"
DEFAULT_ENCODING_FALLBACKS: list[str] = []
DEFAULT_KINDS = ["traceability", "trend", "defect", "repair"]
# real_ingest.convert.by_kind.{kind} の既定値（config 未指定時。docs/real_data_repair_design.md §2）。
DEFAULT_CONVERT_BY_KIND = {
    "repair": {
        "encoding": "cp932",
        "time_column": "PB_ON",
    },
}
DEFAULT_SOURCE_ALIASES = {"repair/defect": "修正"}
# VIN 列が必須の kind（trend は DATETIME キーなので対象外）
_KINDS_REQUIRING_VIN = {"traceability", "defect", "repair"}


@dataclass(frozen=True)
class RawSource:
    kind: str            # "traceability" | "trend" | "defect" | "repair"（= サブディレクトリ名）
    name: str            # 正規化済みソース名（例: "前処理_電着"）
    files: list[Path]    # 同一ソースに属する raw CSV（複数月/複数日ぶん）
    vin_column: str | None   # 正規化後の VIN 列名。trend は None
    time_column: str         # アンカー日時列（正規化後）
    columns: list[str]       # 正規化後の全列名（先頭ファイルのヘッダ由来）
    rename_map: dict[str, str]   # 正規化後 -> 元列名
    encoding: str = DEFAULT_ENCODING   # このソースの読取エンコーディング（R1）


def probe_header(path: Path, encoding: str) -> list[str]:
    """pd.read_csv(path, nrows=0) でヘッダのみ取得する。"""
    df = pd.read_csv(path, encoding=encoding, nrows=0)
    return list(df.columns)


def resolve_encoding(kind: str, cfg: Config) -> str:
    """`real_ingest.convert.by_kind.{kind}.encoding` があればそれ、無ければ `convert.encoding`。"""
    convert_cfg = cfg.get("real_ingest.convert", {}) or {}
    default_encoding = convert_cfg.get("encoding", DEFAULT_ENCODING)
    by_kind = dict(DEFAULT_CONVERT_BY_KIND)
    by_kind.update(convert_cfg.get("by_kind", {}) or {})
    kind_cfg = by_kind.get(kind, {}) or {}
    return kind_cfg.get("encoding", default_encoding)


def _resolve_encoding_fallbacks(cfg: Config) -> list[str]:
    convert_cfg = cfg.get("real_ingest.convert", {}) or {}
    return list(convert_cfg.get("encoding_fallbacks", DEFAULT_ENCODING_FALLBACKS))


def _resolve_time_column_override(kind: str, cfg: Config) -> str | None:
    convert_cfg = cfg.get("real_ingest.convert", {}) or {}
    by_kind = dict(DEFAULT_CONVERT_BY_KIND)
    by_kind.update(convert_cfg.get("by_kind", {}) or {})
    kind_cfg = by_kind.get(kind, {}) or {}
    return kind_cfg.get("time_column")


def _resolve_source_alias(kind: str, source_key_: str, cfg: Config) -> str:
    aliases = dict(DEFAULT_SOURCE_ALIASES)
    aliases.update(cfg.get("real_ingest.source_aliases", {}) or {})
    return aliases.get(f"{kind}/{source_key_}", source_key_)


def probe_header_with_encoding(
    path: Path, encoding: str, fallbacks: Sequence[str]
) -> tuple[list[str], str]:
    """encoding で試し、UnicodeDecodeError なら fallbacks を順に試す。

    採用したエンコーディングを WARN ログに出す。全滅なら最後の例外を再送出する。
    戻り値: (ヘッダ列名リスト, 実際に使ったエンコーディング)
    """
    try:
        return probe_header(path, encoding), encoding
    except UnicodeDecodeError as exc:
        last_exc: Exception = exc
        for fallback in fallbacks:
            try:
                header = probe_header(path, fallback)
            except UnicodeDecodeError as fallback_exc:
                last_exc = fallback_exc
                continue
            logger.warning(
                "%s: encoding=%s で読めなかったため %s で読みました", path, encoding, fallback
            )
            return header, fallback
        raise last_exc


def _resolve_time_column(normalized_columns: list[str]) -> str | None:
    """(a) DATETIME があればそれ、(b) なければ '通過日時' を含む最初の列。"""
    if "DATETIME" in normalized_columns:
        return "DATETIME"
    for col in normalized_columns:
        if "通過日時" in col:
            return col
    return None


def _check_header_consistency(
    kind: str, name: str, base_normalized: list[str], other_files: list[Path], encoding: str
) -> None:
    """先頭ファイル以降のヘッダを基準ヘッダと突合し、差分を WARN する。"""
    base_set = set(base_normalized)
    for other in other_files:
        try:
            other_header = probe_header(other, encoding)
        except Exception as exc:
            logger.error("[%s/%s] ヘッダ読込に失敗しました: %s (%s)", kind, name, other, exc)
            continue
        other_normalized, _ = normalize_columns(other_header)
        other_set = set(other_normalized)
        if other_set != base_set:
            added = sorted(other_set - base_set)
            removed = sorted(base_set - other_set)
            logger.warning(
                "[%s/%s] ファイル間でヘッダが異なります: %s (追加=%s 欠落=%s)",
                kind, name, other.name, added, removed,
            )


def _build_source(kind: str, name: str, files: list[Path], cfg: Config) -> RawSource | None:
    first = files[0]
    encoding = resolve_encoding(kind, cfg)
    fallbacks = _resolve_encoding_fallbacks(cfg)
    try:
        raw_header, encoding = probe_header_with_encoding(first, encoding, fallbacks)
    except Exception as exc:
        logger.error("[%s/%s] ヘッダ読込に失敗したためスキップ: %s (%s)", kind, name, first, exc)
        return None

    normalized, rename_map = normalize_columns(raw_header)

    vin_column = "VIN" if "VIN" in normalized else None
    if vin_column is None and kind in _KINDS_REQUIRING_VIN:
        logger.error("[%s/%s] VIN 列が見つからないためスキップ: %s", kind, name, first)
        return None

    time_column_override = _resolve_time_column_override(kind, cfg)
    if time_column_override is not None:
        if time_column_override not in normalized:
            logger.error(
                "[%s/%s] 指定された時刻アンカー列が見つからないためスキップ: %s (列=%s)",
                kind, name, first, time_column_override,
            )
            return None
        time_column = time_column_override
    else:
        time_column = _resolve_time_column(normalized)
        if time_column is None:
            logger.error("[%s/%s] 日時列が見つからないためスキップ: %s", kind, name, first)
            return None

    _check_header_consistency(kind, name, normalized, files[1:], encoding)

    return RawSource(
        kind=kind,
        name=name,
        files=files,
        vin_column=vin_column,
        time_column=time_column,
        columns=normalized,
        rename_map=rename_map,
        encoding=encoding,
    )


def discover_sources(raw_dir: Path, cfg: Config) -> list[RawSource]:
    """data/raw/*/ を走査して RawSource 群を返す。"""
    known_kinds = cfg.get("real_ingest.kinds", DEFAULT_KINDS)

    if not raw_dir.exists():
        logger.info("raw_dir が存在しません: %s", raw_dir)
        return []

    sources: list[RawSource] = []
    for kind_dir in sorted(p for p in raw_dir.iterdir() if p.is_dir()):
        kind = kind_dir.name
        if kind not in known_kinds:
            logger.warning("未知のサブディレクトリのためスキップ: %s", kind_dir)
            continue

        files = sorted(kind_dir.glob("*.csv"))
        if not files:
            logger.info("[%s] CSV が見つかりません（空ディレクトリ）: %s", kind, kind_dir)
            continue

        groups: dict[str, list[Path]] = {}
        for f in files:
            key = source_key(f.stem)
            groups.setdefault(key, []).append(f)

        for key, group_files in sorted(groups.items()):
            name = _resolve_source_alias(kind, key, cfg)
            source = _build_source(kind, name, sorted(group_files), cfg)
            if source is not None:
                sources.append(source)

    return sources
