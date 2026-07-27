"""コマンドラインインターフェース（サブコマンド方式）。

パイプライン工程:
    python main.py all                 # generate -> ingest -> integrate -> features
    python main.py generate            # 合成データ生成
    python main.py ingest              # 分割CSV収集・統合（ステップ1）
    python main.py integrate           # VIN軸で結合（ステップ2）
    python main.py features            # 特徴量作成（ステップ3）

ユーティリティ:
    python main.py catalog             # CSVの各列名・型・元ファイル名等を YAML に記録
    python main.py category --input data/sample/defect_categories.csv \
                            --output reports/category_integrated.csv
                                       # 大/中/小カテゴリから統合カテゴリを生成

共通オプション: --config, --log-level（各サブコマンドの後ろに付与可）
"""

from __future__ import annotations

import argparse
import logging

from .config import Config
from .features import build_features
from .generate import generate
from .ingest import ingest
from .integrate import integrate
from .logging_utils import setup_logging

logger = logging.getLogger(__name__)

# 工程名 -> 実行関数（Config を受け取り dict を返す）
STAGES = {
    "generate": generate,
    "ingest": ingest,
    "integrate": integrate,
    "features": build_features,
}
ALL_ORDER = ["generate", "ingest", "integrate", "features"]


def build_parser() -> argparse.ArgumentParser:
    # 全サブコマンド共通のオプション
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="設定ファイルのパス（既定: config/config.yaml）")
    common.add_argument("--log-level", default="INFO", help="ログレベル（DEBUG/INFO/WARNING/ERROR）")

    parser = argparse.ArgumentParser(
        prog="defect-analysis",
        description="VINを主キーに設備データ・不良・修正を統合し不良原因を追求するパイプライン",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("all", parents=[common], help="全工程を順に実行")
    for stage in ALL_ORDER:
        sub.add_parser(stage, parents=[common], help=f"{stage} 工程のみ実行")

    p_catalog = sub.add_parser("catalog", parents=[common], help="CSVのスキーマを YAML カタログに記録")
    p_catalog.add_argument("--input", default=None, help="対象glob/ディレクトリ/ファイル（既定: catalog.input_glob）")
    p_catalog.add_argument("--output", default=None, help="出力YAMLパス（既定: catalog.output_path）")

    p_category = sub.add_parser("category", parents=[common], help="大/中/小カテゴリから統合カテゴリを生成")
    p_category.add_argument("--input", required=True, help="入力CSV（大/中/小カテゴリ列を含む）")
    p_category.add_argument("--output", required=True, help="統合カテゴリ列を付与した出力CSV")
    p_category.add_argument("--map", default=None, help="変換設定yaml（既定: config/category_map.yaml）")

    # 分析ステージ（features 生成後に実行）
    sub.add_parser("eda", parents=[common], help="ステップ4: EDAグラフ生成")
    sub.add_parser("stats", parents=[common], help="ステップ5: 統計検定（相関・群間差）")
    sub.add_parser("ml", parents=[common], help="ステップ6: 機械学習（LightGBM主軸）")

    return parser


def run_pipeline(command: str, cfg: Config) -> dict:
    """パイプライン工程（all または個別工程）を実行する。"""
    stages = ALL_ORDER if command == "all" else [command]
    results: dict[str, dict] = {}
    for stage in stages:
        logger.info("=== 工程開始: %s ===", stage)
        results[stage] = STAGES[stage](cfg)
    return results


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    setup_logging(cfg.path("paths.logs_dir", create=True), getattr(logging, args.log_level.upper(), logging.INFO))

    if args.command in STAGES or args.command == "all":
        run_pipeline(args.command, cfg)
    elif args.command == "catalog":
        from .schema_catalog import build_catalog

        build_catalog(cfg, input_glob=args.input, output_path=args.output)
    elif args.command == "category":
        from .category_integrate import run_category_integration

        run_category_integration(cfg, input_path=args.input, output_path=args.output, map_path=args.map)
    elif args.command == "eda":
        from .eda import run_eda

        run_eda(cfg)
    elif args.command == "stats":
        from .stats_tests import run_stats

        run_stats(cfg)
    elif args.command == "ml":
        from .ml import run_ml

        run_ml(cfg)

    logger.info("完了: %s", args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
