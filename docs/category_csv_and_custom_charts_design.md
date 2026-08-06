# カテゴリ統合の CSV 化 ＆ config 宣言によるカスタム EDA グラフ 設計（2026-08-05）

対象の 2 機能を 1 本にまとめた設計書。既存設計（`docs/real_data_ingest_design.md` /
`docs/real_data_repair_design.md` / `docs/filter_and_annotation_design.md`）を置き換えるものではなく、
**差分だけ**を定義する。実データ経路（`convert` → `assemble` → `eda`/`stats`/`ml`）の枠組みは変えない。

- 機能1: 統合カテゴリの変換ルールを YAML → **CSV の 1 対 1 マッピング表**に全面移行（後方互換なし）
- 機能2: `config.yaml` から **追加の EDA グラフ**を宣言的に指定（既存の固定 7 系統は変更せず、追加出力）

---

## 0. 結論（決定事項と根拠）

### 機能1: カテゴリ統合の CSV 化

| # | 決定 | 根拠 |
|---|---|---|
| C1 | **マッピング CSV のヘッダは固定で `value,category` の 2 列**。UTF-8（BOM 可）、`#` 始まりの行はコメント | 列名を固定すると「どの列を写像するか」がファイル内容から独立し、同じ表を別の入力列（例 `大分類` と `中分類`）に再利用できる。ヘッダを `大分類,統合カテゴリ` のように可変にする案は、ヘッダ文字列が暗黙の仕様になりエラーメッセージも曖昧になるため採らない |
| C2 | **写像元の列は CLI `--source-column`（必須）で指定する** | YAML 廃止により `source_columns` の置き場が無くなる。CSV 側に持たせると C1 の再利用性が崩れる。`--help` に出て発見可能な CLI 引数が最も素直 |
| C3 | **生成する列名は CLI `--output-column`（既定 `統合カテゴリ`）**。`--output` は従来どおり出力ファイルパス | 既存の `--output`（ファイルパス）と衝突させない。既定値があるので通常運用は指定不要 |
| C4 | **マッピング表に無い値は「元の値をそのまま通す」＋ WARNING で未一致の値と件数を報告** | データを失わないことを最優先。静かに欠損化すると下流（EDA の集計・ML の学習）で行が消え、原因追跡が困難になる。運用上「マッピング表の育て漏れ」は必ず起きるので、WARNING に未一致値の実物と件数を出して追記作業に直結させる |
| C5 | **写像元が欠損（NaN）の行は NaN のまま**。未一致件数には数えず、INFO で別途件数を報告 | C4 の「元の値をそのまま通す」を機械的に適用すると文字列 `"nan"` が生まれてカテゴリが汚れる。欠損は欠損として保つのが情報として正しい |
| C6 | **`value` の重複行は `ValueError` で即停止**（後勝ち・先勝ちの暗黙採用をしない） | 矛盾したマッピング表は設定ミスであり、黙って一方を採ると「なぜこの分類になったか」が説明不能になる。CSV は人が編集するファイルなので入口で弾く |
| C7 | **照合は文字列一致（両辺 `str` 化 + 前後空白 strip）。大小文字・全半角の正規化はしない** | 実データのカテゴリは日本語で、余計な正規化はかえって意図しない一致を生む。Excel 由来の前後空白だけは事故が多いので落とす |
| C8 | **公開 API は `load_mapping` / `apply_category_mapping` / `run_category_integration` の 3 本。`load_spec` / `integrate_categories` / `_apply_default` は削除。YAML 読込（`import yaml`）も削除** | 責務: 読む / 写像する（副作用なし・純粋） / ファイル I/O とログを含む実行。後方互換は不要と合意済みなので旧 API は残さない |
| C9 | **`--map` の既定は `config/category_map.csv`。`config/category_map.yaml` は削除する** | 移行は完全置換。旧ファイルを残すと「どちらが正か」が曖昧になる |

### 機能2: config によるカスタム EDA グラフ

| # | 決定 | 根拠 |
|---|---|---|
| V1 | **新セクションは `analysis.custom_charts`（リスト。既定 `[]`）**。`run_eda()` の最後に `_render_custom_charts()` を呼び、既存 7 系統の図の**後ろに追加**する | 既存図を一切変更しないという合意。`analysis` 配下に置けば `filters` など既存の分析設定と一箇所にまとまる。既定が空リストなら現行挙動と完全一致 |
| V2 | **共通フィールドは `type` / `title` / `output` / `filters` / `hue`。型ごとの軸指定は「その型にとって自然な形」にする**（scatter/box/bar/histogram は `x`・`y`、heatmap は `columns` リスト） | x/y に無理に寄せると heatmap が破綻する。型ごとの必須フィールドを表で明示し、検証もそこに従う |
| V3 | **フィルタは `analysis_data` の句 DSL をそのまま再利用する。そのために `_apply_clause` を使う公開関数 `apply_filter_clauses(df, rules, on_missing)` を新設し、`apply_filters` はその薄いラッパにする** | DSL の実装を二重に持たない。`apply_filters` は「0 行になったら `ValueError`」という全体停止の意味論を持つが、グラフ単位では「WARN してその図だけスキップ」にしたいので、0 行判定は `apply_filters` 側に残して分離する |
| V4 | **グラフ単位フィルタは全体フィルタ（`analysis.filters`）の**上に**AND で追加適用する**（`run_eda` が読む df は既に全体フィルタ適用済み） | 全体フィルタは「分析対象母集団の定義」で全ステージに効く前提（`docs/filter_and_annotation_design.md`）。ここを個別図で無効化できると脚注の意味論が壊れる |
| V5 | **設定ミス（未知の `type`・必須欄欠落・列が存在しない・型不一致・フィルタ後 0 行）は WARNING を出してその図だけスキップし、処理は継続する** | 既存 eda の全図がこの方針（`_fig_*` は条件を満たさなければ WARN + `None` 返却）。EDA は探索用のレポート生成であり、1 枚の設定ミスで他の図を落とすのは損失が大きい |
| V6 | **1 図の描画は個別に `try/except Exception` で囲み、失敗時は `logger.warning(..., exc_info=True)` でスキップして次の図へ進む** | V5 の事前検証で全ての失敗は拾いきれない（matplotlib 側の想定外など）。ただしスタックトレースを WARN に載せて「握り潰し」にはしない |
| V7 | **hue の反映方法は型ごとに固定**（scatter=水準ごとの点群 / bar=横並びサブグループ / histogram=重ね書き（alpha=0.55）/ box=カテゴリ内の横並び / heatmap=**非対応、WARN して無視**） | heatmap は相関行列であり色軸が既に相関係数に使われている。エラーにせず無視 + WARN が V5 の方針と整合 |
| V8 | **hue の水準数は既定 8（`analysis.custom_chart_max_hue`）に制限し、頻度上位のみ描画して WARN**。bar/box の x カテゴリ数も既定 15（既存 `analysis.eda_max_categories` を流用）で同様に制限 | パレット `viz_style.CATEGORICAL` が 8 色固定（循環禁止）で 9 水準目から色が潰れる。既存 `_cap_group_columns` も「上限超過は上位に絞って WARN」で統一されている |
| V9 | **カスタム図は `resolve_predictors` によるリーク列制限を受けない。パネルの任意の列を指定できる** | ユーザーが明示的に指定した探索用の図であり、モデルを学習しないためリークの概念が適用されない（目的変数そのものを y に取りたいのが自然） |
| V10 | **出力先は `reports/eda/`。ファイル名は `output` 指定があればその basename、無ければ `custom_{通番:02d}_{type}.png`（通番はリスト内 1 始まりの位置）** | 日本語タイトルからのスラグ生成はファイル名が壊れやすい。リスト位置ベースなら決定的で衝突せず、config を見れば対応が分かる。`output` にディレクトリ区切りが含まれる場合は basename に丸めて WARN（出力先の逸脱を防ぐ） |
| V11 | **脚注は既存と同じ `AnnotationMeta.footnote()` を使い、グラフ単位フィルタ適用後の行数・フィルタ文言を反映した複製メタ（`dataclasses.replace`）で焼き込む** | 脚注の件数が図の実データと食い違うのは既存設計が明示的に避けている点（`build_annotation_meta` の docstring）。`AnnotationMeta` 自体は変更しない |
| V12 | **scatter は既定 20000 点で決定的サンプリング（`analysis.custom_chart_max_points`、seed は `project.random_seed`）** | パネルは VIN 単位で数万行になりうる。全点描画は PNG が肥大化し重なって読めない。サンプリングした場合は脚注のデータ種に「サンプリング n/N」を明記する |

---

## 1. 機能1: カテゴリ統合の CSV 化

### 1.1 マッピング CSV の仕様

`config/category_map.csv`（既定パス）:

```csv
# 統合カテゴリ変換表（value = 入力データ側の値 / category = 変換後の統合カテゴリ）
# どの列を写像するかは CLI の --source-column で指定する。
# 表に無い値はそのまま出力され、WARNING に未一致値と件数が出る。
value,category
締結,締結不良
溶接,機能系
塗装,外観系
メッキ,外観系
圧入,寸法系
切削,寸法系
```

- 列は `value` と `category` の 2 列。**この 2 列以外のヘッダがあれば `ValueError`**（列名のタイポを早期に検出する）。
  余分な列は許さない（将来 `note` 列が欲しくなったら `#` コメント行で足りる）。
- 読み込みは `pd.read_csv(path, dtype=str, encoding="utf-8-sig", comment="#", keep_default_na=False)`。
  - `dtype=str`: `01` のような値がゼロ落ちするのを防ぐ。
  - `keep_default_na=False`: `NA` `null` のような値を持つカテゴリを NaN 化しない。
- 空行は無視。`value` または `category` が空文字の行は `ValueError`。
- `value` の重複は `ValueError`（C6）。エラーメッセージに重複した値を列挙する。
- ファイルが無ければ `FileNotFoundError`（メッセージにパスを含める。既存 `load_spec` と同じ方針）。

上のサンプルは `data/sample/defect_categories.csv` の `中カテゴリ` 列（締結/溶接/塗装/メッキ/圧入/切削）を
全てカバーしており、README の実行例で WARNING が出ない状態になる。

### 1.2 新 API（`src/defect_analysis/category_integrate.py` を全面書き換え）

```python
DEFAULT_MAP_REL = Path("config") / "category_map.csv"
DEFAULT_OUTPUT_COLUMN = "統合カテゴリ"

def load_mapping(path: Path) -> dict[str, str]:
    """マッピング CSV を読み {value: category} を返す。検証エラーは ValueError。"""

def apply_category_mapping(
    values: pd.Series, mapping: dict[str, str]
) -> tuple[pd.Series, dict[str, int]]:
    """写像後の Series と、未一致だった値→件数の dict を返す（ログは出さない純粋関数）。"""

def run_category_integration(
    cfg: Config,
    input_path: str,
    output_path: str,
    *,
    source_column: str,
    output_column: str = DEFAULT_OUTPUT_COLUMN,
    map_path: str | None = None,
) -> dict:
    """入力 CSV に統合カテゴリ列を付与して出力 CSV に書き出す。"""
```

`apply_category_mapping` の挙動（純粋関数・ログなし）:

1. `keys = values.astype("string").str.strip()`（NaN は NaN のまま保持される）。
2. `mapped = keys.map(mapping)`。
3. 未一致（`mapped` が NaN かつ `keys` が非 NaN）の位置は `keys` の値をそのまま採用（C4）。
4. `keys` が NaN の位置は NaN のまま（C5）。
5. 戻り値の第 2 要素は「未一致だった値 → 件数」の dict（件数降順、値昇順で決定的に並べる）。

`run_category_integration` の挙動:

1. `map_path`（未指定なら `DEFAULT_MAP_REL`）をルート基準で解決 → `load_mapping`。
2. 入力 CSV を `pd.read_csv(in_p)` で読む（従来どおり）。
3. `source_column` が入力に無ければ `KeyError`（メッセージに実在列の一覧を含める）。
   ここは「設定ミスで全行が壊れる」ケースなので、EDA と違い**即エラー**にする。
4. `output_column` が既に存在する場合は WARN のうえ上書きする。
5. `apply_category_mapping` を呼び、結果を `df[output_column]` に代入。
6. ログ:
   - INFO: `統合カテゴリ生成: {in} -> {out} ({n} 行, {k} 種)`、`統合カテゴリ分布: {...}`（従来どおり）
   - INFO: 欠損行数（`写像元が欠損の行: {n} 行（NaN のまま出力）`）
   - **WARNING（未一致がある場合のみ）**: `マッピング表に無い値 {u} 種 / {m} 行を元の値のまま出力しました: {上位20件の 値=件数}`
     （20 件を超える場合は「他 N 種」で省略。ログが数千行になるのを防ぐ）
7. 出力 CSV を書き出し、戻り値 `{"n_rows", "output", "distribution", "n_unmatched", "unmatched_values"}`
   （`unmatched_values` は未一致値→件数の dict。テストと運用の両方で使う）。

### 1.3 CLI（`src/defect_analysis/cli.py`）

```
p_category.add_argument("--input", required=True, ...)
p_category.add_argument("--output", required=True, help="統合カテゴリ列を付与した出力CSV")
p_category.add_argument("--source-column", required=True, help="写像元の列名（例: 中カテゴリ）")
p_category.add_argument("--output-column", default="統合カテゴリ", help="生成する列名（既定: 統合カテゴリ）")
p_category.add_argument("--map", default=None, help="変換表CSV（既定: config/category_map.csv）")
```

`main()` の分岐は `run_category_integration(cfg, input_path=args.input, output_path=args.output,
source_column=args.source_column, output_column=args.output_column, map_path=args.map)` に変更。
モジュール冒頭 docstring の使用例も新引数に合わせて更新する。

---

## 2. 機能2: config 宣言によるカスタム EDA グラフ

### 2.1 スキーマ（`config.yaml` の `analysis.custom_charts`）

共通フィールド:

| フィールド | 必須 | 既定 | 意味 |
|---|---|---|---|
| `type` | ○ | — | `scatter` / `bar` / `histogram` / `box` / `heatmap` |
| `title` | | 自動生成（2.3） | 図のタイトル（`suptitle` ではなく `ax.set_title`） |
| `output` | | `custom_{通番:02d}_{type}.png` | 出力ファイル名（`reports/eda/` 配下の basename） |
| `filters` | | `[]` | この図だけに追加適用する句のリスト（DSL は `analysis.filters` と同一） |
| `hue` | | なし | 色分けに使う列（heatmap は非対応で無視） |

型ごとのフィールド:

| type | 必須 | 任意 | 補足 |
|---|---|---|---|
| `scatter` | `x`（数値）, `y`（数値） | `hue`, `alpha`(既定 0.5) | 点数が上限超過なら決定的サンプリング（V12） |
| `bar` | `x`（カテゴリ） | `y`（数値）, `agg`(`mean`/`sum`/`median`/`count`, 既定 `mean`), `hue` | `y` 省略時は `x` の件数（`agg` は無視して WARN） |
| `histogram` | `x`（数値） | `bins`(既定 30), `density`(既定 `false`), `hue` | hue 併用時は `density: true` 推奨（群サイズ差の影響を除く）。コメントで案内する |
| `box` | `y`（数値） | `x`（カテゴリ）, `hue` | `x` 省略時は `y` 単体の 1 本 |
| `heatmap` | `columns`（数値列 2 本以上のリスト） | `method`(`pearson`/`spearman`, 既定 `pearson`) | `hue` 指定は WARN して無視 |

`config/config.yaml` に追記する内容（既定は空リスト。コメントで書式を示す）:

```yaml
  # 追加の EDA グラフ（既存の固定図に「追加」で出力される。空なら従来どおり固定図のみ）。
  #   type: scatter | bar | histogram | box | heatmap
  #   共通: title / output（reports/eda 配下のファイル名）/ filters（analysis.filters と同じ句）/ hue（色分け列）
  #   scatter: x,y（数値）| bar: x（カテゴリ）,y（数値・省略で件数）,agg | histogram: x（数値）,bins,density
  #   box: y（数値）,x（カテゴリ・任意）| heatmap: columns（数値列リスト）,method
  #   設定ミス（列が無い・型違い・フィルタ後 0 行）はその図だけ WARN でスキップし処理は継続する。
  #   例:
  #     custom_charts:
  #       - {type: scatter, x: EQ-01__pressure, y: EQ-01__stroke, hue: production_shift,
  #          title: 圧入 圧力×ストローク, output: custom_pressure_stroke.png}
  #       - {type: bar, x: 車種, y: has_repair_record, agg: mean,
  #          filters: [{column: process_month, in: ["2026-07"]}]}
  #       - {type: histogram, x: lead_time_sec, bins: 40, density: true, hue: has_repair_record}
  #       - {type: box, x: production_shift, y: EQ-01__pressure}
  #       - {type: heatmap, columns: [EQ-01__pressure, EQ-01__stroke, lead_time_sec], method: spearman}
  custom_charts: []
  custom_chart_max_hue: 8          # hue 水準数の上限（超過分は頻度上位のみ描画し WARN）
  custom_chart_max_points: 20000   # scatter の最大描画点数（超過時は決定的サンプリング）
```

### 2.2 検証とスキップ条件（V5）

各図について以下を順に検証し、1 つでも該当したら `logger.warning` を出してその図をスキップする
（メッセージ先頭は必ず `[custom_charts/{通番} {type}]` で始め、どの設定が悪いか特定できるようにする）。

1. `type` が未知 / 未指定。
2. その型の必須フィールドが欠落。
3. 指定列（`x`/`y`/`hue`/`columns` の各要素）が `df.columns` に無い。
4. 数値が要求される列（scatter の x/y、histogram の x、box の y、bar の y、heatmap の columns）が
   `pd.api.types.is_numeric_dtype` を満たさない。
5. グラフ単位フィルタ適用後に 0 行。
6. 描画に使えるデータが全て欠損（対象列を `dropna` した結果が空）。
7. heatmap で `columns` の有効数値列が 2 本未満。

`bar` の `x`、`box` の `x`、`hue` は型を問わない（数値でもカテゴリとして扱う。`astype(str)` で
ラベル化する）。ただし `nunique` が上限（V8）を超える場合は頻度上位に絞り WARN する
（スキップはしない。上位カテゴリだけでも情報価値があるため）。

### 2.3 自動タイトル・自動ファイル名

| type | 自動タイトル |
|---|---|
| scatter | `{y} と {x} の関係`（hue あり: `（{hue} 別）` を付す） |
| bar | `y` あり: `{x} 別 {y}（{agg}）` / `y` なし: `{x} 別 件数` |
| histogram | `{x} の分布`（hue あり: `（{hue} 別）`） |
| box | `x` あり: `{x} 別 {y} の分布` / なし: `{y} の分布` |
| heatmap | `相関ヒートマップ（{n}列・{method}）` |

ファイル名は V10 のとおり `custom_{通番:02d}_{type}.png`。`output` 指定時は
`Path(output).name` を使い、拡張子が無ければ `.png` を付与、元の指定と変わったら WARN。

### 2.4 `analysis_data.py` の変更（フィルタ再利用・V3）

```python
def apply_filter_clauses(df: pd.DataFrame, rules: list[dict], *, on_missing: str = "warn") -> pd.DataFrame:
    """句リストを順に AND 適用した DataFrame を返す（0 行でも例外にしない）。"""

def filters_summary(df: pd.DataFrame, rules: list[dict]) -> str:
    """既存 _filters_summary の公開版（脚注・ログ共通）。"""
```

- `apply_filter_clauses` は既存 `apply_filters` のループ部分（`on_missing` 正規化 + `_apply_clause` の
  逐次適用 + 件数 INFO ログ）をそのまま切り出したもの。**0 行時の `ValueError` は含めない**。
- `apply_filters(df, cfg)` は「cfg から rules と on_missing を読む → `apply_filter_clauses` → 0 行なら
  `ValueError`」の薄いラッパにする。**既存の挙動・例外・ログ内容は変えない**（`tests/test_filters.py` は
  無変更で通ること）。
- `_filters_summary` は `filters_summary` にリネームし、内部呼び出しを差し替える（`_describe_clause` は private のまま）。

### 2.5 `eda.py` の実装方針

`run_eda()` の末尾（`07_defect_category_breakdown` の後）に以下を追加する:

```python
    figures += _render_custom_charts(df, cfg, out_dir, meta)
```

戻り値の `figures` に追加されるので `n_figures` は自動的に増える。ログは
`logger.info("カスタム図: %d/%d 枚を出力（スキップ %d）", ...)` を `_render_custom_charts` 内で出す。

構成:

```python
def _render_custom_charts(df, cfg, out_dir: Path, meta: AnnotationMeta) -> list[str]: ...
def _render_one_custom_chart(df, spec: dict, index: int, cfg, out_dir, meta) -> str | None: ...
def _custom_scatter(sub: pd.DataFrame, spec: dict, cfg) -> plt.Figure: ...
def _custom_bar(sub, spec, cfg) -> plt.Figure: ...
def _custom_histogram(sub, spec, cfg) -> plt.Figure: ...
def _custom_box(sub, spec, cfg) -> plt.Figure: ...
def _custom_heatmap(sub, spec, cfg) -> plt.Figure: ...
_CUSTOM_BUILDERS = {"scatter": _custom_scatter, "bar": _custom_bar, ...}
```

- `_render_custom_charts`: `cfg.get("analysis.custom_charts", []) or []` を取得。空なら即 `[]` を返す
  （ログ不要）。要素が dict でなければ WARN スキップ。1 件ずつ `_render_one_custom_chart` を
  `try/except Exception` で囲む（V6）。
- `_render_one_custom_chart`:
  1. 共通検証（2.2 の 1〜4）。
  2. `sub = apply_filter_clauses(df, spec.get("filters", []) or [], on_missing=cfg.get("analysis.filters_on_missing_column", "warn"))`。
     0 行なら WARN スキップ（2.2 の 5）。
  3. ビルダーを呼んで `plt.Figure` を得る。ビルダーは検証失敗時に `ValueError` を投げてよい（呼び出し側で WARN スキップ）。
  4. 脚注: `chart_meta = dataclasses.replace(meta, n_rows=len(sub), filters_summary=<全体 + 図単位>)` として
     `chart_meta.footnote(data_kind=f"カスタム図/{type}")`。図単位フィルタがある場合の `filters_summary` は
     `f"{meta.filters_summary} ＋ 図単位: {filters_summary(sub, chart_rules)}"`。scatter でサンプリングした場合は
     `data_kind` に `（サンプリング {n}/{N} 点）` を付す。
  5. `_save(fig, out_dir / filename, footnote)` で保存し、パスを返す。
- 描画スタイルは既存図と統一する:
  - 色は `vs.color_for(i)`（hue 水準の並び順は頻度降順で決定的に）。単色の場合は `vs.CATEGORICAL[0]`。
  - `zorder=3`、bar は `width=0.62`（グループ化時は水準数で割る）、直接ラベルは `fontsize=9, color=vs.INK_SECONDARY`。
  - hue ありは `ax.legend(fontsize=8)`、凡例タイトルに hue 列名。
  - `fig.tight_layout(rect=(0, 0.04, 1, 1))`（脚注用の下余白を確保。既存図と同じ）。
  - heatmap は既存 `_draw_corr_heatmap(sub, columns, title)` をそのまま再利用する（`method` を渡せるよう
    `method: str = "pearson"` のキーワード引数を追加。既定値は現行挙動と同一なので既存呼び出しは無変更）。
  - `figsize` は scatter/bar/histogram/box とも `(10, 6)` を基本とし、bar/box は x 水準数に応じて
    横幅を `min(18, 6 + 0.5 * n_levels)` で伸ばす。

### 2.6 hue の型別挙動（V7）

| type | hue の反映 |
|---|---|
| scatter | 水準ごとに `ax.scatter` を重ねる（色は `vs.color_for(i)`、`alpha` は spec の値） |
| bar | 水準ごとに棒を横並び（`x` 位置を `w = 0.8 / n_levels` でオフセット） |
| histogram | 水準ごとに `ax.hist(..., alpha=0.55, density=spec.density)` を重ね書き（既存 `_draw_histogram_grid` と同じ見た目） |
| box | `x` の各カテゴリ内に水準ごとの箱を横並び（`positions` を手計算、`patch_artist=True` で水準色を facecolor に） |
| heatmap | 非対応。`hue` が指定されていたら WARN して無視 |

---

## 3. coder への実装タスク分解

### T1: `category_integrate.py` の全面書き換え（機能1のコア）

- 対象: `src/defect_analysis/category_integrate.py`
- 変更内容:
  - `load_spec` / `integrate_categories` / `_apply_default` / `import yaml` を削除。
  - `DEFAULT_MAP_REL = Path("config") / "category_map.csv"`、`DEFAULT_OUTPUT_COLUMN = "統合カテゴリ"` を定義。
  - §1.2 のシグネチャで `load_mapping` / `apply_category_mapping` / `run_category_integration` を実装。
  - 検証: ヘッダが `value,category` 以外 → `ValueError` / 空セル → `ValueError` / `value` 重複 → `ValueError` /
    ファイル無し → `FileNotFoundError`。
  - モジュール docstring を新仕様（CSV の 1 対 1 マッピング、未一致は素通し + WARN）に書き換える。
- 完了条件:
  - `apply_category_mapping` がログを出さず、`(Series, 未一致dict)` を返す。
  - 未一致値は元の値のまま、NaN は NaN のまま出力される。
  - `run_category_integration` の戻り値に `n_unmatched` / `unmatched_values` が含まれる。
  - `python -c "import defect_analysis.category_integrate"` が通り、`yaml` を import していない。

### T2: CLI の引数変更（機能1）

- 対象: `src/defect_analysis/cli.py`
- 変更内容: §1.3 のとおり `--source-column`（必須）/ `--output-column`（既定 `統合カテゴリ`）を追加、
  `--map` のヘルプと既定パス表記を CSV に変更、`main()` の呼び出しを新シグネチャに合わせる。
  モジュール冒頭 docstring の `category` 実行例も更新。
- 完了条件: `python main.py category --help` が新引数を表示し、`--source-column` 未指定でエラーになる。

### T3: マッピング表ファイルの置き換え（機能1）

- 対象: `config/category_map.yaml`（削除）、`config/category_map.csv`（新規）
- 変更内容: §1.1 の内容で CSV を作成し、YAML を `git rm` する。
- 完了条件:
  `uv run python main.py category --input data/sample/defect_categories.csv --output reports/category_integrated.csv --source-column 中カテゴリ`
  が WARNING なしで完了し、出力 CSV に `統合カテゴリ` 列（締結不良/機能系/外観系/寸法系）が入る。

### T4: 機能1のテスト書き直し

- 対象: `tests/test_transforms.py`（`CategoryIntegrateTest` を差し替え、冒頭 import も更新）
- 変更内容: 旧 5 ケースを削除し、以下を実装する（テスト名は挙動が読める英文にする）。
  1. `test_load_mapping_reads_value_category_pairs` — 正常な CSV から `{value: category}` を得る。
  2. `test_load_mapping_ignores_comment_lines_and_blank_lines` — `#` 行・空行が無視される。
  3. `test_load_mapping_raises_when_header_is_not_value_category` — 列名が違えば `ValueError`。
  4. `test_load_mapping_raises_when_value_is_duplicated` — 重複 `value` で `ValueError`（C6）。
  5. `test_load_mapping_raises_when_category_cell_is_empty` — 空セルで `ValueError`。
  6. `test_load_mapping_raises_when_file_is_missing` — 未存在パスで `FileNotFoundError`。
  7. `test_apply_mapping_converts_known_values` — 表にある値が変換される。
  8. `test_apply_mapping_keeps_original_value_when_not_in_table` — 未一致は元の値のまま（C4）。
  9. `test_apply_mapping_reports_unmatched_values_with_counts` — 第 2 戻り値が `{値: 件数}` になる。
  10. `test_apply_mapping_keeps_null_as_null` — NaN は NaN のまま、未一致件数に含めない（C5）。
  11. `test_apply_mapping_strips_surrounding_whitespace_before_lookup` — 前後空白付きの値が一致する（C7）。
  12. `test_run_writes_output_csv_with_specified_output_column` — end-to-end（`tempfile` + `Config({}, root=tmp)`）。
  13. `test_run_raises_when_source_column_is_missing_from_input` — `KeyError`。
  14. `test_run_returns_unmatched_summary_when_table_is_incomplete` — 戻り値の `n_unmatched` / `unmatched_values`。
- 完了条件: `.venv/bin/python -m pytest tests/test_transforms.py -q` が全通し。
  ハードコードで通す実装（値の埋め込み等）を作らないこと。

### T5: フィルタ DSL の公開関数化（機能2の前提）

- 対象: `src/defect_analysis/analysis_data.py`
- 変更内容: §2.4 のとおり `apply_filter_clauses` / `filters_summary` を追加し、`apply_filters` を
  ラッパに再構成。`_filters_summary` の呼び出し箇所（`apply_filters` / `build_annotation_meta`）を更新。
- 完了条件: `tests/test_filters.py` と `tests/test_annotation.py` を**変更せずに**全通し。
  `apply_filter_clauses` は 0 行になっても例外を投げない。

### T6: `eda.py` にカスタム図レンダラを追加（機能2のコア）

- 対象: `src/defect_analysis/eda.py`
- 変更内容:
  - §2.5 の関数群と `_CUSTOM_BUILDERS` を追加。
  - `_draw_corr_heatmap` に `method: str = "pearson"` キーワードを追加（`df[cols].corr(method=method, numeric_only=True)`）。既存呼び出しは無変更。
  - `run_eda()` の末尾に `figures += _render_custom_charts(df, cfg, out_dir, meta)` を 1 行追加。
    **既存の `_fig_*` 関数とその呼び出し順は一切変更しない。**
- 完了条件:
  - `analysis.custom_charts` 未設定・空リストのとき、`run_eda` の出力図が現行と同一（追加も欠落もなし）。
  - 5 種すべてが PNG を生成し、脚注テキストが焼き込まれている。
  - 設定ミス（未知 type / 列なし / 型違い / フィルタ後 0 行）で例外が外に出ず、WARN ログを出して
    残りの図の生成が継続する。

### T7: `config.yaml` へのスキーマ追加（機能2）

- 対象: `config/config.yaml`
- 変更内容: `analysis:` 配下（`eda_cross_equipment_max_columns` の下）に §2.1 のコメント付きブロックと
  `custom_charts: []` / `custom_chart_max_hue: 8` / `custom_chart_max_points: 20000` を追加。既存キーは変更しない。
- 完了条件: `uv run python main.py eda` が従来と同じ図を出力する（`custom_charts` が空なので挙動不変）。

### T8: 機能2のテスト新設

- 対象: `tests/test_custom_charts.py`（新規。`tests/test_viz.py` は `viz_style` 単体のテストなので混ぜない）
- 前提: 小さな合成 DataFrame（数値列 2〜3、カテゴリ列 1〜2、目的変数 0/1、30〜50 行）を各テストで組み、
  `Config({...}, root=tmp)` を直接組み立てる（`config.yaml` は読まない。`tests/test_real_ingest_smoke.py` と同じ流儀）。
  `_render_custom_charts` を直接呼び、`tmp` 配下に出た PNG を検証する。
- テストケース:
  1. `test_returns_empty_list_when_custom_charts_is_absent` — 未設定で `[]`、ファイル出力なし。
  2. `test_renders_scatter_chart_to_png` / `..._bar_...` / `..._histogram_...` / `..._box_...` / `..._heatmap_...`
     — 5 種それぞれ 1 ファイル生成され、パスが戻り値に含まれる。
  3. `test_uses_auto_generated_filename_when_output_is_omitted` — `custom_01_scatter.png` が出る。
  4. `test_uses_given_output_filename` — `output` 指定名で出る。
  5. `test_strips_directory_from_output_and_keeps_file_under_eda_dir` — `output: ../x.png` でも `out_dir` 直下に出る。
  6. `test_skips_chart_when_type_is_unknown` — WARN でスキップ、他の図は生成される（2 件並べて確認）。
  7. `test_skips_chart_when_required_column_is_missing` — 存在しない列名でスキップ。
  8. `test_skips_scatter_when_axis_column_is_not_numeric` — 文字列列を x に指定してスキップ。
  9. `test_skips_chart_when_filters_exclude_all_rows` — 図単位フィルタで 0 行 → スキップ（例外を出さない）。
  10. `test_chart_level_filter_narrows_rows_used_for_the_figure` — フィルタ有無で描画に使われた行数が変わることを
      脚注テキスト（`n_rows`）または戻り値の検証で確認する。実装は `_render_one_custom_chart` の戻り値ではなく、
      `AnnotationMeta` を組んで `footnote()` 文字列に件数が入る点を突く。
  11. `test_hue_levels_are_capped_and_warned_when_exceeding_limit` — `custom_chart_max_hue: 2` で 3 水準を与え、
      WARN が出つつ図は生成される（`assertLogs` を使う）。
  12. `test_heatmap_ignores_hue_with_warning` — heatmap + hue で WARN、図は生成される。
  13. `test_one_broken_chart_does_not_prevent_the_others` — 壊れた設定と正常設定を並べ、正常側だけ出力される。
- 完了条件: `.venv/bin/python -m pytest tests/test_custom_charts.py -q` が全通し。
  matplotlib は `viz_style` が `Agg` を設定済みなので追加設定は不要。`vs.apply_style()` はテスト内で呼ぶ。

### T9: README 更新

- 対象: `README.md`
- 変更内容:
  1. 「ディレクトリ構成」の `config/category_map.yaml    統合カテゴリの変換ルール` を
     `config/category_map.csv     統合カテゴリの変換表（value → category）` に変更。
  2. 「### ユーティリティ」の**統合カテゴリ生成**節を書き換え:
     - 実行例を新引数（`--source-column` / `--output-column` / `--map config/category_map.csv`）に更新。
     - 説明文を「`value,category` の 1 対 1 マッピング表。表に無い値は**元の値のまま**出力され、
       未一致の値と件数が WARNING ログに出る（欠損は欠損のまま）」に差し替え、
       旧 `rules` / `default`（concat/major/middle/minor/const）の記述を削除。
     - CSV の内容例を 4〜5 行だけ掲載。
  3. 「### 分析ステージ」に `#### 追加グラフを config で指定する（analysis.custom_charts）` を新設
     （「#### フィルタで対象を絞る」の直後）。内容: 5 種の type、型ごとの必須フィールド表（§2.1 の要約）、
     YAML 例 3〜4 行、hue の扱い、設定ミスは WARN スキップで継続すること、出力先 `reports/eda/custom_*.png`。
  4. 「成果物」表の `reports/eda/*.png` の説明に「＋ `analysis.custom_charts` で指定した追加図」を追記。
  5. 「設定（config.yaml の主なキー）」に `analysis.custom_charts` の 1 行を追加。
- 完了条件: README の `category_map.yaml` への言及が 0 件になり、リンク先が実在ファイルを指す。

### 実装順序

T1 → T2 → T3 → T4（機能1で完結）→ T5 → T6 → T7 → T8（機能2で完結）→ T9。
T5 は T6 の前提なので順序厳守。T9 は両機能の実装完了後にまとめて行う。

---

## 4. 非目標（今回やらないこと）

- 既存の固定 EDA 図（`_fig_*` 7 系統）の見た目・出力名・生成条件の変更。
- カスタム図の `stats` / `ml` ステージへの展開（EDA のみ）。
- カテゴリ統合の複数列同時写像・AND 条件・階層構造（合意により廃止した機能の復活）。
- カスタム図の凡例配置やカラーマップのユーザー指定（パレットは `viz_style` 固定）。
