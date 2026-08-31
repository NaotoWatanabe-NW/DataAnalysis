# 変更履歴

## 2026-08-30 — 複数行/VIN ソース集約のレビュー対応（numeric_aggs を mean のみに・by_source の2段マージ修正）

### 変更

- **ユーザー判断: ロボット3ソース（上塗/下塗/ホイ黒）の統計量を mean のみにする**
  - `real_ingest.multi_row.numeric_aggs` 既定を `[mean, std, min, max]` → `[mean]` に変更（3ソース共通）
  - 根拠（実測）: `__min` は「非稼働ロボットの 0」を拾っているだけで工程情報を持たない列が多かった
    （上塗の `__min` 列12本中8本が定数落ち、残り4本も値がほぼ0固定）。列数を必要以上に増やさない方針
  - `numeric_aggs` のみが対象。日時列 `{source}__{col}__min`（`datetime_aggs` 由来。trend アンカーとして
    `_discover_anchor_columns` が認識する名前）・代表値（サフィックス無し）・`n_rows` は変更なし
  - `by_source.ホイ黒ロボット.numeric_aggs: [mean]` は3ソース共通既定と重複するため削除（`by_source` の
    仕組み自体は M5 のとおり存置）

### 修正

- **`_multi_row_config()` の `by_source` が2段マージになっていなかったバグを修正**
  （`src/defect_analysis/assemble.py`。レビュー指摘）
  - 修正前は `by_source[source]` の値（per-source 辞書）を丸ごと置換していたため、ユーザーが
    `by_source.{source}` に1キーだけ書くと組み込み既定の他キーが消えていた
  - `_repair_category_map_config()` の `labels`/`key_columns` と同じ流儀で、per-source 辞書自体もキー単位で
    2段マージするよう修正

### 実測（2026-01 分・1か月。上記2件反映後）

- パネル: 21,020 行 × **1,357 列**（剪定前 1,549 列。うち trend 由来 577 列。前回実測 1,399 列から -42 列）
- 剪定: 削除 192 列（全 NaN 83・定数 109）、保護により残した無情報列 15 列（変化なし）
- ロボット3ソース復活列（剪定後）: 上塗ロボット 21（前回48）・下塗ロボット 12（前回27）・
  ホイ黒ロボット 11（変化なし。既に mean のみだったため）
- 日時アンカー列（`{source}__入口_通過日時__min` / `{source}__通過日時__min`）・`閾値判定フラグ` は
  3ソースとも維持を確認

## 2026-08-29 — VIN パネルの列剪定と複数行/VIN ソース（ロボット3ソース）集約復活

### 追加

- **低カーディナリティ列の剪定**（`assemble` の最後、trend 結合・0埋め・`has_repair_record` 付与の後に1回だけ実行）
  - 全 NaN 列（`nunique(dropna=True) == 0`）と定数列（`== 1`）をパネルから削除する。2 値（`== 2`）は削除しない
  - 保護規約（`real_ingest.assemble.prune_low_cardinality`）: `protect_columns`（完全一致。`vin`/`vin_base`/
    `vin_pass_no`/`vin_format`/`has_repair_record`）/ `protect_prefixes`（前方一致。`present__`/`defect_`/`repair_`）/
    `protect_name_substrings`（部分一致。既定 `[フラグ]`）のいずれかに該当する列は、定数でも削除しない
  - `src/defect_analysis/assemble.py` に `is_protected_column()` / `prune_low_cardinality_columns()` を追加
  - `reports/panel_pruned_columns.csv` を新設。削除した列・保護されて残った無情報列（`action=kept_protected`）を
    列名・由来ソース・dtype・ユニーク数・欠損数・削除理由・保護規約とともに記録する
  - `assemble()` の戻り値に `n_columns_pruned` を追加（`n_columns` は剪定後の値）

- **複数行/VIN ソース（上塗/下塗/ホイ黒ロボット）の集約を復活**
  - `docs/real_data_ingest_design.md` D5（2026-08-28、統計量集約の全廃）を部分撤回。レイクにある実測値
    54 列（上塗25・下塗13・ホイ黒16 − 行識別列 `ロボット`）が `{source}__n_rows` 1 列のみに捨てられていたのを、
    列名の末尾一致規約（`stat_suffixes`）で「統計量」（`{source}__{col}__{agg}`）と「代表値」
    （`{source}__{col}`。`groupby("vin").min()`。`first` は行順依存のため使わない）に振り分けて復活させる
  - 設備別 pivot は行わない（D5 の当該部分は維持。`exclude_columns: [ロボット]`）
  - `numeric_aggs` 既定 `[mean, std, min, max]`。ホイ黒ロボットのみ `by_source` で `[mean]` に上書き
    （2 行/VIN・ほぼ全列 VIN 内一定のため）
  - `src/defect_analysis/assemble.py` に `plan_multi_row_aggregation()` を追加、`prepare_multi_row_source()`
    のシグネチャに `cfg` を追加して集約を実装。集約計画の列数が `max_columns_per_source` を超えると
    データを畳む前に `ValueError`
  - `config/config.yaml` に `real_ingest.multi_row` を追加

- 詳細は [docs/panel_prune_and_multirow_agg_design.md](docs/panel_prune_and_multirow_agg_design.md)

### 変更

- `config/config.yaml` の `real_ingest.assemble` に `prune_low_cardinality`（既定 on）を追加

### 実測（2026-01 分・1か月）

- パネル: 21,020 行 × 1,399 列（剪定前 1,603 列。うち trend 由来 577 列）
- 剪定: 削除 204 列（全 NaN 83・定数 121）、保護により残した無情報列 15 列
  （`浮遊ゴミ__*_閾値判定フラグ` 9・`defect_{上塗,電着}ブツ検__has` 2・`repair_修正__大分類__プレス` 1・
  ロボット3ソースの `閾値判定フラグ` 3）
- ロボット3ソース復活列（剪定後）: 上塗ロボット 48・下塗ロボット 27・ホイ黒ロボット 11
- `docs/panel_prune_and_multirow_agg_design.md` §3 の見積り（最終 ≈1,411 列）に対し実測は 1,399 列
  （-12 列）。差分は主にロボット統計量列の `__min` が VIN 横断で定数になるケースが見積りより多かったため
  （設計書 §3 は行レベルの VIN 内一定率から見積もった予測値であり、実行後の実測ではないと明記されている）

## 2026-08-28 — repair 統合カテゴリ（塗装課内不良対比表 4 キー写像）対応

### 追加

- **repair（修正実績）に統合カテゴリを付与する 4 キー厳密一致写像**
  - `config/塗装課内不良対比表_まとめ.csv`（作業工程 + 大分類 + 中分類 + 小分類 → グラフ項目、666 行）を
    唯一の正とし、`入力工程`/`大分類`/`中分類`/`小分類` の 4 キー厳密一致（フォールバック無し）で
    `統合カテゴリ` 列を付与する
  - `src/defect_analysis/category_integrate.py` に `CompositeCategoryTable` /
    `load_composite_category_table` / `apply_composite_category` / `summarize_unmatched_keys` を追加
    （既存の 1 キー写像 `load_mapping` / `apply_category_mapping` / `run_category_integration` とは
    別系統として併存。CLI `category` サブコマンドは無変更）
  - 未一致は NaN にせず `対象外工程`（対比表に無い入力工程）/ `未分類`（対象工程だが組合せが無い）/
    `グラフ対象外`（グラフ項目が `-`）の 3 ラベルで必ず埋める
  - `src/defect_analysis/assemble.py` に `add_integrated_category()` を追加し、`prepare_repair_source`
    の直前で呼び出す。既存の `category_columns` 展開の枠組みをそのまま利用（実データ実測 43 列）
  - `reports/repair_category_unmatched.csv` を新設。未一致の 4 キー組合せを区分・件数・VIN 数付きで出力し、
    対比表を育てる作業の入力とする
  - 詳細は [docs/repair_integrated_category_design.md](docs/repair_integrated_category_design.md)

### 変更

- `config/config.yaml` の `real_ingest.repair` に `category_columns.統合カテゴリ: true` /
  `max_category_columns: 30 -> 100` / `category_map`（対比表のパス・キー列対応・ラベル・重複時挙動）を追加
- `_top_value`（`assemble.py`）のタイブレークを `value_counts()` の出現順から
  `(件数降順, 値昇順)` に変更し、`__top_大分類` 等の最頻値選択を決定的にした

### 削除

- `config/category_map.yaml` を再削除（生成元スクリプトが存在せず再生成不能な死蔵ファイル。
  `docs/category_csv_and_custom_charts_design.md` C9 で削除決定済みの再発）

---

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
