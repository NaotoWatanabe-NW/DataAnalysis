# repair（修正実績）ソース対応 設計追補（2026-07-31）

`docs/real_data_ingest_design.md` の **D8「repair は optional（ディレクトリが空）」を破棄**し、
`data/raw/repair/defect.csv`（198KB / 514行 / 36列 / cp932）を取り込むための追補。
本書は本体設計を置き換えるものではなく、**差分だけ**を定義する。用語・モジュール構成・責務境界は本体設計に従う。

---

## 0. 結論（決定事項と根拠）

| # | 決定 | 根拠 |
|---|---|---|
| R1 | **エンコーディングは「kind 単位の config 明示」を正とし、ヘッダ読取時のみ自動フォールバックを許す。`RawSource` に `encoding` フィールドを持たせ、変換もレポート出力もこれを使う** | 現状 `discover_sources` / `_write_column_name_mapping` / `convert_file` の3箇所が `real_ingest.convert.encoding` 単一値を参照しており、cp932 ファイルはヘッダ読取の時点で `UnicodeDecodeError` → ソースごと ERROR スキップになる（実測）。解決は「ソースが自分のエンコーディングを持つ」に一本化するのが最小 |
| R2 | **アポストロフィ接頭辞 `'` の除去は `repair` kind 限定の convert 前処理（`strip_apostrophe`）。`vin_key.normalize_vin` は変更しない** | `'` は当該エクスポート固有の Excel 文字列保護であり VIN の一般規則ではない。`vin_key` に入れると他ソースの異常値を黙って握り潰し、確定済みの 148 本のテスト契約も変わる。kind スコープなら他ソースへの副作用がゼロで、`VIN` 以外（`ライン` / `AB共通No` / `不良No`）も同時に片付く |
| R3 | **`date` パーティションのアンカーは `PB_ON`（塗装ライン投入時刻）。`修正日` は使わない** | 実測: repair と traceability で一致した 148 VIN について `PB_ON − ブース入口通過` の中央値 **−5.20h**（q10 −6.55h / q90 −4.50h）と分布が狭い。一方 `修正日時 − ブース入口通過` は中央値 **+109.9h**。パーティションは「その車体がいつ生産されたか」を表すべきで、修正日で切ると 07/24 生産分の修正が 07/29 パーティションに入り、`--date-from/--date-to` で traceability と永久にすれ違う |
| R4 | **repair 由来の列は必ず `repair_` で始める。`analysis.leakage_prefixes` は変更不要** | `config/config.yaml:148` に **`repair` は既に含まれている**（実測）。`defect_` に寄せる必要はなく、むしろブツ検 defect と区別できなくなる |
| R5 | **`repair` kind のソースは VIN 台帳（和集合）に加えない。既存台帳への left join のみ** | repair は説明変数を1つも供給しない結果情報。台帳に加えると traceability に存在しない車体（07/23・07/29 生産分など）で「全特徴量 NaN・目的変数だけ有り」の行が増え、欠損率レポートと下流のモデリングを汚す。マッチしなかった件数は品質レポートに記録して可視化する |
| R6 | **`修正員`（個人名）は convert 段階でハッシュ化する（既定 `mode: hash`）。レイクにも生の氏名を残さない** | 「convert は情報を捨てない」原則の**唯一の例外**として明記する。`data/lake/` は長期保存され `reports/vin_panel_dictionary.csv` の `example` 列にも値が出るため、入口で落とすのが安全側。作業者要因の分析は ID（ハッシュ）で可能なので分析価値はほぼ失われない。既定の是非はユーザー確認事項 §9-1 |
| R7 | **ソース名の衝突を `real_ingest.source_aliases` で解決する。既定 `{"repair/defect": "修正"}`** | ファイル名が `defect.csv` のため `source_key` は `defect` になる（実測）。ソース名はレイクのディレクトリ名・列接頭辞・`assemble` の `frames` キーの3役を兼ねており、kind をまたいだ同名は設計上の穴。alias は一般機構としてこれを塞ぐ（他ソースに副作用なし） |
| R8 | **repair と defect（ブツ検）は紐付けない。結合キーは VIN のみ** | ブツ検の列は `VIN#, 入口 通過日時, 塗色, 不良ｻｲｽﾞ, 検査箇所, 検査箇所X/Y, 車種, 検査部位, 不良種類` の9列のみで、`不良No` に対応する列が存在しない（実測）。`不良No` は repair 内で 514/514 ユニークな行 ID にすぎない |

---

## 1. 実測事実（本書の根拠。全行 514 行を読んで確認済み）

`data/raw/repair/defect.csv` は 1 ファイル 514 行しかないため全行読んでいる（1年分でも同規模なら問題ないが、
実装側は他ソースと同じくチャンク読みにする）。

1. **エンコーディング `cp932`**。`utf-8-sig` で `read_csv(nrows=0)` すると
   `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x8f in position 0`。
2. **VIN**: 全 514 行が `'` 接頭辞。除去・全空白除去後の長さは 12 文字（型式ベース）457 行 / 17 文字（フル VIN）57 行。
   **末尾英小文字サフィックス（`a`〜`c`）は 1 件も無い**（514 行すべて）。→ `vin_key` の分解規則をそのまま通しても
   `vin_pass_no` は全件 1 になり、副作用は無い。**coder が再確認する必要はない。**
3. **VIN の他ソースとの重複**（全 383 ユニーク VIN に対して）:
   ブース 148（38.6%）/ 上塗ロボット 148（38.6%）/ 上塗ブツ検 87（22.7%）/ 電着ブツ検 180（47.0%）。
   → VIN 表記の正規化は `'` 除去＋全空白除去で**実際に結合できる**ことが確認済み。
4. **アポストロフィ接頭辞が付く列**: `VIN` / `ライン` / `AB共通No` / `不良No`（いずれも非欠測 100%）。他の列には付かない。
5. **日時列**:
   - `修正日` = `%Y/%m/%d`（全 514 行 `2026/07/29`）、`修正時間` = `%H:%M:%S`。
   - `WB_ON,WB_OK,PB_ON,PB_OK,AB_ON,EG_OK,AB_OK,FC_OK` = `%Y%m%d %H%M%S`。
     欠測番兵 `00000000 000000` の出現率: WB_ON **100%** / WB_OK 1.0% / PB_ON 1.8% / PB_OK 2.9% /
     AB_ON 5.1% / EG_OK 24.3% / AB_OK 29.2% / FC_OK 32.5%。番兵を除けばパース失敗 0%。
   - `PB_ON` の日付分布: 2026-07-23 が 7 行 / **07-24 が 321 行** / 07-29 が 177 行 / NaT 9 行。
6. **カテゴリ列のカーディナリティ**（514 行時点。8,570 行では `大分類` は 29 値。
   `docs/repair_integrated_category_design.md` §1-5）:
   `部位` 135 / `中分類` 42 / `色` 40 / `部品名` 39 / `修正員` 30 / `ライン` 24 / `小分類` 19 /
   `大分類` 10 / `入力工程` 10 / `修正内容` 10 / `機種` 6 / `責任課` 6 / `不良原因` 4 / `発見工程` 4 /
   `内外` 3 / `シフト` 2 / `左右` 2 / `責任シフト` 2。
7. **`修正工数` は全 514 行が `0`**（`to_numeric` 失敗率 0%）。この期間では分散ゼロで特徴量にならない。
8. **欠測率が高い列**: `ミッションNo` 100% / `不具合申告` 98.6% / `不良原因` 98.6% / `備考` 97.9% /
   `発見工程` 59.5% / `エンジンNo` 32.5%。
9. **粒度**: 514 行 / 383 ユニーク VIN（最大 5 行/VIN）。`不良No` は 514/514 ユニーク（行 ID）。
   `AB共通No` は 321 ユニーク。
10. ブツ検（defect）側に `不良No` に相当する列は無い（→ R8）。

---

## 2. config 追補（`config/config.yaml`）

既存キーは変更しない。以下を `real_ingest:` 配下に**追加**する。
**`config.yaml` を読まずに `Config` を直接組み立てるテスト（`tests/test_real_ingest_smoke.py`）があるため、
同じ既定値をコード側の `DEFAULT_*` 辞書にも必ず置くこと**（`_convert_config` 等は浅いマージなので、
yaml 側に `by_kind` を書かなければコード既定が生き残る）。

```yaml
real_ingest:
  source_aliases:                    # "{kind}/{source_key}" -> 別名（レイクのディレクトリ名・列接頭辞に使う）
    repair/defect: 修正

  convert:
    encoding: utf-8-sig              # 既定（変更しない）
    encoding_fallbacks: ["cp932"]    # ヘッダ読取が UnicodeDecodeError のとき順に試す
    by_kind:                         # kind 単位の上書き（書かれていない kind は従来どおり）
      repair:
        encoding: cp932
        time_column: PB_ON           # date パーティションのアンカー（_resolve_time_column より優先）
        strip_apostrophe: true       # 全 object 列の先頭 "'" を1文字だけ除去
        na_values: ["00000000 000000"]   # 日時パース前に NA へ置換する番兵
        combine_datetime:            # 日付列＋時刻列 → 新規日時列
          - {name: 修正日時, date_column: 修正日, time_column: 修正時間, format: "%Y/%m/%d %H:%M:%S"}
        datetime_columns:            # datetime_column_keywords で拾えない日時列
          - columns: [WB_ON, WB_OK, PB_ON, PB_OK, AB_ON, EG_OK, AB_OK, FC_OK]
            format: "%Y%m%d %H%M%S"
        pii:
          columns: [修正員]
          mode: hash                 # hash | drop | keep
          salt: ""                   # 空なら環境変数 DEFECT_ANALYSIS_PII_SALT、それも無ければソルト無し
          suffix: _id                # 修正員 -> 修正員_id

  repair:                            # assemble 側（VIN 集約）の設定
    time_column: 修正日時            # first_ts / last_ts の元
    production_time_column: PB_ON
    workload_column: 修正工数
    category_columns:                # 列 -> カウント展開するか
      大分類: true
      中分類: false
      小分類: false
      部品名: false
      修正内容: false
      部位: false
    worker_column: 修正員_id         # pii.suffix 適用後の名前
    max_category_columns: 30         # 展開後カウント列の合計上限。超過で ValueError
```

**カウント展開の既定を `大分類` だけにした根拠**: `大分類` は 10 値（上塗り/修正/電着修正/シーラー…）で
工程レベルの粗い分類。`中分類`(42)・`部品名`(39)・`部位`(135) は 1年分でさらに増え、
383 VIN しかない結果側の情報に対して列が明らかに過剰。`max_category_columns: 30` は
「気付かずに数百列作る」ことへのガード（本体設計 `max_columns_per_source` と同じ思想）。

統合カテゴリ（対比表のグラフ項目）を追加。詳細は `docs/repair_integrated_category_design.md`。

---

## 3. 実装仕様

### 3.1 `raw_sources.py`

```python
@dataclass(frozen=True)
class RawSource:
    ...                    # 既存フィールドは順序も含めて変更しない
    encoding: str = "utf-8-sig"     # ★追加（既定値付きで末尾に追加する）

def resolve_encoding(kind: str, cfg: Config) -> str:
    """real_ingest.convert.by_kind.{kind}.encoding があればそれ、無ければ convert.encoding。"""

def probe_header_with_encoding(path: Path, encoding: str, fallbacks: Sequence[str]) -> tuple[list[str], str]:
    """encoding で試し、UnicodeDecodeError なら fallbacks を順に試す。
       採用したエンコーディングを WARN ログに出す。全滅なら最後の例外を再送出する。
       戻り値: (ヘッダ列名リスト, 実際に使ったエンコーディング)"""
```

- `probe_header(path, encoding) -> list[str]` は**シグネチャを変えず残す**（既存呼び出しの互換のため）。
- `_build_source(...)` に `cfg` を渡し、以下の順で決める:
  1. `encoding = resolve_encoding(kind, cfg)` → `probe_header_with_encoding` で確定（フォールバックした場合は
     その値を `RawSource.encoding` に採用する）。
  2. ソース名: `name = cfg.get("real_ingest.source_aliases", {}).get(f"{kind}/{source_key}", source_key)`。
     alias 適用後の名前を `RawSource.name` とする（レイクのパス・列接頭辞・frames キーがすべてこれに従う）。
  3. `time_column`: `by_kind.{kind}.time_column` があれば**それを最優先**。無ければ従来の `_resolve_time_column`。
     指定列が正規化後ヘッダに存在しなければ ERROR ログでソースをスキップ。
- `_check_header_consistency` にも同じ encoding を渡す。

**実データでの期待値**: `RawSource(kind="repair", name="修正", vin_column="VIN", time_column="PB_ON",
encoding="cp932", files=[data/raw/repair/defect.csv])`。

### 3.2 `raw_convert.py`

- `convert_file` / `_write_column_name_mapping` は `real_ingest.convert.encoding` ではなく
  **`source.encoding` を使う**（現状 `_write_column_name_mapping(cfg, sources, encoding)` が
  グローバル encoding 固定で、ここでも cp932 が読めない）。引数からグローバル encoding を落とす。
- `convert_file` のチャンク処理に、列名正規化の直後・日時パースの前に以下を挿入する
  （すべて `by_kind.{source.kind}` が無ければ**完全に no-op**）:

  1. **アポストロフィ除去**（`strip_apostrophe: true` のとき）
     対象は object / string dtype の全列。`s.str.replace(r"^'", "", regex=True)`（先頭1個だけ）。
     `nrows` を絞った実測で対象は4列（`VIN`/`ライン`/`AB共通No`/`不良No`）だが、列名を列挙せず
     「kind 内の全文字列列」に適用する。理由: 1年分で新しい列に `'` が付く可能性があり、
     kind スコープなら他ソースへの副作用が原理的に無いため列指定で守る必要がない。
  2. **番兵の NA 化**（`na_values`）: 対象は `datetime_columns` に列挙された列のみ。
     `chunk[c] = chunk[c].replace(na_values, pd.NA)`。
     **`errors="coerce"` に任せず明示的に NA にする**理由: `00000000 000000` は coerce でも NaT になるが、
     既存の「日時パース失敗率 > 1% で WARN」判定に引っかかり、WB_ON では 100% 失敗の WARN が毎回出て
     本物の異常を見落とすため。
  3. **日時結合**（`combine_datetime`）: `pd.to_datetime(date_col.astype(str) + " " + time_col.astype(str),
     format=..., errors="coerce")` を新列 `name` として追加。元の `修正日` / `修正時間` は**残す**
     （convert は情報を捨てない）。どちらかが欠測の行は NaT。
  4. **追加日時列**（`datetime_columns`）: 指定 format で `errors="coerce"` パース。
     既存の keyword ベースのパース（`通過日時` / `DATETIME`）とは**排他**にする
     （repair はどちらのキーワードにも当たらないので実際には競合しない）。
     失敗率の WARN 判定は既存ロジックを流用する。
  5. **PII ハッシュ**（`pii`）:
     - `mode: hash` → `hashlib.sha256((salt + str(v)).encode("utf-8")).hexdigest()[:12]` を
       新列 `{col}{suffix}` に入れ、**元列 `{col}` を drop する**。欠測は欠測のまま。
     - `mode: drop` → 元列を drop するだけ。
     - `mode: keep` → 何もしない（WARN「個人情報列をそのまま保存します: 修正員」を出す）。
     - salt は `pii.salt` → 環境変数 `DEFECT_ANALYSIS_PII_SALT` → `""` の順で解決。
       salt を変えると過去のレイクと ID が変わるため、変更時は `--force` 再変換が必要である旨をログに出す。
- 処理順の全体像（既存 + 追加）:
  列名正規化 → **①アポストロフィ除去 → ②番兵 NA → ③日時結合 → ④追加日時列 → ⑤PII** →
  既存の keyword 日時パース → VIN 派生列 → float ダウンキャスト → `date` 算出 → 書き出し。
- `date` は `source.time_column`（repair は `PB_ON`）から算出される。既存コードのまま動く。
  実データでの期待パーティション: `date=2026-07-23`(7) / `date=2026-07-24`(321) /
  `date=2026-07-29`(177) / `date=unknown`(9)。

### 3.3 `assemble.py`

1. `assemble()` のソースループの条件を
   `if source.kind not in ("traceability", "defect", "repair"): continue` に広げる。
2. **新規関数**

```python
def prepare_repair_source(df: pd.DataFrame, source: str, cfg: Config) -> pd.DataFrame:
    """repair ソースを VIN 単位に集約する。列は必ず `repair_` で始める（R4）。
       出力は下表のホワイトリスト列のみ（raw 列を機械的に集約しない）。"""
```

   `P = f"repair_{source}"`（実データでは `repair_修正`）。出力列:

   | 列 | 内容 |
   |---|---|
   | `{P}__count` | 修正件数（行数） |
   | `{P}__has` | 1（存在フラグ。結合後 0 埋め） |
   | `{P}__first_ts` / `__last_ts` | `修正日時` の min / max |
   | `{P}__PB_ON__min` | `PB_ON`（塗装投入時刻）の min |
   | `{P}__lead_time_h` | `(first_ts − PB_ON__min)` の時間。両方非 NaT の行のみ |
   | `{P}__工数_sum` / `__max` | `修正工数` を `to_numeric(errors="coerce")` した合計 / 最大 |
   | `{P}__n_大分類` / `__top_大分類` | `大分類` の nunique / 最頻値 |
   | `{P}__大分類__{正規化値}` | `category_columns.大分類 == true` のときカウント展開 |
   | `{P}__{列}__{正規化値}` | `category_columns` で true にした他の列も同形式 |
   | `{P}__修正員_id__nunique` | 関与した作業者数（`worker_column` があるときのみ） |

   - カテゴリ値は既存 defect と同じく `normalize_name(str(v))` を通してから `crosstab`。
   - 展開後のカウント列合計が `max_category_columns` を超えたら
     `ValueError(f"[repair/{source}] カウント展開列が上限を超えました: {n} > {limit}")` で中断。
   - 生成したカテゴリ列の一覧は `ingest_quality.csv` の `categories` に `;` 区切りで記録する
     （defect と同じ扱い）。
   - `修正工数` が全 0 のときは WARN
     「修正工数が全て 0 です。この期間では特徴量になりません」を出す（実データでは必ず出る）。
   - 使わない列（`備考`/`エンジンNo`/`ミッションNo`/`AB共通No`/`不良No`/`色`/`機種`/`責任課` 等）は
     単に無視する。drop リストは持たない（ホワイトリスト方式のため不要）。

3. **台帳（R5）**: `build_vin_ledger` の**シグネチャは変えない**。呼び出し側で
   `ledger = build_vin_ledger({k: v for k, v in frames.items() if k not in repair_source_names})` とする。
   横結合ループは従来どおり `frames` 全体（repair を含む）を回す。
4. `_zero_fill_after_merge(base, before_cols, name)` を **prefix 対応**にする。
   現状 `prefix = f"defect_{name}"` 固定。`for prefix in (f"defect_{name}", f"repair_{name}")` に広げるか、
   引数で prefix を受け取る（どちらでもよいが private 関数なので既存テストへの影響なし）。
   0 埋め対象: `__has` / `__count` / `__{任意}__` を含むカウント列。
   **`__first_ts` / `__last_ts` / `__PB_ON__min` / `__lead_time_h` / `__工数_*` は 0 埋めしない**（NaN のまま）。
   → 「修正が無かった」と「工数が 0 だった」を混同しないため。
5. `_infer_column_source` に分岐を追加:
   ```python
   if col.startswith("repair_"):
       rest = col[len("repair_"):]
       return f"repair_{rest.split('__')[0]}"
   ```
6. `ingest_quality.csv` に repair 行を追加し、**新規列 `n_vin_not_in_ledger`** を持たせる
   （台帳に存在しなかった repair VIN 数。実データでは 383 − 台帳との積集合）。
   他 kind の行は空欄で構わない。

---

## 4. 期待される実データ結果（完了条件の数値）

| 項目 | 期待値 |
|---|---|
| `discover_sources` の repair | 1 ソース。`name="修正"`, `encoding="cp932"`, `time_column="PB_ON"`, `vin_column="VIN"` |
| レイク | `data/lake/repair/修正/date=2026-07-23|24|29/` と `date=unknown/`、合計 514 行 |
| `修正員` | レイクに `修正員` 列が存在せず `修正員_id` が存在する（既定 `mode: hash`） |
| `VIN` 由来 | `vin` の先頭が `'` でない（例 `HE93S-122065`）、`vin_pass_no` が全件 1 |
| パネル | `repair_修正__count` 等が生成され、**すべて `repair_` で始まる** |
| パネル行数 | repair によって増えない（R5。1300 行前後のまま） |
| 結合 | `merge(validate="one_to_one")` を通る |
| リーク除外 | `resolve_predictors` の説明変数に `repair_*` が 1 列も含まれない（`leakage_prefixes` の `repair` が効く） |

---

## 5. 実装タスク分解（coder 用）

**前提**: `src/` と `config/config.yaml` は他エージェントが並行編集中。着手前に最新状態を読み直すこと。
実行は `.venv/bin/python`。追加インストール不可。

### RT1: `raw_sources.py` — エンコーディングと alias、time_column 上書き
- 対象: `src/defect_analysis/raw_sources.py`
- 内容: §3.1（`RawSource.encoding` 追加 / `resolve_encoding` / `probe_header_with_encoding` /
  `source_aliases` / `by_kind.{kind}.time_column`）。
- 完了条件:
  - 実 `data/raw/` に対して repair が 1 ソース返り、§4 の1行目と一致する。
  - `RawSource` を既存の位置引数・キーワード引数で構築している既存テスト
    （`tests/test_raw_convert.py::_make_source`）が**無改修で通る**。
  - cp932 の CSV を fallback だけで（config 無指定で）読めることを確認する。

### RT2: `raw_convert.py` — kind 別前処理
- 対象: `src/defect_analysis/raw_convert.py`
- 内容: §3.2（`source.encoding` の使用、①〜⑤の前処理、`_write_column_name_mapping` の修正、
  `DEFAULT_CONVERT` への `by_kind` 既定追加）。
- 完了条件:
  - `.venv/bin/python main.py convert` が §4 の「レイク」「修正員」「VIN 由来」行を満たす。
  - 2回目実行で repair も `skipped=True`。
  - **WB_ON（100% 番兵）で日時パース失敗率の WARN が出ない**。
  - `reports/column_name_mapping.csv` に `kind=repair` の 36 行が出る。
  - traceability / trend / defect のレイク出力が**変換前と bit 単位で同じ**（行数・列名・dtype が一致）。

### RT3: `config/config.yaml`
- 対象: `config/config.yaml`
- 内容: §2 のブロックを `real_ingest:` に追記。`analysis.leakage_prefixes` は**触らない**
  （`repair` が既に入っている）。
- 完了条件: 既存キーの差分がゼロ。`.venv/bin/python -c "import yaml,pathlib;yaml.safe_load(...)"` が通る。

### RT4: `assemble.py` — repair 集約と結合
- 対象: `src/defect_analysis/assemble.py`
- 内容: §3.3 全部（`prepare_repair_source` / ループ条件 / 台帳除外 / 0 埋め / `_infer_column_source` /
  品質レポート列追加、`DEFAULT_REPAIR` 既定辞書）。
- 完了条件: §4 の「パネル」以降 5 行を実データで満たす。

### RT5: ドキュメント
- 対象: `docs/real_data_ingest_design.md`（**D8 の行の書き換え＋本書への参照 1 行のみ**）、`README.md`。
- 内容: D8 を「repair は `data/raw/repair/defect.csv`（cp932）を取り込む。詳細は
  `docs/real_data_repair_design.md`」に差し替え。README の実データ経路の章に repair の1段落と
  **個人情報の取り扱い（既定でハッシュ化）** を明記。
- 完了条件: 本体設計と本書の記述が矛盾しない。

---

## 6. tester への申し送り（テスト観点）

原則は本体設計 §12 と同じ。**実データは smoke 1本のみ**、他は小さな自作 fixture（cp932 で書き出す）。

### 6.1 `raw_sources.py`
- `cp932 で書いた CSV のヘッダが fallback で読める`（config で encoding を指定しない場合）。
- `config の by_kind.encoding が fallback より優先される`。
- `source_aliases によりソース名が別名になる`（`repair/defect` → `修正`。レイクのパスにも反映）。
- `by_kind.time_column が _resolve_time_column より優先される`。
- `by_kind.time_column が存在しない列名のときソースがスキップされ ERROR ログが出る`。

### 6.2 `raw_convert.py`
- `先頭のアポストロフィだけが除去される`（`"'ab'c"` → `"ab'c"`。全部除去してはいけない）。
- `strip_apostrophe が有効でない kind では ' が保持される`（他ソースへの副作用が無いことの担保）。
- `番兵 00000000 000000 が NaT になり、日時パース失敗率の WARN が出ない`
  （全行が番兵の列を含む fixture で `assertNoLogs` 相当を使う）。
- `修正日と修正時間から修正日時が組み立てられる` / `どちらかが欠測なら NaT になる`。
- `%Y%m%d %H%M%S 形式の列が datetime dtype になる`。
- `PII 列が hash モードで元列を失い _id 列を得る` / `同じ氏名は同じ ID になる` /
  `salt が違えば ID が変わる` / `drop モードで列が消える` / `keep モードで WARN が出る`。
- `date パーティションが PB_ON から作られる`（`修正日` と異なる日付になる fixture を必ず用意する。
  これが R3 の回帰テスト）。

### 6.3 `assemble.py`
- `repair が VIN 単位に集約され、count が行数と一致する`（3行/2VIN の fixture で手計算）。
- `repair 由来の列がすべて repair_ で始まる`。
- `大分類のカウント展開が crosstab と一致する` / `既定では中分類・部位が展開されない`。
- `max_category_columns を超える設定で ValueError`。
- **`台帳に無い VIN を持つ repair 行がパネルの行数を増やさない`**（R5 の回帰テスト。
  traceability に A・B、repair に B・C が居る fixture で、パネルが A・B の 2 行であること）。
- `修正がない VIN で count / has / カウント列が 0、first_ts / 工数 が NaN のまま`
  （0 埋めの対象と非対象の切り分け）。
- `lead_time_h が (修正日時 min − PB_ON min) の時間と一致する`。

### 6.4 リークガード
- `resolve_predictors` に `repair_修正__count` 等を含む DataFrame を渡し、
  **説明変数に 1 列も残らない**こと（`tests/test_predictors.py` に 1 本追加）。

### 6.5 実データ smoke（既存 `tests/test_real_ingest_smoke.py` を拡張）
- 既存アサーション（`n_vin > 1000`、trend 列あり、`trend_match_rate == 0`、レポート 5 種）は**維持**。
- 追加: パネルに `repair_` で始まる列が 1 つ以上ある / `修正員` を含む列名が 1 つも無い /
  `ingest_quality.csv` に `kind == "repair"` の行がある。
- 注意: この smoke は `config/config.yaml` を読まず `Config` を直接構築するため、
  **repair の既定値がコード側 `DEFAULT_*` に無いと落ちる**。これは仕様（RT2/RT4 の完了条件）。

### 6.6 回帰条件
- 既存 148 本が無改修で緑であること。特に:
  - `RawSource` への `encoding` 追加は**末尾＋既定値付き**（`tests/test_raw_convert.py::_make_source` が
    キーワード指定で構築している）。
  - `build_vin_ledger` / `prepare_single_row_source` / `prepare_multi_row_source` /
    `prepare_defect_source` のシグネチャと出力列を変えない。
  - `probe_header(path, encoding) -> list[str]` を残す。
  - `cli.STAGES` / `ALL_ORDER` を変更しない。

---

## 7. エラー時挙動（追補分）

| 事象 | 挙動 |
|---|---|
| ヘッダが config の encoding で読めない | `encoding_fallbacks` を順に試す。成功したら WARN「cp932 で読みました」＋その encoding を採用 |
| フォールバックも全滅 | 従来どおり ERROR ログでソースをスキップ（全体は継続） |
| `by_kind.time_column` が実ヘッダに無い | ERROR ログでソースをスキップ |
| `combine_datetime` の元列が無い | WARN、その結合をスキップ（他の処理は継続） |
| `pii.columns` の列が無い | INFO のみ（設定を消す必要はない） |
| `pii.mode: keep` | WARN「個人情報列をそのまま保存します」 |
| カウント展開列が `max_category_columns` 超過 | `ValueError` で中断 |
| repair VIN が台帳に 1 件もマッチしない | WARN＋`n_vin_not_in_ledger` に記録。`repair_*` 列は全 NaN / 0 で生成（中断しない） |
| `修正工数` が全 0 | WARN（中断しない） |

---

## 8. 未確定事項

1. **【確定・2026-07-31】`PB` の意味** — ユーザー確認により `PB-ON` = 塗装工場投入時刻、
   `PB-OK` = 出荷時刻と判明。R3（date アンカー = `PB_ON`）の推定は正しかったことが確認された。
   `WB` / `AB` / `EG` / `FC` の正式名称は未確認のまま。
2. **`PB_OK` がブース通過の +103h 中央値になる理由** — `PB_OK` が出荷時刻と確認されたことで、
   ブース（塗装）通過から出荷までの後工程（組立・検査等）の所要時間を表していると理解できる。
   選択バイアス（修正済み車体のみ）の寄与は未分離。本書では `PB_OK` を使わないので実装への影響はない。
3. **【確定・2026-07-31】`修正工数` が全 0 の理由** — ユーザー確認により「入力員が入力していないため」
   と判明（実測ではなく未入力）。列自体は保持するが、`assemble.py` の該当 WARNING メッセージを
   「未入力のため実測値ではない。特徴量として使わないこと」に更新済み。
4. **`AB共通No`（321 ユニーク / 514 行）の意味** — 車体共通番号らしいが VIN と 1:1 ではない。現時点で未使用。
5. **repair に VIN サフィックス（`a`〜`c`）が現れるか** — 現サンプル 514 行では 0 件。
   1年分で出現する可能性はあるが、`vin_key` がそのまま扱うので追加対応は不要。
6. **【確定・2026-07-31】repair の `VIN` に 17 桁フル VIN が 57 行ある**（本体設計 §13-2 と同じ論点）。
   ユーザー確認により、型式ベースと17桁フルVINは国内/国外仕様の差であり同一車体の別表記ではないと
   判明。したがって両形式が同じ車体を指して衝突することはなく、追加の変換・統合は不要。
7. **【確定・2026-08-06】1年分の repair ファイルの命名** — 当初は `defect.csv`（日付サフィックスなし）
   のみで提供されていたが、`data/raw/repair/defect_202607.csv` / `defect_202608.csv` のように月単位で
   分割して置く運用でも**コード変更なしで動作することを確認済み**。`naming.source_key()` の
   `_DATE_SUFFIX_RE`（`_YYYY`/`_YYYYMM`/`_YYYYMMDD` を吸収）が末尾の6桁連続数字（`_202607` 等）を
   正しく除去するため、`defect_202607.csv` と `defect_202608.csv` はどちらも `source_key` = `defect`
   に正規化され、既存の `source_aliases` 既定 `{"repair/defect": "修正"}` がそのまま当たって
   `discover_sources()` は自動的に両ファイルを同一 `RawSource`（`files` に複数月ぶん）としてグルーピングする。
   ユーザーは `data/raw/repair/defect_202607.csv`, `defect_202608.csv`, ... と月単位でファイルを
   置くだけでよく、alias の追記は不要（`tests/test_raw_sources.py` で固定化済み）。

---

## 9. ユーザー確認事項（要回答）

**回答済み（2026-07-31）**:

- `修正員` の扱い → ハッシュ化（既定 `mode: hash`）で確定。「同一人物は同一ハッシュ」という要件も
  実装済み（`hashlib.sha256(salt + 値)` による決定的ハッシュ。ソルト固定は未回答のため既定の空ソルトのまま）。
- `PB-ON` → 塗装工場投入時刻で確定（R3 の推定が正しかった）。`PB-OK` → 出荷時刻と判明（§8-1/§8-2 参照）。
- `修正工数` が全 0 の理由 → 入力員が未入力のため（§8-3 参照。実測値ではない）。
- repair の VIN に17桁フルVINが混在する件 → 国内/国外仕様の差であり、キー統合は不要（§8-6 参照）。

**未回答（引き続き要回答）**:

1. **ハッシュのソルトを固定してよいですか。** 未回答のため既定の空ソルト
   （`real_ingest.convert.by_kind.repair.pii.salt` 未設定時は環境変数 `DEFECT_ANALYSIS_PII_SALT`、
   それも無ければ空文字）のまま運用している。固定ソルトを設定したい場合は指定されたい
   （ソルト変更時は過去レイクとハッシュ ID が一致しなくなるため `--force` 再変換が必要）。
2. **`WB` / `AB` / `EG` / `FC` の正式名称** — `PB` 以外は未確認のまま。
3. **`修正工数` が全 514 行 `0` です。** 未入力運用ですか、それとも別の単位・列に工数が入りますか。
4. 【確定・2026-08-28】対比表の「グラフ項目」を統合カテゴリとして採用。
   `docs/repair_integrated_category_design.md` 参照。
5. **repair（修正実績）とブツ検（defect）は同じ不良を指しますか。**
   ブツ検側に `不良No` に対応する列が無いため、現状は VIN でしか紐付けていません。
   同一不良の対応表があれば教えてください。
6. **repair に載らなかった車体は「修正なし」ですか、「未入力」ですか。**
   既定は「修正なし」として `repair_*__count = 0` で埋めます（本体設計 §14-3 と同じ論点）。
7. **repair の VIN が traceability と 38.6% しか重なりません。** これは
   「traceability が 07/24-25 の 2 日分しか無いのに、repair の生産日（PB-ON）が 07/23・07/24・07/29 に
   分散しているため」と理解しています。1年分では改善しますか。
