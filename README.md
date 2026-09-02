# 不良原因追求データ分析パイプライン

設備トレーサビリティ・設備トレンド・不良・修正の4データを **VIN を主キー** に統合・加工し、
不良原因の追求（可視化・統計・機械学習）につなげるためのパイプライン。

実データ（`data/raw` 配下の CSV）を `convert` → `assemble` で VIN 単位のパネル
（`data/interim/vin_panel.parquet`）にまとめ、`eda` / `stats` / `ml` で分析する。
詳細設計は [docs/real_data_ingest_design.md](docs/real_data_ingest_design.md)。
最近の変更は [CHANGELOG.md](CHANGELOG.md) を参照してください。

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
  category_integrate.py     ユーティリティ: カテゴリ列→統合カテゴリ生成（CSVマッピング表）
  naming.py                 実データ経路: 列名・ソース名の機械的正規化
  vin_key.py                実データ経路: VIN 正規化（空白除去・base/pass_no 分解・ダミー判定）
  raw_sources.py             実データ経路: data/raw/ の走査・ソース定義
  raw_convert.py             実データ経路: raw CSV → data/lake/ Parquet 変換・読取
  assemble.py                実データ経路: data/lake/ → VIN パネル組立（trend時刻結合含む）
  cli.py                    argparse CLI（サブコマンド方式）
config/category_map.csv     統合カテゴリの変換表（value → category。CLI category サブコマンド用）
config/塗装課内不良対比表_まとめ.csv   repair の統合カテゴリ変換表（作業工程+大分類+中分類+小分類 → グラフ項目。4キー厳密一致）
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

**統合カテゴリ生成**（カテゴリ列 → 統合カテゴリ。変換は [config/category_map.csv](config/category_map.csv)）:

```bash
uv run python main.py category \
  --input data/sample/defect_categories.csv \
  --output reports/category_integrated.csv \
  --source-column 中カテゴリ \
  --output-column 統合カテゴリ \
  --map config/category_map.csv
```

変換表は `value,category` の1対1マッピング表。表に無い値は**元の値のまま**出力され、
未一致の値と件数が WARNING ログに出る（欠損は欠損のまま）。写像元の列は `--source-column`
（必須）で指定する。

```csv
value,category
締結,締結不良
溶接,機能系
塗装,外観系
```

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
  設備数に追従。固定リストは持たない）。全図の下端に **設備名・データ種・データ範囲（件数/適用フィルタ）
  の脚注**を焼き込む。配色は検証済みパレット、日本語対応。
- **stats**: 数値×2群（Welch t / Mann-Whitney U）、カテゴリ×2群（カイ二乗）、数値×連続（Pearson/Spearman）、
  カテゴリ×連続（ANOVA/Kruskal-Wallis）。効果量と BH-FDR 補正後 p 値を `statistical_tests.csv` と
  `statistical_summary.md` に出力。
- **ml**: 分類（`has_repair_record`）・回帰（`repair_修正__count`）を交差検証で比較し、
  LightGBM の保持テスト評価（ROC/PR 曲線・予測vs実測）と特徴量重要度（gain / permutation）を出力。
  各図には eda と同じ脚注（設備名/データ種/データ範囲）を付与。

#### フィルタで対象を絞る（`analysis.filters`）

`config.yaml` の `analysis.filters` にフィルタ句を並べると、`load_real_panel` の読込直後に
**AND 適用**され、**EDA・統計・機械学習の全ステージに同時に効く**（未指定なら全件で従来どおり）。
絞り込んだ条件はグラフ脚注の「データ範囲」にも自動反映される。

```yaml
analysis:
  filters:
    - {column: ブース__Line, in: [1.0, 2.0]}                          # ラインで絞る（NaN=不明は除外される）
    - {column: vin_pass_no,  eq: 1}                                    # 初回通過の車だけ（再通過を除く）
    - {column: vin_format,   not_in: ["full17"]}                      # 特定の VIN 表記形式を除外
    - {column: 電着__本槽_極液_電導度_測定値, min: 990, max: 1010}    # 数値レンジ（境界含む）
    - {column: シーラー炉__入口_通過日時, min: "2026-01-10", max: "2026-01-20"}  # 期間で絞る（datetime 列）
    - {query: "vin_pass_no == 1"}                                      # 任意式（pandas query。単純名の列のみ）
  filters_on_missing_column: warn   # warn=その句だけスキップ / error=即エラー
```

- 演算子: `eq` / `in` / `not_in` / `min` / `max`（`min`+`max` で数値レンジ）/ `query`。
- 存在しない列（実データ次第で無い列。例: 設備が稼働していない期間の `上塗ロボット__Line`）は
  `filters_on_missing_column` に従い、既定 `warn` でその句だけスキップ。全行が除外される設定は
  `ValueError` で明確に停止する。
- `電着__本槽_極液_電導度_測定値` のように `__` や日本語を含む列は `query` の識別子にできないため
  `min`/`max`/`in` で指定する（`query` は `vin_pass_no` や `has_repair_record` のような単純名の列のみ）。

#### 追加グラフを config で指定する（`analysis.custom_charts`）

`config.yaml` の `analysis.custom_charts` にグラフ定義を並べると、既存の固定7系統に**加えて**
追加の EDA グラフを `reports/eda/custom_*.png` に出力する（空リスト `[]` なら従来どおり固定図のみ）。

| type | 必須 | 任意 | 補足 |
|---|---|---|---|
| `scatter` | `x`,`y`（数値） | `hue`, `alpha` | 点数が上限超過なら決定的サンプリング |
| `bar` | `x`（カテゴリ） | `y`（数値）, `agg`, `hue` | `y` 省略時は件数 |
| `histogram` | `x`（数値） | `bins`, `density`, `hue` | hue 併用時は `density: true` 推奨 |
| `box` | `y`（数値） | `x`（カテゴリ）, `hue` | `x` 省略時は `y` 単体 |
| `heatmap` | `columns`（数値列2本以上） | `method`(`pearson`/`spearman`) | `hue` は非対応（WARN して無視） |

共通フィールド: `title` / `output`（`reports/eda/` 配下のファイル名） /
`filters`（`analysis.filters` と同じ句を図単位で AND 追加適用） / `hue`（色分け列）。

型ごとの例（列名は実パネルに実在する列。2026-09-01 実測。5型・全パターンを網羅）:

```yaml
analysis:
  custom_charts:
    # --- box（箱ひげ） -----------------------------------------------------------
    - {type: box, x: ブース__Line, y: ブース__中塗_リサイクル空調_給気_乾球温度_測定値,
       title: "ライン別 中塗リサイクル空調 給気乾球温度", output: custom_box_line_temp.png}
       # 設備の測定値をライン（ブース__Line）で層別 → ライン間で工程条件がどれだけ違うか
    - {type: box, y: 電着__本槽_極液_電導度_測定値,
       title: "電着 本槽極液電導度の分布（全体）", output: custom_box_single.png}
       # x 省略で単体の箱ひげ → 全体のばらつき・外れ値の有無
    # repair_group__* を x にした「修正なし vs 特定カテゴリ」比較は下記「修正なし車と対比する」参照

    # --- histogram -----------------------------------------------------------------
    - {type: histogram, x: 浮遊ゴミ__PA_ON_相対湿度_測定値, hue: has_repair_record,
       density: true, bins: 30,
       title: "浮遊ゴミ PA_ON 相対湿度の分布（リペア有無別）", output: custom_hist_hue.png}
       # hue 併用。群でサンプル数が違うため density: true で正規化しないと母数の多い群の山が
       # 見かけ上大きくなる → リペア有無で分布に差があるか
    - {type: histogram, x: 前処理__湯洗_1_SS濃度_測定値, bins: 40,
       filters: [{column: vin_pass_no, eq: 1}],
       title: "前処理 湯洗1 SS濃度の分布（初回通過のみ）", output: custom_hist_bins.png}
       # bins 指定で分布形状を細かく見る（filters に eq 指定の例も兼ねる）

    # --- scatter -------------------------------------------------------------------
    - {type: scatter, x: 浮遊ゴミ__PA_ON_露点温度_測定値, y: 浮遊ゴミ__PA_ON_相対湿度_測定値,
       filters: [{query: "has_repair_record == 1"}],
       title: "浮遊ゴミ PA_ON 露点温度と相対湿度の関係（リペアあり車のみ）",
       output: custom_scatter_measures.png}
       # 2測定値の関係（filters に query 指定の例も兼ねる。単純名の列のみ使える）
    - {type: scatter, x: ブース__中塗_リサイクル空調_給気_乾球温度_測定値,
       y: ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値, hue: has_repair_record,
       title: "中塗給気温度とフラッシュオフ絶対湿度（リペア有無別）", output: custom_scatter_hue.png}
       # hue 併用 → 2測定値の関係がリペア有無で変わるか
    - {type: scatter, x: 電着__本槽_極液_電導度_測定値, y: 電着__整流器_1_積算電流値_測定値,
       alpha: 0.25, filters: [{column: 電着__本槽_極液_電導度_測定値, min: 990, max: 1010}],
       title: "電着 本槽極液電導度と整流器1積算電流値（電導度990-1010に限定）",
       output: custom_scatter_alpha.png}
       # 点が多く重なるため alpha を下げて密度を見やすくする（filters に min+max レンジの例も兼ねる）

    # --- bar -----------------------------------------------------------------------
    - {type: bar, x: vin_pass_no, title: "通過回数(vin_pass_no)別 件数", output: custom_bar_count.png}
       # y 省略で件数 → 再通過（2回目以降）がどれくらいの頻度で起きているか
    - {type: bar, x: ブース__Line, y: has_repair_record, agg: mean,
       title: "ライン別 リペア率（平均）", output: custom_bar_mean.png}
       # y + agg: mean → ラインによってリペア率に差があるか

    # --- heatmap ---------------------------------------------------------------------
    - type: heatmap
      method: spearman
      title: "設備横断 測定値相関（Spearman）"
      output: custom_heatmap_spearman.png
      columns:
        - ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値
        - ブース__中塗_リサイクル空調_給気_乾球温度_測定値
        - 浮遊ゴミ__PA_ON_露点温度_測定値
        - 浮遊ゴミ__PA_ON_相対湿度_測定値
        - 電着__本槽_極液_電導度_測定値
        - 電着__整流器_1_積算電流値_測定値
       # columns に複数設備の測定値、method: spearman（外れ値に頑健）→ 設備間の相関構造

  custom_chart_max_hue: 8          # hue 水準数の上限（超過分は頻度上位のみ描画し WARN）
  custom_chart_max_points: 20000   # scatter の最大描画点数（超過時は決定的サンプリング）
```

設定ミス（未知の `type`・列が存在しない・型不一致・フィルタ後0行）は**その図だけ** WARNING を出して
スキップし、他の図の生成は継続する（処理全体は止まらない）。上記の例はすべて `main.py eda` で実際に
描画できることを確認済み（2026-09-01。一時的に全件有効化して実行し、WARNING・スキップが0件だったことを確認）。

#### 修正なし車と対比する（`analysis.repair_groups`）

「修正なしの車」と「特定カテゴリの修正があった車」を同じ図で比較したいことがある
（例: 修正なし vs タレ修正 でブース絶対湿度を箱ひげ比較）。`repair_修正__top_統合カテゴリ`
は修正が無い VIN で NaN になるため `custom_charts` の `x` にそのまま指定しても比較にならない。
`config.yaml` の `analysis.repair_groups` に群定義を並べると、比較用の列
`repair_group__{name}`（+ 2群のときは `repair_group__{name}__bin`）が分析時に導出され、
`custom_charts` からただの列として使えるようになる。列はパネル（parquet）には保存されない
（`load_real_panel` が読込直後に毎回作り直す）。設計は
[docs/repair_group_comparison_design.md](docs/repair_group_comparison_design.md)。

```yaml
analysis:
  repair_groups:
    - name: タレ
      groups:
        - {label: 修正なし, column: has_repair_record, eq: 0}
        - {label: タレ,     column: repair_修正__統合カテゴリ__タレ, min: 1}

  custom_charts:
    - type: box
      x: repair_group__タレ
      y: ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値
      filters:
        - {column: repair_group__タレ, in: [修正なし, タレ]}
      title: 修正なし vs タレ修正 ｜ ブース フラッシュオフHAB1 送気 絶対湿度
      output: repair_group_タレ_絶対湿度.png
```

`groups`/`base_column` それぞれのバリエーション（3群+`na_label`、既存カテゴリ列の流用）の例:

```yaml
analysis:
  repair_groups:
    # 形式A・3群（na_label）: 3つの句で名前付き群を定義し、どれにも該当しない行を na_label で
    # 第4のラベル（ここでは「タレ/色ブツ/修正なし」以外＝他カテゴリの修正）にまとめる。
    # repair_修正__top_統合カテゴリ（VIN あたり1値）への eq で群分けすると重複該当が起きない。
    # groups が3要素のため __bin は生成されない（__bin は groups がちょうど2群のときだけ）。
    - name: 修正系統3群
      groups:
        - {label: タレ,     column: repair_修正__top_統合カテゴリ, eq: タレ}
        - {label: 色ブツ,   column: repair_修正__top_統合カテゴリ, eq: "色ブツ (黒ブツ・白ブツ)"}
        - {label: 修正なし, column: has_repair_record, eq: 0}
      na_label: その他修正

    # 形式B: 既存のカテゴリ列（repair_修正__top_統合カテゴリ、42種）をそのまま群として使い、
    # NaN（＝修正なし車）を na_label でラベル化する（42カテゴリ+修正なしの多群比較）。
    - name: 上位カテゴリ対比
      base_column: repair_修正__top_統合カテゴリ
      na_label: 修正なし

  custom_charts:
    - type: box
      x: repair_group__上位カテゴリ対比
      y: ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値
      filters:
        - {column: repair_group__上位カテゴリ対比, in: [修正なし, タレ, "色ブツ (黒ブツ・白ブツ)"]}
      title: 上位統合カテゴリ(base_column) 別 ブース フラッシュオフHAB1 絶対湿度
      output: repair_group_base_column_絶対湿度.png
```

- `name` はサフィックスのみで、接頭辞 `repair_group__` は**常にコードが付ける**。
  `analysis.leakage_prefixes` の `repair` に前方一致するため、`resolve_predictors` は
  `repair_group__*`（`__bin` も含む）を**必ず**説明変数から除外する。ユーザーが列名を
  自由に決められる仕様にしないのは、これにより修正実績（結果側）由来の列が ML の
  説明変数へ混入するリークを構造的に防ぐため。
- 群定義の形式は2つ（併用不可）。`groups`: `analysis.filters` と同じ句のリスト
  （`label` + `eq`/`in`/`not_in`/`min`/`max`/`query`。複数の群に該当する行は**リスト順で先勝ち**、
  重複件数は WARNING でログに出る）。`base_column`: 既存のカテゴリ列（例:
  `repair_修正__top_統合カテゴリ`）をそのまま群として使い、`na_label` で NaN 行をラベル化する。
- ライン層別（交絡対策）は図単位 `filters` を2枚に分ける方法が分かりやすい。
  設定例は設計書 §6.2 を参照。
- 設定ミス（`groups`/`base_column` の両方・どちらも無し、参照列が存在しない、列名の衝突等）は
  そのスペックだけ WARNING でスキップし、他のスペックと処理全体は継続する。

**`stats` との連携**: `groups` がちょうど2群のスペックは `repair_group__{name}__bin`
（`0.0`/`1.0`/NaN）も対で生成される。`analysis.targets.classification` に追加すると、
`stats_tests.py` を一切変更せずに BH-FDR 補正付きの群間差検定（`main.py stats`）が回る。

```yaml
analysis:
  targets:
    classification: [has_repair_record, repair_group__タレ__bin]   # ← 足すだけ
```

`analysis.targets` は `ml` も読むため、`stats` だけ回したくても `main.py ml` を実行すると
同じ目的変数でモデルが学習される点に注意（実行時間が増える）。

**注意（多重比較・交絡）**: 群分け列を使うと 1,300 列超から「群間で差のある列」を目で
探すことになる。図を大量に作れば偶然だけで効果量の大きい「当たり」が必ず出るため、
1枚の図から結論を出さないこと。最低限、(1) ライン層別で再現するか、(2) 群サイズ n が
十分か（目安30台以上）、(3) `present__*` や `ブース__Line` の分布が両群で揃っているか
（交絡チェック）、(4) 有望な仮説は上記の `__bin` + `stats` で `p_adjusted` を確認する、
の4点を確認してから判断する。詳細は設計書 §8。

## 成果物

| パス | 内容 |
|---|---|
| `reports/eda/*.png` | EDA グラフ（全体4図＋設備別の箱ひげ/ヒートマップ。設備数に追従、脚注つき）＋ `analysis.custom_charts` で指定した追加図 |
| `reports/stats/statistical_tests.csv` / `statistical_summary.md` | 統計検定結果・サマリ |
| `reports/ml/model_performance.csv` | 交差検証の性能比較 |
| `reports/ml/feature_importance_<target>.csv` | 特徴量重要度（gain / permutation） |
| `reports/ml/*.png` | モデル比較・ROC/PR・予測vs実測・重要度の図（脚注つき） |
| `logs/pipeline.log` | 実行ログ |

## 設定（config.yaml の主なキー）

- `storage.format`: `parquet` | `csv`
- `analysis.targets`: 分類/回帰の目的変数
- `analysis.filters` / `filters_on_missing_column`: 分析対象の行フィルタ（EDA/統計/ML 共通。上記「フィルタで対象を絞る」参照）
- `analysis.custom_charts` / `custom_chart_max_hue` / `custom_chart_max_points`: EDA の追加グラフ宣言（上記「追加グラフを config で指定する」参照）
- `analysis.repair_groups`: 修正なし車 vs 特定カテゴリの修正あり車の対比列（上記「修正なし車と対比する」参照）
- `analysis.leakage_columns` / `leakage_prefixes` / `leakage_regex`: 説明変数から除外する結果由来列
  （明示リスト＋接頭辞＋正規表現の規約で自動除外。新しい結果列が増えても規約で拾えるようにしリーク防止）
- `analysis.cv_folds` / `test_size` / `random_state`: 交差検証・保持テストの設定
- `catalog.input_glob` / `output_path`: データカタログの対象と出力先

## 実データ経路（`convert` / `assemble`）

`data/raw/{traceability,trend,defect,repair}/*.csv` の**実データ**を VIN 単位の分析用パネル
`data/interim/vin_panel.parquet` にするための経路（出力先: `data/lake/` / `data/interim/vin_panel.parquet`）。
詳細設計は [docs/real_data_ingest_design.md](docs/real_data_ingest_design.md)、
実データの実測事実は [docs/real_data_facts.md](docs/real_data_facts.md)。

`repair`（修正実績、`data/raw/repair/defect_202601.csv` など）は cp932 エンコーディングかつ独自のクセ（VIN
先頭の `'`、`00000000 000000` という欠測番兵、`修正日`ではなく`PB_ON`が生産日のアンカー等）を
持つため専用の追補設計 [docs/real_data_repair_design.md](docs/real_data_repair_design.md) に従う。
月単位のファイル分割（`defect_202601.csv`, `defect_202602.csv` など）にも
コード変更なしで対応している（同一ソースとして自動的にグルーピングされる）。
repair は VIN 台帳（他ソースとの和集合）には加えず、既存台帳への left join のみで
`repair_*` 列（すべて `analysis.leakage_prefixes` でリーク除外される）を付与する。

repair には `config/塗装課内不良対比表_まとめ.csv`（作業工程+大分類+中分類+小分類 → グラフ項目）による
**統合カテゴリ**も付与される（`repair_修正__統合カテゴリ__*` 列。既定 43 種）。照合は
`入力工程`/`大分類`/`中分類`/`小分類` の**4キー厳密一致のみ**（3キーへのフォールバックは無い）で、
未一致は `対象外工程`（対比表に無い入力工程）/ `未分類`（対象工程だが組合せが表に無い）/
`グラフ対象外`（グラフ項目が `-`）の3ラベルで必ず埋まる（NaN にはならない）。
`未分類` の組合せは `reports/repair_category_unmatched.csv` に出力されるので、対比表に行を追記して
`assemble` を再実行すれば反映される（レイクの再変換は不要）。任意の CSV に対する1対1の値変換なら
`config/category_map.csv`（CLI `category` サブコマンド）、repair の4キー写像は本機能、と使い分ける。
詳細は [docs/repair_integrated_category_design.md](docs/repair_integrated_category_design.md)。

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

1年分では変換後のレイクでも列数が最大 621（trend/ブース）に達する。パネルは **1か月分（2026-01）で
21,020行 × 1,357列（列剪定後。剪定前は1,549列、うち trend 由来が577列）と p ≫ n になる**
（特徴量選択が下流で別途必須）。1年分（約32万
VIN）を扱う場合は `--date-from`/`--date-to` と `real_ingest.trend.include_columns` による絞り込みが前提。

`assemble` は最後に低カーディナリティ列（全 NaN・定数）を落とす（既定 on。列名に「フラグ」を
含む列・`present__`/`defect_`/`repair_` 接頭辞の列・`vin`/`vin_base`/`vin_pass_no`/`vin_format`/
`has_repair_record` は値が1種類でも保護されて残る）。落とした列・保護された無情報列は
`reports/panel_pruned_columns.csv` に記録される。剪定は入力データ（期間）に応じて対象列が変わるため、
月次比較などで列集合を揃えたい場合は `real_ingest.assemble.prune_low_cardinality.enabled: false` にする
（レイク `data/lake/` は無傷なので、いつでも全列パネルに戻せる）。
詳細は [docs/panel_prune_and_multirow_agg_design.md](docs/panel_prune_and_multirow_agg_design.md)。

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
| `reports/repair_category_unmatched.csv` | repair の統合カテゴリで未一致だった4キー組合せ（区分・件数・VIN数） |
| `reports/panel_pruned_columns.csv` | 剪定で落とした列・保護されて残った無情報列（列名・由来ソース・dtype・ユニーク数・欠損数・削除理由・保護規約） |

> **ブツ検にレコードが無い VIN の扱い（確定・2026-07-31）**: 「不良ゼロ」ではなく「未検査」として扱う。
> defect ソースの出力列は `defect_{source}__has`（存在フラグ。1 = そのソースに登場した）1 列のみ
> （2026-08-08 の D11 で `__count`/`__kind__*`/`__part__*` 等は全廃済み）。その VIN が defect ソースに
> 一切登場しない場合は **0 埋めせず NaN のまま**残す（`__has` は「登場した」ことしか表さないため、
> 登場しない = 未検査を「1」以外の値=NaNで表現する。`by_size_bin: true` 時の
> `defect_{source}__size_bin__*` も同様に 0 埋めしない）。
>
> **`has_repair_record` 列**: `assemble` は全ての `repair_*__has` 列（複数 repair ソースがあれば
> それらの OR）から VIN 単位の修正記録有無フラグ `has_repair_record`（0/1, int）をパネルに追加する。
> `has_repair` が `analysis.leakage_prefixes` に含まれるため ML 特徴量からは自動除外される
> （EDA での可視化には使ってよいが、予測の説明変数には使わないこと）。

### config（`real_ingest:` セクション）の主なキー

- `real_ingest.vin.suffix_policy`: VIN サフィックス（`a`/`b`/`c`。同一車体の2回目以降の通過を意味する
  ことが確認済み）の扱い。`keep`（既定・別キー） | `merge`（`vin_base` に丸める）
- `real_ingest.multi_row`: 複数行/VIN ソース（上塗/下塗/ホイ黒ロボット。自動判定・宣言不要）の集約設定。
  列名の末尾一致（`stat_suffixes`）で「統計量」（`numeric_aggs` を適用。`{source}__{col}__{agg}`）と
  「代表値」（VIN 内最小値 1 列。`{source}__{col}`）に振り分ける（値からは判定しない）。
  `numeric_aggs` 既定は `[mean]`（2026-08-30 ユーザー判断で min/std/max を撤廃。`__min` は非稼働ロボットの
  0 を拾うだけで工程情報が無い列が多く、列数を必要以上に増やさない方針）。日時列は変更対象外で
  `datetime_aggs`（既定 `[min]`）のまま。`exclude_columns`（既定 `[ロボット]`。pivot はしない）、
  `by_source`（ソース別に `numeric_aggs` 等を上書き。3ソースとも mean のため既定は空）。
  `enabled: false` で従来どおり `{source}__n_rows` のみに戻る
- `real_ingest.assemble.prune_low_cardinality`: 低カーディナリティ列の剪定設定（`enabled`/`drop_all_nan`/
  `drop_constant`/`protect_columns`/`protect_prefixes`/`protect_name_substrings`/`report_filename`）
- `real_ingest.defect`: `size_column`（不良サイズの列名。`by_size_bin` 有効時のみ使用）、
  `by_size_bin`（0.1mm 刻みのビンカウント列 `{source}__size_bin__*` を作るか。既定 off）、
  `size_bin_min` / `size_bin_max` / `size_bin_width`（ビン範囲・幅[mm]。固定値でのみ指定可能）
- `real_ingest.repair`: repair（修正実績）の VIN 集約設定。`time_column`（`修正日時`）/
  `production_time_column`（`PB_ON`。date パーティションのアンカーと同じ）/ `workload_column`（`修正工数`）/
  `category_columns`（列ごとにカウント展開するか。既定は `大分類` と `統合カテゴリ` のみ）/
  `worker_column`（`修正員_id`）/ `max_category_columns`（超過で `ValueError`）/
  `category_map`（対比表による統合カテゴリ付与の設定。上記「実データ経路」の repair 節を参照）。
  詳細は [docs/real_data_repair_design.md](docs/real_data_repair_design.md) /
  [docs/repair_integrated_category_design.md](docs/repair_integrated_category_design.md)
- `real_ingest.source_aliases`: `"{kind}/{source_key}" -> 別名` でソース名の衝突を解決する
  （既定 `repair/defect: 修正`。ファイル名 `defect.csv` が `defect` kind の `defect` ソースと衝突するため）
- `real_ingest.convert.encoding_fallbacks` / `by_kind`: kind 単位でエンコーディング・時刻アンカー列・
  repair 専用の前処理（アポストロフィ除去・欠測番兵の NA 化・日時列の組み立て・PII ハッシュ化）を上書きする
- `real_ingest.trend`: trend 採用列（`include_suffixes`/`include_columns`）、窓集約（`mode`/`window_minutes`/`tolerance_minutes`）、
  アンカー解決（`anchor_map`/`fallback_anchor_source`）、trend×VIN 非重複時の挙動（`on_no_overlap`: `warn_empty`/`skip`/`error`）
- `real_ingest.assemble.date_from` / `date_to` / `require_sources` / `max_columns_per_source`

> trend（2026-01-01 05:00 〜 2026-02-01 04:59）と traceability/defect の期間は重複しており、`assemble` は
> trend 列を実際に結合する。マッチ率（VIN 21,020 件）は、アンカー別に
> ブース 85.5% / 電着 83.9% / 前処理 83.8% / 浮遊ゴミ 83.2% に達する。
> 詳細は [docs/real_data_facts.md](docs/real_data_facts.md) §6 を参照（旧サンプル固有の「期間非重複」という制約は実装の誤りではなく
> サンプルの構成に由来する既知の制約だったことが実データで裏付けられた）。
> `on_no_overlap: warn_empty` は期間が重複しない場合の既定挙動として機能する。

## テスト

```bash
.venv/bin/python -m pytest tests/ -q
```

15 ファイル・242 テストで、コア変換ロジック（`test_transforms.py`, unittest）・フィルタ（`test_filters.py`）・
グラフ注記（`test_annotation.py`, `test_viz.py`）・設備グルーピング（`test_equipment_groups.py`）・
リーク規約（`test_predictors.py`）ほかを pytest で検証する。

## 実装状況（設計書ステップ）

- ✅ ステップ4〜6: EDA・統計検定・機械学習（`eda` / `stats` / `ml`）
- ⬜ ステップ7: 深層学習（LSTM 等）— LightGBM 基準を上回る場合のみ採用
- ⬜ ステップ8: 運用・自動化（定期実行・設定外部化の拡充）

不良/修正の結果由来列はリークとして説明変数から除外しているため、機械学習の性能は「工程データのみから
どこまで不良を説明できるか」を表す正直な値になっている。
