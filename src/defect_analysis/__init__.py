"""VIN を主キーに設備データ・不良・修正を統合し不良原因を追求するパイプライン。

工程:
    generate  -> 合成データ生成（実データ導入時は差し替え）
    ingest    -> 分割 CSV 群の自動収集・統合（ステップ1）
    integrate -> VIN 軸で不良集約・修正結合（ステップ2）
    features  -> 特徴量作成・カテゴリ統合（ステップ3）
"""

__all__ = ["config", "io_utils", "logging_utils"]
