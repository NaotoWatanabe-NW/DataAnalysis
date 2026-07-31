"""raw CSV → Parquet レイク変換、およびレイク読取 API。

`data/lake/{kind}/{source}/date=YYYY-MM-DD/part-{file_stem}-{chunk:04d}.parquet` に
日付パーティション形式で書き出す。増分変換（size/mtime による差分判定）・チャンク読み・
列名正規化・VIN 派生列付与・型ダウンキャストを行う。業務判断（除外・集約）は行わない
（`docs/real_data_ingest_design.md` §7 責務境界）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import Config
from .naming import normalize_columns
from .raw_sources import DEFAULT_CONVERT_BY_KIND, RawSource, discover_sources, probe_header
from .vin_key import normalize_vin, policy_from_config

logger = logging.getLogger(__name__)

# config/config.yaml に real_ingest セクションが無い間の既定値（docs/real_data_ingest_design.md §9）。
# config にセクションが追加されればそちらを優先する。
DEFAULT_RAW_DIR = "data/raw"
DEFAULT_LAKE_DIR = "data/lake"
DEFAULT_MANIFEST_PATH = "data/lake/_manifest.json"

# repair kind 限定の convert 前処理の既定値（docs/real_data_repair_design.md §2/§3.2）。
# `real_ingest.convert.by_kind` が config.yaml に無くても smoke テストが repair を処理できるよう
# コード側にも持たせる。
DEFAULT_CONVERT_BY_KIND_PREPROCESS = {
    "repair": {
        **DEFAULT_CONVERT_BY_KIND.get("repair", {}),
        "strip_apostrophe": True,
        "na_values": ["00000000 000000"],
        "combine_datetime": [
            {"name": "修正日時", "date_column": "修正日", "time_column": "修正時間", "format": "%Y/%m/%d %H:%M:%S"},
        ],
        "datetime_columns": [
            {
                "columns": ["WB_ON", "WB_OK", "PB_ON", "PB_OK", "AB_ON", "EG_OK", "AB_OK", "FC_OK"],
                "format": "%Y%m%d %H%M%S",
            },
        ],
        "pii": {"columns": ["修正員"], "mode": "hash", "salt": "", "suffix": "_id"},
    },
}

DEFAULT_CONVERT = {
    "encoding": "utf-8-sig",
    "datetime_format": "%Y/%m/%d %H:%M:%S",
    "chunksize": 200_000,
    "downcast_float": True,
    "keep_float64_suffixes": ["積算値", "総合計"],
    "datetime_column_keywords": ["通過日時", "DATETIME"],
    "max_datetime_parse_failure_rate": 0.01,
    "by_kind": DEFAULT_CONVERT_BY_KIND_PREPROCESS,
}


@dataclass
class ConvertResult:
    kind: str
    source: str
    file: str
    n_rows: int
    n_partitions: int
    skipped: bool
    reason: str | None


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def convert_config(cfg: Config) -> dict:
    """`real_ingest.convert.*` を既定値とマージして返す（raw_convert / assemble 共通）。"""
    merged = dict(DEFAULT_CONVERT)
    merged.update(cfg.get("real_ingest.convert", {}) or {})
    return merged


def should_keep_float64(column: str, keep_suffixes: Sequence[str]) -> bool:
    """列名が keep_float64_suffixes のいずれかで終わるか判定する（downcast 対象外判定）。

    raw_convert のチャンク単位 downcast と、assemble の最終出力直前の再downcast
    （High-1）の両方で同じ規則を使うための共通判定。
    """
    return any(column.endswith(suf) for suf in keep_suffixes)


def _manifest_key(kind: str, source: str, filename: str) -> str:
    return f"{kind}/{source}/{filename}"


def _clear_existing_parts(source_dir: Path, stem: str) -> None:
    """再変換前に、このファイル由来の既存 part ファイルを削除する（行数減少時の残骸防止）。"""
    if not source_dir.exists():
        return
    for date_dir in source_dir.glob("date=*"):
        for part in date_dir.glob(f"part-{stem}-*.parquet"):
            part.unlink()


def _kind_convert_config(convert_cfg: dict, kind: str) -> dict:
    """`real_ingest.convert.by_kind.{kind}` を返す（無ければ空 dict）。"""
    return dict((convert_cfg.get("by_kind") or {}).get(kind, {}) or {})


def _parse_datetime_with_warning(
    chunk: pd.DataFrame, column: str, fmt: str, *,
    kind: str, name: str, chunk_idx: int, file_name: str, max_failure_rate: float,
) -> pd.Series:
    """日時パースし、既存 NA を除いた失敗率が閾値超過なら WARN する（keyword 方式と repair 方式で共通化）。"""
    orig_na = chunk[column].isna()
    parsed = pd.to_datetime(chunk[column], format=fmt, errors="coerce")
    newly_failed = parsed.isna() & ~orig_na
    fail_rate = float(newly_failed.mean()) if len(chunk) else 0.0
    if fail_rate > max_failure_rate:
        logger.warning(
            "[%s/%s] 日時パース失敗率 %.2f%% が閾値を超えました（列=%s, チャンク=%d, ファイル=%s）",
            kind, name, fail_rate * 100, column, chunk_idx, file_name,
        )
    return parsed


def _strip_apostrophe(chunk: pd.DataFrame) -> pd.DataFrame:
    """object/string dtype の全列で、先頭の `'` を1文字だけ除去する（R2）。"""
    str_cols = chunk.select_dtypes(include=["object", "string"]).columns
    for c in str_cols:
        chunk[c] = chunk[c].astype("string").str.replace(r"^'", "", regex=True)
    return chunk


def _replace_sentinels(chunk: pd.DataFrame, columns: Sequence[str], na_values: Sequence[str]) -> pd.DataFrame:
    """欠測番兵（例 `00000000 000000`）を明示的に NA へ置換する。"""
    for c in columns:
        if c in chunk.columns:
            chunk[c] = chunk[c].replace(list(na_values), pd.NA)
    return chunk


def _combine_datetime_columns(
    chunk: pd.DataFrame, specs: Sequence[dict], *, kind: str, name: str,
) -> pd.DataFrame:
    """日付列＋時刻列から新規日時列を組み立てる（元列は残す）。"""
    for spec in specs:
        new_name = spec["name"]
        date_col = spec["date_column"]
        time_col = spec["time_column"]
        fmt = spec["format"]
        if date_col not in chunk.columns or time_col not in chunk.columns:
            logger.warning(
                "[%s/%s] combine_datetime の元列が見つからないためスキップします: %s (date=%s, time=%s)",
                kind, name, new_name, date_col, time_col,
            )
            continue
        combined = chunk[date_col].astype("string") + " " + chunk[time_col].astype("string")
        chunk[new_name] = pd.to_datetime(combined, format=fmt, errors="coerce")
    return chunk


def _apply_extra_datetime_columns(
    chunk: pd.DataFrame, specs: Sequence[dict], *,
    kind: str, name: str, chunk_idx: int, file_name: str, max_failure_rate: float,
) -> pd.DataFrame:
    """`datetime_columns`（keyword 方式では拾えない列）を指定 format でパースする。"""
    for spec in specs:
        fmt = spec["format"]
        for c in spec["columns"]:
            if c not in chunk.columns:
                continue
            chunk[c] = _parse_datetime_with_warning(
                chunk, c, fmt, kind=kind, name=name, chunk_idx=chunk_idx,
                file_name=file_name, max_failure_rate=max_failure_rate,
            )
    return chunk


def _resolve_pii_salt(pii_cfg: dict) -> str:
    salt = pii_cfg.get("salt") or ""
    if salt:
        return salt
    return os.environ.get("DEFECT_ANALYSIS_PII_SALT", "")


def _hash_pii_value(value: object, salt: str) -> object:
    if pd.isna(value):
        return pd.NA
    return hashlib.sha256((salt + str(value)).encode("utf-8")).hexdigest()[:12]


def _apply_pii(
    chunk: pd.DataFrame, pii_cfg: dict, *, kind: str, name: str, chunk_idx: int,
) -> pd.DataFrame:
    """個人情報列をハッシュ化 / 削除 / そのまま保持する（R6）。"""
    if not pii_cfg:
        return chunk
    mode = pii_cfg.get("mode", "hash")
    columns = pii_cfg.get("columns", [])
    suffix = pii_cfg.get("suffix", "_id")
    salt = _resolve_pii_salt(pii_cfg)

    for c in columns:
        if c not in chunk.columns:
            logger.info("[%s/%s] PII 列が見つかりません: %s", kind, name, c)
            continue
        if mode == "hash":
            if chunk_idx == 0:
                logger.info(
                    "[%s/%s] 個人情報列をハッシュ化します: %s -> %s%s"
                    "（salt 変更時は過去のレイクと ID が変わるため --force 再変換が必要です）",
                    kind, name, c, c, suffix,
                )
            chunk[f"{c}{suffix}"] = chunk[c].map(lambda v: _hash_pii_value(v, salt))
            chunk = chunk.drop(columns=[c])
        elif mode == "drop":
            chunk = chunk.drop(columns=[c])
        elif mode == "keep":
            logger.warning("[%s/%s] 個人情報列をそのまま保存します: %s", kind, name, c)
    return chunk


def _apply_kind_preprocessing(
    chunk: pd.DataFrame, kind_cfg: dict, *,
    kind: str, name: str, chunk_idx: int, file_name: str, max_failure_rate: float,
) -> pd.DataFrame:
    """列名正規化の直後・既存の keyword 日時パースの前に行う kind 別前処理（R2/R3/R6）。

    `kind_cfg`（`real_ingest.convert.by_kind.{kind}`）が空なら完全に no-op。
    """
    if not kind_cfg:
        return chunk

    if kind_cfg.get("strip_apostrophe"):
        chunk = _strip_apostrophe(chunk)

    na_values = kind_cfg.get("na_values")
    datetime_columns_specs = kind_cfg.get("datetime_columns", [])
    if na_values and datetime_columns_specs:
        sentinel_columns = [c for spec in datetime_columns_specs for c in spec["columns"]]
        chunk = _replace_sentinels(chunk, sentinel_columns, na_values)

    combine_specs = kind_cfg.get("combine_datetime", [])
    if combine_specs:
        chunk = _combine_datetime_columns(chunk, combine_specs, kind=kind, name=name)

    if datetime_columns_specs:
        chunk = _apply_extra_datetime_columns(
            chunk, datetime_columns_specs, kind=kind, name=name, chunk_idx=chunk_idx,
            file_name=file_name, max_failure_rate=max_failure_rate,
        )

    pii_cfg = kind_cfg.get("pii")
    if pii_cfg:
        chunk = _apply_pii(chunk, pii_cfg, kind=kind, name=name, chunk_idx=chunk_idx)

    return chunk


def convert_file(
    path: Path, source: RawSource, lake_dir: Path, cfg: Config, *, force: bool = False
) -> ConvertResult:
    """1 ファイルを CSV → Parquet 変換する（増分判定込み）。"""
    kind = source.kind
    name = source.name
    convert_cfg = convert_config(cfg)
    vin_policy = policy_from_config(cfg)
    manifest_path = cfg.path("real_ingest.manifest_path", default=DEFAULT_MANIFEST_PATH)
    manifest = load_manifest(manifest_path)
    key = _manifest_key(kind, name, path.name)

    stat = path.stat()
    size = stat.st_size
    mtime_ns = stat.st_mtime_ns

    if not force:
        entry = manifest.get(key)
        if entry and entry.get("size") == size and entry.get("mtime_ns") == mtime_ns:
            return ConvertResult(
                kind=kind, source=name, file=path.name,
                n_rows=int(entry.get("n_rows", 0)), n_partitions=int(entry.get("n_partitions", 0)),
                skipped=True, reason="unchanged",
            )

    source_dir = lake_dir / kind / name
    _clear_existing_parts(source_dir, path.stem)

    encoding = source.encoding
    chunksize = int(convert_cfg["chunksize"])
    datetime_format = convert_cfg["datetime_format"]
    downcast_float = bool(convert_cfg["downcast_float"])
    keep_float64_suffixes = tuple(convert_cfg["keep_float64_suffixes"])
    datetime_keywords = convert_cfg["datetime_column_keywords"]
    max_failure_rate = float(convert_cfg["max_datetime_parse_failure_rate"])
    kind_cfg = _kind_convert_config(convert_cfg, kind)

    try:
        reader = pd.read_csv(path, encoding=encoding, chunksize=chunksize)
    except Exception as exc:
        logger.error("[%s/%s] 読込失敗のためスキップ: %s (%s)", kind, name, path.name, exc)
        return ConvertResult(
            kind=kind, source=name, file=path.name, n_rows=0, n_partitions=0,
            skipped=True, reason=f"read_error: {exc}",
        )

    normalized_columns: list[str] | None = None
    total_rows = 0
    dates_seen: set[str] = set()
    n_unknown_total = 0

    try:
        for chunk_idx, chunk in enumerate(reader):
            if normalized_columns is None:
                normalized_columns, _ = normalize_columns(list(chunk.columns))
                if source.time_column not in normalized_columns:
                    logger.error(
                        "[%s/%s] 時刻アンカー列が見つからないためスキップ: %s (列=%s)",
                        kind, name, path.name, source.time_column,
                    )
                    return ConvertResult(
                        kind=kind, source=name, file=path.name, n_rows=0, n_partitions=0,
                        skipped=True, reason="time_column_missing",
                    )
            chunk.columns = normalized_columns

            chunk = _apply_kind_preprocessing(
                chunk, kind_cfg, kind=kind, name=name, chunk_idx=chunk_idx,
                file_name=path.name, max_failure_rate=max_failure_rate,
            )

            dt_cols = [c for c in normalized_columns if any(kw in c for kw in datetime_keywords)]
            for c in dt_cols:
                chunk[c] = _parse_datetime_with_warning(
                    chunk, c, datetime_format, kind=kind, name=name, chunk_idx=chunk_idx,
                    file_name=path.name, max_failure_rate=max_failure_rate,
                )

            if source.vin_column and source.vin_column in chunk.columns:
                vin_derived = normalize_vin(chunk[source.vin_column], vin_policy)
                chunk = chunk.rename(columns={source.vin_column: "vin_raw"})
                chunk = pd.concat([chunk, vin_derived], axis=1)

            if downcast_float:
                for c in chunk.select_dtypes(include="float64").columns:
                    if should_keep_float64(c, keep_float64_suffixes):
                        continue
                    chunk[c] = chunk[c].astype("float32")

            time_col = source.time_column
            date_str = chunk[time_col].dt.strftime("%Y-%m-%d")
            is_unknown = chunk[time_col].isna()
            n_unknown = int(is_unknown.sum())
            n_unknown_total += n_unknown
            date_str = date_str.where(~is_unknown, "unknown").rename("_date_partition")

            chunk = pd.concat(
                [chunk, pd.Series(name, index=chunk.index, name="__source"), date_str], axis=1
            )

            for date_val, group in chunk.groupby("_date_partition", sort=False):
                out_group = group.drop(columns=["_date_partition"])
                part_dir = source_dir / f"date={date_val}"
                part_dir.mkdir(parents=True, exist_ok=True)
                part_path = part_dir / f"part-{path.stem}-{chunk_idx:04d}.parquet"
                out_group.to_parquet(part_path, index=False, compression="snappy")
                dates_seen.add(str(date_val))

            total_rows += len(chunk)
    except Exception as exc:
        logger.error("[%s/%s] 変換中にエラーが発生したためスキップ: %s (%s)", kind, name, path.name, exc)
        return ConvertResult(
            kind=kind, source=name, file=path.name, n_rows=0, n_partitions=0,
            skipped=True, reason=f"convert_error: {exc}",
        )

    if n_unknown_total:
        logger.warning(
            "[%s/%s] 日時パース不能行を date=unknown に格納: %d 行（ファイル=%s）",
            kind, name, n_unknown_total, path.name,
        )

    manifest[key] = {
        "size": size,
        "mtime_ns": mtime_ns,
        "n_rows": total_rows,
        "n_partitions": len(dates_seen),
        "converted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dates": sorted(dates_seen),
    }
    save_manifest(manifest_path, manifest)

    return ConvertResult(
        kind=kind, source=name, file=path.name, n_rows=total_rows, n_partitions=len(dates_seen),
        skipped=False, reason=None,
    )


def convert_all(cfg: Config, *, force: bool = False) -> dict[str, int]:
    """全ソース・全ファイルを変換。戻り値 {"n_files":.., "n_skipped":.., "n_rows":..}"""
    raw_dir = cfg.path("real_ingest.raw_dir", default=DEFAULT_RAW_DIR)
    lake_dir = cfg.path("real_ingest.lake_dir", default=DEFAULT_LAKE_DIR, create=True)

    sources = discover_sources(raw_dir, cfg)
    if not sources:
        logger.warning("変換対象のソースが見つかりません（raw_dir=%s）", raw_dir)

    _write_column_name_mapping(cfg, sources)

    n_files = 0
    n_skipped = 0
    n_rows = 0
    for source in sources:
        for f in source.files:
            result = convert_file(f, source, lake_dir, cfg, force=force)
            n_files += 1
            if result.skipped:
                n_skipped += 1
            n_rows += result.n_rows
            logger.info(
                "[%s/%s] %s: skipped=%s rows=%d partitions=%d%s",
                result.kind, result.source, result.file, result.skipped,
                result.n_rows, result.n_partitions,
                f" reason={result.reason}" if result.reason else "",
            )

    return {"n_files": n_files, "n_skipped": n_skipped, "n_rows": n_rows}


def _write_column_name_mapping(cfg: Config, sources: list[RawSource]) -> None:
    """正規化後 -> 元列名の対応表を reports/column_name_mapping.csv に出力する。

    各ファイルの実ヘッダを個別に読む（同一ソース内でヘッダが異なる場合も拾う）。
    ソースごとの `source.encoding` を使う（R1。global encoding 固定だと cp932 ソースで落ちる）。
    """
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for source in sources:
        for f in source.files:
            try:
                header = probe_header(f, source.encoding)
            except Exception as exc:
                logger.error("[%s/%s] ヘッダ読込に失敗しました: %s (%s)", source.kind, source.name, f, exc)
                continue
            _, rename_map = normalize_columns(header)
            for normalized, original in rename_map.items():
                key = (source.kind, source.name, normalized, original)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "kind": source.kind, "source": source.name,
                    "normalized": normalized, "original": original,
                })

    reports_dir = cfg.path("paths.reports_dir", create=True)
    out_path = reports_dir / "column_name_mapping.csv"
    pd.DataFrame(rows, columns=["kind", "source", "normalized", "original"]).to_csv(out_path, index=False)
    logger.info("列名対応表を出力: %s (%d 行)", out_path, len(rows))


def read_source(
    lake_dir: Path, kind: str, source: str, *,
    columns: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> pd.DataFrame:
    """レイクから1ソースを読む。columns で列を絞れる（Parquet の列プルーニング）。

    日付は hive パーティション列 `date` に対する filters で絞る。
    ディレクトリが無ければ空 DataFrame を返す（エラーにしない）。
    """
    source_dir = lake_dir / kind / source
    if not source_dir.exists() or not any(source_dir.rglob("*.parquet")):
        return pd.DataFrame(columns=columns or [])

    filters = []
    if date_from is not None:
        filters.append(("date", ">=", date_from))
    if date_to is not None:
        filters.append(("date", "<=", date_to))

    return pd.read_parquet(
        source_dir, columns=columns, filters=filters or None, engine="pyarrow"
    )
