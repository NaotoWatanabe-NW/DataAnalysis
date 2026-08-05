# 不良原因追求データ分析パイプライン

設備トレーサビリティ・設備トレンド・不良・修正の4データを **VIN を主キー** に統合・加工し、
不良原因の追求（可視化・統計・機械学習）につなげるためのパイプライン。

実データ（`data/raw` 配下の CSV）を `convert` → `assemble` で VIN 単位のパネル
（`data/interim/vin_panel.parquet`）にまとめ、`eda` / `stats` / `ml` で分析する。
詳細設計は [docs/real_data_ingest_design.md](docs/real_data_ingest_design.md)。

## データモデル（VIN 基数）

| データ | 粒度 | VIN との基数 | 生成先 |
|---|---|---|---|
| トレーサビリティ | 設備ごと・月ごと CSV | 1:1（設備単位） | `data/raw/traceability/` |
| トレンド | 設備ごと・月ごと CSV | 1:1（設備単位） | `data/raw/trend/` |
| 不良 | 月ごと CSV | 1:N（無い VIN は行なし） | `data/raw/defect/` |
| 修正 | 月ごと CSV | 0..N（修正なしは行なし。実データでは最大5行/VIN） | `data/raw/repair/` |

## ディレクトリ構成

```text
config/config.yaml          パス・対象期間・閾値・カテゴリ統合の一元設定
src/defect_analysis/
  config.py                 設定読込とパス解決
  logging_utils.py          ログ設定（コンソール+ファイル）
  io_utils.py               parquet/csv 入出力（pyarrow 未導入時は csv に自動フォールバック）
  eda.py                    ステップ4: EDAグラフ生成
  stats_tests.py            ステップ5: 統計検定（相関・群間差）
  ml.py                     ステップ6: 機械学習（LightGBM主軸）
  analysis_data.py          分析共通: リーク安全な説明変数の解決
  viz_style.py              可視化スタイル（検証済みパレット+日本語フォント）
  schema_catalog.py         ユーティリティ: CSVスキーマを YAML カタログ化
  category_integrate.py     ユーティリティ: 大/中/小→統合カテゴリ生成
  naming.py                 実データ経路: 列名・ソース名の機械的正規化
  vin_key.py                実データ経路: VIN 正規化（空白除去・base/pass_no 分解・ダミー判定）
  raw_sources.py             実データ経路: data/raw/ の走査・ソース定義
  raw_convert.py             実データ経路: raw CSV → data/lake/ Parquet 変換・読取
  assemble.py                実データ経路: data/lake/ → VIN パネル組立（trend時刻結合含む）
  cli.py                    argparse CLI（サブコマンド方式）
config/category_map.yaml    統合カテゴリの変換ルール
data/sample/                サンプル入力（大/中/小カテゴリ）
tests/test_transforms.py    コア変換ロジックのテスト（unittest）
main.py                     エントリポイント
data/                       raw / lake / interim（git 管理外）
reports/                    データ辞書・カタログ・数値サマリ（git 管理外）
```

## セットアップ

```bash
uv sync           # 依存導入（.venv を作成）
```

中間・処理済みテーブルは設計に従い **parquet** を推奨。`pyarrow` が未導入の場合は
自動的に **csv** へフォールバックして動作する（生データ CSV は常に CSV）。
parquet を使うには:

```bash
uv add pyarrow
```

## 使い方

### ユーティリティ

**CSVスキーマカタログ**（各列名・型・元ファイル名・欠損/ユニーク数・例を YAML 化）:

```bash
uv run python main.py catalog                                   # config の catalog.input_glob を対象
uv run python main.py catalog --input "data/raw/**/*.csv" --output reports/data_catalog.yaml
```

出力 `reports/data_catalog.yaml` はファイルごとに `file` / `source_name` / `n_rows` /
`columns[].{name, logical_type, pandas_dtype, n_missing, n_unique, example}` を記録する。

**統合カテゴリ生成**（大/中/小カテゴリ → 統合カテゴリ。変換は [config/category_map.yaml](config/category_map.yaml)）:

```bash
uv run python main.py category \
  --input data/sample/defect_categories.csv \
  --output reports/category_integrated.csv \
  --map config/category_map.yaml
```

`category_map.yaml` の `rules` を上から評価し最初に一致した割当を採用、未一致は
`default`（`concat`/`major`/`middle`/`minor`/`const`）で生成する。

### 分析ステージ（EDA → 統計 → 機械学習）

`data/interim/vin_panel.parquet`（`assemble` の出力）を入力に、設計書ステップ4〜6を実行する。
**不良/修正の結果由来列はリークとして説明変数から除外**し（[config.yaml](config/config.yaml) の `analysis`）、
「工程データ（トレンド・通過時間・設備・時間帯）から不良を説明/予測できるか」を検証する。

```bash
uv run python main.py eda      # ステップ4: EDAグラフ（reports/eda/*.png）
uv run python main.py stats    # ステップ5: 統計検定（相関・群間差、BH-FDR補正）
uv run python main.py ml       # ステップ6: 機械学習（ベースライン→RF→LightGBM）
```

- **eda**: カテゴリ別不良率・目的変数との相関・月次トレンド・不良種類別件数に加え、
  **測定値分布（箱ひげ）と相関ヒートマップは設備ごとに動的生成**する（トレンド列から設備を検出し、
  設備数に追従。固定リストは持たない）。全図の下端に **設備名・データ種・データ範囲（年月/件数/適用フィルタ）
  の脚注**を焼き込む。配色は検証済みパレット、日本語対応。
- **stats**: 数値×2群（Welch t / Mann-Whitney U）、カテゴリ×2群（カイ二乗）、数値×連続（Pearson/Spearman）、
  カテゴリ×連続（ANOVA/Kruskal-Wallis）。効果量と BH-FDR 補正後 p 値を `statistical_tests.csv` と
  `statistical_summary.md` に出力。
- **ml**: 分類（`has_defect` / `has_severe_defect`）・回帰（`defect_count`）を交差検証で比較し、
  LightGBM の保持テスト評価（ROC/PR 曲線・予測vs実測）と特徴量重要度（gain / permutation）を出力。
  各図には eda と同じ脚注（設備名/データ種/データ範囲）を付与。

#### フィルタで対象を絞る（`analysis.filters`）

`config.yaml` の `analysis.filters` にフィルタ句を並べると、`load_real_panel` の読込直後に
**AND 適用**され、**EDA・統計・機械学習の全ステージに同時に効く**（未指定なら全件で従来どおり）。
絞り込んだ条件はグラフ脚注の「データ範囲」にも自動反映される。設計は
[docs/filter_and_annotation_design.md](docs/filter_and_annotation_design.md)。

```yaml
analysis:
  filters:
    - {column: process_month, in: ["2026-01", "2026-02"]}  # 年月
    - {column: plant_code,    eq: P01}                       # 工場・ライン
    - {column: operator,      not_in: [op_ito]}              # 作業者を除外
    - {column: is_weekend,    eq: 0}                          # 平日のみ（0/1）
    - {column: lead_time_sec, min: 60, max: 3600}            # 数値レンジ（境界含む）
    - {query: "ng_rate < 0.5"}                                # 任意式（pandas query）
  filters_on_missing_column: warn   # warn=その句だけスキップ / error=即エラー
```

- 演算子: `eq` / `in` / `not_in` / `min` / `max`（`min`+`max` で数値レンジ）/ `query`。
- 存在しない列（実データ次第で無い列）は `filters_on_missing_column` に従い、既定 `warn` で
  その句だけスキップ。全行が除外される設定は `ValueError` で明確に停止する。
- `EQ-01__pressure` のようにハイフンを含む列は `query` の識別子にできないため `min`/`max`/`in` で指定する。

## 成果物

| パス | 内容 |
|---|---|
| `reports/eda/*.png` | EDA グラフ（全体4図＋設備別の箱ひげ/ヒートマップ。設備数に追従、脚注つき） |
| `reports/stats/statistical_tests.csv` / `statistical_summary.md` | 統計検定結果・サマリ |
| `reports/ml/model_performance.csv` | 交差検証の性能比較 |
| `reports/ml/feature_importance_<target>.csv` | 特徴量重要度（gain / permutation） |
| `reports/ml/*.png` | モデル比較・ROC/PR・予測vs実測・重要度の図（脚注つき） |
| `logs/pipeline.log` | 実行ログ |

## 設定（config.yaml の主なキー）

- `storage.format`: `parquet` | `csv`
- `analysis.targets`: 分類/回帰の目的変数
- `analysis.filters` / `filters_on_missing_column`: 分析対象の行フィルタ（EDA/統計/ML 共通。上記「フィルタで対象を絞る」参照）
- `analysis.leakage_columns` / `leakage_prefixes` / `leakage_regex`: 説明変数から除外する結果由来列
  （明示リスト＋接頭辞＋正規表現の規約で自動除外。新しい結果列が増えても規約で拾えるようにしリーク防止）
- `analysis.cv_folds` / `test_size` / `random_state`: 交差検証・保持テストの設定
- `catalog.input_glob` / `output_path`: データカタログの対象と出力先

## 実データ経路（`convert` / `assemble`）

`data/raw/{traceability,trend,defect,repair}/*.csv` の**実データ**を VIN 単位の分析用パネル
`data/interim/vin_panel.parquet` にするための経路（出力先: `data/lake/` / `data/interim/vin_panel.parquet`）。
詳細設計は [docs/real_data_ingest_design.md](docs/real_data_ingest_design.md)、
実データの実測事実は [docs/real_data_facts.md](docs/real_data_facts.md)。

`repair`（修正実績、`data/raw/repair/defect.csv`）は cp932 エンコーディングかつ独自のクセ（VIN
先頭の `'`、`00000000 000000` という欠測番兵、`修正日`ではなく`PB_ON`が生産日のアンカー等）を
持つため専用の追補設計 [docs/real_data_repair_design.md](docs/real_data_repair_design.md) に従う。
repair は VIN 台帳（他ソースとの和集合）には加えず、既存台帳への left join のみで
`repair_*` 列（すべて `analysis.leakage_prefixes` でリーク除外される）を付与する。

> **個人情報の取り扱い（既定でハッシュ化）**: repair の `修正員`（作業者氏名）列は `convert` 段階で
> 不可逆ハッシュ化し（既定 `real_ingest.convert.by_kind.repair.pii.mode: hash`）、`data/lake/` を含め
> 生の氏名をどこにも保存しない。ハッシュ列は `修正員_id` という名前になる。氏名のまま保持したい場合は
> `mode: keep`、列ごと削除したい場合は `mode: drop` に変更できるが、既定は `hash` を推奨する
> （ソルトは `pii.salt` または環境変数 `DEFECT_ANALYSIS_PII_SALT` で指定可能。ソルトを変えると
> 過去のレイクとハッシュ ID が一致しなくなるため `--force` 再変換が必要）。

```text
convert   → raw CSV を列名正規化・VIN 正規化・日付パーティション付き Parquet に変換（増分・冪等）
              data/raw/{kind}/*.csv → data/lake/{kind}/{source}/date=YYYY-MM-DD/*.parquet
assemble  → レイクをソース別に集約・VIN 横結合・trend 時刻結合してパネル化
              data/lake/ → data/interim/vin_panel.parquet
```

```bash
.venv/bin/python main.py convert                                  # 増分変換（変更ファイルのみ）
.venv/bin/python main.py convert --force                          # 全ファイル強制再変換
.venv/bin/python main.py assemble                                 # 全期間でパネル組立
.venv/bin/python main.py assemble --date-from 2026-07-24 --date-to 2026-07-25  # 期間を絞る
```

1年分では変換後のレイクでも列数が最大 621（trend/ブース）に達し、パネルは **1日分でも
約1,300〜1,500行 × 約1,870列と p ≫ n になる**（特徴量選択が下流で別途必須）。1年分（約32万
VIN）を扱う場合は `--date-from`/`--date-to` と `real_ingest.trend.include_columns` による絞り込みが前提。

### 主な出力

| 出力 | 内容 |
|---|---|
| `data/lake/{kind}/{source}/date=.../*.parquet` | 変換済みレイク（`convert`） |
| `data/lake/_manifest.json` | 増分変換の管理台帳（size/mtime） |
| `data/interim/vin_panel.parquet` | VIN × 全列のパネル（`assemble`） |
| `reports/column_name_mapping.csv` | 列名正規化の対応表（正規化後 → 元列名。`convert` 出力） |
| `reports/ingest_quality.csv` | ソース別: 採用ファイル数・行数・VIN数・ダミー除外数・重複数など |
| `reports/vin_panel_dictionary.csv` | パネルの列名・dtype・欠損数・ユニーク数・例・由来ソース |
| `reports/trend_anchor_map.csv` | trend 列トークン → アンカーとなる traceability ソース・列・解決経路 |
| `reports/trend_join_report.csv` | アンカー別の trend マッチ率と期間 |

> **ブツ検にレコードが無い VIN の扱い（確定・2026-07-31）**: 「不良ゼロ」ではなく「未検査」として扱う。
> `defect_{source}__count` / `__has` / `__kind__*` / `__part__*` は、その VIN が defect ソースに
> 一切登場しない場合は **0 埋めせず NaN のまま**残す（`prepare_defect_source` 内で「その VIN は
> 登場するが特定の種類が無い」場合の 0 埋めは従来どおり行う。区別されるのは「登場すらしない」場合のみ）。
>
> **`has_repair_record` 列**: `assemble` は全ての `repair_*__has` 列（複数 repair ソースがあれば
> それらの OR）から VIN 単位の修正記録有無フラグ `has_repair_record`（0/1, int）をパネルに追加する。
> `has_repair` が `analysis.leakage_prefixes` に含まれるため ML 特徴量からは自動除外される
> （EDA での可視化には使ってよいが、予測の説明変数には使わないこと）。

### config（`real_ingest:` セクション）の主なキー

- `real_ingest.vin.suffix_policy`: VIN サフィックス（`a`/`b`/`c`。同一車体の2回目以降の通過を意味する
  ことが確認済み）の扱い。`keep`（既定・別キー） | `merge`（`vin_base` に丸める）
- `real_ingest.sources` / `defaults`: 複数行/VIN ソース（自動判定・宣言不要）の集約方法（`aggs`）・pivot 列（`pivot_by`）。`sources` は defaults と異なる挙動にしたいソースのみ上書き記入する
- `real_ingest.defect`: 不良サイズ/種類/検査部位の列名、種類別カウント列を作るか（`by_kind`）
- `real_ingest.repair`: repair（修正実績）の VIN 集約設定。`time_column`（`修正日時`）/
  `production_time_column`（`PB_ON`。date パーティションのアンカーと同じ）/ `workload_column`（`修正工数`）/
  `category_columns`（列ごとにカウント展開するか。既定は `大分類` のみ）/ `worker_column`（`修正員_id`）/
  `max_category_columns`（超過で `ValueError`）。詳細は [docs/real_data_repair_design.md](docs/real_data_repair_design.md)
- `real_ingest.source_aliases`: `"{kind}/{source_key}" -> 別名` でソース名の衝突を解決する
  （既定 `repair/defect: 修正`。ファイル名 `defect.csv` が `defect` kind の `defect` ソースと衝突するため）
- `real_ingest.convert.encoding_fallbacks` / `by_kind`: kind 単位でエンコーディング・時刻アンカー列・
  repair 専用の前処理（アポストロフィ除去・欠測番兵の NA 化・日時列の組み立て・PII ハッシュ化）を上書きする
- `real_ingest.trend`: trend 採用列（`include_suffixes`/`include_columns`）、窓集約（`mode`/`window_minutes`/`tolerance_minutes`）、
  アンカー解決（`anchor_map`/`fallback_anchor_source`）、trend×VIN 非重複時の挙動（`on_no_overlap`: `warn_empty`/`skip`/`error`）
- `real_ingest.assemble.date_from` / `date_to` / `require_sources` / `max_columns_per_source`

> 現在配布されているサンプル（1日分）は **trend の期間（07/29-30）と traceability/defect の期間（07/24-25）が
> 重複していない**ため、`assemble` は毎回 WARN を出して trend 列を全 NaN で生成する（既定 `on_no_overlap: warn_empty`）。
> これは実装の誤りではなく、期間が重複する実データが提供されるまで検証できない既知の制約（設計書 §8.4(4) / §13）。

## テスト

```bash
.venv/bin/python -m pytest tests/ -q
```

コア変換ロジック（`test_transforms.py`, unittest）に加え、フィルタ（`test_filters.py`）・
グラフ注記（`test_annotation.py`, `test_viz.py`）・設備グルーピング（`test_equipment_groups.py`）・
リーク規約（`test_predictors.py`）を pytest で検証する。

## 実装状況（設計書ステップ）

- ✅ ステップ4〜6: EDA・統計検定・機械学習（`eda` / `stats` / `ml`）
- ⬜ ステップ7: 深層学習（LSTM 等）— LightGBM 基準を上回る場合のみ採用
- ⬜ ステップ8: 運用・自動化（定期実行・設定外部化の拡充）

不良/修正の結果由来列はリークとして説明変数から除外しているため、機械学習の性能
（例: `has_defect` の ROC-AUC ≈ 0.74、`defect_count` の R² ≈ 0.2）は「工程データのみから
どこまで不良を説明できるか」を表す正直な値になっている。
