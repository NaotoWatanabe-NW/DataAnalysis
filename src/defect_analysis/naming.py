"""列名・ソース名の機械的正規化。

人手のマッピング表を持たず、NFKC 正規化＋特殊文字の置換という機械的規則だけで
実データの列名（半角カナ・空白・`#`・`(大)` 等を含む）を安全な列名へ変換する。
規則の根拠・実測結果は `docs/real_data_ingest_design.md` §5 を参照。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

SPECIAL_CHARS: str = " 　#()-&・/.,"   # すべて "_" に置換
SAFE_PATTERN = r"[^0-9A-Za-z_぀-ゟ゠-ヿ一-鿿]"

_SPECIAL_RE = re.compile("[" + re.escape(SPECIAL_CHARS) + "]")
_SAFE_RE = re.compile(SAFE_PATTERN)
_UNDERSCORE_RUN_RE = re.compile("_+")
_DATE_SUFFIX_RE = re.compile(r"_(\d{4}|\d{6}|\d{8})$")


def normalize_name(raw: str) -> str:
    """1) NFKC 正規化（半角カナ→全角カナ、全角英数→半角）
    2) 前後空白除去
    3) SPECIAL_CHARS を "_" に置換
    4) SAFE_PATTERN に該当する残りの文字も "_" に置換
    5) "_" の連続を1つに畳み、前後の "_" を除去
    空文字になった場合は "col" を返す（呼び出し側で衝突解決される）。
    """
    s = unicodedata.normalize("NFKC", raw)
    s = s.strip()
    s = _SPECIAL_RE.sub("_", s)
    s = _SAFE_RE.sub("_", s)
    s = _UNDERSCORE_RUN_RE.sub("_", s).strip("_")
    return s or "col"


def normalize_columns(cols: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """列リストを正規化。衝突時は 2 件目以降に "__2", "__3" ... を付与。

    戻り値: (正規化後リスト, {正規化後: 元列名})
    """
    result: list[str] = []
    rename_map: dict[str, str] = {}
    seen: dict[str, int] = {}
    for col in cols:
        base = normalize_name(str(col))
        if base not in seen:
            seen[base] = 1
            name = base
        else:
            seen[base] += 1
            name = f"{base}__{seen[base]}"
        result.append(name)
        rename_map[name] = str(col)
    return result, rename_map


def prefixed(source: str, column: str) -> str:
    """f"{source}__{column}"。source は既に正規化済みであること。"""
    return f"{source}__{column}"


def source_key(stem: str) -> str:
    """ファイル名 stem の日付サフィックスを除去して正規化したソース名を返す。"""
    s = _DATE_SUFFIX_RE.sub("", stem)
    return normalize_name(s)
