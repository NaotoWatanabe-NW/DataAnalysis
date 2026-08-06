"""集計済み parquet ファイルの中身をターミナルで手早く確認するスクリプト。

`defect_analysis` パッケージには依存せず、pandas だけで動く単体スクリプト。

使用例:
    python scripts/inspect_parquet.py data/interim/vin_panel.parquet
    python scripts/inspect_parquet.py data/interim/vin_panel.parquet --rows 10 --describe
    python scripts/inspect_parquet.py data/interim/vin_panel.parquet --columns vin,has_repair_record
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="parquet ファイルの中身を確認する")
    parser.add_argument("path", help="確認する parquet ファイルのパス")
    parser.add_argument("--rows", type=int, default=5, help="先頭表示行数（既定 5）")
    parser.add_argument("--columns", default=None, help="表示する列名（カンマ区切り。省略時は全列）")
    parser.add_argument("--describe", action="store_true", help="数値列の describe() も表示する")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        df = pd.read_parquet(args.path)
    except Exception as exc:
        print(f"ERROR: parquet ファイルを読み込めませんでした: {args.path} ({exc})", file=sys.stderr)
        return 1

    if args.columns:
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]
        missing = [c for c in columns if c not in df.columns]
        if missing:
            print(f"ERROR: 指定された列が存在しません: {missing}", file=sys.stderr)
            return 1
        df = df[columns]

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 40)

    print(f"ファイル: {args.path}")
    print(f"形状: {df.shape[0]} 行 x {df.shape[1]} 列")

    print("\n--- 列名と dtype ---")
    for col, dtype in df.dtypes.items():
        print(f"  {col}: {dtype}")

    print("\n--- 欠損数（多い順） ---")
    n_missing = df.isna().sum()
    n_missing = n_missing[n_missing > 0].sort_values(ascending=False)
    if n_missing.empty:
        print("  欠損なし")
    else:
        for col, n in n_missing.items():
            print(f"  {col}: {n}")

    print(f"\n--- 先頭 {args.rows} 行 ---")
    print(df.head(args.rows))

    if args.describe:
        print("\n--- 数値列の describe() ---")
        numeric = df.select_dtypes(include="number")
        if numeric.empty:
            print("  数値列なし")
        else:
            print(numeric.describe())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
