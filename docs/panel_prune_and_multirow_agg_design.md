# VIN パネルの列剪定と複数行/VIN ソース集約 設計仕様書（2026-08-28）

対象: `assemble`（`data/lake/` → `data/interim/vin_panel.parquet`）における
(1) **低カーディナリティ列の剪定**、(2) **複数行/VIN ソース（上塗/下塗/ホイ黒ロボット）の集約復活**。

この 2 つは相互作用する（復活させた列にも剪定が掛かる／剪定の保護規約が復活列の扱いを決める）ため、
1 本の設計書にまとめる。前提の取り込みパイプラインは [`real_data_ingest_design.md`](real_data_ingest_design.md)、
repair の不変条件は [`repair_integrated_category_design.md`](repair_integrated_category_design.md)。

本書は `real_data_ingest_design.md` の **D5 を部分的に上書きする**（§8）。
既存設計書の本文更新は後続タスクで行い、本書が正となる。

---

## 0. 結論（決定事項と根拠）

### 剪定（P: Prune）

| # | 決定 | 根拠 |
|---|---|---|
| P1 | **全 NaN 列（`nunique(dropna=True) == 0`）と定数列（`== 1`）をパネルから削除する。2 値（`== 2`）は削除しない** | ユーザー確定方針。説明変数として情報量が無く、`p ≫ n`（21,020 行 × 1,498 列）を悪化させるだけ。2 値は閾値判定フラグ 53 列など実質的な情報を持つ（実測 §1） |
| P2 | **NaN は水準として数えない（`dropna=True`）。値が 1 種類 + NaN の列は定数として削除対象** | 「レコードが無い」情報は `present__{source}` と `defect_{P}__has` が既に担う。値の水準が 1 種類なら説明変数として無情報 |
| P3 | **保護規約は `analysis.leakage_prefixes` と同じ「規約で自動的に拾う」方式にする**: `protect_columns`（完全一致）/ `protect_prefixes`（前方一致）/ `protect_name_substrings`（部分一致）の 3 種を config に置く。既定は `protect_name_substrings: [フラグ]`・`protect_prefixes: [present__, defect_, repair_]`・`protect_columns: [vin, vin_base, vin_pass_no, vin_format, has_repair_record]` | 明示リスト（列名の直書き）は列名が増えるたびに保守が必要で D3 の思想に反する。接頭辞・部分一致なら日次実行で列が増えても自動追従する。`defect_` / `repair_` は `leakage_prefixes` にも載っている「結果側」の列群で、目的変数・分析対象そのものなので一括保護が正しい |
| P4 | **剪定は `assemble` の最後（trend 結合・0 埋め・has_repair_record 付与の後）に 1 回だけ実行する** | `repair` の 0 埋めや trend 結合の結果として初めて定数になる列があるため、最終パネルに対して 1 回判定するのが唯一正しい。ソース別フレームの段階で判定すると結合後の状態と食い違う |
| P5 | **剪定は `assemble` 側で行い、`analysis_data.load_real_panel` では行わない** | ユーザーの運用が「convert → assemble → 出力を確認」であり、`reports/vin_panel_dictionary.csv` と parquet 自体が剪定後になっていないと確認にならない。原本喪失の懸念は「レイク `data/lake/` が無傷の一次情報であること」「`enabled: false` で即座に全列パネルに戻せること」で解消される（§4.5 で比較） |
| P6 | **既定は on（コード側 `DEFAULT_ASSEMBLE` も `config/config.yaml` も `enabled: true`）** | コード既定と yaml 既定を食い違わせると、yaml を読まない経路（テスト・smoke）で挙動が変わる事故が過去に起きている（`tests/test_real_ingest_smoke.py` の repair 既定値コメント）。単一の既定値に揃える |
| P7 | **削除列は `reports/panel_pruned_columns.csv` に記録する。保護されて残った定数列も `action=kept_protected` として同じファイルに記録する** | 既存レポート群（`ingest_quality.csv` / `trend_join_report.csv` / `repair_category_unmatched.csv`）と同じ流儀。「なぜ落ちたか」と「保護のせいで無情報な列が残っている」の両方が 1 ファイルで追える |
| P8 | **`assemble()` の戻り値に `n_columns_pruned` を追加する**（`n_columns` は剪定後の値） | 剪定が効いたかを戻り値だけで検証でき、smoke テストが列名を知らずにアサートできる |

### 複数行/VIN ソースの集約（M: Multi-row）

| # | 決定 | 根拠 |
|---|---|---|
| M1 | **D5 のうち「統計量集約の全廃」を撤回し、「列名の末尾一致規約で `統計量` と `代表値` に振り分ける集約」を導入する。pivot 廃止は維持する** | レイクにある実測値 54 列が丸ごと捨てられている（`n_rows` 1 列のみ）。旧 D5 の統計量集約と違い、(a) 全数値列一律ではなく規約で振り分ける、(b) per-source で集約関数を指定できる、(c) 列数ガードを掛ける、(d) 剪定とセットで**総列数は純減**にする。§8 に D5 改定文を置く |
| M2 | **振り分けは列名の末尾一致（`stat_suffixes`）で決める。データの値（VIN 内一定率）から動的に決めてはならない** | VIN 内一定率で自動判定すると、日次実行のたびに列集合が変わる（D10 が禁じた失敗そのもの）。実測（§1.3）では「VIN 内で変動する列」＝「末尾が `測定値/設定値/RT値/使用量/充填量/排出量/サイクルタイム` の列」に**完全に一致**しており、規約で表現できる。`trend.include_suffixes` / `convert.keep_float64_suffixes` と同じ書式 |
| M3 | **統計量列は `{source}__{col}__{agg}`、代表値列は `{source}__{col}`（サフィックス無し）、日時列は `{source}__{col}__min`、行数は `{source}__n_rows`** | 代表値をサフィックス無しにすることで 1 行/VIN ソースと同形になり、`analysis_data.traceability_measure_columns` / `equipment_measure_groups`（`{eq}__{measure}` で分割）が無改修で動く。日時 `__min` は既存 `_discover_anchor_columns` が既にサポートしている名前（§5.4） |
| M4 | **代表値は `groupby("vin").min()`。`first`（行順依存）は使わない** | 代表値を取るのは VIN 内一定率 1.000 前後の列だけなので、どの代表値でも結果はほぼ同じ。その上で `min` は決定的・ベクトル化済みで高速。`first` は part ファイルの読み込み順に依存し再現性が無い |
| M5 | **`numeric_aggs` は全 3 ソース共通で `[mean]` のみ（2026-08-30 撤回）** | 当初（2026-08-28 時点）は「上塗 30 行/VIN・下塗 19 行/VIN は平均だけでは不足、ホイ黒は mean 1 本で足りる」として `by_source` で非対称に設定する案だったが、実装時のユーザー判断により全ソース共通で簡潔に統一した。理由: `__min` は非稼働ロボットが記録した 0 を拾うだけで工程情報を持たず（実測で上塗 `__min` 列 12 本中 8 本が定数落ち）、`std`・`max` も不要と判定。2 回以上登場するデータは保持する方針のもと、必要以上に列を増やさない（統計量による列数増加を避けて剪定効果を最大化）。 |
| M6 | **`exclude_columns: [ロボット]`。pivot キー候補である行識別列は集約しない** | `ロボット` は VIN 内一定率 0.000（＝行の識別子）で、代表値に意味が無い。台数情報は `n_rows` が持つ。この 1 列を除くことで「pivot しない」という D5 の判断が名前の上でも明確になる |

### 相互作用（X）

| # | 決定 | 根拠 |
|---|---|---|
| X1 | **順序は「集約 → 結合 → trend → 剪定」。復活させた列にも剪定ルールを適用する** | 全域定数の設備列（上塗 `高電圧_設定値`・`塗装ロボットブラシ_段数`、下塗 `温度使用量判定結果`、ホイ黒 6 列）を復活直後に自動で落とせる。復活と剪定を別パスにすると無情報列が 11 列残る |
| X2 | **3 ソースの `閾値判定フラグ`（全域 nunique=1）は保護して残す。ただし `panel_pruned_columns.csv` に `action=kept_protected` で記録し、WARN ログを 1 行出す** | ユーザーの明示指示「フラグとついた列はそのままにする」を字義どおり守る。コストは 3 列（剪定後 1,411 列に対して 0.2%）でしかなく、例外規則を増やしてまで削る価値が無い。「保護のせいで無情報な列が残っている」事実はレポートと WARN で可視化する |
| X3 | **`repair_` 接頭辞を保護するので、IC6 の不変条件 `Σ repair_修正__統合カテゴリ__* == repair_修正__count` は剪定の影響を受けない** | 統合カテゴリ列も `__count` も 1 列も落ちない。加えて、仮に保護しなくても落ちるのは全行 0 の列だけなので和は変わらない（実測: `repair_修正__*` 93 列中 `nunique<=1` は `repair_修正__大分類__プレス` の 1 列のみで、統合カテゴリ列には定数列が無い）。二重に安全（§6.3） |
| X4 | **`trend__*` は保護しない。定数 62 列を落とす** | 剪定の最大の稼ぎ頭。ただし trend マッチ率 0（D7 `warn_empty`）のときは trend 列が全 NaN になり全滅するため、その挙動を §6.4 に明記する（現行の実データはマッチ率 78〜85% なので発生しない） |

---

## 1. 前提事実（2026-08-28 実測。再測定不要）

### 1.1 パネル現況

`data/interim/vin_panel.parquet` = 21,020 行 × 1,498 列。ソース別内訳（`reports/vin_panel_dictionary.csv`）:

| source | 列数 | source | 列数 |
|---|---|---|---|
| trend | 639 | 電着炉 | 25 |
| 電着 | 250 | ledger（vin 系・present__） | 15 |
| 浮遊ゴミ | 161 | シーラー炉 | 10 |
| 各工程滞在時間 | 123 | defect_上塗ブツ検 / defect_電着ブツ検 | 1 / 1 |
| ブース | 98 | 上塗/下塗/ホイ黒ロボット | 1 / 1 / 1（`n_rows` のみ） |
| repair_修正 | 93 | other | 1 |
| 前処理 | 48 | 中上炉 | 30 |

### 1.2 低カーディナリティ列の実測

| nunique | 列数 | 主な内訳 |
|---|---|---|
| 0（全 NaN） | 83 | 浮遊ゴミ 56・各工程滞在時間 27。**レイク側でも全 NaN**（元データが空）。うち 10 本は `_通過日時` 列。**フラグを含む列は 0 本** |
| 1（定数） | 111 | trend 62・電着 16・浮遊ゴミ 13・中上炉 8・ブース 4・電着炉 3・各工程滞在時間 2・シーラー炉 2・defect `__has` 2・repair 1 |
| 2 | 130 | 閾値判定フラグ 53・測定値 12・設定値 5・`present__*` 11・`has_repair_record`・`vin_format` 等 |

P1〜P3 の規約を当てはめた結果（実測）: **候補 194 列 → 保護 12 列 / 削除 182 列**。
保護される 12 列は `浮遊ゴミ__*_閾値判定フラグ` 9 列・`defect_上塗ブツ検__has` / `defect_電着ブツ検__has` 2 列・
`repair_修正__大分類__プレス` 1 列。

### 1.3 ロボット 3 ソースのレイク側スキーマと VIN 内一定率

レイクの Parquet スキーマ（`data/lake/traceability/{source}/date=*/`）から、
VIN 派生列（`vin_raw`/`vin`/`vin_base`/`vin_pass_no`/`vin_is_dummy`/`__source`/`date`）を除いた**業務列は
上塗 25・下塗 13・ホイ黒 16 の計 54 列**で、ユーザー計測の「未収載 54 列」と一致する。
`ロボット` 以外はすべて `int64` または `timestamp[us]`（＝ dtype では性質を判別できない。M2 の根拠）。

| ソース | 行数 | VIN 数 | 行/VIN | `ロボット` 種別 |
|---|---|---|---|---|
| 上塗ロボット | 143,939 | 4,817 | 29.9 | 30 |
| 下塗ロボット | 243,693 | 12,931 | 18.8 | 19 |
| ホイ黒ロボット | 35,156 | 17,584 | 2.0 | 2 |

VIN 内一定率（1.000 = VIN 内で必ず同じ値）:

- **VIN 内一定（≧ 0.98）**: キャリア・閾値判定フラグ・高電圧_設定値・塗装ロボットブラシ_段数・Line・派生・
  上塗通過回数・判定結果_3Bit（上塗）/ キャリア・閾値判定フラグ・温度使用量判定結果・Line（下塗）/
  ほぼ全列（ホイ黒）
- **VIN 内で変動（≦ 0.36）**: 台車 0.356・高電圧_RT値 0.751・車種 0.056・塗色 0.049・高電圧電流_RT値 0.049 と、
  塗料使用量／霧化エア系／パターンエア系／シェープエア系／サイクルタイム／カートリッジ充填量・排出量／
  材料温度_測定値／材料使用量_測定値／ドーザー電流_測定値／ドーザー材料圧力_測定値（すべて 0.003〜0.005）、
  入口_通過日時 0.001、ロボット 0.000
- **全域定数（全体 nunique=1）**: 上塗 `閾値判定フラグ`・`高電圧_設定値`・`塗装ロボットブラシ_段数` /
  下塗 `閾値判定フラグ`・`温度使用量判定結果` /
  ホイ黒 `閾値判定フラグ`・`塗料使用量`・`霧化エア_設定値`・`パターンエア_設定値`・`塗装ロボットブラシ_段数`・`判定結果_3Bit`

**重要な観察**: 「0.003〜0.005 の測定系」は末尾が
`測定値 / 設定値 / RT値 / 使用量 / 充填量 / 排出量 / サイクルタイム` のいずれかであり、
それ以外の列（キャリア・車種・塗色・派生・台車・Line・プログラム・塗装仕様・通過回数・段数・
判定結果_3Bit・温度使用量判定結果・閾値判定フラグ）は VIN 内一定または準一定。
→ **末尾一致規約だけで振り分けが成立する**（M2）。
例外は `台車`（0.356）と `車種`/`塗色`（0.05）だが、いずれもコード値であり平均を取る意味が無いので
代表値扱いで良い（代表値 = VIN 内最小のコード値）。

### 1.4 trend アンカーの現況

`reports/trend_anchor_map.csv` のトークンは コンベア / ブース / 作業場空調 / 前処理 / 塗料供給 /
塗料供給空調 / 浮遊ゴミ / 電着 の 8 種で、**上塗・下塗・ホイ黒は 1 つも含まれない**。
→ 複数行/VIN ソースの通過日時を復活させてもアンカー解決結果は変わらない（§5.4）。

---

## 2. スコープ

### やる
- `assemble.prepare_multi_row_source()` の仕様変更（集約の復活）
- `assemble` 末尾での列剪定と `reports/panel_pruned_columns.csv` 出力
- `config/config.yaml` への `real_ingest.multi_row` / `real_ingest.assemble.prune_low_cardinality` 追加
- 既存テストの更新方針の提示（§10）

### やらない
- `raw_convert`（レイク）の変更。レイクは一次情報として無傷のまま（責務境界: `real_data_ingest_design.md` §3）
- `analysis_data` / `eda` / `stats_tests` / `ml` の変更。剪定後も列名規約は不変なので下流は無改修で動く
- `defect` / `repair` / `trend` の集約仕様の変更（D11・R\*・D6 は据え置き）
- 特徴量選択・次元削減（p ≫ n の本丸。本書は「無情報列を捨てる」までで、選択は別タスク）

---

## 3. 列数予算（本書の合否判定）

**実測値（2026-08-30）に基づく実績**:

| 段階 | 列数 | 差分 |
|---|---|---|
| 変更前（剪定・復活なし） | 1,498 | — |
| 剪定前（複数行集約で復活させた直後） | 1,549 | **+51**（復活） |
| **最終（剪定後）** | **1,357** | **-192**（剪定） |
| **正味削減** | **−141 列（-9.4%）** | **1,498 → 1,357** |

削除 192 列の内訳: 既存ソース由来 182 列 / ロボット 3 ソース由来 10 列。
理由別: `all_nan` 83 列 / `constant` 109 列。
保護されて残った定数列: 15 列（`kept_protected`）。
パネル行数は 21,020 で変わらず。

復活列の詳細（剪定後の実測）:

| ソース | 計 | 代表値 | 統計量（mean） | 日時（`__min`） | n_rows |
|---|---|---|---|---|---|
| 上塗ロボット | 21 | 7 | 12 | 1 | 1 |
| 下塗ロボット | 12 | 5 | 5 | 1 | 1 |
| ホイ黒ロボット | 11 | 6 | 3 | 1 | 1 |
| **計** | **44** | **18** | **20** | **3** | **3** |

上記の「代表値」には各ソースの `閾値判定フラグ`（全域定数・保護により残る）も含まれる。
剪定で落ちたロボット由来の 10 列: 上塗 `高電圧_設定値__mean` / `塗装ロボットブラシ_段数` / `判定結果_3Bit` / `派生`（4 列）/ 下塗 `温度使用量判定結果`（1 列）/ ホイ黒 `塗料使用量__mean` / `霧化エア_設定値__mean` / `パターンエア_設定値__mean` / `塗装ロボットブラシ_段数` / `判定結果_3Bit`（6 列）。

メモリ: 21,020 × 1,357 × 4B（float32）≈ 114MB。1 年分（≈ 32 万 VIN）でも `assemble.date_from/date_to` 前提は変わらない。

---

## 4. 列剪定の仕様

### 4.1 判定ルール（正規定義）

列 `c` について:

```
n_unique(c) := panel[c].nunique(dropna=True)

全NaN列  : n_unique(c) == 0
定数列    : n_unique(c) == 1
削除対象  : (drop_all_nan かつ 全NaN列) または (drop_constant かつ 定数列)
            かつ not is_protected_column(c)
```

- `nunique(dropna=True)` を正とする（P2）。高速化のため数値/日時列で `count()==0` / `min()==max()` に
  置き換えても等価だが、`-0.0` と `0.0` の扱いなど微差があるため**まずは `nunique` で実装する**。
  1,498 列 × 21,020 行では実測不要な程度のコスト（数秒）と見積もる。遅ければ後で最適化する。
- 判定は**剪定直前の最終パネル**に対して行う（P4）。

### 4.2 保護規約（P3）

```python
def is_protected_column(col: str, prune_cfg: dict) -> bool:
    """剪定から保護する列かを列名だけで判定する（値を見ない）。"""
```

真になる条件（いずれか 1 つでも満たせば保護）:

1. `col in prune_cfg["protect_columns"]`（完全一致）
2. `col.startswith(tuple(prune_cfg["protect_prefixes"]))`（前方一致）
3. `any(s in col for s in prune_cfg["protect_name_substrings"])`（**部分一致**）

部分一致にする理由: ユーザー指示は「フラグとついた列はそのままにする」であり、
`浮遊ゴミ__PA_ON_閾値判定フラグ`（中間トークン）と `上塗ロボット__閾値判定フラグ`（末尾）の
両方を 1 つの規約で拾う必要がある。末尾一致では前者を取りこぼす。

既定値と、それぞれが守るもの:

| キー | 既定値 | 守る対象 |
|---|---|---|
| `protect_columns` | `[vin, vin_base, vin_pass_no, vin_format, has_repair_record]` | 識別子と ML 分類目的変数。狭い期間で `assemble` すると `vin_format`・`has_repair_record` が定数になり得る |
| `protect_prefixes` | `[present__, defect_, repair_]` | 存在フラグ 11 列、`defect_*__has`（現に nunique=1。落とすと defect 情報が全滅）、repair 結果列 93 列（回帰目的変数 `repair_修正__count` と統合カテゴリ列。IC6 を守る） |
| `protect_name_substrings` | `[フラグ]` | ユーザー明示指示。現行 9 列 + 復活 3 列 |

`trend__` は**意図的に保護しない**（X4）。

### 4.3 関数シグネチャ

`src/defect_analysis/assemble.py` に追加:

```python
PRUNE_REPORT_COLUMNS = [
    "column", "source", "dtype", "n_unique", "n_missing", "value", "reason", "action", "rule",
]

def _prune_config(cfg: Config) -> dict:
    """real_ingest.assemble.prune_low_cardinality を DEFAULT_PRUNE で補完して返す
       （既存 _defect_config / _repair_config と同じ流儀。部分上書きで他キーの既定が消えないこと）。"""

def is_protected_column(col: str, prune_cfg: dict) -> bool: ...

def prune_low_cardinality_columns(panel: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """低カーディナリティ列を落とし、(剪定後パネル, 剪定レポート) を返す。

    - `enabled: false` なら panel をそのまま返し、レポートは PRUNE_REPORT_COLUMNS のヘッダのみ
      0 行で返す（レポートのスキーマは常に一定。IC12 と同じ流儀）。
    - 列順は元の panel の順序を保つ（`panel.drop(columns=...)` を使う。再構築しない）。
    """
```

レポート行の意味:

| 列 | 内容 |
|---|---|
| `column` | 列名 |
| `source` | `_infer_column_source(col)`（既存関数を再利用） |
| `dtype` | 剪定前の dtype |
| `n_unique` | `nunique(dropna=True)` |
| `n_missing` | `isna().sum()` |
| `value` | 定数列の唯一値（全 NaN 列は空） |
| `reason` | `all_nan` / `constant` |
| `action` | `dropped` / `kept_protected` |
| `rule` | 保護に効いた規約（`protect_columns` / `protect_prefixes:present__` / `protect_name_substrings:フラグ`）。`dropped` のときは空 |

並び順: `action` 昇順 → `source` 昇順 → `column` 昇順（決定的）。

### 4.4 ログ

- INFO: `列剪定: 削除 %d 列（全NaN %d / 定数 %d）、保護により残した無情報列 %d 列。詳細: reports/panel_pruned_columns.csv`
- WARN: `action=kept_protected` が 1 件以上あるとき 1 行だけ
  `保護規約により、値が1種類しかない列を %d 列残しました（例: %s）。分析上は無情報です。`
  （X2 の可視化。列ごとに WARN を出すと 12 行以上出てうるさいので集約して 1 行）

### 4.5 実行場所の比較（P5 の根拠）

| | `assemble`（採用） | `analysis_data.load_real_panel` |
|---|---|---|
| ユーザーの確認手順との整合 | ○ parquet と `vin_panel_dictionary.csv` が剪定後になる | × 1,498 列のままの成果物を見ることになる |
| 原本の保全 | △ パネルからは消える。ただし**レイクは無傷**で、`enabled: false` + 再 assemble で完全復元できる | ○ パネルに残る |
| 方針変更時のコスト | △ 再 assemble（実データで数分オーダー） | ○ 再読込のみ |
| 下流への波及 | ○ eda/stats/ml すべてに一律で効く | △ `load_real_panel` を通らない経路（アドホック `pd.read_parquet`）には効かない |
| 保存容量・I/O | ○ 列が減る | × 変わらない |
| 列集合の安定性 | △ 期間を変えると剪定される列が変わる（§4.6） | 同左 |

決め手は「ユーザーの確認手順」と「アドホック読込にも効くこと」。原本保全は
`data/lake/` が一次情報であるという既存の責務境界（`real_data_ingest_design.md` §3）で担保されている。

### 4.6 D10（列集合の安定性）との関係

D10 は「列集合が入力データに依存して変わってはならない」と決めた。剪定は原理的にこれに抵触する
（狭い期間で assemble すると定数列が増え、より多く落ちる）。ただし D10 が防ぎたかったのは
**データ由来で列が無制限に生成される**こと（電着ブツ検の外れ値 1 件で 1,460 万列）であり、
剪定は逆方向で単調・有界（列は増えない・最大 194 列減るだけ）である。その上で:

- 目的変数・識別子・存在フラグ・結果列は保護規約で必ず残るので、**下流が名前で参照する列は消えない**
- どの実行で何が落ちたかは `panel_pruned_columns.csv` に必ず残る
- 期間を跨いで列集合を揃えたい比較タスクでは `enabled: false` にする

この 3 点を README にも 1 行書く。

---

## 5. 複数行/VIN ソース集約の仕様

### 5.1 集約計画（値を読まずに決める）

```python
MULTI_ROW_AGG_KINDS = ("stat", "rep", "datetime")

def plan_multi_row_aggregation(df: pd.DataFrame, source: str, cfg: Config) -> dict[str, list[str]]:
    """列名と dtype だけから集約計画 {"stat": [...], "rep": [...], "datetime": [...]} を返す。

    値（VIN 内一定率など）は一切見ない（M2）。したがって同じ CSV ヘッダなら
    どの期間で実行しても同じ列集合になる。
    """
```

振り分け（上から順に評価）:

1. `_DROP_ALWAYS`（`vin_raw`/`vin_base`/`vin_pass_no`/`vin_is_dummy`/`__source`/`date`）と `vin` → 除外
2. `col in multi_row_cfg["exclude_columns"]` → 除外（既定 `[ロボット]`）
3. `pd.api.types.is_datetime64_any_dtype(df[col])` → `datetime`
4. `col.endswith(tuple(stat_suffixes))` かつ `is_numeric_dtype` → `stat`
5. `col.endswith(tuple(stat_suffixes))` かつ非数値 → `rep` にフォールバックし WARN
   （`[traceability/%s] 列 %s は stat_suffixes に一致しますが数値ではないため代表値にします`）
6. それ以外 → `rep`

### 5.2 集約と列名（M3）

```python
def prepare_multi_row_source(df: pd.DataFrame, source: str, cfg: Config) -> pd.DataFrame:
    """複数行/VIN ソースを 1 行/VIN に畳む。結合キー `vin` を列として持つ DataFrame を返す。"""
```

| 種別 | 集約 | 出力列名 |
|---|---|---|
| stat | `numeric_aggs` の各関数（`groupby("vin").agg(...)`） | `{source}__{col}__{agg}` |
| rep | `min`（M4） | `{source}__{col}` |
| datetime | `datetime_aggs` の各関数（既定 `[min]`） | `{source}__{col}__{agg}` |
| 行数 | `size()` | `{source}__n_rows`（従来どおり必ず出す） |

- 列名生成は既存 `naming.prefixed(source, name)` を使う。
- 実装は「種別ごとに `groupby("vin").agg(...)` を 1 回ずつ（計 3 回）実行し、`vin` で横結合」。
  列ごとに groupby すると 25 回走って遅い。
- `std` は VIN の行数が 1 のとき NaN になる（正常。`ddof=1`）。0 埋めはしない。

### 5.3 列数ガード

`plan_multi_row_aggregation` の結果から**データを畳む前に**列数を計算する:

```
n_cols = len(stat) * len(numeric_aggs) + len(rep) + len(datetime) * len(datetime_aggs) + 1
```

`n_cols > assemble.max_columns_per_source`（既定 200）なら `ValueError` で中断。
既存の size_bin ガードと同じキー・同じ流儀（`real_data_ingest_design.md` §10）。
実測見込みは上塗 64 / 下塗 28 / ホイ黒 17 なので通常は発動しない（設定ミスの検知用）。

### 5.4 trend アンカーとしての適格性（D5 の該当部分の巻き戻し）

D5 は「複数行/VIN ソースは trend アンカーになり得ない」と決めたが、`{source}__{time_column}__min` が
復活することで**再びアンカー候補になる**。既存 `_discover_anchor_columns()` は
`prefixed(source.name, f"{source.time_column}__min")` を既にサポートしているため**コード変更は不要**。

実データへの影響は無い: trend トークンは 8 種（§1.4）で上塗/下塗/ホイ黒を含まず、
`fallback_anchor_source: ブース` も変わらない。→ `reports/trend_anchor_map.csv` と
`trend_join_report.csv` の内容は変化しない見込み（smoke テストの trend アサーションは影響を受けない）。

### 5.5 下流への波及（無改修で動くことの確認）

- `analysis_data.traceability_measure_columns()`: `defect_`/`repair_`/`trend__`/`present__` 以外で
  `__` を含む列を返す → 復活列はすべて条件を満たし、自動的に設備測定値として扱われる
- `analysis_data.equipment_measure_groups()`: 最初の `__` で分割 → `上塗ロボット` / `下塗ロボット` /
  `ホイ黒ロボット` が新しい設備グループとして出現する（現在は `n_rows` 1 列だけの貧弱なグループ）
- EDA の列数上限（`eda_max_measures_boxplot: 20` / `eda_cross_equipment_max_columns: 60` /
  `eda_histogram_max_per_equipment: 6`）が既にあるので、図の爆発は起きない
- ML: `resolve_predictors` は数値列を拾うので復活列が説明変数に入る（意図どおり）

---

## 6. `assemble()` への組み込み

### 6.1 呼び出し順序（X1）

```
既存: ソース別集約 → 台帳 → merge/0埋め → has_repair_record → ingest_quality
      → build_trend_wide → join_trend → _downcast_final_panel → to_parquet → _build_dictionary
変更後:
      ソース別集約（prepare_multi_row_source が cfg を受け取る）
      → 台帳 → merge/0埋め → has_repair_record → ingest_quality
      → build_trend_wide → join_trend
      → ★ prune_low_cardinality_columns(base, cfg)        # 新規・ここ 1 箇所だけ
      → ★ _write_report(reports_dir, "panel_pruned_columns.csv", prune_report)
      → _downcast_final_panel → to_parquet → _build_dictionary（剪定後のパネルから作る）
```

`_build_dictionary` は剪定後に呼ぶ（現在の呼び出し位置のままで良い）。
→ `vin_panel_dictionary.csv` は「実際にパネルにある列」、`panel_pruned_columns.csv` は
「落とした列」を表し、2 ファイルの和が剪定前の全列になる。

### 6.2 戻り値（P8）

```python
{"n_vin": int, "n_columns": int, "n_columns_pruned": int,
 "n_trend_columns": int, "trend_match_rate": float}
```

- `n_columns` は**剪定後**の列数
- `n_columns_pruned` は削除した列数（`kept_protected` は数えない）
- `n_trend_columns` は剪定後に数える（`base.columns` から数えるので現行コードのままで自動的にそうなる）

### 6.3 IC6 不変条件の検証（X3）

`repair_` は `protect_prefixes` に含まれるため、`repair_修正__統合カテゴリ__*` と
`repair_修正__count` は 1 列も落ちない → `Σ 統合カテゴリ列 == repair_修正__count` は**定義上維持される**。

保護が無かった場合の議論（念のため）: 落ちるのは `nunique<=1` の列であり、
0 埋め済みのカウント列がこの条件を満たすのは**全行 0 のときだけ**。全行 0 の列は和に寄与しないので
`Σ` は不変。テスト側も `panel.columns` から `cat_cols` を作るので列の消失で壊れない。
唯一壊れ得るのは `repair_修正__count` 自体が定数（全 VIN でリペア 0 件、または全 VIN 同数）になる
狭い期間の実行だが、これも保護で防がれている。

実測（`reports/vin_panel_dictionary.csv`）: `repair_修正__*` 93 列のうち `nunique<=1` は
`repair_修正__大分類__プレス`（全行 0）の 1 列のみ。統合カテゴリ列に定数列は無い。

### 6.4 trend マッチ率 0 のときの挙動（X4）

D7 `warn_empty` は「trend 列を全 NaN で生成する」と決めている。剪定を入れると、この全 NaN 列
（639 列）は**すべて削除される**（`trend__` は保護しない）。結果として `warn_empty` は
実質 `skip` と同じパネルを作る。これは意図的な設計判断として受け入れる:

- 「結合できていない」ことは `trend_join_report.csv`（マッチ率 0）と WARN ログ、
  および `panel_pruned_columns.csv`（reason=all_nan の trend 列 639 行）で十分に可視化される
- 下流（eda/ml）は列名をハードコードせず動的に列を選ぶため、列の消失で壊れない
- `analysis_data.drop_all_missing()` が学習前に同じことをしている（剪定後は no-op になるだけ）

現行の実データはマッチ率 78〜85% なので、この経路は実運用では発生しない。
`join_trend` 単体のテスト（`JoinTrendNonOverlappingFixtureTest`）は `assemble()` を通らないため影響を受けない。

---

## 7. config スキーマ

`config/config.yaml` の `real_ingest:` に追記する。既存キーは変更しない。

```yaml
real_ingest:
  # ...（既存）...

  # 複数行/VIN ソース（上塗/下塗/ホイ黒ロボット。自動判定・宣言不要）の集約。
  # docs/panel_prune_and_multirow_agg_design.md §5（M1〜M6。D5 の統計量全廃を上書き）
  multi_row:
    enabled: true
    # 末尾一致したら「ロボットごとに異なる測定値」とみなし統計量に畳む。
    # 一致しない列は VIN 内一定とみなし代表値 1 列にする（値からは判定しない。M2）
    stat_suffixes: [測定値, 設定値, RT値, 使用量, 充填量, 排出量, サイクルタイム]
    # 2026-08-30 ユーザー判断: min/std/max を外し mean のみ（上塗/下塗/ホイ黒 3 ソース共通）。
    # __min は非稼働ロボットの 0 を拾うだけで工程情報を持たない列が多く、列数を必要以上に
    # 増やさない方針とした（実測で上塗の __min 列12本中8本が定数落ち）。
    numeric_aggs: [mean]                  # stat 列に適用
    datetime_aggs: [min]                  # 日時列。__min は trend アンカーにも使われる（対象外・変更なし）
    exclude_columns: [ロボット]            # 行の識別子。代表値に意味が無く n_rows と情報が重複（M6）
    by_source: {}                         # ソース別上書き（M5）。3 ソースとも mean で揃ったため既定は無し

  assemble:
    date_from: null
    date_to: null
    require_sources: []
    max_columns_per_source: 200   # size_bin と複数行/VIN 集約の両方の列数ガード（§5.3）

    # 低カーディナリティ列の剪定。docs/panel_prune_and_multirow_agg_design.md §4（P1〜P8）
    prune_low_cardinality:
      enabled: true
      drop_all_nan: true          # nunique(dropna=True) == 0
      drop_constant: true         # nunique(dropna=True) == 1（2 値は落とさない）
      protect_columns: [vin, vin_base, vin_pass_no, vin_format, has_repair_record]
      protect_prefixes: [present__, defect_, repair_]
      protect_name_substrings: [フラグ]   # 部分一致。ユーザー指示「フラグとついた列はそのままにする」
      report_filename: panel_pruned_columns.csv
```

`assemble.py` 側の既定（yaml を読まない経路でも同じ挙動になるよう P6 に従って揃える）:

```python
DEFAULT_MULTI_ROW = {
    "enabled": True,
    "stat_suffixes": ["測定値", "設定値", "RT値", "使用量", "充填量", "排出量", "サイクルタイム"],
    "numeric_aggs": ["mean"],
    "datetime_aggs": ["min"],
    "exclude_columns": ["ロボット"],
    "by_source": {},
}
DEFAULT_PRUNE = {
    "enabled": True,
    "drop_all_nan": True,
    "drop_constant": True,
    "protect_columns": ["vin", "vin_base", "vin_pass_no", "vin_format", "has_repair_record"],
    "protect_prefixes": ["present__", "defect_", "repair_"],
    "protect_name_substrings": ["フラグ"],
    "report_filename": "panel_pruned_columns.csv",
}
```

`by_source` の解決は「既定 dict を `dict(DEFAULT_MULTI_ROW)` でコピー → `by_source[source]` の
キーだけ上書き」（部分上書きで他キーの既定が消えないこと。`_repair_category_map_config` と同じ流儀）。

---

## 8. `real_data_ingest_design.md` D5 の改定文（後続タスクで反映）

D5 は**破棄せず、以下の追記で上書きする**。pivot 廃止は維持し、統計量全廃だけを撤回する形にする
（D8 が「破棄」と明記されている前例に倣い、どこがいつ変わったかを追える形にする）。

> **2026-08-28 再改定（D5 の一部撤回）**: 同日中に決めた「統計量集約の全廃」は、
> レイクにある実測値 54 列がパネルから完全に失われるため**撤回する**。
> 代わりに [`panel_prune_and_multirow_agg_design.md`](panel_prune_and_multirow_agg_design.md) M1〜M6 の
> 規約集約（列名の末尾一致で統計量／代表値に振り分け、per-source で集約関数を指定、列数ガードあり）を採用する。
> **維持する部分**: 設備別 pivot は行わない（列数が設備台数に依存しないこと）。
> **撤回する部分**: 「数値列・日時列・文字列列の集約を一切行わない」「`{source}__n_rows` 1 列のみ」
> 「複数行/VIN ソースは trend アンカーになり得ない」。
> 全廃の理由だった「p ≫ n 対策」は、同設計書 P1〜P8 の列剪定（-182 列）が
> 復活分（+95 列）を上回ることで達成する（純減 -87 列）。

あわせて後続タスクで直す箇所: §8.1.2（集約仕様）、§8.4(2)（アンカー適格）、§8.5（列数試算・出力に
`panel_pruned_columns.csv` 追加）、§9（config）、§10（エラー時挙動に列数ガードと剪定を追記）、
§12.5（テスト観点）。

---

## 9. エラー時挙動

| 事象 | 挙動 |
|---|---|
| `multi_row.enabled: false` | 従来どおり `{source}__n_rows` 1 列のみ（D5 の 2026-08-28 版に戻る） |
| `stat_suffixes` に一致するが非数値の列 | WARN、代表値にフォールバック（§5.1-5） |
| 集約計画の列数 > `max_columns_per_source` | `ValueError` で中断（データを畳む前に検査） |
| `numeric_aggs` / `datetime_aggs` が空リスト | `ValueError`（設定ミス。`enabled: false` を使うべき） |
| `numeric_aggs` に pandas が知らない関数名 | pandas の例外をそのまま伝播（捕まえない） |
| `prune_low_cardinality.enabled: false` | 剪定せず、レポートはヘッダのみ 0 行で出力 |
| 剪定で全列が消える（`vin` だけ残る） | 起こり得ない（`vin` は保護、`present__*` も保護）。ただし削除率が 50% を超えたら WARN を出す |
| `panel_pruned_columns.csv` の書き込み失敗 | 既存 `_write_report` の挙動に従う（例外を握りつぶさない） |

---

## 10. テスト観点

### 10.1 既存テストへの影響（実測ベース）

`tests/` 全体を grep した結果、影響があるのは **`test_assemble.py` と `test_real_ingest_smoke.py` の 2 本だけ**。
他ファイルの `n_rows` / `ロボット` の出現は無関係（`AnnotationMeta.n_rows`、`normalize_name("ﾛﾎﾞｯﾄ#")` 等）。

| 対象 | 影響 | 更新方針 |
|---|---|---|
| `test_assemble.py::PrepareMultiRowSourceTest::test_three_rows_per_vin_produce_only_vin_and_n_rows_columns_with_value_three` | **落ちる**（列が増える）。`prepare_multi_row_source` のシグネチャに `cfg` が増える | 「`n_rows` が 3 であること」＋「測定値列が `__mean/__std/__min/__max` に畳まれること」を検証するテストに書き換え。テスト名も実態に合わせて変更 |
| `test_assemble.py::PrepareMultiRowSourceTest::test_no_aggregate_or_pivot_suffixed_columns_are_generated` | **仕様が変わるので廃止**（集約サフィックスは正常な出力になった） | 「`ロボット` 名を含む pivot 列（`上塗ロボット__R1__*` 等）が生成されないこと」＝ pivot 廃止が維持されていることを検証するテストに置換 |
| `test_assemble.py` の end-to-end `_cfg()` ヘルパ 5 箇所（`AssembleEndToEndDummyExclusionTest` 他、行 549 / 600 / 753 / 1105 / 1204 付近） | **落ちる可能性が高い**。小さな fixture ではほぼ全列が定数になり、剪定が既定 on だと検証対象の列が消える | 各 `_cfg()` に `"assemble": {"prune_low_cardinality": {"enabled": False}}` を追加する。これは「テストを通すための値の直書き」ではなく、**列生成の仕様**と**剪定の仕様**を別々に検証するための明示的な分離。剪定自体は 10.2 の専用テストで検証する |
| `test_real_ingest_smoke.py`（実データ 1 本） | `cat_cols >= 30` と `Σ == count` は `repair_` 保護により**不変**。`n_trend_columns > 0` は 639-62=577 で**不変**。`trend_match_rate > 0` も不変 | 落ちない見込み。10.3 のアサーションを追加する |

### 10.2 新規テスト（fixture ベース）

剪定:

1. `全NaN列と定数列が削除され2値列は残る`
2. `値が1種類とNaNだけの列は定数として削除される`（P2 の明文化）
3. `名前にフラグを含む定数列は削除されず kept_protected として記録される`（X2）
4. `present__ / defect_ / repair_ 接頭辞の定数列は削除されない`（P3）
5. `vin と has_repair_record は定数でも削除されない`
6. `enabled=false のとき列は1つも削除されずレポートはヘッダのみ0行になる`
7. `剪定後も列順が元のパネルの順序を保つ`
8. `剪定レポートに削除理由と保護規約名が記録される`
9. `assemble の戻り値 n_columns は剪定後の列数で n_columns_pruned が削除数と一致する`（P8）

複数行集約:

10. `末尾が設定値の数値列は統計量列になり末尾が一致しない列は代表値1列になる`（M2）
11. `代表値はVIN内の最小値でありレコードの並び順に依存しない`（M4。行順をシャッフルした 2 つの入力で同じ結果）
12. `日時列は __min に畳まれ trend アンカーとして解決される`（M3・§5.4）
13. `exclude_columns に指定した列は集約されない`（M6）
14. `by_source の numeric_aggs 上書きが他ソースの既定を変えない`（M5・部分上書き）
15. `集約計画の列数が max_columns_per_source を超えるとデータを読む前に ValueError`（§5.3）
16. `stat_suffixes に一致する非数値列は WARN のうえ代表値になる`
17. `ロボット名を含む pivot 列は生成されない`（D5 の維持部分）
18. `enabled=false のとき出力は vin と n_rows の 2 列だけになる`

相互作用（end-to-end / fixture）:

19. `複数行ソースの全域定数列は集約後に剪定で削除される`（X1・順序の検証）
20. `複数行ソースの閾値判定フラグは全域定数でも保護されて残る`（X2）
21. `剪定後も Σ 統合カテゴリ列 == repair_修正__count が成り立つ`（X3）

### 10.3 smoke テスト（実データ）に追加するアサーション

```
- reports/panel_pruned_columns.csv が存在する
- assemble_result["n_columns_pruned"] > 0
- パネルに nunique(dropna=True) <= 1 の列が残っている場合、それはすべて保護規約に一致する
- defect_上塗ブツ検__has / defect_電着ブツ検__has がパネルに存在する（定数だが保護される）
- 上塗ロボット__塗料使用量__mean など、複数行ソース由来の統計量列が存在する
- 上塗ロボット__n_rows が存在する（従来列の維持）
- パネルの列数が剪定前（n_columns + n_columns_pruned）より小さい
```

「1,411 列ちょうど」のような**実測値の直書きはしない**（データ差し替えで壊れるうえ、
値をテストに固定する意味が無い）。列数は §3 の予算に照らして人が確認する。

---

## 11. coder タスク分解

| # | 対象ファイル | 変更内容 | 完了条件 |
|---|---|---|---|
| T1 | `src/defect_analysis/assemble.py` | `DEFAULT_MULTI_ROW` / `DEFAULT_PRUNE` と `_multi_row_config(cfg, source)` / `_prune_config(cfg)` を追加（既存 `_repair_config` と同じ部分上書き流儀） | `by_source` の一部キー上書きで他キーの既定が残ることを単体で確認できる |
| T2 | 同上 | `plan_multi_row_aggregation()` を新設し、`prepare_multi_row_source(df, source, cfg)` を §5.2 の仕様に置換。`assemble()` 内の呼び出しに `cfg` を渡す | §10.2 の 10〜18 が通る |
| T3 | 同上 | §5.3 の列数ガード（畳む前に `ValueError`） | 15 が通る |
| T4 | 同上 | `PRUNE_REPORT_COLUMNS` / `is_protected_column()` / `prune_low_cardinality_columns()` を新設 | §10.2 の 1〜8 が通る |
| T5 | 同上 | `assemble()` に §6.1 の順序で剪定を組み込み、`panel_pruned_columns.csv` を出力、戻り値に `n_columns_pruned` を追加 | 9・19〜21 が通る |
| T6 | `config/config.yaml` | `real_ingest.multi_row` と `real_ingest.assemble.prune_low_cardinality` を §7 のとおり追記（既存キーは触らない） | yaml パースが通り、既定値がコード側と一致する |
| T7 | `tests/test_assemble.py` | §10.1 の 3 箇所（2 テスト書き換え・5 ヘッダに `enabled: false` 追加）＋ §10.2 の新規テスト | 既存 242 テストが緑に戻る |
| T8 | `tests/test_real_ingest_smoke.py` | §10.3 のアサーション追加 | `-m slow` で緑 |
| T9 | `README.md` / `CHANGELOG.md` | 剪定の既定 on・レポート `panel_pruned_columns.csv`・複数行集約の復活・§4.6 の注意（期間を跨いで列集合を揃えたいときは `enabled: false`）を追記 | — |
| T10 | `docs/real_data_ingest_design.md` | §8 の D5 改定文を反映（§8.1.2 / §8.4(2) / §8.5 / §9 / §10 / §12.5 も同時に） | 本書と矛盾が無い |

T1〜T5 は 1 ファイルに閉じるので順に実施する。T6 は T5 の後（既定がコードにある状態で yaml を足す）。

---

## 12. 未決事項・リスク

1. **`std` を入れるかは実データでの有用性未検証**。上塗 30 台のばらつきが不良と相関するかは EDA で確認してから。
   相関が見えなければ `numeric_aggs: [mean, min, max]` に落とす（-13 列）。
2. **`車種` / `塗色` が VIN 内で変動する理由が未解明**（一定率 0.05）。「ロボット別の設定値」との推定は
   ユーザー計測に基づく仮説であり、裏付けは未取得。代表値（VIN 内最小のコード値）で扱うが、
   これらを説明変数として使う前に業務側確認が要る。
3. **剪定後の列集合が期間依存になる**（§4.6）。月次比較などで列を揃えたい場合の運用は未定。
   必要になったら「保存済みの列リストを config で固定する」オプションを追加検討する（今回は作らない）。
4. **`p ≫ n` は本書では解消しない**（1,498 → 約 1,411）。特徴量選択は別タスクのまま。
5. **§3 の列数は 2026-08-30 実装完了時の実測値**。パネル行数 21,020・剪定前 1,549・剪定後 1,357 列はこの運用での確定値。ただし期間を変えて `assemble` すると低カーディナリティ列の判定が変わり（§4.6）、剪定される列数が前後する点に注意（保護された列は必ず残る）。日次実行で列集合を揃えるには `--date-from/date-to` を固定するか `enabled: false` にすること。
