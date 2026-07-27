# フィルタ / グラフ注記 / ハードコード解消 実装仕様書

対象: `DataAnalysis`（`src/defect_analysis/`）
読者: 実装担当 coder / テスト担当 tester
状態: 設計確定（この仕様のみで実装可能な粒度）

---

## 0. 決定サマリ（先に結論）

1. **行フィルタ**は `analysis_data.load_features()` の読込直後に一括適用する。EDA / ML / stats は全てこの関数を入口にしているため、ここ一箇所で3系統に効く。設定は `config.yaml` の `analysis.filters`（リスト）+ `analysis.filters_on_missing_column`（欠損列ポリシ）。
2. **グラフ注記**は `viz_style.stamp_footnote(fig, text)` を新設し、`analysis_data.AnnotationMeta`（データ範囲・件数・適用フィルタを保持）と各図固有の「設備名 / データ種」から脚注文字列を組み立てて、`eda.py` / `ml.py` の全 `_save` 経路で焼き込む。メタは load 直後に1回だけ生成し、各 `_fig_*` に引数で渡す（グローバル変数やハードコードは使わない）。
3. **ハードコード解消**:
   - eda の `_DRIVER_MEASURES` 固定リストと `_fig_corr_heatmap` の固定列を撤廃。features のトレンド列（`{EQ-xx}__{measure}`）を **設備プレフィクスで動的にグルーピング**し、箱ひげ・相関ヒートマップを **設備ごとにループして設備名入りで保存**する。
   - `ingest.SourceSpec` に **列名マッピング `column_map`** を追加。`config.yaml` の `ingest.column_maps` から流し込み、`read_csv` 直後に `rename` してから必須列チェックする。
   - `analysis.leakage_columns` の手動保守リスクは、**接頭辞/接尾辞/正規表現ベースの規約除外＋起動時セルフチェックログ＋規約テスト**で低減する（方針提案。features 列自体のリネームは本スコープ外）。

**後方互換**: `analysis.filters` 未指定なら全件（現行と完全一致）。トレンド列が存在すれば設備ループは自動で従来と同等以上の図を出す。`_save` の脚注引数は任意（未指定で従来動作）。

---

## 1. 変更/新規ファイル一覧と責務境界

| ファイル | 区分 | 変更内容 | 責務境界 |
|---|---|---|---|
| `config/config.yaml` | 変更 | `analysis.filters` / `analysis.filters_on_missing_column` / `analysis.eda_driver_highlight`（任意）/ `ingest.column_maps` を追加。`analysis.leakage_prefixes` 等の規約強化 | 設定の唯一のソース |
| `src/defect_analysis/analysis_data.py` | 変更 | `apply_filters()` 追加＋`load_features()` から呼ぶ / `AnnotationMeta` dataclass ＋ `build_annotation_meta()` 追加 / `equipment_measure_groups()`・`equipment_name_map()` 追加 / `excluded_columns()` に規約除外を追加 | **データの単一入口（読込＋フィルタ）と、分析横断メタ（注記・設備グルーピング・除外列）** を集約。図モジュールを薄く保つ |
| `src/defect_analysis/viz_style.py` | 変更 | `stamp_footnote(fig, text)` 追加 | **描画ヘルパのみ**（脚注は描画操作） |
| `src/defect_analysis/eda.py` | 変更 | `_DRIVER_MEASURES` 撤廃 / `_save` に脚注引数 / 各 `_fig_*` に `meta` を渡す / 箱ひげ・ヒートマップを設備ループ化 | **図の構築のみ**。メタは受け取るだけで計算しない |
| `src/defect_analysis/ml.py` | 変更 | `_save` に脚注引数 / 各 `_fig_*` に `meta` を渡す | 同上 |
| `src/defect_analysis/ingest.py` | 変更 | `SourceSpec.column_map` 追加 / rename→必須列チェック順 / config の `column_maps` をマージ | **生CSVの列名正規化** |
| `src/defect_analysis/stats_tests.py` | 変更なし | フィルタは `load_features` 経由で自動適用。図が無いため脚注不要 | — |

> 新規ファイルは作らない。全て既存モジュール内の追加で完結する。

---

## 2. config.yaml 追加スキーマ（コメント付き具体例）

`analysis:` ブロックに追記:

```yaml
analysis:
  # ---- 既存（targets / leakage_* / id_columns / cv_folds ...）はそのまま ----

  # 行フィルタ: load_features 読込直後に「リスト順」で AND 適用する。
  #   省略 or 空リストなら全件（現行挙動と完全一致）。
  #   1要素 = 「column + 演算子」か「query 単独」。演算子: eq / in / not_in / min / max。
  #   min と max は同一句で併用可（数値レンジ）。
  filters:
    - {column: process_month, in: ["2026-01", "2026-02"]}   # 年月の複数指定
    - {column: plant_code,    eq: P01}                       # 存在時のみ有効（無ければ後述ポリシで処理）
    - {column: operator,      not_in: [op_ito]}              # 特定作業者を除外
    - {column: is_weekend,    eq: 0}                          # 平日のみ（is_weekend は 0/1）
    - {column: production_shift, in: ["1直", "2直"]}          # シフト
    - {column: lead_time_sec, min: 60, max: 3600}            # 数値レンジ（任意の数値列に対して）
    - {query: "ng_rate < 0.5"}                                # 任意式（pandas.DataFrame.query）

  # 存在しない列を指定した句の扱い: warn=その句だけスキップして継続 / error=即エラー
  #   plant_code・line_code のように「実データ次第で無い列」を warn で安全に無視する用途。
  filters_on_missing_column: warn   # warn | error（既定 warn）

  # （任意）設備別ドライバ測定値のハイライト対象。空なら synthesize.defect_drivers を参照し、
  #   それも無ければハイライトなし。図の生成対象は features のトレンド列から動的に導出するので、
  #   この設定が無くても図は出る（ハイライト＝箱ひげ小見出しに信号測定値の印を付けるだけ）。
  eda_driver_highlight: []
```

`ingest:` ブロックを新設（トップレベル）:

```yaml
# ---------------------------------------------------------------------
# 取り込み時の列名正規化（実データの列名 -> パイプライン標準列名）
#   read_csv 直後に rename し、その後で必須列チェックを行う。
#   マップが無い/空なら現行どおり（CSV の列名がそのまま標準列名である前提）。
# ---------------------------------------------------------------------
ingest:
  column_maps:
    traceability:            # 例: 実データの日本語列名 -> 標準列名
      車台番号: vin
      設備ID: equipment_id
      投入時刻: in_ts
      払出時刻: out_ts
    trend: {}
    defect: {}
    repair: {}
```

`analysis.leakage_prefixes` の規約強化（§7 参照。実装は小改修）:

```yaml
analysis:
  # 結果由来列を接頭辞ファミリで自動除外（手動リスト保守を減らす）
  leakage_prefixes: [defect, repair, severe, severity, top_defect, max_severity, time_to_repair, has_defect, has_severe, has_repair]
  # 名前が prefix に乗らない結果列（例: first_defect_date / last_defect_date）向けの正規表現（任意）
  leakage_regex: ["_defect_", "defect_date$"]
  # 上記で拾えない例外だけを明示（従来の leakage_columns は残すが縮小可能）
  leakage_columns: [first_defect_date, last_defect_date]
```

---

## 3. フィルタ実装（analysis_data.py）

### 3.1 シグネチャ

```python
def load_features(cfg: Config) -> pd.DataFrame:
    """processed/features を読み込み、analysis.filters を適用して返す。"""
    # 既存の読込 …
    df = load_df(...)
    df = apply_filters(df, cfg)   # ← 追加。これで EDA/ML/stats 全てに効く
    return df


def apply_filters(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """analysis.filters をリスト順に AND 適用した DataFrame を返す。"""
```

### 3.2 処理フロー

1. `rules = cfg.get("analysis.filters", []) or []`。空なら `df` をそのまま返す（早期 return、ログは出さない＝現行挙動維持）。
2. `on_missing = cfg.get("analysis.filters_on_missing_column", "warn")`。
3. `n_before = len(df)`。
4. `rules` を順に評価し、各句で boolean マスクを作り `df = df[mask]`。
5. `n_after = len(df)`。INFO ログ: `フィルタ適用: {n_before} -> {n_after} 行（{len(rules)} 句）`。各句適用後の行数は DEBUG で `[filter] {clause説明}: {残行数} 行`。
6. `n_after == 0` の場合: WARNING を出したうえで **`ValueError` を送出**（`適用フィルタが全行を除外しました: {適用サマリ}`）。空 DataFrame を下流に流すと EDA/ML が不明瞭な例外を出すため、ここで明確に落とす。

### 3.3 1句の評価規則

各 `clause`（dict）の判定:

- `"query" in clause`（column なし）: `df.query(clause["query"], engine="python")` 相当のマスクを作る。式評価に失敗したら `ValueError`（`不正な query 式: {expr} ({例外})`）。
  - 注意: `EQ-01__pressure` のようにハイフンを含む列名は query の識別子として扱えない。**EQ 列はレンジ/集合系の句（min/max/in）で指定する**旨をコメントに明記。
- それ以外は `col = clause["column"]` を要求。
  - `col not in df.columns` のとき: `on_missing == "error"` なら `ValueError`。`warn`（既定）なら WARNING（`[filter] 列が存在しないためスキップ: {col}`）してこの句を無視（マスク=全 True 相当、行数不変）。
  - 演算子キー（`col` 以外のキー）を解釈:
    - `eq`: `df[col] == value`（スカラ）
    - `in`: `df[col].isin(value)`（`value` は list。list でなければ `ValueError`）
    - `not_in`: `~df[col].isin(value)`（同上）
    - `min`: `df[col] >= value`
    - `max`: `df[col] <= value`（`min` と併用時は AND）
  - 認識できる演算子キーが1つも無い場合: `ValueError`（`フィルタ句に演算子がありません: {clause}`）。
  - 認識外の余分なキーがある場合: WARNING（`[filter] 未知のキーを無視: {keys}`）。

### 3.4 評価順序・エッジケース（明文化）

- **評価順序**: リスト定義順に上から適用（AND）。順序で結果集合は変わらないが、DEBUG の残行数ログは順序依存で出る。
- **process_month**: features 上は文字列 `"YYYY-MM"`。`eq`/`in` は完全一致。`min`/`max` は辞書順比較だがゼロ詰め `YYYY-MM` では暦順と一致するため範囲指定に使える（コメントで明記）。
- **is_weekend**: int 0/1。`eq: 0` で平日。
- **数値レンジ**: `min` のみ / `max` のみ / 両方いずれも可。境界は含む（>= / <=）。
- **plant_code / line_code**: 実データに無い可能性 → 既定 `warn` で安全にスキップ。
- **型不一致**: 例えば数値列に文字列 `min` を与えた場合は pandas の比較例外をそのまま `ValueError` に包んで再送出（`[filter] {col} の比較に失敗: {例外}`）。
- **全除外**: §3.2-6 のとおり `ValueError`。

---

## 4. グラフ注記（脚注スタンプ）

### 4.1 メタの生成元とデータ構造（analysis_data.py）

```python
@dataclass(frozen=True)
class AnnotationMeta:
    n_rows: int                 # フィルタ後の行数（＝図に使われた台数）
    month_min: str | None       # process_month の最小（無ければ None）
    month_max: str | None
    n_months: int               # process_month のユニーク数（無ければ 0）
    filters_summary: str        # 設定された filters を人間可読化（無ければ "なし"）

    def footnote(self, *, data_kind: str, equipment: str | None = None) -> str:
        ...


def build_annotation_meta(df: pd.DataFrame, cfg: Config) -> AnnotationMeta:
    """フィルタ適用後の df と cfg から注記メタを1回だけ生成する。"""
```

生成元:
- `n_rows = len(df)`（＝実データ由来。捏造しない）。
- `process_month` 列があれば `month_min/max = sorted(unique)[0/-1]`, `n_months = nunique`。無ければ `None/None/0`。
- `filters_summary` は **設定された `analysis.filters` を整形**（実際に要求されたフィルタを正直に表示）:
  - `eq` → `col=val` / `in` → `col∈{v1,v2}` / `not_in` → `col∉{...}` / `min,max` → `col[min,max]`（片側は `col≥min` / `col≤max`）/ `query` → `query(式)`。
  - スキップされた句（欠損列 warn）は `col(欠損)` と付記して透明性を保つ。
  - 空なら `"なし"`。

### 4.2 脚注フォーマット（`AnnotationMeta.footnote`）

2行。1行目=図固有、2行目=データ範囲。

```
設備: {equipment or "全設備"} ｜ データ種: {data_kind}
範囲: {month_min}〜{month_max}（{n_months}ヶ月・{n_rows:,}台） ｜ フィルタ: {filters_summary}
```

- `process_month` が無い場合の範囲部: `範囲: 全期間（{n_rows:,}台）`。
- `equipment` は設備ループ図では `"EQ-01 圧入"`（id＋name）、全体図では `None`→`"全設備"`。
- `data_kind` は各図が渡す短い種別名（下表）。

### 4.3 描画関数（viz_style.py）

```python
def stamp_footnote(fig, text: str) -> None:
    """図の左下端に脚注を焼き込む。tight_layout 実行後・savefig 前に呼ぶ。"""
    fig.text(0.005, 0.005, text, ha="left", va="bottom",
             fontsize=7, color=MUTED, linespacing=1.3)
```

- `savefig.bbox="tight"`（既存 rcParam）により `fig.text` も出力領域に含まれるため、クリップされない。
- x 軸ラベルとの重なりが気になる図では、呼び出し側が既に使っている `fig.tight_layout(rect=(0, 0, 1, 0.97))` の下端を `rect=(0, 0.04, 1, 0.97)` に広げてよい（任意・図単位判断）。

### 4.4 `_save` シグネチャ変更と受け渡し

eda.py / ml.py 双方の `_save`:

```python
def _save(fig, path, footnote: str | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if footnote:
        vs.stamp_footnote(fig, footnote)
    fig.savefig(path)
    plt.close(fig)
    logger.info("図を保存: %s", path)
    return str(path)
```

- 各 `_fig_*` 関数に `meta: AnnotationMeta` を引数追加し、末尾で
  `return _save(fig, out, meta.footnote(data_kind="…", equipment=…))` とする。
- `run_eda` / `run_ml` は `df = load_features(cfg)` の直後に
  `meta = build_annotation_meta(df, cfg)` を作り、各 `_fig_*` へ渡す。

**data_kind の割当（変更の指針。文言は例）**

| 関数 | data_kind | equipment |
|---|---|---|
| `_fig_rate_by_category` | `不良率(カテゴリ別)` | None |
| 箱ひげ（設備ループ） | `ドライバ測定値の分布` | `EQ-xx name` |
| `_fig_corr_with_target` | `目的変数との相関` | None |
| ヒートマップ（設備ループ） | `測定値相関` | `EQ-xx name` |
| `_fig_monthly_trend` | `月次トレンド` | None |
| `_fig_defect_category_breakdown` | `不良カテゴリ内訳` | None |
| `_fig_model_comparison` | `モデル比較(CV)` | None |
| `_fig_roc_pr` | `ROC/PR(保持テスト)` | None |
| `_fig_pred_vs_actual` | `予測vs実測` | None |
| `_fig_importance` | `特徴量重要度` | None |

---

## 5. ハードコード解消(1): 設備別 箱ひげ / ヒートマップ の動的化（eda.py）

### 5.1 設備グルーピング（analysis_data.py に追加）

```python
def equipment_measure_groups(df: pd.DataFrame, *, include_pass_sec: bool = False) -> dict[str, list[str]]:
    """トレンド列 '{EQ-xx}__{measure}' を設備プレフィクスでグルーピングして返す。
    キーは EQ-id、値はその設備の測定値列（既定で '__pass_sec' は除外）。決定的に列順ソート。"""


def equipment_name_map(cfg: Config) -> dict[str, str]:
    """synthesize.equipments の id->name を返す（無ければ空 dict）。表示名 'EQ-01 圧入' 用。"""
```

- 導出の唯一のソースは **features のトレンド列**（`"__" in col`）。`_DRIVER_MEASURES` は削除する。
- 表示名は `equipment_name_map` で解決。map に無い id は素の id を表示名にする。
- （任意）ハイライト: `analysis.eda_driver_highlight` か `synthesize.defect_drivers` から `{eq}__{measure}` を作り、その測定値のサブプロット見出しに印（例 末尾に `*`）を付ける。設定が無ければ印なし。**図の生成有無はハイライト設定に依存しない**。

### 5.2 箱ひげ（`_fig_trend_boxplot_by_target` を設備ループへ）

- 変更後シグネチャ:
  `def _fig_trend_boxplot_by_target(df, target, out_dir, meta, groups, name_map, highlight) -> list[str]`
- `groups = equipment_measure_groups(df)` の各 `eq -> cols` について1図生成:
  - サブプロットはその設備の測定値数に応じたグリッド（例 2×2、cols が4未満なら余りは非表示）。
  - 各サブプロットは `target`(0/1) で層別した箱ひげ（既存の描画ロジックを流用）。
  - タイトルに設備表示名、脚注 `equipment=f"{eq} {name_map.get(eq, '')}".strip()`, `data_kind="ドライバ測定値の分布"`。
  - 保存名: `out_dir / f"02_trend_boxplot__{eq}.png"`。
- 戻り値は生成パスの list。`run_eda` の figures 集約を list 連結に変更。
- トレンド列が皆無なら WARNING を出し空 list を返す（図スキップ）。

### 5.3 相関ヒートマップ（`_fig_corr_heatmap` を設備ループへ）

- `_DRIVER_MEASURES` と `["ng_rate","wait_time_sec","max_gap_sec","lead_time_sec"]` の固定を撤廃。
- 各 `eq -> cols`（＋その設備の `__pass_sec` があれば含める＝`include_pass_sec=True` で取得）＋ `target` の相関行列で1図生成:
  - 列が `target` 含め2列未満ならスキップ（相関が意味を持たない）。
  - タイトルに設備表示名、脚注 `equipment`, `data_kind="測定値相関"`。
  - 保存名: `out_dir / f"04_correlation_heatmap__{eq}.png"`。
- 設計判断: ヒートマップも設備別にする（各設備は torque/temperature/pressure/vibration 等 複数測定値を持つため設備内相関が意味を持つ）。全設備横断の単一ヒートマップは廃止。

### 5.4 run_eda の集約変更

```python
figures = []
figures.append(_fig_rate_by_category(df, spec.categorical, target, out_dir/"01_...", meta))
figures += _fig_trend_boxplot_by_target(df, target, out_dir, meta, groups, name_map, highlight)
figures.append(_fig_corr_with_target(df, spec.numeric, target, out_dir/"03_...", meta))
figures += _fig_corr_heatmap(df, target, out_dir, meta, groups, name_map)
figures.append(_fig_monthly_trend(df, target, out_dir/"05_...", meta))
figures.append(_fig_defect_category_breakdown(df, out_dir/"06_...", meta))
```

（`_fig_corr_heatmap` も list を返す設備ループ関数に変更。）

---

## 6. ハードコード解消(2): ingest の列名リネーム（ingest.py）

### 6.1 SourceSpec 変更

```python
@dataclass(frozen=True)
class SourceSpec:
    name: str
    subdir: str
    filename_regex: str
    required_columns: list[str]
    key_columns: list[str]
    date_columns: list[str] = field(default_factory=list)
    column_map: dict[str, str] = field(default_factory=dict)  # 追加: 生列名 -> 標準列名
```

`SOURCES` の各定義はそのまま（`column_map` 既定=空）。`required_columns` などの構造はコードのデフォルトを維持し、**YAML 化するのはリネームマップのみ**（実データで列名がずれる現実的シナリオに限定）。

### 6.2 config からのマージ

`ingest()` 冒頭で `analysis` と同様に読み込み、各 spec に `column_map` を注入した spec 群を作る:

```python
def _apply_column_maps(sources: list[SourceSpec], cfg: Config) -> list[SourceSpec]:
    maps = cfg.get("ingest.column_maps", {}) or {}
    return [replace(s, column_map=maps.get(s.name, {}) or {}) for s in sources]
```

（`dataclasses.replace` を使用。frozen dataclass のため再生成。）

### 6.3 読込順（`_load_source`）

`pd.read_csv` の直後、必須列チェックの **前** に rename を挟む:

```python
df = pd.read_csv(path)
if spec.column_map:
    df = df.rename(columns=spec.column_map)
missing = [c for c in spec.required_columns if c not in df.columns]  # rename 後に判定
```

エッジケース:
- 同一 canonical へ複数の生列がマップされて衝突する場合: rename 後に列が重複する → WARNING（`[{name}] rename 後に列が重複: {col}`）。判定は `df.columns` の重複検出で行い、先勝ちにするか当該ファイルをスキップ（＝スキップ扱いにして skipped++、安全側）。
- map の適用で既存の canonical 列を上書きする場合も同様に WARNING。
- map に無い列はそのまま（部分マップ可）。

---

## 7. ハードコード解消(3): leakage 手動保守リスク低減（方針提案＋小改修）

**問題**: `analysis.leakage_columns` は結果由来列を人手で列挙しており、features 側に新しい結果列が増えると漏れてリークする。

**推奨方針（本スコープで実施可能な最小改修）**:

1. **接頭辞ファミリ拡張**: `leakage_prefixes` を `defect / repair / severe / severity / top_defect / max_severity / time_to_repair / has_defect / has_severe / has_repair` に拡張。
   - 安全性: 現状の説明変数（`EQ-xx__*`, `lead_time_*`, `wait_*`, `*_gap_sec`, `operator`, `production_*`, `is_weekend`, `process_month`, `plant_code`, `line_code`, `lot_no`）はいずれもこれらの接頭辞に該当しないため、正当な説明変数を誤って除外しない。
2. **正規表現ゲート追加**: 接頭辞で拾えない結果列（`first_defect_date` / `last_defect_date` 等）向けに `analysis.leakage_regex`（任意）を新設し、`excluded_columns()` / `resolve_predictors()` で `re.search` によりマッチ列を除外。
3. **起動時セルフチェックログ**: `resolve_predictors()` の最後で、除外集合とターゲットを突き合わせ、`get_targets()` の目的変数が predictor に残っていないかを検査し、残っていれば ERROR ログ。また、除外された列を「明示除外 / 接頭辞除外 / 正規表現除外」で分類して DEBUG 出力し、規約ドリフトを可視化する。
4. **規約テスト**（tester 申し送り。§9）: 「結果由来を示す命名の列は predictor に絶対に現れない」ことを features 実データで検査。新しい `defect_*` 列を足しても自動で落ちることを担保。

**スコープ外**: features.py 側で結果列に統一プレフィクス（例 `y__`）を付与するリネームは影響範囲が広いため本仕様では扱わない。上記1〜4で「増えても既定の規約で拾える」状態にすることを目的とする。

`excluded_columns()` / `resolve_predictors()` の改修点（骨子）:

```python
def excluded_columns(cfg, columns) -> set[str]:   # columns を受けて regex 判定も行う
    # 既存: targets + leakage_columns + id_columns
    # 追加: leakage_regex にマッチする列を除外集合へ
```

`resolve_predictors()` の prefix 判定は既存の `startswith(prefixes)` を流用し、拡張した `leakage_prefixes` がそのまま効く。

---

## 8. 処理フロー全体（変更後）

```
run_eda / run_ml / run_stats
  └─ load_features(cfg)
        ├─ load_df(processed/features)
        └─ apply_filters(df, cfg)            # §3 行フィルタ（3系統共通）
  └─ resolve_predictors(df, cfg)             # §7 規約除外強化
  └─ build_annotation_meta(df, cfg)          # §4 注記メタ（eda/ml のみ）
  └─ equipment_measure_groups(df) / name_map # §5（eda のみ）
  └─ 各 _fig_*(… meta …)
        └─ _save(fig, path, meta.footnote(data_kind=…, equipment=…))
              └─ vs.stamp_footnote(fig, text) → savefig
```

---

## 9. テスト観点（tester 申し送り）

> 私的規約遵守: ハードコードで通すテスト・無意味テスト禁止。小さな **実 DataFrame** を組んで実挙動を検証する（モックは I/O のみ最小限）。テスト名は挙動を説明する。

### フィルタ（apply_filters）
- `eq` で対象値の行のみ残る / `in`・`not_in` が集合で正しく機能する。
- `min` のみ・`max` のみ・`min`+`max` 併用で境界（含む）が正しい。
- `query` 式が行を正しく絞る / 不正式で `ValueError`。
- 複数句が AND で積み重なる（順序を変えても結果集合は同一）。
- `filters` 空/未指定なら入力 df と同一（行数・内容一致）。
- 欠損列 × `warn` → その句がスキップされ行数不変＋警告ログ / 欠損列 × `error` → `ValueError`。
- 全除外になる設定で `ValueError`（メッセージに適用サマリを含む）。
- `process_month`（文字列）で `in`・`min/max` が期待どおり効く。
- 適用前後の行数が INFO ログに出る（ログ捕捉で確認）。

### 注記メタ / 脚注
- `build_annotation_meta`: `n_rows` がフィルタ後 len と一致 / `month_min/max/n_months` が実データ由来 / `filters_summary` が **設定値を反映**（固定文字列ではないことを、異なる filters で出力が変わることで確認）。
- `process_month` 無しの df で範囲部が「全期間」になる。
- `footnote(equipment=None)` が「全設備」、指定時は id＋name を含む。
- `stamp_footnote` 後、`fig.texts` に当該文字列を持つ artist が1つ増える（ピクセル比較はしない）。

### 設備別図の動的化
- `equipment_measure_groups`: `EQ-01__pressure` 等を EQ 単位に正しくグルーピング / 既定で `__pass_sec` 除外・`include_pass_sec=True` で包含。
- ドライバ測定値のハードコードが無いこと＝**設備が増減した df で図数が設備数に追従**する（例: EQ を1つ減らした df で箱ひげ図が1枚減る）。
- トレンド列が無い df で箱ひげ・ヒートマップがスキップされ、他の図は生成される。
- 保存ファイル名に設備 id が入る（`02_trend_boxplot__EQ-01.png` 等）。

### ingest リネーム
- 生列名の CSV ＋ `column_map` で、rename 後に標準列が揃い必須列チェックを通過し採用される。
- map 無しで非標準列名 → 従来どおり必須列欠落でスキップ＋警告。
- 衝突/上書きマップで警告が出る（安全側でスキップされる）。

### leakage 規約
- `resolve_predictors` の出力に目的変数・結果由来命名の列が一切含まれない。
- features に新規 `defect_foo` を足した df で、接頭辞規約により自動除外される（明示リストに足さなくても落ちる）。
- 正当な説明変数（`EQ-xx__*`, `lead_time_sec`, `operator` 等）は除外されない。

### 結合スモーク
- 小さな features を用意し、filters を設定した状態で `run_eda` / `run_ml` が例外なく完走し、生成図数が設備ループ数と整合する。

---

## 10. 実装順序（推奨）

1. `analysis_data.apply_filters` ＋ `load_features` 組み込み（→ stats/EDA/ML 全系に即効）。
2. `AnnotationMeta` / `build_annotation_meta` / `viz_style.stamp_footnote`。
3. `eda.py` の設備ループ化＋脚注（`equipment_measure_groups`/`name_map` 追加込み）。
4. `ml.py` の脚注。
5. `ingest.py` の `column_map`。
6. `excluded_columns`/`resolve_predictors` の規約強化＋config 更新。
7. tester へ §9 を申し送り。
