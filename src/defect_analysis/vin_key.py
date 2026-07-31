"""VIN 正規化（空白除去・base/pass_no 分解・ダミー判定）。

サフィックス付き VIN（例 `"HE93S-122023     a"`）は空白が文字列の**内部**にあるため
`str.strip()` では除去できない（`docs/real_data_facts.md` §5 実測補正）。
本モジュールは全空白除去のうえで base/サフィックスを分解する。
大文字化（`upper()`）は行わない（サフィックスの小文字が唯一の識別情報のため）。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from .config import Config

_VIN_SPLIT_RE = r"^(?P<base>.*?)(?P<suf>[a-z])?$"

DEFAULT_SUFFIX_POLICY = "keep"
DEFAULT_EXCLUDE_REGEX = r"(?i)(DUMMY|EMPTY)"


@dataclass(frozen=True)
class VinPolicy:
    suffix_policy: str = "keep"     # "keep" | "merge"
    exclude_regex: str = r"(?i)(DUMMY|EMPTY)"


def policy_from_config(cfg: "Config") -> VinPolicy:
    """`real_ingest.vin.*` から VinPolicy を組み立てる（raw_convert / assemble 共通）。"""
    vin_cfg = cfg.get("real_ingest.vin", {}) or {}
    return VinPolicy(
        suffix_policy=vin_cfg.get("suffix_policy", DEFAULT_SUFFIX_POLICY),
        exclude_regex=vin_cfg.get("exclude_regex", DEFAULT_EXCLUDE_REGEX),
    )


def split_vin_base_pass_no(vin: pd.Series) -> pd.DataFrame:
    """既に正規化済み（全空白除去済み）の vin 文字列から vin_base / vin_pass_no を分解する。

    戻り値の列:
        vin_base     : vin から末尾英小文字1字を除去（例 "HE93S-122023"）
        vin_pass_no  : サフィックス無し=1, a=2, b=3, c=4 ...（ord(suf)-ord('a')+2）
    """
    parts = vin.str.extract(_VIN_SPLIT_RE)
    vin_base = parts["base"]
    vin_pass_no = parts["suf"].apply(lambda c: (ord(c) - ord("a") + 2) if pd.notna(c) else 1).astype("Int64")
    return pd.DataFrame({"vin_base": vin_base, "vin_pass_no": vin_pass_no}, index=vin.index)


def normalize_vin(s: pd.Series, policy: VinPolicy | None = None) -> pd.DataFrame:
    """VIN 列（原文）から派生列を作る。

    戻り値の列:
        vin          : 全空白除去後の正規化キー（例 "HE93S-122023a"）
        vin_base     : vin から末尾英小文字1字を除去（例 "HE93S-122023"）
        vin_pass_no  : サフィックス無し=1, a=2, b=3, c=4 ...（ord(suf)-ord('a')+2）
        vin_is_dummy : exclude_regex にマッチ、または空文字（bool）

    実装: s.astype("string").str.replace(r"\\s+", "", regex=True) のあと
          split_vin_base_pass_no で base/suffix を分解する。
    """
    policy = policy or VinPolicy()
    vin = s.astype("string").str.replace(r"\s+", "", regex=True)
    parts_df = split_vin_base_pass_no(vin)
    vin_base = parts_df["vin_base"]
    vin_pass_no = parts_df["vin_pass_no"]

    vin_filled = vin.fillna("")
    # exclude_regex は判定にのみ使い、キャプチャグループの中身は使わないため警告を抑止する。
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="This pattern is interpreted as a regular expression.*")
        matches = vin_filled.str.contains(policy.exclude_regex, regex=True, na=False)
    vin_is_dummy = (vin_filled == "") | matches

    return pd.DataFrame(
        {
            "vin": vin,
            "vin_base": vin_base,
            "vin_pass_no": vin_pass_no,
            "vin_is_dummy": vin_is_dummy,
        },
        index=s.index,
    )


def join_key(df: pd.DataFrame, policy: VinPolicy) -> pd.Series:
    """policy に応じた結合キーを返す（keep→vin, merge→vin_base）。"""
    if policy.suffix_policy == "merge":
        return df["vin_base"]
    return df["vin"]
