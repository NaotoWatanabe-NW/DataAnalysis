# repair 統合カテゴリ（塗装課内不良対比表）取り込み 設計（2026-08-28）

`config/塗装課内不良対比表_まとめ.csv`（作業工程 + 大分類 + 中分類 + 小分類 → グラフ項目）を使って
repair（修正実績）に**統合カテゴリ**を付与し、VIN パネルへ接続するための設計。

既存設計（`docs/real_data_repair_design.md` / `docs/real_data_ingest_design.md` /
`docs/category_csv_and_custom_charts_design.md`）を置き換えるものではなく、**差分だけ**を定義する。
実データ経路（`convert` → `assemble` → `eda`/`stats`/`ml`）の枠組み・責務境界は本体設計に従う。

本書は `docs/real_data_repair_design.md` §9-4（ユーザー確認事項「修正の分類でモデリングに使いたい粒度はどれか」）
に対する回答でもある。粒度は**対比表の「グラフ項目」（48 種）**に確定した。

---

## 0. 結論（決定事項と根拠）

| # | 決定 | 根拠 |
|---|---|---|
| IC1 | **マッピングの唯一の正は `config/塗装課内不良対比表_まとめ.csv`（5 列そのまま）。中間形式は一切作らない** | この表は Excel から人が更新する運用データで、666 行 5 列は機械が直接読める形になっている。中間形式（複合キー CSV / YAML）を挟むと「表を更新したのに反映されない」同期ずれが必ず起きる。実際 `config/category_map.yaml`（7,349 行）は生成元スクリプトごと失われ、誰にも読まれない死んだ生成物になっている（実測。§7-1） |
| IC2 | **`config/category_map.yaml` は削除する。`scripts/_gen_category_map.py` は作らない** | `docs/category_csv_and_custom_charts_design.md` C9 で削除決定済みの再発。読み手ゼロ・再生成不能・IC1 と重複する第 3 の表であり、残す価値が無い。CHANGELOG にも「廃止」と書かれており現状が文書と矛盾している |
| IC3 | **`config/category_map.csv`（`value,category`）と CLI `category` サブコマンドは無変更で存続。4 キー方式とは別機能として併存させる** | 用途が違う（任意 CSV への 1 列写像ユーティリティ ↔ repair パイプライン内の 4 キー写像）。既存 205 テストと `docs/category_csv_and_custom_charts_design.md` の契約を壊す理由が無い。4 キーを CLI に一般化すると「複数の写像元列 + 表側列との対応」という新しい CLI DSL が必要になり、いま要らない |
| IC4 | **照合は 4 キー厳密一致のみ（`入力工程`↔`作業工程` / `大分類` / `中分類` / `小分類`）。3 キーへのフォールバックはしない**。両辺 `str` 化 + 前後空白 strip のみ行い、全半角・大小文字の正規化はしない | ユーザー確定方針。C7（既存 1 キー方式）と同じ照合規約なので、リポジトリ内で「カテゴリ照合＝strip のみの厳密一致」に統一される。実測でキー 4 列に前後空白・欠損は 0 件（8,570 行）なので strip は安全側の保険 |
| IC5 | **写像後の値（グラフ項目）だけは NFKC 正規化する** | `電着2次タレ`(205 行) と `電着２次タレ`(83 行) が同じカテゴリの全半角ゆれで表に併存している（実測）。NFKC で畳まないと `normalize_name` 後の列名が衝突し、`prepare_repair_source` の crosstab が**同名列を 2 本作る**。キー（IC4）は正規化せず値だけ正規化することで、実測済みの一致率 93.65% を変えずに列名衝突を原理的に排除できる |
| IC6 | **統合カテゴリは決して NaN にしない。未一致は 3 種のラベルで必ず埋める**: `対象外工程`（入力工程が対比表に無い）/ `未分類`（対象工程だが 4 キー組合せが表に無い）/ `グラフ対象外`（表のグラフ項目が `-`） | (a) crosstab は NaN を落とすため、NaN を許すと「カウント列の合計 = `__count`」という検証可能な不変条件が壊れ、分類できなかった行が集計から静かに消える。(b) C4 の「未一致は元の値を通す」は**複合キーでは適用できない**（元の値が 4 つあり一意に決まらない）。ラベルは C4 の精神（データを失わない）を複合キーで実現する唯一の形 |
| IC7 | **`対象外工程` と `未分類` は区別する。ログ水準も変える（`対象外工程`=INFO / `未分類`=WARNING）** | 意味と対処が違う。対比表に無い 7 工程は**全 491 行が 100% 未一致**（部分一致が 1 件も無い）で、表が塗装課の 3 工程を網羅的に定義した設計物であることの実証（§1-3）。育て漏れなら部分的に当たるはずで、全滅は「範囲外」を意味する。一方 3 工程内の未一致は 53 行（0.3〜1.2%）で、これは表の育て漏れか入力ミスであり人が直す対象。桁の違う 2 つを同じ扱いにすると本当に直すべき 53 行が 491 行に埋もれる |
| IC8 | **グラフ項目 `-`（表が明示する「グラフ集計対象外」。表 6 行 / repair 134 行）は `グラフ対象外` ラベルに写す** | `-` は欠損ではなく「完成課責任のためグラフ項目なし」という表の明示的な回答（実測: 大分類=完成課 / 小分類=完成課責任 の 6 行のみ）。`normalize_name("-")` は `"col"` になるため、そのまま列名にすると意味不明な `repair_修正__統合カテゴリ__col` が生まれる。ラベル化で防ぐ |
| IC9 | **4 キー重複は「完全重複行は静かに除去 → 残る競合は `グラフ項目` の昇順先頭を採用し WARNING」。`on_duplicate_key: error` で即停止も選べる（既定は `first`）** | 実測 3 件のうち 1 件は 5 列すべて同一の完全重複（情報として無矛盾）、残り 2 件が真の競合。**行の並び順に依存しない**（値でソートしてから先頭を採る）ため、Excel から再エクスポートして行順が変わっても結果が変わらない決定的規則になる。C6（1 キー方式は重複＝即 ValueError）と揃えて `error` を既定にする案は採らない: 影響は repair 8,570 行中 **1 行**で、666 行の運用表の 2 行の曖昧さのために ingest 全体を止めるのは不均衡。WARNING を毎回出して表の修正を促す |
| IC10 | **接続は assemble 段階。`prepare_repair_source` の直前に `統合カテゴリ` 列を派生させ、既存 `real_ingest.repair.category_columns` の枠組みで `統合カテゴリ: true` としてカウント展開する** | 新しい展開機構を作らずに済み、列名（`repair_{source}__統合カテゴリ__{値}`）・0 埋め（`count_infixes`）・`ingest_quality.csv` の `categories` 記録・`_infer_column_source` が**すべて既存コードのまま効く**。convert 段階でレイクに焼くと、表を 1 行直すたびに `--force` 全再変換が要る（convert は情報を捨てない原則にも反しない代わりに再現コストが跳ねる）。assemble ならレイクはそのままで `assemble` の再実行だけで反映される |
| IC11 | **列名は `repair_{source}__統合カテゴリ__{normalize_name(値)}`。増加は実測 +45 列（カウント 43 + `__n_統合カテゴリ` + `__top_統合カテゴリ`）**。`max_category_columns` は 30 → **100** に引き上げる | `repair_` 始まりなので `analysis.leakage_prefixes` の `repair` に自動的に乗り、説明変数には 1 列も入らない（R4 の規約を維持）。現行 30 は今日の `大分類` 展開 29 列で既に上限ギリギリ（`大分類` は 514 行時点の 10 値ではなく現行レイクで **29 値**。§1-5）。29 + 43 = 72 なので 100 は妥当な余裕。`部位`(135 値) の誤展開は依然として弾ける |
| IC12 | **未一致の 4 キー組合せは `reports/repair_category_unmatched.csv` に毎回出力する（0 行でもヘッダのみ出す）** | 「表を育てる」作業の入力そのもの。WARNING ログだけだと Excel への転記ができず、未一致 53 行の解消が進まない。区分・4 キー・行数・VIN 数を決定的な順序で並べる |

---

## 1. 実測事実（本設計の根拠。2026-08-28 時点）

対象: `data/lake/repair`（8,570 行 / 6,559 VIN）と `config/塗装課内不良対比表_まとめ.csv`（666 行）。

1. **対比表の構造**: `作業工程` は 3 値のみで、各 222 行ずつの均等構造
   （`Ｎ完修(塗装)` 222 / `Nトラッカー修正` 222 / `Nト焼付修正` 222）。`グラフ項目` は 48 種。
   空セルは 0 件。キー 4 列に前後空白は 0 件。
2. **4 キー厳密一致率 = 8,026 / 8,570 行（93.65%）**。repair のキー 4 列に欠損・空文字は 0 件。
3. **未一致 544 行の内訳（IC7 の根拠）**:

   | 区分 | 入力工程 | 未一致行数 / 総行数 | 未一致率 |
   |---|---|---|---|
   | 対象 3 工程 | Nトラッカー修正 | 33 / 2,841 | 1.2% |
   | 対象 3 工程 | Nト焼付修正 | 13 / 2,789 | 0.5% |
   | 対象 3 工程 | Ｎ完修(塗装) | 7 / 2,449 | 0.3% |
   | 対象外 | Nト塗装回送 / Ｎ完修(板金) / Ｎシオフライン(汎A) / Ｎ完修(組立) / Ｎシオフライン(汎Ⅲ) / Nト部品交換 / Ｎ完修(樹脂) | 各 100%（計 491 / 491） | 100% |

   対象 3 工程内の未一致は 53 行 / 51 VIN。7 工程が**部分一致すらゼロ**である非対称性が
   「対比表は塗装課 3 工程のみを対象にした表であり、他工程は範囲外」という解釈の実証的根拠。
4. **グラフ項目 `-`**: 表に 6 行（3 工程 × {組付けキズ, 組付け凹凸} / 小分類 `完成課責任`）。
   repair では 134 行が該当。**欠損ではなく「塗装課のグラフ対象外」という表の明示的回答**。
5. **`大分類` の実カーディナリティは 29**（`docs/real_data_repair_design.md` §1-6 の「10」は 514 行時点の値）。
   → 現行の `大分類: true` 展開は既に 29 列を作っており `max_category_columns: 30` の直下にいる。
6. **4 キー重複 3 件**（IC9 の根拠）:

   | 行番号 | 4 キー | グラフ項目 | 種別 |
   |---|---|---|---|
   | 442 / 443 | Nトラッカー修正 / 入れ込み / 凹凸、キズ / 治具当たり | `当りキズ` / `当りキズ` | 完全重複（無矛盾） |
   | 664 / 665 | Nト焼付修正 / 入れ込み / 凹凸、キズ / 治具当たり | `キズ （触り・当り）` / `当りキズ` | 競合 |
   | 220 / 221 | Ｎ完修(塗装) / 入れ込み / 凹凸、キズ / 治具当たり | `キズ` / `当りキズ` | 競合 |

   競合キーに当たる repair 行は **全体で 1 行**（Ｎ完修(塗装) の 1 行）。
7. **全半角ゆれ**: `電着2次タレ`(205 行) と `電着２次タレ`(83 行)。NFKC で同一（合計 288 行）。
   NFKC 後は 48 → 47 種で、`normalize_name` 後の列名衝突は 0 件になる（IC5）。
8. **本設計の規則を適用した結果の統合カテゴリ分布（全 8,570 行・43 種）**:
   シーラー不良・付着 1036 / 塗料カス 944 / 上塗ブツ 883 / シーラー不良 870 / 修正 714 /
   **対象外工程 491** / タレ 485 / 塗不足 443 / マスキング不良 436 / 当りキズ 368 / ワキ 324 /
   電着2次タレ 288 / 電着ブツ 251 / **グラフ対象外 134** / …（中略）… / **未分類 53** / … / 変色/色ムラ 1。
   NaN は 0 行。43 種は `normalize_name` 後も 43 種（衝突なし）。

---

## 2. マッピング表の仕様

### 2.1 ファイル

`config/塗装課内不良対比表_まとめ.csv`（UTF-8 BOM 付き。**現状のまま。リネームも整形もしない**）:

```csv
作業工程,大分類,中分類,小分類,グラフ項目
Ｎ完修(塗装),2トーン塗装,かぶり・差し込み,作業者要因,マスキング不良
Ｎ完修(塗装),2トーン塗装,かぶり・差し込み,是正,マスキング不良
...
```

- 読み込み: `pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)`。
  `comment="#"` は**使わない**（業務データ側に `#` が現れうるため。1 キー方式の `load_mapping` とはここが違う）。
- 検証（すべて `ValueError`。メッセージにパスと該当内容を含める）:
  - `key_columns` / `value_column` に指定した列が存在しない。
  - いずれかのキー列または値列が空文字の行がある（実測 0 件）。
- ファイルが無い場合は `FileNotFoundError`（呼び出し側の扱いは §4.3）。

### 2.2 正規化と重複解決（決定的手順）

1. 値列（`グラフ項目`）に **NFKC 正規化**を適用（IC5）。キー 4 列は `str` 化 + 前後 strip のみ（IC4）。
2. **5 列すべて同一の行を除去**（`drop_duplicates()`）。除去件数を INFO ログ。
3. 残った行で 4 キーが重複するキーを検出する。
   - `on_duplicate_key == "error"`: `ValueError`。メッセージにキーと候補値をすべて列挙。
   - `on_duplicate_key == "first"`（既定）: **キーごとに値を昇順ソートし先頭を採用**。
     WARNING に「キー / 採用値 / 不採用値」を列挙（実測 2 件が必ず出る）。
     行の並び順に依存しないため、Excel 再エクスポートで結果が変わらない。
4. `{4キーtuple: 値}` の dict と、`作業工程` の値集合（=対象工程集合）を保持する。

### 2.3 写像規則（3 分岐。この順に評価する）

repair の 1 行について:

| 条件 | 出力 |
|---|---|
| `入力工程` が対象工程集合に無い（欠損含む） | `対象外工程` |
| 対象工程で、4 キーが表にある & 値が `-` 以外 | その値（NFKC 済み） |
| 対象工程で、4 キーが表にある & 値が `-` | `グラフ対象外` |
| 対象工程で、4 キーが表に無い | `未分類` |

- キー列に欠損がある行は「表の値と一致しない」ので自然に `対象外工程` / `未分類` に落ちる。特別扱いはしない。
- **出力に NaN は存在しない**（IC6 の不変条件）。

---

## 3. API 仕様

### 3.1 `src/defect_analysis/category_integrate.py`（**追加のみ**。既存 3 関数は無変更）

```python
DEFAULT_CATEGORY_TABLE_REL = Path("config") / "塗装課内不良対比表_まとめ.csv"
DEFAULT_CATEGORY_KEY_COLUMNS: dict[str, str] = {   # 表側の列 -> repair 側の列
    "作業工程": "入力工程", "大分類": "大分類", "中分類": "中分類", "小分類": "小分類",
}
DEFAULT_CATEGORY_VALUE_COLUMN = "グラフ項目"
DEFAULT_CATEGORY_EXCLUDED_VALUES = ("-",)
DEFAULT_CATEGORY_LABELS = {
    "out_of_scope_process": "対象外工程",
    "unmatched": "未分類",
    "excluded": "グラフ対象外",
}


@dataclass(frozen=True)
class CompositeCategoryTable:
    """4 キー → 統合カテゴリの写像表（読み込み済み・検証済み）。"""
    mapping: dict[tuple[str, ...], str]   # キー tuple（table_key_columns の順）-> 値（NFKC 済み）
    scope_values: frozenset[str]          # 第1キー（作業工程）の値集合
    table_key_columns: tuple[str, ...]    # 表側のキー列名（順序固定）
    n_rows: int                           # 重複除去後の行数
    n_exact_duplicates: int               # 除去した完全重複行数
    conflicts: dict[tuple[str, ...], tuple[str, ...]]   # 競合キー -> 候補値（昇順・採用値が先頭）


def load_composite_category_table(
    path: Path,
    *,
    key_columns: Sequence[str] = tuple(DEFAULT_CATEGORY_KEY_COLUMNS),
    value_column: str = DEFAULT_CATEGORY_VALUE_COLUMN,
    on_duplicate_key: str = "first",
) -> CompositeCategoryTable:
    """対比表 CSV を読み検証する。§2.1/§2.2。ログは出さない（結果は戻り値から判定できる）。"""


def apply_composite_category(
    df: pd.DataFrame,
    table: CompositeCategoryTable,
    *,
    source_key_columns: Sequence[str],          # repair 側の列名（table_key_columns と同順）
    excluded_values: Sequence[str] = DEFAULT_CATEGORY_EXCLUDED_VALUES,
    labels: Mapping[str, str] = DEFAULT_CATEGORY_LABELS,
) -> pd.Series:
    """§2.3 の規則で統合カテゴリ Series（df と同じ index / 全行非 NaN / dtype=object）を返す純粋関数。"""


def summarize_unmatched_keys(
    df: pd.DataFrame,
    category: pd.Series,
    *,
    source_key_columns: Sequence[str],
    labels: Mapping[str, str] = DEFAULT_CATEGORY_LABELS,
    vin_column: str | None = "vin",
) -> pd.DataFrame:
    """未一致（`未分類` / `対象外工程`）の 4 キー組合せ別サマリを返す純粋関数。

    列: 区分, {source_key_columns...}, n_rows, n_vin（vin_column が df に無ければ n_vin を出さない）。
    並び: 区分（未分類 → 対象外工程）→ n_rows 降順 → キー昇順（決定的）。
    """
```

実装メモ（coder 向け）:

- キー突合は **区切り文字 `"\x1f"` で連結した複合キー文字列 + `Series.map`** で行う。
  `merge` は左右の dtype（レイクは `string[pyarrow]`、CSV は `object`）差や行順の影響を受けやすく、
  `map` なら index と行順が保存され dtype に依存しない。
- 欠損キーは `pd.NA` を含む行として `map` が自然に未一致になる。`astype(str)` で `"nan"` を作らないこと
  （`astype("string")` を使い、欠損行は突合前にマスクする）。
- `yaml` は import しない（`category_integrate.py` の現行方針を維持）。

### 3.2 `src/defect_analysis/assemble.py`（追加）

```python
DEFAULT_REPAIR_CATEGORY_MAP = {
    "enabled": True,
    "path": "config/塗装課内不良対比表_まとめ.csv",
    "key_columns": {"作業工程": "入力工程", "大分類": "大分類", "中分類": "中分類", "小分類": "小分類"},
    "value_column": "グラフ項目",
    "output_column": "統合カテゴリ",
    "excluded_values": ["-"],
    "labels": {"out_of_scope_process": "対象外工程", "unmatched": "未分類", "excluded": "グラフ対象外"},
    "on_duplicate_key": "first",   # first | error
}


def add_integrated_category(df: pd.DataFrame, source: str, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """repair の 1 ソース分に統合カテゴリ列を付与する（config 解決・ログ出力を含む）。

    戻り値: (統合カテゴリ列を追加した df のコピー, 未一致サマリ DataFrame)
    表が無効/不在なら (df, 空 DataFrame) を返す（§4.3）。
    """
```

- config は `_repair_config(cfg)["category_map"]` を `DEFAULT_REPAIR_CATEGORY_MAP` と**サブ辞書単位でマージ**する
  （`_repair_config` は浅いマージなので、yaml 側に `category_map` を書くと既定辞書ごと置き換わる。
  `labels` はさらにその内側なので 2 段のマージが要る。`docs/real_data_repair_design.md` §2 と同じ落とし穴）。
- `path` は絶対ならそのまま、相対なら `cfg.root` 基準（`run_category_integration` と同じ解決）。

---

## 4. assemble への組み込み

### 4.1 呼び出し位置

`assemble()` の repair 分岐（現行 `elif source.kind == "repair":`）で、`prepare_repair_source` の**直前**に挿入する:

```python
elif source.kind == "repair":
    clean_df, unmatched = add_integrated_category(clean_df, source.name, cfg)
    if not unmatched.empty:
        unmatched.insert(0, "source", source.name)
        unmatched_parts.append(unmatched)
    prepared = prepare_repair_source(clean_df, source.name, cfg)
    ...
```

`prepare_repair_source` 自体は**変更しない**。`統合カテゴリ` は単なる 1 列として渡り、既存の
`category_columns` ループがそのまま `__n_統合カテゴリ` / `__top_統合カテゴリ` / カウント展開を作る。

ループ終了後、`_write_report(reports_dir, "repair_category_unmatched.csv", pd.concat(unmatched_parts))`
を出力する（repair ソースが無い / 未一致が無い場合もヘッダのみの空 DataFrame を出す）。

### 4.2 生成される列と列数

`P = repair_修正` として:

| 列 | 内容 | 0 埋め |
|---|---|---|
| `{P}__統合カテゴリ__{normalize_name(値)}` | カテゴリ別の修正件数（43 列。実データ実測） | する（`count_infixes` に `__統合カテゴリ__` が自動で入る） |
| `{P}__n_統合カテゴリ` | VIN あたりのカテゴリ種類数 | しない（NaN のまま） |
| `{P}__top_統合カテゴリ` | 最頻カテゴリ | しない（NaN のまま） |

- **不変条件**: 各 VIN について `Σ {P}__統合カテゴリ__* == {P}__count`（IC6。テストで固定する）。
- **パネル列数への影響**: 1,453 列 → 約 1,498 列（**+45 列 / +3.1%**）。
  すべて `repair_` 始まりなので `resolve_predictors` の説明変数には 1 列も入らない（結果列扱い）。
  目的変数として使いたい場合のみ `analysis.targets` に列名を明記する。
- 43 列のうち `虫付着` / `鉄粉外・内` / `変色/色ムラ` は全期間で 1 行しか無く、パネル上ではほぼ定数列になる。
  これは既存の定数列（194 列）と同じ扱いで EDA/ML 側の準定数フィルタが処理する。
  列を増やしたくない場合の逃げ道は `category_columns.統合カテゴリ: false`（`n_`/`top_` の 2 列だけになる）。
- `max_category_columns` は 100 に引き上げる（29 + 43 = 72 に対する余裕。`部位`(135 値) の誤展開は依然弾ける）。

### 4.3 表が無い / 無効なときの挙動

| 事象 | 挙動 |
|---|---|
| `category_map.enabled: false` | INFO 1 行を出して統合カテゴリを生成しない。他の repair 列は従来どおり |
| `path` のファイルが存在しない | **WARNING（解決後の絶対パス付き）＋ 統合カテゴリのみスキップ。assemble は継続** |
| 表の検証エラー（列不足・空セル） | `ValueError` で中断（設定/データの明確な誤り） |
| 4 キー重複（競合） | `on_duplicate_key` に従う（§2.2） |
| repair 側に `入力工程` 等のキー列が無い | WARNING ＋ 統合カテゴリのみスキップ |
| 展開列が `max_category_columns` 超過 | 既存どおり `ValueError`（`prepare_repair_source`） |

「ファイル不在は WARN + スキップ」にするのは、`Config` を直接組み立ててレイクを合成する既存テスト
（`tests/test_assemble.py` / `tests/test_real_ingest_smoke.py` は `root=tmp_path`）が、
リポジトリの対比表を参照できないため。表が無くても repair 集約は成立する独立機能であり、
ここで `ValueError` にすると合成 fixture のテスト経路がすべて落ちる。

### 4.4 ログ方針

`add_integrated_category` が出す（`[repair/{source}]` を先頭に付ける）:

- INFO: `統合カテゴリを付与: {n} 行 / {k} 種（一致 {m} 行 = {rate:.2%}）`
- INFO: `対象外工程（対比表に無い入力工程）: {n} 行 / {工程名=件数 …}` ← 表を育てる対象ではない（IC7）
- INFO: 完全重複行の除去件数（> 0 のとき）
- WARNING: `対比表に無い組合せ: {u} 種 / {n} 行を「未分類」にしました: {上位20件}`（20 超は「他 N 種」で省略）
- WARNING: 4 キー競合（採用値 / 不採用値）
- WARNING: 表が見つからない場合（§4.3）

### 4.5 レポート `reports/repair_category_unmatched.csv`

| 列 | 例 |
|---|---|
| `source` | 修正 |
| `区分` | 未分類 / 対象外工程 |
| `入力工程` / `大分類` / `中分類` / `小分類` | 4 キーの実値 |
| `n_rows` | 12 |
| `n_vin` | 11 |

並び順は §3.1 のとおり決定的。`ingest_quality.csv` は列を増やさない（未一致情報は本レポートに集約する）。

---

## 5. config 追記（`config/config.yaml`）

`real_ingest.repair` に以下を**追加/変更**する。他キーは変更しない。

```yaml
  repair:
    time_column: 修正日時
    production_time_column: PB_ON
    workload_column: 修正工数
    category_columns:                # 列 -> カウント展開するか
      大分類: true
      中分類: false
      小分類: false
      部品名: false
      修正内容: false
      部位: false
      統合カテゴリ: true             # ★追加: 対比表のグラフ項目（43 列。docs/repair_integrated_category_design.md）
    worker_column: 修正員_id
    max_category_columns: 100        # ★30 -> 100（大分類 29 + 統合カテゴリ 43 = 72 に対する余裕）

    # ★追加: 塗装課内不良対比表による統合カテゴリ付与（4キー厳密一致。IC4）
    category_map:
      enabled: true
      path: config/塗装課内不良対比表_まとめ.csv   # cfg.root 基準の相対パス
      key_columns:                   # 対比表の列 -> repair の列（この順で完全一致。フォールバック無し）
        作業工程: 入力工程
        大分類: 大分類
        中分類: 中分類
        小分類: 小分類
      value_column: グラフ項目
      output_column: 統合カテゴリ
      excluded_values: ["-"]         # 表が「グラフ集計対象外」を明示する値
      labels:                        # 未一致行に必ず入る値（NaN を作らない。IC6）
        out_of_scope_process: 対象外工程   # 入力工程が対比表に無い（塗装課以外の工程）
        unmatched: 未分類                  # 対象工程だが4キーの組合せが表に無い（表の育て漏れ）
        excluded: グラフ対象外             # グラフ項目が "-"
      on_duplicate_key: first        # first（グラフ項目の昇順先頭を採用＋WARN） | error（即停止）
```

同じ既定値を `assemble.py` の `DEFAULT_REPAIR` / `DEFAULT_REPAIR_CATEGORY_MAP` にも置く
（`config.yaml` を読まないテストがあるため。`docs/real_data_repair_design.md` §2 と同じ理由）。

---

## 6. 既存挙動の副次的な修正

**`_top_value` のタイブレークを決定的にする**（`assemble.py`）。現行は `value_counts()` の順（同数タイは
出現順＝レイクの読み取り順）に依存する。統合カテゴリでは「1 VIN に 1 件ずつ別カテゴリ」が普通に起き、
`__top_統合カテゴリ` が環境やファイル読み順で変わりうる。`(件数降順, 値昇順)` で先頭を採る 2 行の変更に留める。
既存 `__top_大分類` の値が変わる可能性があるため、既存テストが緑であることを完了条件に含める。

---

## 7. 既存ドキュメントとの矛盾と、その修正箇所

1. **`docs/category_csv_and_custom_charts_design.md` C9 と現状の矛盾**:
   「`config/category_map.yaml` は削除する」と決定済みだが、実ファイル（175KB / 7,349 行）が
   再びリポジトリに存在する。生成元 `scripts/_gen_category_map.py` は存在せず再生成不能で、
   コードからの読み手も 0（`grep` 実測）。→ **IC2 で削除を再確定**。C9 の記述自体は正しいので変更不要。
2. **C4（未一致は元の値を通す）の適用範囲**: これは 1 キー写像の規約であり、複合キーには適用できない
   （元の値が一意でない）。→ 本書 IC6 で上書きする。`category_csv_and_custom_charts_design.md` の
   §1.2 冒頭に「本節は 1 キー写像（CLI `category`）の仕様。repair の 4 キー写像は
   `docs/repair_integrated_category_design.md` を参照」の 1 行を追記する。
3. **`docs/real_data_repair_design.md` §1-6 の実測値が古い**: `大分類` は「10 値」と書かれているが
   現行レイク（8,570 行）では **29 値**。設計判断（`max_category_columns`）に直接効くので、
   同節に「（514 行時点。8,570 行では 29 値。`docs/repair_integrated_category_design.md` §1-5）」を追記する。
4. **`docs/real_data_repair_design.md` §2 の「カウント展開の既定を `大分類` だけにした根拠」**:
   本書で `統合カテゴリ` が加わるため、同節に「統合カテゴリ（対比表のグラフ項目）を追加。
   詳細は `docs/repair_integrated_category_design.md`」の 1 行を追記する。
5. **`docs/real_data_repair_design.md` §9-4（未回答のユーザー確認事項）**: 本書が回答なので
   「【確定・2026-08-28】対比表の『グラフ項目』を統合カテゴリとして採用。
   `docs/repair_integrated_category_design.md` 参照」に書き換える。

---

## 8. 実装タスク分解（coder 用）

実行は `.venv/bin/python`。追加インストール禁止。着手前に対象ファイルの最新状態を読むこと。

### IT1: `category_integrate.py` に 4 キー API を追加

- 対象: `src/defect_analysis/category_integrate.py`
- 内容: §3.1 の定数・`CompositeCategoryTable`・`load_composite_category_table` /
  `apply_composite_category` / `summarize_unmatched_keys` を追加。§2.1/§2.2/§2.3 の規則を実装。
  モジュール docstring に「1 キー写像（CLI）と 4 キー写像（repair）の 2 系統がある」ことを 2 行で追記。
- **既存の `load_mapping` / `apply_category_mapping` / `run_category_integration` は 1 文字も変えない。**
- 完了条件:
  - 3 関数がログを出さない純粋関数であること（`import logging` の既存 logger を新関数から呼ばない）。
  - `apply_composite_category` の戻り値に NaN が 1 つも無い。
  - 実表を読んで `n_exact_duplicates == 1` / `len(conflicts) == 2` / `len(mapping) == 663` になる。
  - `yaml` を import していない。

### IT2: `assemble.py` への組み込み

- 対象: `src/defect_analysis/assemble.py`
- 内容:
  1. `DEFAULT_REPAIR_CATEGORY_MAP` を追加し、`DEFAULT_REPAIR` の `category_columns` に
     `"統合カテゴリ": True` を追加、`max_category_columns` を 100 に変更。
  2. `add_integrated_category()` を §3.2 の仕様で実装（config の 2 段マージ・パス解決・§4.4 のログ）。
  3. `assemble()` の repair 分岐に §4.1 の 4 行を挿入し、
     ループ後に `reports/repair_category_unmatched.csv` を出力。
  4. `_top_value` のタイブレークを `(件数降順, 値昇順)` に変更（§6）。
- **`prepare_repair_source` のシグネチャ・既存出力列は変更しない。**
- 完了条件:
  - `.venv/bin/python main.py assemble` が完走し、`repair_修正__統合カテゴリ__*` が 43 列出る。
  - 各 VIN で `Σ 統合カテゴリ列 == repair_修正__count`。
  - パネル列数が 1,453 → 1,498 前後（+45）。行数は変わらない。
  - `reports/repair_category_unmatched.csv` に `未分類` 53 行 / `対象外工程` 491 行が集計される。
  - 既存 205 テストが緑。

### IT3: `config/config.yaml`

- 内容: §5 の差分（`統合カテゴリ: true` / `max_category_columns: 100` / `category_map` ブロック）。
- 完了条件: 既存キーの差分がこの 3 点のみ。`yaml.safe_load` が通る。

### IT4: 死んだ生成物の削除

- 対象: `config/category_map.yaml`（`git rm`）、`CHANGELOG.md`（追記）
- 内容: IC2。CHANGELOG に「repair 統合カテゴリ（対比表 4 キー）対応」と
  「`config/category_map.yaml` を再削除（生成スクリプト不在の死蔵ファイル）」を追記。
- 完了条件: `grep -rn "category_map.yaml" --include=*.py --include=*.yaml .` が 0 件
  （CHANGELOG / 設計書の歴史的記述は除く）。

### IT5: ドキュメント更新

- 対象: `README.md`、`docs/category_csv_and_custom_charts_design.md`、`docs/real_data_repair_design.md`
- 内容:
  - README: ディレクトリ構成に `config/塗装課内不良対比表_まとめ.csv`（repair の統合カテゴリ変換表）を追加。
    「実データ経路」の repair 段落に、4 キー厳密一致・3 ラベル・未一致レポートの育て方（対比表に行を足す）を
    5 行程度で追記。`config/category_map.csv`（CLI `category` 用の 1 キー表）との**使い分けを 1 行で明記**。
    成果物表に `reports/repair_category_unmatched.csv` を追加。
  - 既存 2 設計書には §7 の 1 行追記のみ（本文の書き換えはしない）。
- 完了条件: README の記述が実ファイルと一致し、2 つのカテゴリ表の用途が読んで区別できる。

実装順序: IT1 → IT2 → IT3 →（実データで完了条件を確認）→ IT4 → IT5。

---

## 9. テスト観点（tester 用）

小さな自作 fixture を基本とし、実データは既存 smoke の拡張 1 本のみ。テスト名は挙動が読める英文にする。

### 9.1 `load_composite_category_table`（新規 `tests/test_category_table.py`）

- 4 キーと値の dict が読める / `作業工程` の値集合が scope として得られる。
- キー列名が違う CSV で `ValueError`（メッセージに実在列が出る）。
- 値が空文字の行がある CSV で `ValueError`。
- 5 列すべて同一の重複行が除去され、`n_exact_duplicates` に数えられる（`conflicts` は空）。
- 4 キー競合 + `on_duplicate_key="error"` で `ValueError`。
- 4 キー競合 + `on_duplicate_key="first"` で**グラフ項目の昇順先頭**が採用され `conflicts` に記録される。
- **行の並びを逆にした同内容の CSV でも採用値が変わらない**（IC9 の決定性の回帰テスト）。
- 全半角ゆれ（`電着2次タレ` / `電着２次タレ`）が NFKC で同一値に畳まれる（IC5）。
- ファイル不在で `FileNotFoundError`。

### 9.2 `apply_composite_category` / `summarize_unmatched_keys`

- 4 キー一致で表の値が入る。
- **3 キー（大中小）が一致していても入力工程が違えば一致しない**（フォールバック無しの回帰テスト。IC4 の核心）。
- 対象工程集合に無い入力工程 → `対象外工程`。
- 対象工程だが組合せが無い → `未分類`。
- 表の値が `-` → `グラフ対象外`。
- 入力工程が欠損の行 → `対象外工程`（例外を投げない）。
- 出力 Series に NaN が 1 つも無く、index が入力と一致する。
- 前後に空白がある値が一致する（strip のみ。全角/半角違いは一致しない＝ IC4 の非正規化を固定する）。
- `summarize_unmatched_keys` が区分・キー別に `n_rows` / `n_vin` を集計し、
  区分 → 件数降順 → キー昇順で決定的に並ぶ。一致のみの入力では空 DataFrame（列は定義どおり）。

### 9.3 `add_integrated_category` + assemble 統合（`tests/test_assemble.py` に追加）

- 対比表 fixture（tmp に CSV を書く）を指す config で、`統合カテゴリ` 列が付き
  `repair_修正__統合カテゴリ__{値}` に展開される。
- **各 VIN で統合カテゴリのカウント列合計が `repair_修正__count` と一致する**（IC6 の不変条件）。
- 生成列がすべて `repair_` で始まる。
- `enabled: false` で統合カテゴリ列が生成されず、他の repair 列は従来どおり。
- `path` のファイルが無いとき WARNING が出て assemble が完走し、他の repair 列は従来どおり
  （`assertLogs` を使用）。
- 修正が無い VIN で統合カテゴリのカウント列が 0 埋めされ、`__n_統合カテゴリ` / `__top_統合カテゴリ` は NaN のまま。
- `max_category_columns` を小さくした config で `ValueError`。
- `reports/repair_category_unmatched.csv` が出力され、未一致組合せの行数・VIN 数が手計算と一致する。
  未一致ゼロでもファイルが存在しヘッダを持つ。
- `resolve_predictors` に統合カテゴリ列を含むパネルを渡し、説明変数に 1 列も残らない
  （`tests/test_predictors.py` に 1 本追加）。

### 9.4 `_top_value` の決定性

- 同数タイのとき値の昇順で先頭が返る（入力行順を入れ替えても結果が同じ）。

### 9.5 実データ smoke（`tests/test_real_ingest_smoke.py` を拡張）

- config に `real_ingest.repair.category_map.path` を
  `str(PROJECT_ROOT / "config" / "塗装課内不良対比表_まとめ.csv")`（絶対パス）で明示する
  （smoke は `root=tmp_path` のため相対既定では表を見つけられない）。
- 追加アサーション（データ更新で壊れないよう緩めに置く）:
  - `repair_修正__統合カテゴリ__` で始まる列が 30 列以上ある。
  - 全 VIN で統合カテゴリ列の合計 == `repair_修正__count`。
  - `repair_修正__統合カテゴリ__未分類` 列が存在する（ラベル化の回帰テスト）。
  - `reports/repair_category_unmatched.csv` が存在する。
- 既存アサーション（`n_vin > 1000` / trend 列 / レポート 5 種）は維持。

### 9.6 回帰条件

- 既存 205 テストが**無改修で**緑。特に `tests/test_transforms.py`（1 キー方式の 14 ケース）と
  `tests/test_assemble.py::prepare_repair_source` 系のシグネチャ・出力列。
- CLI `category` サブコマンドの引数・挙動が変わらない。

---

## 10. 非目標（今回やらないこと）

- 3 キー（大中小）フォールバックや部分一致・あいまい一致の実装。
- CLI `category` の複合キー対応、`config/category_map.csv` の廃止。
- 対比表そのものの内容修正（競合 2 件・`-` 行の扱いはユーザーの業務判断。§11）。
- 統合カテゴリを convert 段階でレイクに焼くこと（IC10）。
- 統合カテゴリを説明変数に使うための仕組み（結果列であり原理的に使えない）。
- `analysis.targets` / EDA 図の追加設定（必要になれば `analysis.custom_charts` で宣言的に足せる）。

---

## 11. ユーザー確認事項

1. **対比表の 4 キー競合 2 件を統一しますか。** 現状は `on_duplicate_key: first` によりグラフ項目の
   昇順先頭（`Ｎ完修(塗装)…治具当たり` → `キズ` / `Nト焼付修正…治具当たり` → `キズ （触り・当り）`）が
   採用され、毎回 WARNING が出ます。影響する repair 行は全体で **1 行**。
   他 1 工程が同じキーに `当りキズ` を割り当てていることから、**3 工程とも `当りキズ` に統一する**のが
   自然と考えますが、業務判断のため表の修正はしていません。
2. **`グラフ対象外`（グラフ項目 `-`。134 行）と `対象外工程`（491 行）を分けたままでよいですか。**
   前者は「表が明示的にグラフ集計対象外とした行（完成課責任）」、後者は「対比表が扱わない 7 工程」です。
   同じ扱いでよければ両ラベルに同じ文字列を設定すれば 1 列に畳めます。
3. **`未分類` 53 行（51 VIN）を対比表に追加しますか。** `reports/repair_category_unmatched.csv` に
   4 キーと件数を出力するので、そのまま表に追記できます。追記後は `assemble` の再実行だけで反映されます
   （レイクの再変換は不要）。
4. **統合カテゴリを目的変数に使いますか。** 使う場合は `analysis.targets` に
   `repair_修正__統合カテゴリ__上塗ブツ` のような列名（または `repair_修正__top_統合カテゴリ` を
   多クラス分類の目的変数として扱う対応）が必要です。現状の targets（`has_repair_record` /
   `repair_修正__count`）は変更していません。
