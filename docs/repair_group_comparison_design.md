# 「修正なし車 vs 特定カテゴリの修正あり車」対比グラフ 設計（2026-08-31）

`analysis.custom_charts` で「修正なしの車」と「特定の統合カテゴリの修正があった車」を
同じ図の中で比較できるようにするための**群分け列**の設計。既存設計
（`docs/category_csv_and_custom_charts_design.md` / `docs/repair_integrated_category_design.md` /
`docs/panel_prune_and_multirow_agg_design.md`）を
置き換えるものではなく、**差分だけ**を定義する。

解きたい問題: `repair_修正__top_統合カテゴリ` は修正が無い VIN で NaN になるため、
`box` の `x` に指定しても「修正なし」群が図に現れず対比にならない。

---

## 0. 結論（決定事項と根拠）

| # | 決定 | 根拠 |
|---|---|---|
| G1 | **群分けは「該当カテゴリのカウント列が 1 件以上」を第一級で扱う。`top_統合カテゴリ`（最頻）方式も併存させるが既定の推奨は前者** | `top_` は 1 台につき 1 カテゴリしか表せず、複数種類の修正がある車で情報が落ちる。実測で群サイズが増える（タレ 377→438 / 色ブツ 86→118 / マスキング不良 336→376）だけでなく、ライン層別で再現する所見が 2 件→6 件に増えた（§1）。カウント列（`repair_修正__統合カテゴリ__*`、43 列・0 埋め済み）は既にパネルにあるので新しい集計は不要 |
| G2 | **群分け列は `analysis_data.load_real_panel` で分析時に導出する。`assemble`（パネルに焼く）はしない** | (a) この列は完全に分析専用で、定義はユーザーが探索中に何度も書き換える。assemble 側だと 1 カテゴリ試すたびに数分の再 assemble が要る。(b) パネルは既に 21,020 行 × 1,357 列で p≫n。分析専用列で列数を増やす理由が無い。(c) 設定の置き場が `analysis.*` になり、読む場所と効く場所が一致する。`docs/panel_prune_and_multirow_agg_design.md` P5（剪定は assemble 側）と矛盾しない: P5 の決め手は「ユーザーが parquet と `vin_panel_dictionary.csv` を見て剪定結果を確認する」ことだったが、本機能の確認対象は `reports/eda/*.png` であり assemble の成果物ではない（§4.1 で比較）。**P5 の適用範囲は列剪定に限る** |
| G3 | **列名はコード側で `repair_group__{name}` に固定する。config に書かせるのは `name`（サフィックス）だけで、接頭辞をユーザーに書かせない** | `analysis.leakage_prefixes` に `repair` があり、`resolve_predictors` は `c.startswith(tuple(prefixes))` で除外するため、`repair_group__` で始まる限り**必ず**説明変数から外れる（§3.3 で実装を追跡）。ユーザーが自由に列名を決められると `タレ群` のような名前が付いて ML の説明変数に混入し、修正実績（結果側）でのリークが起きる。命名をコードが握るのが唯一の安全な形 |
| G4 | **群定義の DSL は `analysis.filters` の句をそのまま使う。1 群 = 「`label` + 既存の句（`column` + `eq`/`in`/`not_in`/`min`/`max`、または `query`）」** | 新しい書式を発明しない方針。ユーザーは既に `filters` と `custom_charts.filters` で同じ書式を書いている。実装も `_apply_clause` を再利用するので DSL の二重実装が起きない（`docs/category_csv_and_custom_charts_design.md` V3 と同じ判断） |
| G5 | **スペックは 2 形式のみ。`groups`（句のリスト）形式と `base_column`（既存カテゴリ列を流用）形式。併用は禁止（WARN + そのスペックだけスキップ）** | 要求は「カウント列 > 0 の 2 群」と「最頻カテゴリ + 修正なしの多群」の 2 つ。前者は `groups`、後者は `base_column: repair_修正__top_統合カテゴリ` + `na_label: 修正なし` で過不足なく表せる（`top_統合カテゴリ` が NaN なのは修正が 1 件も無い VIN だけ＝IC6 により修正がある行のカテゴリは必ず非 NaN）。42 カテゴリを `groups` に手書きさせるのは非現実的で、逆に `base_column` だけでは 2 群が作れない。併用を許すと評価順の意味論が増えるだけで得が無い |
| G6 | **複数の群に該当した行は「リスト順で最初に一致した群」に入れる（先勝ち）。重複件数は WARNING で必ず報告する** | 決定的で説明可能。複数カテゴリの修正がある車は G1 の前提そのものなので必ず発生する。黙って先勝ちにすると偏りが見えないため件数をログに出す。「重複行を除外する」オプションは追加しない（同じことは 2 群スペックを 1 カテゴリずつ作れば起きない。ユーザーの用途は 1 カテゴリ対比） |
| G7 | **どの群にも該当しない行は既定で NaN。`na_label` を書いたときだけ第 3 群としてラベル化する** | 「修正なし vs タレ」では、タレ以外の修正だけがある車はどちらの群でもない。NaN にすれば `_custom_box` / `_custom_histogram` の既存挙動（`dropna` / `astype(str).isin(levels)`）で自動的に図から外れる。一方「修正なし / タレ / その他の修正あり」の 3 群比較も 1 キー（`na_label`）で表現できる |
| G8 | **`groups` が 2 群ちょうどのスペックでは、文字列列に加えて `repair_group__{name}__bin`（float 0/1/NaN）を対で生成する。0 = 1 番目のラベル、1 = 2 番目のラベル** | 図には日本語ラベル（`修正なし` / `タレ`）が要るが、`stats`/`ml` の目的変数は 0/1 でなければならない（`run_stats` は `y == 0` / `y == 1` で群を割るため文字列列では 0 検定になる）。派生元は文字列列の `map` なので両者は定義上必ず整合する。これで **`stats_tests.py` を 1 行も変えずに**「修正なし vs タレ」の BH-FDR 補正付き検定が回る（§7） |
| G9 | **設定の誤り（`name` 空 / 形式の併用 / `label` 欠落・重複 / 参照列がパネルに無い / 既存列名との衝突）は WARNING を出してそのスペックだけスキップし、他は継続する。例外は投げない** | `custom_charts` の V5 と同じ方針。`load_real_panel` は eda/stats/ml の全経路が通るため、1 スペックの誤りで全ステージを止めるのは損失が大きい。ただし参照列が無い場合に「句をスキップして全行一致」にはしない（`filters_on_missing_column: warn` の意味論をここに持ち込むと、群が静かに全車になり誤読を生む）。**列そのものを作らない**ので、その列を使う図は既存の V5 検証で WARN スキップされる |
| G10 | **各スペックについて群ごとの行数を INFO ログに必ず出す**（`[repair_groups/タレ] 修正なし=20,582 / タレ=438 / 未割当=...`） | 群定義が意図どおりかを図を見る前に確認できる唯一の手段。G1 の「377 と 438 の違い」のような差はログに出ないと気付けない |
| G11 | **`build_repair_group_columns` は df に in-place で列を追加して同じ df を返す** | 21,020 行 × 1,357 列の全複製は約 230MB。`load_real_panel` が `read_parquet` 直後に呼ぶ唯一の呼び出し元であり、他所有者がいないので in-place で安全。docstring に破壊的である旨を明記する |
| G12 | **`stats_tests.py` / `ml.py` / `eda.py` の既存ロジックは変更しない。多重比較の補正機能も新規実装しない** | 要求は「修正なし車と対比するグラフを作れるようにする」こと。G8 の `__bin` 列により既存の BH-FDR 済み検定にそのまま乗るので、新しい検定コードは不要。多重比較は README と本書の注意書き（§8）で扱う |

---

## 1. 前提事実（2026-08-31 実測。再測定不要）

### 1.1 パネル

- `data/interim/vin_panel.parquet`: 21,020 行 × **1,357 列**。
- `repair_修正__top_統合カテゴリ`（large_string）: VIN ごとの最頻統合カテゴリ 42 種。**修正が無い VIN は NaN**。
- `repair_修正__統合カテゴリ__{値}`（float、43 列）: カテゴリ別の修正件数。**0 埋め済みで NaN が無い**。
- `has_repair_record`（int64）: 0/1。全 VIN で定義される。
- `ブース__Line`（float、値は 1 / 2）: ライン層別に使う列。
- 例に使う数値列: `ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値`（float）。
  ※ 「絶対湿度」を素で持つのは `フラッシュオフ_HAB1/HAB2_送気_絶対湿度_測定値` の 2 列で、
  他の空調系は `絶対湿度差`（差分）である。config 例では前者を使う。

### 1.2 群定義方式の比較（G1 の根拠）

| カテゴリ | `top_統合カテゴリ` による群サイズ | カウント列 > 0 による群サイズ |
|---|---|---|
| タレ | 377 | **438** |
| 色ブツ(黒/白) | 86 | **118** |
| マスキング不良 | 336 | **376** |

ライン層別で同方向に再現する所見は 2 件 → **6 件**に増えた。

### 1.3 交絡（層別が必須である根拠）

単純比較では効果量 0.8 級のヒットが出るが、中身が交絡である実例:

- マスキング不良の車は `present__ホイ黒ロボット` が **0.018**（全体基準 0.856）＝ 2 トーン車の別ルート。
- 総称カテゴリ「修正」の車は ブース `Line 2` が **85.1%**（全体基準 48.5%）＝ ライン設備の差。

一方、層別で再現した有効な所見の例:

- 「色ブツ(黒/白)」の修正があった車は `trend__浮遊ゴミ_中上塗ブース前_粒子数_小_測定値` の
  中央値が 1.36 → 2.36（+73%）で、**両ラインで再現**。

---

## 2. スコープ

### やる

- `analysis.repair_groups` の新設（群分け列の宣言）。
- `analysis_data.py` に群分け列の導出を追加し、`load_real_panel` に組み込む。
- `analysis_data._apply_clause` から bool マスク生成部分を `clause_mask` として切り出す（DSL の再利用）。
- `config/config.yaml` への既定（空リスト）と書式コメントの追加。
- README への使い方と多重比較の注意喚起の追加。
- テスト（新規 `tests/test_repair_groups.py` ＋ 既存 2 ファイルへの追加）。

### やらない

- `eda.py` / `stats_tests.py` / `ml.py` / `assemble.py` の変更（G12）。
- 群間差の検定・p 値の図への焼き込み（`custom_charts` は図のみ）。
- 多重比較補正の新規実装（既存 `stats` の BH-FDR に導線を張るだけ。§8）。
- 群分け列のパネル（parquet）への保存（G2）。
- 「修正なし」の定義の変更。`has_repair_record == 0` をそのまま使う（`defect_*__has` は未検査が NaN で 0/1 が揃わないため使わない。`docs/real_data_ingest_design.md` §13-7）。

---

## 3. 群分け列の仕様

### 3.1 config スキーマ（`analysis.repair_groups`）

リスト。既定 `[]`（空なら現行挙動と完全一致・列は 1 本も増えない）。1 要素 = 1 本の群分け列。

| キー | 必須 | 既定 | 意味 |
|---|---|---|---|
| `name` | ○ | — | 生成列名のサフィックス。実列名は `repair_group__{name}`（接頭辞はコードが付ける。G3） |
| `groups` | △ | — | 群の定義リスト。1 要素 = `label` + `analysis.filters` と同じ句。**リスト順で先勝ち**（G6） |
| `base_column` | △ | — | 既存のカテゴリ列をそのまま群として使う（`groups` と排他。G5） |
| `na_label` | | なし | どの群にも該当しない行 / `base_column` が欠損の行に付けるラベル。未指定なら NaN（G7） |

`groups[i]` は「`label`（必須・文字列）」＋「`analysis.filters` の 1 句」。
句の演算子は既存どおり `eq` / `in` / `not_in` / `min` / `max`（`min`+`max` 併用可）/ `query` 単独。

```yaml
analysis:
  repair_groups:
    # 形式A: 句で 2 群を定義（カウント列 > 0。推奨）
    - name: タレ
      groups:
        - {label: 修正なし, column: has_repair_record, eq: 0}
        - {label: タレ,     column: repair_修正__統合カテゴリ__タレ, min: 1}

    # 形式B: 最頻カテゴリ + 修正なし の多群
    - name: 最頻カテゴリ
      base_column: repair_修正__top_統合カテゴリ
      na_label: 修正なし
```

生成される列:

| スペック | 生成列 | dtype | 値 |
|---|---|---|---|
| 形式A（2 群） | `repair_group__タレ` | object | `修正なし` / `タレ` / NaN |
| 形式A（2 群） | `repair_group__タレ__bin` | float64 | `0.0`（修正なし） / `1.0`（タレ） / NaN |
| 形式A（3 群以上） | `repair_group__{name}` のみ | object | ラベル / NaN |
| 形式B | `repair_group__最頻カテゴリ` | object | 元の値 / `na_label` |

### 3.2 評価規則（決定的手順）

1. スペックを検証する（§3.4）。不合格ならそのスペックをスキップして次へ。
2. 初期値 `s`:
   - 形式A: `pd.Series(np.nan, index=df.index, dtype=object)`
   - 形式B: `df[base_column].astype(object)`（値は文字列化せずそのまま保持する）
3. 形式A のみ: `groups` をリスト順に走査し、各句のマスク `m = clause_mask(df, clause)` を得て
   **まだ未割当の行だけ**に `label` を書く: `s = s.mask(m & s.isna(), label)`（先勝ち。G6）。
4. `na_label` があれば `s = s.fillna(na_label)`。
5. `df[f"repair_group__{name}"] = s`。
6. 形式A かつ `len(groups) == 2` なら
   `df[f"repair_group__{name}__bin"] = s.map({labels[0]: 0.0, labels[1]: 1.0}).astype("float64")`（G8）。
   `na_label` を付けた第 3 群は `__bin` では NaN になる（2 群比較の母集団から自動的に外れる）。
7. 群ごとの件数を INFO ログに出す（G10）。2 つ以上の句に一致した行数が 1 以上なら WARNING。

**なぜ `s.mask(m & s.isna(), label)` か**: `where`/`loc` の代入順に依存せず、
「未割当の行にだけ書く」を式として明示できるため。ラベル文字列が numpy 側で切り詰められないよう
`dtype=object` で初期化する（`dtype="U"` 相当の推論を避ける）。

### 3.3 リーク安全性（G3 の検証）

`src/defect_analysis/analysis_data.py:314` の実装:

```python
def resolve_predictors(df: pd.DataFrame, cfg: Config) -> FeatureSpec:
    excl = excluded_columns(cfg, df.columns)
    prefixes = tuple(a.get("leakage_prefixes", []) or [])
    predictors = [
        c for c in df.columns
        if c not in excl and not (prefixes and c.startswith(prefixes))
    ]
```

- `config/config.yaml` の `leakage_prefixes` は `[defect, repair, has_repair]`
  （2026-09-01 実測で 0 列だった `severe`/`severity`/`top_defect`/`max_severity`/`time_to_repair`/
  `has_defect`/`has_severe` は削除済み）。接頭辞は `repair`（アンダースコア無し）なので
  `repair_group__*` と `repair_group__*__bin` は**両方とも前方一致で除外される**。追加の config 変更は不要。
- `traceability_measure_columns`（同ファイル 74 行）の `excluded_prefixes` にも `repair_` があるため、
  `repair_group__x`（`__` を含む）が設備 `repair_group` として `equipment_measure_groups` に
  混入することも無い。
- 固定図 01（`_fig_rate_by_category`）は `spec.categorical` を使うため、群分け列で図が増えることも無い。
- `__bin` 列を `analysis.targets.classification` に追加した場合は `excluded_columns` が
  targets を除外集合に入れるので、**接頭辞と目的変数の二重**で説明変数から外れる。

テストで固定する不変条件（§10-12）: `resolve_predictors(df, cfg).all` に `repair_group__` で始まる列が 1 つも無いこと。

### 3.4 検証・スキップ条件（G9）

以下のいずれかに該当したら `logger.warning("[repair_groups/%s] ...", name or index)` を出し、
**そのスペックの列を 1 本も作らずに**次のスペックへ進む。

1. 要素が dict でない / `name` が空・非文字列。
2. `groups` と `base_column` の**両方**が無い、または**両方**ある（G5）。
3. `groups` の要素が dict でない / `label` が無い / `label` が重複している。
4. `groups` の句に演算子が 1 つも無い（既存 `_apply_clause` と同じ検査）。
5. 句が参照する `column`、または `base_column` が `df.columns` に無い（G9。**全行一致にはしない**）。
6. `repair_group__{name}`（または `__bin`）が既に `df.columns` にある（パネル列の上書き防止）。
7. `name` の重複（同じ列名を作る 2 つ目のスペック）。

以下は WARNING を出すが**列は作る**:

- どれか 1 つの群の件数が 0（`群 '{label}' に該当する行がありません`）。
- 2 つ以上の句に一致した行がある（`N 行が複数の群に該当したため、先に宣言された群に割り当てました`）。

例外（`query` 式の誤りなど）はスペック単位で捕捉し、`exc_info=True` の WARNING にしてスキップする
（`_render_custom_charts` の V6 と同じ方針）。**`load_real_panel` を例外で止めない**。

### 3.5 関数シグネチャ

`src/defect_analysis/analysis_data.py` に追加:

```python
REPAIR_GROUP_PREFIX = "repair_group__"   # analysis.leakage_prefixes の "repair" に必ず乗せる（G3）
REPAIR_GROUP_BINARY_SUFFIX = "__bin"

def clause_mask(df: pd.DataFrame, clause: dict, *, on_missing: str = "warn") -> pd.Series:
    """1 句を評価し、行を残すかの bool Series（index は df と同一）を返す。

    既存 _apply_clause から抽出した公開関数。_apply_clause は
    `df[clause_mask(df, clause, on_missing=on_missing)]` + DEBUG ログの薄いラッパにする
    （挙動・例外・ログ内容は現状維持。tests/test_filters.py は無変更で通ること）。
    """

def build_repair_group_columns(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """analysis.repair_groups の宣言に従い群分け列を df に追加して返す。

    df を **破壊的に更新**する（列の追加のみ。既存列は変更しない）。呼び出し元は
    load_real_panel（read_parquet 直後）のみを想定。パネル全体の複製を避けるため（G11）。
    宣言が空なら df をそのまま返す。設定誤りは WARNING + そのスペックのみスキップ（G9）。
    """
```

`_apply_clause` の `query` 分岐はマスク版では `df.index.isin(df.query(expr, engine="python").index)` で表現する。
存在しない列の分岐（`on_missing`）はマスク版では「全 True」を返し、既存の warn/error 挙動をそのまま踏襲する。

---

## 4. `load_real_panel` への組み込み

```python
def load_real_panel(cfg: Config) -> pd.DataFrame:
    panel_path = cfg.path("real_ingest.panel_path", default="data/interim/vin_panel.parquet")
    df = pd.read_parquet(panel_path)
    df = build_repair_group_columns(df, cfg)   # ★追加（apply_filters より前）
    df = apply_filters(df, cfg)
    return df
```

**`apply_filters` の前**に置く理由: 群分け列は行ごとに独立に決まるので、フィルタの前後で値は変わらない。
一方、前に置けば `analysis.filters` でも群分け列を参照できる（例: 分析母集団そのものを
「修正なし + タレあり」に絞る）。後に置くとその自由度だけが失われる。

副次的な効果:

- `eda` / `stats` / `ml` は全て `load_real_panel` を通るので、3 系統すべてで同じ群分け列が使える。
- `build_annotation_meta` は `load_real_panel` の後に呼ばれるため、脚注の件数・フィルタ表示は
  従来どおり整合する（`AnnotationMeta` は変更しない）。

---

## 5. 列剪定（`prune_low_cardinality`）との相互作用

| 論点 | 結論 |
|---|---|
| 群分け列自体が剪定されるか | **されない。** 剪定は `assemble` の最後にパネルに対して 1 回走る（P4）が、群分け列は `load_real_panel` で作られるので判定対象に一度も入らない。「片方の群が空 → 定数列 → 剪定される」問題は G2 の選択によって原理的に消える |
| 群定義の入力列が剪定で消えないか | **消えない。** `repair_修正__統合カテゴリ__*` と `repair_修正__count` は `protect_prefixes: [repair_]`、`has_repair_record` は `protect_columns` で保護済み（P3 / X3 と同じ根拠）。実測でも統合カテゴリ列に定数落ちは無い |
| assemble 側に作っていたら何が起きたか | 2 群スペックで片群が空のとき、`repair_group__*` は `repair_` 保護に当たって **`kept_partial`＝無情報の定数列として毎回パネルに残り**、`panel_pruned_columns.csv` に `action=kept_protected` として蓄積したはずである。保護規約を緩めれば今度は列が黙って消えて図が壊れる。どちらも避けられるのが G2 の実利 |
| 層別に使う `ブース__Line` は安全か | 全期間では 2 値（1/2）なので P1（2 値は落とさない）により残る。ただし `real_ingest.assemble.date_from/date_to` で片ライン分だけを assemble すると定数化して**剪定で消える**。その状態で `{column: ブース__Line, eq: 1}` を書くと `filters_on_missing_column: warn`（既定）により**句が黙ってスキップされ、層別が効いていない図ができる**。層別図を作る運用では `filters_on_missing_column: error` を検討する（§8 の注意書きに入れる） |

---

## 6. `custom_charts` からの使い方（config 実例）

`analysis.custom_charts` 側は**一切変更しない**。生成された `repair_group__*` は
ただの列なので、`box` / `histogram` の `x` / `hue`、`bar` の `x` にそのまま指定できる。

### 6.1 修正なし vs タレ（ブース絶対湿度）— 要求そのもの

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

- 図単位 `filters` の `in` 句は**必須ではない**（`_custom_box` は `_cap_categories` で
  水準を `dropna()` から作るため、どの群にも該当しない NaN 行は自動的に箱から外れる）。
  ただし脚注の台数（`n_rows`）はフィルタ後の行数なので、**`in` 句を書かないと脚注に
  「21,020 台」と出て図の実データ（修正なし + タレ）と食い違う**。書くことを推奨する。
- 箱の並び順は頻度降順なので `修正なし`（多数）が左、`タレ` が右になる。

### 6.2 ライン層別（交絡対策。§1.3）

方法 1: 図を 2 枚に分け、図単位 `filters` で層を切る（脚注に層が明記されるので推奨）。

```yaml
    - type: box
      x: repair_group__タレ
      y: ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値
      filters:
        - {column: repair_group__タレ, in: [修正なし, タレ]}
        - {column: ブース__Line, eq: 1}
      title: "[Line1] 修正なし vs タレ修正 ｜ ブース 絶対湿度"
      output: repair_group_タレ_絶対湿度_line1.png

    - type: box
      x: repair_group__タレ
      y: ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値
      filters:
        - {column: repair_group__タレ, in: [修正なし, タレ]}
        - {column: ブース__Line, eq: 2}
      title: "[Line2] 修正なし vs タレ修正 ｜ ブース 絶対湿度"
      output: repair_group_タレ_絶対湿度_line2.png
```

方法 2: 1 枚で並べる（`x` に層、`hue` に群）。`_custom_box` の x×hue 分岐がそのまま効く。

```yaml
    - type: box
      x: ブース__Line
      hue: repair_group__タレ
      y: ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値
      filters:
        - {column: repair_group__タレ, in: [修正なし, タレ]}
      title: ライン別 修正なし vs タレ修正 ｜ ブース 絶対湿度
      output: repair_group_タレ_絶対湿度_byline.png
```

注意: `ブース__Line` は float なので x のラベルは `1.0` / `2.0` と表示される
（`_cap_categories` が `astype(str)` するため）。読みやすさを優先するなら方法 1 を使う。

### 6.3 最頻カテゴリ + 修正なし（多群）

```yaml
  repair_groups:
    - name: 最頻カテゴリ
      base_column: repair_修正__top_統合カテゴリ
      na_label: 修正なし

  custom_charts:
    - type: box
      x: repair_group__最頻カテゴリ
      y: ブース__フラッシュオフ_HAB1_送気_絶対湿度_測定値
      title: 最頻修正カテゴリ別 ブース 絶対湿度（修正なしを含む）
      output: repair_group_最頻カテゴリ_絶対湿度.png
```

水準は 43 種になるが、既存の `analysis.eda_max_categories`（既定 15）で頻度上位に絞られ WARN が出る
（V8 の挙動そのまま）。**この図はスクリーニング用**であり、気になったカテゴリは 6.1 の 2 群図で見直す。

### 6.4 交絡チェック図（コード追加なしで書ける。§1.3 の再現）

```yaml
    # 2 群で「2 トーン車ルート」の比率が揃っているか
    - {type: bar, x: repair_group__タレ, y: present__ホイ黒ロボット, agg: mean,
       filters: [{column: repair_group__タレ, in: [修正なし, タレ]}],
       title: 群別 ホイ黒ロボット通過率（交絡チェック）, output: repair_group_タレ_confound_ホイ黒.png}
    # 2 群で ライン構成比が揃っているか
    - {type: bar, x: repair_group__タレ, y: ブース__Line, agg: mean,
       filters: [{column: repair_group__タレ, in: [修正なし, タレ]}],
       title: 群別 平均 Line 番号（1.5 から離れるほど偏り）, output: repair_group_タレ_confound_line.png}
```

### 6.5 ヒストグラムで分布形を見る

```yaml
    - {type: histogram, x: trend__浮遊ゴミ_中上塗ブース前_粒子数_小_測定値, bins: 40, density: true,
       hue: repair_group__色ブツ,
       filters: [{column: repair_group__色ブツ, in: [修正なし, 色ブツ]}]}
```

`hue` 併用時は群サイズ差（20,582 対 118）が形を潰すので **`density: true` が実質必須**。

---

## 7. 統計検定との連携（論点のスコープ判断）

**結論: `stats_tests.py` は変更しない。G8 の `__bin` 列を `analysis.targets.classification` に
足せば既存の枠組みでそのまま群間差検定が回る。それ以上はスコープ外。**

`run_stats`（`src/defect_analysis/stats_tests.py:147`）は
`targets.classification` の各列 `y` に対して `_test_numeric_vs_binary` / `_test_categorical_vs_binary`
を全説明変数に適用し、目的変数ごとに BH-FDR で補正する。実装上の要点:

- `g0 = x[y == 0]` / `g1 = x[y == 1]` で群を割る。→ **文字列ラベル列は使えない**（比較が全 False で 0 検定）。
- `y` が NaN の行は `y == 0` にも `y == 1` にも入らない。→ **`__bin` の NaN（どちらの群でもない車）は
  自動的に検定母集団から外れる**。追加の除外処理は不要。

したがってユーザーの手順は:

```yaml
analysis:
  targets:
    classification: [has_repair_record, repair_group__タレ__bin]   # ← 足すだけ
    regression: [repair_修正__count]
```

`uv run python main.py stats` → `reports/stats/statistical_tests.csv` に
`target=repair_group__タレ__bin` の行が出る（Welch t / Mann-Whitney、効果量 Cohen's d / 順位二列相関、
`p_adjusted` は BH-FDR 補正済み）。効果量の符号は「1 番目のラベルを基準に 2 番目のラベルが大きいと正」。

注意点（README に明記する）:

- `analysis.targets` は `ml` も読む。`stats` だけ回したくても `main.py ml` を実行すると
  同じ目的変数でモデルが学習される（実行時間が増える）。
- 説明変数は `resolve_predictors` により `repair_*` が全て除外されるので、
  `__bin` を目的変数にしても修正実績によるリークは起きない（§3.3）。

---

## 8. 多重比較の扱い（機能実装はしない。文書で扱う）

`custom_charts` は p 値を出さないため、補正機能を図側に実装しても表示先が無い。
`stats` 側は既に BH-FDR を実装済み（`_bh_fdr`）。よって **新規実装はしない**（G12）。
代わりに README の新節と本書に次の注意書きを置く:

> 群分け列を使うと、1,300 列の中から「群間で差のある列」を目で探すことになる。
> 図を大量に作れば、偶然だけで効果量の大きい「当たり」が必ず出る。1 枚の図から結論を出さないこと。
>
> 最低限の確認手順:
> 1. **ライン層別で再現するか**（§6.2）。片方のラインでしか出ない差は設備差か偶然である可能性が高い。
> 2. **群サイズ n が十分か**。脚注の台数と箱の幅で確認する（目安として両群 30 台以上）。
> 3. **交絡していないか**（§6.4）。`present__*`（工程ルート）と `ブース__Line`（ライン）の
>    分布が両群で揃っているかを確認する。実測では「マスキング不良」群の `present__ホイ黒ロボット`
>    が 0.018（基準 0.856）、総称カテゴリ「修正」群の Line 2 比率が 85.1%（基準 48.5%）であり、
>    これらは工程ルートの差であって工程条件の差ではない。
> 4. **数値で確かめる**: 有望な仮説は `repair_group__{name}__bin` を
>    `analysis.targets.classification` に追加して `stats` を回し、
>    `reports/stats/statistical_tests.csv` の `p_adjusted`（BH-FDR 補正後）を見る（§7）。
>
> なお、層別に使う列（`ブース__Line` 等）が期間の切り方で剪定されて消えていると、
> `filters_on_missing_column: warn`（既定）では句が黙ってスキップされ、層別が効いていない図ができる。
> 層別図を作る運用では `filters_on_missing_column: error` を検討する（§5）。

---

## 9. 既存ドキュメントとの関係（既存ファイルは編集しない）

| 箇所 | 状態 | 修正案 |
|---|---|---|
| `docs/panel_prune_and_multirow_agg_design.md` P5 / §4.5「剪定は assemble 側」 | **矛盾しない**。P5 は列剪定に固有の判断（ユーザーが parquet と `vin_panel_dictionary.csv` で剪定結果を確認する）であり、分析専用の派生列には適用されない | 既存文は変更不要。読者の誤読を避けるため、本書 G2 に適用範囲を明記した（この行がその記録） |
| `docs/category_csv_and_custom_charts_design.md` V9「カスタム図は `resolve_predictors` のリーク制限を受けない」 | **矛盾しない**。図は任意の列を使ってよい。群分け列がリーク規約に乗る必要があるのは ML 経路のため（G3） | 変更不要 |
| `docs/repair_integrated_category_design.md` §4.2「`{P}__top_統合カテゴリ` は 0 埋めしない（NaN のまま）」 | **本設計の前提そのもの**。形式B（`base_column` + `na_label`）はこの NaN が「修正なし」と 1 対 1 対応することに依存する | 変更不要。ただし将来 `top_` を 0 埋め/ラベル埋めする変更を入れる場合は形式B が壊れるため、本書を参照する旨を将来の設計者向けに残す |
| `README.md` §「設定（config.yaml の主なキー）」 | `analysis.repair_groups` の記載が無い（新規キーのため） | T5 で 1 行追加 + 使い方の節を追加 |
| `config/config.yaml` `analysis` セクション | `repair_groups` が無い | T3 で `repair_groups: []` と書式コメントを追加 |

---

## 10. テスト観点

新規 `tests/test_repair_groups.py`（標準 unittest。fixture は小さな DataFrame を直接構築する）。
`Config` は `tests/test_filters.py` と同じ流儀（`Config({...}, root=Path("/tmp"))`）で組む。

1. `assigns_no_repair_and_category_labels_from_count_column`
   — `has_repair_record` と `repair_修正__統合カテゴリ__タレ` を持つ df で `修正なし` / `タレ` が正しく付く。
2. `assigns_nan_to_cars_repaired_only_in_other_categories`
   — 修正はあるがタレのカウントが 0 の行が NaN であること（2 群のどちらにも入らない）。
3. `first_matching_group_wins_when_row_matches_multiple_clauses`
   — 2 カテゴリの修正がある行が、先に宣言した群に入ること（先勝ち。G6）。
4. `labels_unmatched_rows_when_na_label_is_declared`
   — `na_label: その他修正` を付けると 2 の行が第 3 群になること。
5. `base_column_form_fills_missing_category_with_na_label`
   — `base_column: repair_修正__top_統合カテゴリ` + `na_label: 修正なし` で、NaN 行だけがラベル化され
   既存の値は書き換わらないこと。
6. `emits_binary_column_mapping_first_label_to_zero_and_second_to_one`
   — `repair_group__タレ__bin` が 0.0/1.0/NaN になり、文字列列と行ごとに整合すること。
7. `does_not_emit_binary_column_for_three_group_spec`
   — 3 群スペックでは `__bin` を作らないこと。
8. `skips_spec_without_creating_columns_when_referenced_column_is_missing`
   — 参照列が無いスペックは列を作らない（＝全行が同じ群になったりしない）。df の列数が増えないこと。
9. `skips_spec_when_groups_and_base_column_are_both_declared`
10. `skips_spec_when_name_collides_with_existing_column`
    — 既存パネル列（例: `has_repair_record`）を上書きしないこと。
11. `returns_dataframe_unchanged_when_repair_groups_is_absent`
    — 宣言が無い場合に列数が 1 列も増えないこと。
12. `resolve_predictors_excludes_generated_group_columns`（`tests/test_predictors.py` に追加）
    — `config/config.yaml` と同等の `leakage_prefixes` で、`repair_group__タレ` と
    `repair_group__タレ__bin` が説明変数（numeric/categorical 双方）に含まれないこと。**リーク回帰の要**。
13. `traceability_measure_columns_excludes_group_columns`（`tests/test_equipment_groups.py` に追加）
    — 群分け列が設備扱いされないこと。
14. `box_with_group_column_draws_one_box_per_declared_group`（`tests/test_custom_charts.py` に追加）
    — `_custom_box` を直接呼び、`ax.get_xticklabels()` が宣言した 2 群のラベルだけになること
    （NaN 行が箱にならないことの検証）。
15. `clause_mask_returns_boolean_series_aligned_with_index`（`tests/test_filters.py` に追加）
    — 切り出した公開関数が index 整合の bool Series を返すこと。既存の `apply_filters` テストは**無変更で通ること**。

実データ smoke（`tests/test_real_ingest_smoke.py`）には**追加しない**。群分けは `assemble` 経路に
一切関与せず、実データ実行を伴うテストを増やす利得が無い。

---

## 11. coder タスク分解

### T1: `clause_mask` の切り出し（`src/defect_analysis/analysis_data.py`）

- `_apply_clause` の本体から bool マスク生成部分を `clause_mask(df, clause, *, on_missing="warn")` として公開関数に抽出。
- `_apply_clause` は `clause_mask` + 既存 DEBUG ログの薄いラッパにする。
- **完了条件**: `tests/test_filters.py` が無変更で全て通る。`query` 句・存在しない列（warn/error）・
  `min`+`max` 併用の挙動が現状と一致する。

### T2: 群分け列の生成と組み込み（`src/defect_analysis/analysis_data.py`）

- `REPAIR_GROUP_PREFIX` / `REPAIR_GROUP_BINARY_SUFFIX` 定数と `build_repair_group_columns` を追加（§3.5）。
- 評価規則は §3.2、検証・ログは §3.4 / G10 に従う。in-place 更新（G11）。
- `load_real_panel` に 1 行追加（§4。`apply_filters` の**前**）。
- **完了条件**: §10 の 1〜11 が通る。`analysis.repair_groups` 未設定時に `load_real_panel` の
  戻り値の列数が現行と一致する。

### T3: `config/config.yaml` へのスキーマ追加

- `analysis` セクション（`custom_charts` の直前）に `repair_groups: []` を追加し、
  §3.1 の 2 形式と §6.1 の実例をコメントで示す。
- コメントには「列名は `repair_group__{name}` になり、`leakage_prefixes` の `repair` により
  説明変数から自動除外される」ことと、「2 群スペックでは `__bin` 列も生成され
  `analysis.targets.classification` に足せば `stats` で検定できる」ことを 1 行ずつ書く。
- **完了条件**: 既定値が空リストで、現行の実行結果が変わらない。

### T4: テスト

- 新規 `tests/test_repair_groups.py`（§10 の 1〜11）。
- `tests/test_predictors.py` に 12、`tests/test_equipment_groups.py` に 13、
  `tests/test_custom_charts.py` に 14、`tests/test_filters.py` に 15 を追加。
- **完了条件**: 既存 271 passed を維持したうえで新規テストが通る。

### T5: README 更新

- `#### 追加グラフを config で指定する（analysis.custom_charts）` の直後に
  `#### 修正なし車と対比する（analysis.repair_groups）` を新設し、§6.1・§6.2 の config 例、
  `__bin` と `stats` の連携（§7）、§8 の注意書きを載せる。
- `## 設定（config.yaml の主なキー）` に `analysis.repair_groups` の 1 行を追加。
- **完了条件**: README の例をそのまま `config.yaml` に貼れば「修正なし vs タレ」の図が出る状態。

### 実装順序

T1 → T2 → T3 → T4 → T5。T1 と T2 は同一ファイルなので連続で行い、T1 完了時点で
`tests/test_filters.py` を単体で走らせて回帰が無いことを確認してから T2 に進む。

---

## 12. 非目標（今回やらないこと）

- 群分け列のパネル保存、`assemble` の変更、`vin_panel_dictionary.csv` への反映。
- 図への p 値・効果量の焼き込み、群間差検定の新実装。
- 多重比較補正の図側実装。
- カテゴリの自動探索（「全 43 カテゴリについて自動で 2 群図を作る」）。宣言的に書く方が
  何を見たかが config に残り、探索の記録として機能する。必要になったら別タスクで検討する。
- 交絡調整（層別以外の傾向スコア・回帰調整など）。
