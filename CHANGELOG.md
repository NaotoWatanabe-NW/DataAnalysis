# 変更履歴

## 2026-08-06 — カテゴリ統合の CSV 化・カスタム EDA グラフ・repair 月次分割対応・parquet 確認スクリプト

### 変更

- **カテゴリ統合のルール定義を YAML から CSV へ全面移行**（後方互換なし）
  - `config/category_map.yaml` を廃止し、`config/category_map.csv`（`value,category` の 1 対 1 マッピング表）に置き換え
  - `src/defect_analysis/category_integrate.py` を全面書き換え（`load_mapping` / `apply_category_mapping` / `run_category_integration`）
  - 表に無い値は元の値をそのまま通し、WARNING で未一致値と件数を報告するように変更（データ損失防止）
  - CLI の `category` サブコマンドに `--source-column`（必須）・`--output-column`（既定 `統合カテゴリ`）を追加、`--map` 既定を CSV に変更

- **EDA グラフが config.yaml から追加グラフを宣言的に指定できるように**
  - `config.yaml` の `analysis.custom_charts` に散布図・棒グラフ・ヒストグラム・箱ひげ図・ヒートマップを定義すると、既存の固定図 7 系統に加えて追加の EDA グラフを `reports/eda/custom_*.png` に出力
  - 設定ミス（未知の type・列が存在しない・型不一致・フィルタ後 0 行）はその図だけ WARNING を出してスキップし、処理は継続
  - グラフ単位フィルタを指定可能（`analysis.filters` と同じ句 DSL を再利用）
  - `src/defect_analysis/eda.py` に `_render_custom_charts` 一式を追加

- **repair 月次分割ファイルが正式対応**
  - `data/raw/repair/defect_202607.csv`, `defect_202608.csv` のような月単位ファイル分割に対応（既存の `naming.source_key()` が 6 桁連続数字日付サフィックスを自動吸収する仕組みにより、コード変更は不要だった）
  - 設計書・README の「単一ファイル前提」記述を「月単位での複数ファイル可」に更新

- **フィルタ DSL を analysis_data.py で公開化**
  - `apply_filter_clauses()` / `filters_summary()` を新設し、カスタム図レンダリング時にフィルタを再利用
  - 既存の `apply_filters()` は薄いラッパに再構成（動作・例外・ログは変更なし）

### 追加

- **parquet ファイル確認用スクリプト**: `scripts/inspect_parquet.py`
  - `defect_analysis` パッケージに依存しない独立スクリプト
  - parquet ファイルの形状・列名/dtype・欠損数・先頭 N 行・（オプション）数値列の describe() をターミナルに表示

- **カスタム EDA グラフ設計書**: `docs/category_csv_and_custom_charts_design.md`
  - カテゴリ CSV 化とカスタムグラフの決定事項・根拠・仕様を統合記載

- **category_map.csv**: 統合カテゴリの 1 対 1 マッピング表
  - `value,category` の 2 列構成。表に無い値は元の値のまま出力

- **カスタム EDA グラフのテスト**: `tests/test_custom_charts.py`
  - 5 型（scatter/bar/histogram/box/heatmap）すべてのレンダリング・フィルタ・エラーハンドリング検証

### 削除

- `config/category_map.yaml` — YAML ルール定義（CSV に置き換え）

---

## 2026-08-05 — 旧経路（ingest → integrate → features）の削除

### 削除

- **合成データ前提の旧パイプラインを完全に廃止**
  - ファイル: `src/defect_analysis/ingest.py` / `integrate.py` / `features.py`
  - テスト: `tests/test_ingest_rename.py`
  - CLI サブコマンド: `all` / `ingest` / `integrate` / `features`
  - テストクラス: `test_transforms.py` から旧経路専用の 10 テストクラスを削除

- `analysis_data.py` の `load_features()` 関数

- `config/config.yaml` から削除セクション
  - `ingest:` セクション（CSV 列名マッピング）
  - `features:` セクション（特徴量エンジニアリング設定）
  - `paths.raw_dir` / `interim_dir` / `processed_dir`（旧ディレクトリ構造）

### 変更

- **CLI の構成を簡潔に**（`cli.py`）
  - `STAGES` / `ALL_ORDER` の定義・`run_pipeline()` 関数を削除
  - 実データ経路のみに集約（`convert` → `assemble` → `eda`/`stats`/`ml`）

- **README を実データ経路に統一**
  - 旧経路（ingest/integrate/features）の説明を削除
  - ディレクトリ構成・使い方・テストコマンドを実データ経路に整理

- `src/defect_analysis/__init__.py` / `main.py` — 旧モジュールのインポート削除

### 背景

合成データ前提の旧パイプラインは設計書 `docs/real_data_ingest_design.md` に従い、実データ経路（`convert` → `assemble` → `eda`/`stats`/`ml`）が下流に接続できた時点で廃止する計画になっていました。実装完了に伴い当計画を実行しました。
