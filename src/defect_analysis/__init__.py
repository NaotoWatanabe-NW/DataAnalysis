"""VIN を主キーに設備データ・不良・修正を統合し不良原因を追求するパイプライン。

実データ経路（docs/real_data_ingest_design.md）: convert -> assemble
"""

__all__ = ["config", "io_utils", "logging_utils"]
