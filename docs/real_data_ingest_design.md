# 実データ取り込みパイプライン 設計仕様書（全面改訂 / 2026-07-30）

対象: `data/raw/{traceability,trend,defect,repair}/*.csv` の**実データ**を読み込み、
VIN 単位の分析用パネル `data/interim/vin_panel.parquet` を作るまで。

前提事実は [`real_data_facts.md`](real_data_facts.md)。本書はそれに**実測補正**（§1.1）を加えた上で設計する。
本書は coder がこの1本だけで実装できる粒度を目標とする。実装コードは含まない。

---

## 0. 結論（決定事項と根拠）

| # | 決定 | 根拠 |
|---|---|---|
| D1 | **既存の合成データ経路（`generate`→`ingest`→`integrate`→`features`）は一切変更せず、実データ経路を別モジュール群として並存させる** | 実データ経路は trend 結合をサンプルで検証できない（期間非重複）。旧経路を壊すと「動く参照実装」と 89 本のテストを同時に失う。追加コストは新モジュール＋CLI サブコマンド2つのみ |
| D2 | **2段構成にする: `convert`（raw CSV → Parquet レイク）と `assemble`（レイク → VIN パネル）** | 1年分 10GB を毎回 CSV から読むのは不可。変換は増分・冪等、業務判断は組立側に閉じる |
| D3 | **列名は人手のマッピング表を作らず、機械的規則（NFKC＋特殊文字置換）で正規化する。元名との対応は毎回レポート出力する** | 全 22 ファイル 2446 列を実測した結果、特殊文字は `空白 # ( ) - & _` の7種のみ。規則適用後の衝突は**全ファイルでゼロ**（実測）。表の手保守は不要 |
| D4 | **VIN 正規化は `strip()` ではなく「全空白除去」**。サフィックス（`a`〜`c`）は既定 `keep`（別キー扱い）。`DUMMY*`/`EMPTY` は組立時に除外 | サフィックス付き VIN は空白が**内部**にある（`"HE93S-122023     a"`）ため `strip()` では消えない（§1.1）。keep の根拠は §6 |
| D5 | **複数行/VIN ソース（上塗/下塗/ホイ黒ロボット）は既定で統計量集約（mean/min/max/std＋行数）。pivot は config で opt-in** | `ﾛﾎﾞｯﾄ#` で pivot すると上塗だけで約 600 列。まず集約で当たりを付け、必要な設備だけ pivot を有効化する |
| D6 | **trend は「trend 列の先頭トークン → 対応する traceability ソースの通過日時」を規則で解決し、アンカー別に「rolling 窓集約済み trend への最近傍点参照（`merge_asof`）」で結合する** | 前処理〜上塗は数時間離れており、単一アンカーだと1分粒度データの意味が消える。中心窓平均は `merge_asof` 単体では作れないため、trend 側で先に rolling する（§8.4） |
| D7 | **サンプルでは trend×VIN のマッチ率が 0% になる。既定挙動は「WARN＋trend 列を全 NaN で生成」** | 下流が列の有無で分岐せずに済み、欠損率レポートで「結合できていない」ことが可視化される。`skip`/`error` も config で選択可 |
| D8 | ~~**repair は optional。ソース0件でも INFO ログのみで正常終了**~~ → **2026-07-31 破棄**。`data/raw/repair/defect.csv`（cp932）が配置されたため取り込む。差分設計は [`real_data_repair_design.md`](real_data_repair_design.md) | 当時 `data/raw/repair/` が空だったため。ソース0件でも正常終了する挙動自体は維持する |
| D9 | **defect 由来の列は必ず `defect_` で始める** | 既存 `config.yaml` の `analysis.leakage_prefixes` にそのまま乗り、リーク列の自動除外が効く |

後方互換の要否についての判断: **旧経路との後方互換は「触らない」形で維持する**。
実データ経路の出力先は旧経路と別ファイル（`vin_panel.parquet` / `data/lake/`）にして衝突を避ける。
ただし恒久並存はさせない — 実データパネルが下流（eda/stats/ml）に接続できた時点で、
`generate.py` と合成データ経路を「廃止候補」として README に明記し、次フェーズで削除する。

---

## 1. 前提事実（設計に効くものだけ再掲）

- エンコーディングは全ファイル `utf-8-sig`。
- 日時形式は**全ファイル `%Y/%m/%d %H:%M:%S` で完全一致**（実測: traceability/defect/trend の代表ファイル）。→ `pd.to_datetime(..., format=...)` を明示指定する（10GB 規模では推論のコストが致命的）。
- ファイル名の `_202607` は年月だが中身は1日分。**ファイル名から日付・設備を導出してはならない**（§4）。
- trend は VIN を持たず `DATETIME` 1分グリッド（1173行/日、全 trend ファイルで同一グリッド）。
- trend と traceability/defect の**期間が重複しない**（trend=07/29-30、他=07/24-25）。
- `data/raw/repair/` は空。

### 1.1 `real_data_facts.md` への実測補正（重要）

VIN 列のみを読んで再実測した結果、facts の記述が不足していた。**以下を正とする**。

1. **サフィックス付き VIN の空白は文字列の内部にある。**
   実値: `"HE93S-122023     a"`（18文字, 内部に半角空白5個）、`"JS3JB74V8V5106396a"`（空白なし）。
   → `str.strip()` では `a` の前の空白が残る。**`str.replace(r"\s+", "", regex=True)` を使う**。
   全空白除去後の unique 数は `strip()` 後と全ファイルで一致 → 空白除去による衝突は無い（安全）。
2. **サフィックスは `a` だけではない。`a` / `b` / `c` が実在する**（1日分の実測: ソース毎に a≈252, b=1, c=1）。
   さらにダミーには `d` / `e` が付く。→ 単一文字 `a` のハードコードは誤り。`[a-z]$` で扱う。
3. **ダミーの実値は `DUMMY-YOSHIKI`（+ サフィックス `d`/`e`）と `EMPTY`**。`DUMMY` 完全一致では拾えない → 部分一致で判定する。
4. **VIN は2形式が混在する**: 型式ベース `[A-Z0-9]{5}-\d{6}`（例 `MR92S-617408`）と 17桁フル VIN `[A-Z0-9]{17}`（例 `JS3JB74V8V5106401`）。1ファイル内に両方が出現し、件数は排他的（前処理: 784 + 102 = 886）。→ 別車体を指すと推定するが**未確定**（§13）。

> coder タスクとして、`docs/real_data_facts.md` に本節の補正を追記すること（§11 T0）。

---

## 2. スコープ

### やる
raw CSV の発見 → 列名正規化 → 日付パーティション Parquet 化（増分） → VIN 正規化 →
ソース別集約 → VIN 横結合 → trend 時刻結合 → `vin_panel.parquet` ＋ 品質レポート出力。

### やらない（本書のスコープ外）
- `vin_panel` を既存 `features/eda/stats/ml` に接続する改修（別タスク）。
- 目的変数の定義変更・特徴量選択・モデリング。
- 旧経路（`generate/ingest/integrate/features`）の改修・削除。

---

## 3. モジュール構成

新規（すべて `src/defect_analysis/` 配下）:

| ファイル | 責務 | 依存 |
|---|---|---|
| `naming.py` | 列名・ソース名の機械的正規化。衝突解決。元名対応表の生成 | なし（`unicodedata`, `re`） |
| `raw_sources.py` | `data/raw/` の走査、ソース定義（`RawSource`）の構築、ヘッダのみ読取 | `naming`, `config` |
| `vin_key.py` | VIN 正規化（空白除去・base/pass_no 分解・ダミー判定） | なし |
| `raw_convert.py` | CSV → Parquet レイク変換（チャンク読み・日付パーティション・増分マニフェスト）、レイク読取 API | `raw_sources`, `naming`, `vin_key`, `config` |
| `assemble.py` | ソース別集約・VIN 横結合・trend 時刻結合・パネル出力・品質レポート | `raw_convert`, `naming`, `config` |

変更:

| ファイル | 変更内容 |
|---|---|
| `cli.py` | サブコマンド `convert` / `assemble` を追加。`STAGES` / `ALL_ORDER` は**変更しない** |
| `config/config.yaml` | 新セクション `real_ingest:` を追加（既存キーは変更しない） |
| `README.md` | 実データ経路の章を追加、旧経路を「合成データ用（廃止候補）」と明記 |
| `docs/real_data_facts.md` | §1.1 の補正を追記 |

責務境界のルール:
- `raw_convert` は**情報を捨てない**。可逆・冪等な機械変換のみ（列名正規化・型・日付分割・VIN 派生列の付与）。ダミー除外やサフィックス丸めは行わない。
- 業務判断（除外・集約・窓幅・アンカー）はすべて `assemble` と config に閉じる。→ 方針変更時に 10GB の再変換が不要。

---

## 4. ソース発見（`raw_sources.py`）

```python
@dataclass(frozen=True)
class RawSource:
    kind: str            # "traceability" | "trend" | "defect" | "repair"（= サブディレクトリ名）
    name: str            # 正規化済みソース名（例: "前処理_電着"）
    files: list[Path]    # 同一ソースに属する raw CSV（複数月/複数日ぶん）
    vin_column: str | None   # 正規化後の VIN 列名。trend は None
    time_column: str         # アンカー日時列（正規化後）
    columns: list[str]       # 正規化後の全列名（先頭ファイルのヘッダ由来）
    rename_map: dict[str, str]   # 正規化後 -> 元列名

def probe_header(path: Path, encoding: str) -> list[str]:
    """pd.read_csv(path, nrows=0) でヘッダのみ取得する。"""

def discover_sources(raw_dir: Path, cfg: Config) -> list[RawSource]:
    """data/raw/*/ を走査して RawSource 群を返す。"""
```

規則:

1. `kind` = `data/raw/` 直下のディレクトリ名。既知4種以外のディレクトリは WARN でスキップ。
2. `source_key(stem)`: ファイル名 stem 末尾の日付サフィックス `_(\d{4}|\d{6}|\d{8})$` を除去し、残りを `normalize_name()` に通す。
   - `シーラー炉_202607.csv` → `シーラー炉`
   - `ブース_20260724.csv` → `ブース`（日次命名も同じソースに集約される）
   - `前処理・電着_202607.csv` → `前処理_電着`
   - 日付サフィックスが無い `ブース.csv` → `ブース`
   → **月次ファイル / 日次ファイル / サフィックス無しのすべてを同一ソースに吸収できる**（1年分対応）。
3. 同一 `(kind, source_key)` の全ファイルを 1 つの `RawSource` にまとめる。
4. `vin_column`: 正規化後ヘッダに `VIN`（`VIN#` の正規化結果）があればそれ。無ければ `None`。
   `kind` が traceability/defect/repair で `None` の場合は **ERROR ログを出してソースごとスキップ**。
5. `time_column`: 正規化後ヘッダのうち
   (a) `DATETIME` があればそれ、
   (b) なければ `通過日時` を含む列の**最初のもの**（CSV 列順は上流→下流なので、最初＝最上流の入口時刻）。
   実データでの結果: `シーラー炉`→`入口_通過日時` / `ホイ黒ロボット`→`通過日時` /
   `各工程滞在時間`→`前処理_通過日時` / `浮遊ゴミ`→`PA_ON_通過日時` / trend 全ソース→`DATETIME`。
   どちらも無ければ ERROR ログでソースごとスキップ。
6. **同一ソースの複数ファイル間でヘッダが違う場合**: 先頭ファイルのヘッダを基準とし、差分（追加列/欠落列）を
   WARN ログ＋`reports/ingest_quality.csv` に記録。変換自体は各ファイルの実ヘッダで進める
   （Parquet 読取時に列の和集合になる）。

---

## 5. 列名正規化（`naming.py`）

```python
SPECIAL_CHARS: str = " 　#()-&・/.,"   # すべて "_" に置換
SAFE_PATTERN = r"[^0-9A-Za-z_぀-ゟ゠-ヿ一-鿿]"

def normalize_name(raw: str) -> str:
    """1) NFKC 正規化（半角カナ→全角カナ、全角英数→半角）
       2) 前後空白除去
       3) SPECIAL_CHARS を "_" に置換
       4) SAFE_PATTERN に該当する残りの文字も "_" に置換
       5) "_" の連続を1つに畳み、前後の "_" を除去
       空文字になった場合は "col" を返す（呼び出し側で衝突解決される）。"""

def normalize_columns(cols: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """列リストを正規化。衝突時は 2 件目以降に "__2", "__3" ... を付与。
       戻り値: (正規化後リスト, {正規化後: 元列名})"""

def prefixed(source: str, column: str) -> str:
    """f"{source}__{column}"。source は既に正規化済みであること。"""

def source_key(stem: str) -> str:
    """ファイル名 stem の日付サフィックスを除去して正規化したソース名を返す。"""
```

実データでの適用例（実測確認済み）:

| 元列名 | 正規化後 |
|---|---|
| `VIN#` | `VIN` |
| `ｿﾞｰﾝ#1 循環 還気 温度 測定値` | `ゾーン_1_循環_還気_温度_測定値` |
| `PA-ON 粒子数(大)` | `PA_ON_粒子数_大` |
| `ﾌﾞｰｽ#1 4 結露防止 運転ﾓｰﾄﾞ` | `ブース_1_4_結露防止_運転モード` |
| `中上炉 ゾーン#1&5 制御盤 電力量 積算値` | `中上炉_ゾーン_1_5_制御盤_電力量_積算値` |
| `判定結果_3Bit` | `判定結果_3Bit` |
| `ﾛﾎﾞｯﾄ#` | `ロボット` |

検証済み事実（この方式を採る根拠）:
- 全 22 ファイル・2446 列に適用して **同一ファイル内の衝突は 0 件**。`.1` 等の pandas mangled 列も 0 件。
- ヘッダに出現する非英数・非日本語文字は `空白 # ( ) - & _` の7種のみ（`・` はヘッダには出ないがファイル名に出る）。
- 半角カナ由来の表記揺れは NFKC が吸収する（`ｿﾞｰﾝ`→`ゾーン`）ため、traceability 側の `中上炉` と
  trend 側の `中上炉` がトークン一致する。**§8.4 のアンカー解決はこの性質に依存する。**

**必ず `reports/column_name_mapping.csv`（列: `kind, source, normalized, original`）を convert 実行毎に出力する。**
これがハードコード表の代替であり、現場との突合手段になる。

---

## 6. VIN 正規化（`vin_key.py`）

```python
@dataclass(frozen=True)
class VinPolicy:
    suffix_policy: str = "keep"     # "keep" | "merge"
    exclude_regex: str = r"(?i)(DUMMY|EMPTY)"

def normalize_vin(s: pd.Series) -> pd.DataFrame:
    """VIN 列（原文）から派生列を作る。戻り値の列:
         vin          : 全空白除去後の正規化キー（例 "HE93S-122023a"）
         vin_base     : vin から末尾英小文字1字を除去（例 "HE93S-122023"）
         vin_pass_no  : サフィックス無し=1, a=2, b=3, c=4 ...（ord(suf)-ord('a')+2）
         vin_is_dummy : exclude_regex にマッチ、または空文字（bool）
       実装: s.astype("string").str.replace(r"\s+", "", regex=True) のあと
             str.extract(r"^(?P<base>.*?)(?P<suf>[a-z])?$") で分解する。"""

def join_key(df: pd.DataFrame, policy: VinPolicy) -> pd.Series:
    """policy に応じた結合キーを返す（keep→vin, merge→vin_base）。"""
```

- `vin` / `vin_base` / `vin_pass_no` / `vin_is_dummy` は **convert 時に付与**して Parquet に持たせる（行の除外はしない）。
  元の VIN 列は `vin_raw` にリネームして保持する。
- **ダミー除外は assemble 時**。`vin_is_dummy` の行を落とし、除外件数をソース別に `reports/ingest_quality.csv` に記録。
- 大文字化（`upper()`）は**しない**。サフィックスの小文字が唯一の識別情報であり、`upper()` はそれを壊す。

### サフィックス既定値 `keep` の根拠
サフィックス行は下流工程（ブース・中上炉・上塗ロボット・ホイ黒・上塗ブツ検）にのみ出現し、
上流工程（前処理・電着・電着炉・下塗ロボット・シーラー炉）には**存在しない**（facts §3 実測）。
`merge` にすると上流工程の測定値を2回目通過の行にも複製することになり、
「2回目通過の品質」を上流の値で説明する偽の関係を作ってしまう。
`keep` なら上流由来の列は欠損として残り、「2回目通過に上流情報は無い」という事実がそのまま表現される。
`vin_base` / `vin_pass_no` を常に列として持たせるので、後から `merge` 相当の集計は再変換なしに可能。
**サフィックスの業務的意味（2トーン2回目 / 再塗装 / それ以外）は未確定**（§13-1）。

---

## 7. CSV → Parquet レイク変換（`raw_convert.py`）

### 7.1 レイアウトと API

```
data/lake/
  {kind}/{source}/date=YYYY-MM-DD/part-{file_stem}-{chunk:04d}.parquet
  _manifest.json
```

```python
@dataclass
class ConvertResult:
    kind: str; source: str; file: str
    n_rows: int; n_partitions: int
    skipped: bool; reason: str | None

def convert_all(cfg: Config, *, force: bool = False) -> dict[str, int]:
    """全ソース・全ファイルを変換。戻り値 {"n_files":.., "n_skipped":.., "n_rows":..}"""

def convert_file(path: Path, source: RawSource, lake_dir: Path, cfg: Config,
                 *, force: bool = False) -> ConvertResult

def read_source(lake_dir: Path, kind: str, source: str, *,
                columns: list[str] | None = None,
                date_from: str | None = None,
                date_to: str | None = None) -> pd.DataFrame:
    """レイクから1ソースを読む。columns で列を絞れる（Parquet の列プルーニング）。
       日付は hive パーティション列 `date` に対する filters で絞る。
       ディレクトリが無ければ空 DataFrame を返す（エラーにしない）。"""

def load_manifest(path: Path) -> dict
def save_manifest(path: Path, data: dict) -> None
```

### 7.2 変換手順（1ファイルあたり）

1. **増分判定**: マニフェストの `{kind}/{source}/{filename}` エントリと現ファイルの
   `(size, mtime_ns)` を比較。一致すれば `skipped=True` で即 return（`force=True` で無効化）。
   ハッシュは使わない（10GB を毎回読むのは無駄）。
2. `pd.read_csv(path, encoding="utf-8-sig", chunksize=cfg.chunksize)` でチャンク読み。
3. チャンク毎に:
   1. `normalize_columns()` で列名を正規化（`rename_map` は初回チャンクのものを採用）。
   2. `time_column` および `通過日時` を含む全列を
      `pd.to_datetime(..., format="%Y/%m/%d %H:%M:%S", errors="coerce")` で変換。
      NaT 率が `max_datetime_parse_failure_rate`（既定 1%）を超えたら WARN。
   3. VIN 列があれば `normalize_vin()` で `vin/vin_base/vin_pass_no/vin_is_dummy` を追加し、
      元列を `vin_raw` にリネーム。
   4. `float64` 列を `float32` にダウンキャスト（`downcast_float: true` のとき）。
      **例外**: 列名が `積算値` または `総合計` で終わる列は `float64` を維持
      （積算カウンタは float32 で有効桁を失う）。
   5. `date` 列 = `time_column.dt.strftime("%Y-%m-%d")`。`time_column` が NaT の行は
      `date="unknown"` パーティションに入れ、件数を WARN。
   6. `__source` 列（ソース名）を付与。
4. `date` でグループ分割し、`{lake}/{kind}/{source}/date=.../part-{stem}-{chunk:04d}.parquet` に書き出す
   （`to_parquet(path, index=False, compression="snappy")`）。チャンク毎に別 part ファイルにするので
   `ParquetWriter` の使い回しは不要。
5. 再変換時は**先に** `{lake}/{kind}/{source}/date=*/part-{stem}-*.parquet` を削除してから書く
   （行数が減った場合に古い part が残らないようにする）。
6. マニフェストに `{size, mtime_ns, n_rows, n_partitions, converted_at, dates: [...]}` を記録。

### 7.3 スケール上の要点
- 1ファイル = 最大 5.7MB（1日分）だが 1年分では 1.4GB になり得るため、`chunksize`（既定 200,000 行）を必ず使う。
- 列プルーニングが効くのは Parquet 側なので、`assemble` は必ず `read_source(columns=...)` で必要列のみ読む。
- 日付パーティションにより「ファイル名は月・中身は1日」の乖離が無害化される。1日分を追加投入した場合、
  マニフェスト未登録の新ファイルだけが変換される（既存ファイルは size/mtime 一致でスキップ）。

---

## 8. VIN パネル組立（`assemble.py`）

```python
def assemble(cfg: Config, *, date_from: str | None = None, date_to: str | None = None) -> dict[str, int]
```

### 8.1 ソース別フレームの構築（traceability / defect）

各 `RawSource`（kind != "trend"）について `read_source()` → 以下の前処理:
1. `vin_is_dummy == True` の行を除外（件数記録）。
2. 結合キー `vin`（= `join_key`）を設定。
3. `vin` の重複有無を判定 → **1行/VIN ソース**と**複数行/VIN ソース**に自動分岐
   （config での宣言は不要。1年分でカーディナリティが変わっても自動追従する）。

#### 8.1.1 1行/VIN ソース（シーラー炉・ブース・中上炉・前処理・各工程滞在時間・浮遊ゴミ・電着・電着炉）

```python
def prepare_single_row_source(df: pd.DataFrame, source: str) -> pd.DataFrame
```
- `vin` 以外の全列を `{source}__{col}` にリネーム（`vin_raw`/`vin_base`/`vin_pass_no`/`__source`/`date` は落とす。台帳側で持つ）。
- `time_column` は `{source}__{time_column}` として残す（trend アンカーに使う）。
- 万一重複があれば `vin` で `first` を取り、WARN。

#### 8.1.2 複数行/VIN ソース（上塗ロボット ≈30行・下塗ロボット ≈19行・ホイ黒ロボット 2行）

```python
def prepare_multi_row_source(df, source, aggs: list[str], pivot_by: list[str] | None) -> pd.DataFrame
```
- `pivot_by` が空（既定）:
  - 数値列 → `aggs`（既定 `["mean","min","max","std"]`）→ `{source}__{col}__{agg}`
  - 日時列 → `min`/`max` → `{source}__{col}__min` / `__max`（`time_column` の `min` が trend アンカー）
  - 文字列列 → `nunique` と `first` → `{source}__{col}__nunique` / `__first`
  - 追加: `{source}__n_rows`（行数）
- `pivot_by` に列を指定した場合（例 `["ロボット"]`）:
  `pivot_table(index="vin", columns=pivot_by, values=数値列, aggfunc="mean")` →
  `{source}__{pivot値}__{col}`。pivot 値は `normalize_name()` を通す（`Pi-1L` → `Pi_1L`）。
  pivot 後の列数が `assemble.max_columns_per_source`（既定 200）を超えたら **`ValueError` で中断**
  （気付かずに数千列を作らないためのガード）。実データで `上塗ロボット` を `ロボット` で pivot すると
  約 600 列になるため、既定設定では必ずこのガードに当たる。有効化する場合は同時に閾値も上げる必要がある。

列数試算（既定設定・1日分）: 上塗ロボット ≈ 84、下塗 ≈ 44、ホイ黒 ≈ 56。

#### 8.1.3 defect ソース（上塗ブツ検・電着ブツ検）

```python
def prepare_defect_source(df, source, cfg) -> pd.DataFrame
```
出力列（`P = f"defect_{source}"`。**必ず `defect_` で始める** → D9）:

| 列 | 内容 |
|---|---|
| `{P}__count` | 不良点数（行数） |
| `{P}__has` | 1（存在フラグ。台帳結合後に 0 埋め） |
| `{P}__size_mean` / `__size_max` / `__size_sum` | `不良サイズ` を `to_numeric(errors="coerce")` した統計量 |
| `{P}__first_ts` / `__last_ts` | `入口_通過日時` の min/max |
| `{P}__n_kind` / `__n_part` | `不良種類` / `検査部位` の nunique |
| `{P}__top_kind` | 最頻の `不良種類` |
| `{P}__kind__{正規化不良種類}` | 不良種類別カウント（`crosstab`）。`defect.by_kind: true` のとき |
| `{P}__part__{正規化検査部位}` | 検査部位別カウント。**既定 off**（部位は 20 種以上あり列爆発する） |

- `検査箇所` は `検査箇所X` + `-` + `検査箇所Y` の冗長列なので drop（`defect.drop_columns` で指定）。
- 種類別カウント列は「そのデータに出現した種類」から動的生成し、生成した一覧を
  `reports/ingest_quality.csv` に記録する（config に手書きしない）。

### 8.2 VIN 台帳（base）

```python
def build_vin_ledger(frames: dict[str, pd.DataFrame]) -> pd.DataFrame
```
- 全ソース（traceability + defect）の `vin` の **和集合**を台帳とする。
  列: `vin`, `vin_base`, `vin_pass_no`, `vin_format`（`型式` | `full17` | `other`。§1.1-4 の正規表現で判定）、
  各ソースの存在フラグ `present__{source}`。
- 根拠: 工程ごとに通過 VIN 数が 886〜1218 と異なるため、どれか1ソースを基準にすると必ず取りこぼす。
  絞り込みは分析側の `analysis.filters` で行う（`present__ブース == 1` 等）。
- `assemble.require_sources` に列挙されたソースが 0 行なら `ValueError` で中断（既定 `[]`）。

### 8.3 横結合

台帳に対して各ソースフレームを `merge(on="vin", how="left", validate="one_to_one")` で順に結合。
`validate` は必ず付ける（集約後は 1行/VIN が保証されるはずで、崩れていれば即検知したい）。
結合後に `present__*` / `defect_*__has` / `{P}__count` / `{P}__kind__*` を 0 埋め。

### 8.4 trend の時刻結合

```python
def build_trend_wide(cfg, date_from, date_to) -> pd.DataFrame
def resolve_trend_anchor(trend_column: str, anchor_columns: dict[str, str],
                         anchor_map: dict[str, str], fallback: str) -> str | None
def join_trend(base: pd.DataFrame, trend_wide: pd.DataFrame, cfg) -> tuple[pd.DataFrame, pd.DataFrame]
```

#### 手順

**(1) trend ワイド表の構築**
全 trend ソースを `read_source()` で読み、`DATETIME` を index にして横結合（`concat(axis=1)`）。
列名は `trend__{col}`（col は既に設備名を先頭に含むのでソース名は付けない）。
- 列の絞り込み: `trend.include_suffixes`（既定 `["測定値","計算値"]`）に**末尾トークンが一致**する列のみ採用。
  `trend.include_columns`（明示ホワイトリスト）が非空ならそれを優先。`trend.exclude_columns` は常に適用。
  既定での採用列数 ≈ 891（全 1607 列中）。除外されるのは `理論値`/`積算値`/`設定値`/`出力値`/
  `運転モード`/`保全アラーム`/`運転中` 等。
- 重複 `DATETIME` があれば `groupby(level=0).mean()` で畳み、WARN。
- グリッド欠測は補完しない（rolling の `min_periods=1` で吸収）。

**(2) アンカー解決（trend 列 → どの通過時刻に合わせるか）**
- `anchor_columns` = `{traceability ソース名: base 内のアンカー列名}`
  （1行/VIN は `{source}__{time_column}`、複数行/VIN は `{source}__{time_column}__min`）。
- trend 列名から `trend__` を除去し、`_` で split した**先頭トークン** `t` を取る。
  例: `ブース_1_4_結露防止_運転モード` → `ブース` / `前処理_0_通常_運転モード` → `前処理` /
  `シーラー炉_全体_バーナー_...` → `シーラー炉`。
- 解決規則（この順に評価）:
  1. `trend.anchor_map` に `t` のエントリがあればそのソース（既定 `{}`）。
  2. `t` と完全一致する traceability ソース名。
     → 実測で `シーラー炉` / `中上炉` / `電着炉` / `前処理` / `電着` / `ブース` / `浮遊ゴミ` が一致する。
  3. `t` を接頭辞に持つソース名、または `t` の接頭辞になっているソース名（`ブース_1` → `ブース`）。
  4. 解決不能 → `trend.fallback_anchor_source`（既定 `ブース`）。
     1日分の実測で解決不能なトークンは `コンベア` / `冷水供給` / `温水供給` / `作業場空調` /
     `塗料供給` / `塗料供給空調` — いずれも工場共通ユーティリティで固有の通過時刻を持たない（§14-5 で要確認）。
- 解決結果（トークン → ソース → アンカー列、解決経路）を `reports/trend_anchor_map.csv` に出力。

**(3) 窓集約 → 最近傍点参照**
- `mode: window`（既定）: trend ワイド表を DATETIME index で
  `rolling(window=cfg.window_minutes, center=True, min_periods=1).agg(a)` により**事前に**集約する。
  1分固定グリッドなので整数窓で正確（既定 `window_minutes: 11` = ±5分）。`aggs` 既定 `["mean"]`。
  中心窓平均は `merge_asof` 単体では表現できないため、この「先に rolling してから点参照」が必須。
- `mode: point`: rolling を行わない（窓幅 1 相当）。
- アンカー列ごとに 1 パス（実測で最大 8 パス）:
  ```python
  base_sorted = base.sort_values(anchor_col)
  trend_sorted = trend_agg[cols_of_this_anchor].reset_index().sort_values("DATETIME")
  merged = pd.merge_asof(base_sorted, trend_sorted,
                         left_on=anchor_col, right_on="DATETIME",
                         direction="nearest",
                         tolerance=pd.Timedelta(minutes=cfg.tolerance_minutes))
  ```
  `DATETIME` 列は結合後に drop。アンカーが NaT の VIN はマッチしない（NaN のまま）。
- 列名: `aggs` が1つ（mean のみ）なら `trend__{col}`、複数なら `trend__{col}__{agg}`。
  窓幅は列名に入れず `reports/trend_join_report.csv` に記録する。

**(4) 期間非重複時の挙動（D7）**
- アンカー別にマッチ率（`notna` 率）を計算し、`reports/trend_join_report.csv` に
  `anchor_source, n_vin, n_matched, match_rate, trend_min_ts, trend_max_ts, anchor_min_ts, anchor_max_ts` を出力。
- 全体マッチ率 0 のとき `trend.on_no_overlap` に従う:
  - `warn_empty`（既定）: WARN ログ（trend 期間と anchor 期間を明示）＋ **trend 列は全 NaN で生成する**。
  - `skip`: trend 列を生成しない。
  - `error`: `ValueError` で中断。
- `0 < マッチ率 < trend.min_match_rate`（既定 0.5）なら WARN のみ（列は生成）。
- **現サンプルデータでは必ずマッチ率 0 になる**（trend 07/29-30 vs traceability 07/24-25）。
  これは実装の誤りではない。tester はこの経路を「WARN が出て trend 列が全 NaN」として検証すること（§12）。

### 8.5 出力

| 出力 | 内容 |
|---|---|
| `data/interim/vin_panel.parquet` | VIN × 全列のパネル |
| `reports/vin_panel_dictionary.csv` | 列名・dtype・欠損数・ユニーク数・例・由来ソース |
| `reports/column_name_mapping.csv` | 正規化後 → 元列名（convert 側で出力） |
| `reports/ingest_quality.csv` | ソース別: 採用ファイル数・行数・VIN 数・ダミー除外数・重複数・日時パース失敗数・動的生成カテゴリ一覧 |
| `reports/trend_anchor_map.csv` | trend 列トークン → アンカーソース → アンカー列・解決経路 |
| `reports/trend_join_report.csv` | アンカー別マッチ率と期間 |

`assemble()` の戻り値: `{"n_vin": int, "n_columns": int, "n_trend_columns": int, "trend_match_rate": float}`。

規模試算（既定設定・1日分）: 約 1,300 行 × 約 1,870 列
（traceability 単行 745 ＋ 複数行 184 ＋ defect ≈ 40 ＋ trend ≈ 891）。
**p ≫ n であることを README に明記する**（特徴量選択が別途必須）。
1年分（約 32 万 VIN × 1,870 列 float32 ≈ 2.3GB）では `assemble.date_from/date_to` と
`trend.include_columns` による絞り込みが前提。CLI に `--date-from/--date-to` を必ず付ける。

---

## 9. config スキーマ案（`config/config.yaml` に追記）

既存キーは一切変更しない。以下を新規セクションとして追加する。

```yaml
# ---------------------------------------------------------------------
# 実データ取り込み（docs/real_data_ingest_design.md）
#   旧 ingest/integrate（合成データ経路）とは独立。CLI: convert / assemble
# ---------------------------------------------------------------------
real_ingest:
  raw_dir: data/raw
  lake_dir: data/lake                    # 変換済み Parquet レイク
  manifest_path: data/lake/_manifest.json
  panel_path: data/interim/vin_panel.parquet
  kinds: [traceability, trend, defect, repair]   # repair は空でも可

  convert:
    encoding: utf-8-sig
    datetime_format: "%Y/%m/%d %H:%M:%S"
    chunksize: 200000
    downcast_float: true
    keep_float64_suffixes: ["積算値", "総合計"]   # 精度維持する列の末尾トークン
    datetime_column_keywords: ["通過日時", "DATETIME"]
    max_datetime_parse_failure_rate: 0.01        # 超過で WARN

  vin:
    suffix_policy: keep        # keep（既定・別キー） | merge（vin_base で丸める）
    exclude_regex: "(?i)(DUMMY|EMPTY)"

  sources:                     # ソース名（正規化後）ごとの上書き。書かなければ defaults
    上塗ロボット: {aggs: [mean, min, max, std], pivot_by: []}
    下塗ロボット: {aggs: [mean, min, max, std], pivot_by: []}
    ホイ黒ロボット: {aggs: [mean, min, max, std], pivot_by: []}
  defaults:
    aggs: [mean, min, max, std]
    pivot_by: []

  defect:
    size_column: 不良サイズ    # 正規化後の列名
    kind_column: 不良種類
    part_column: 検査部位
    time_column: 入口_通過日時
    by_kind: true              # 不良種類別カウント列を作る
    by_part: false             # 検査部位別カウント列（列爆発するため既定 off）
    drop_columns: [検査箇所]   # X/Y の冗長列

  trend:
    enabled: true
    include_suffixes: ["測定値", "計算値"]   # 末尾トークン一致で採用
    include_columns: []        # 非空ならこの明示リストのみ採用（include_suffixes より優先）
    exclude_columns: []
    mode: window               # window | point
    window_minutes: 11         # 中心窓（±5分）。mode=window のとき有効
    tolerance_minutes: 5       # merge_asof の許容差
    aggs: [mean]
    anchor_map: {}             # トークン -> traceability ソース名（規則で解決できない場合のみ記述）
    fallback_anchor_source: ブース
    on_no_overlap: warn_empty  # warn_empty | skip | error
    min_match_rate: 0.5        # 下回ると WARN

  assemble:
    date_from: null            # "2026-07-24"（null で全期間）
    date_to: null
    require_sources: []        # 0 行なら ValueError にするソース名
    max_columns_per_source: 200
```

---

## 10. エラー時挙動（一覧）

| 事象 | 挙動 |
|---|---|
| `data/raw/{kind}/` が無い / 空（repair 等） | INFO ログ、そのソース群をスキップ。正常終了 |
| 未知のサブディレクトリ | WARN、スキップ |
| VIN 列が見つからない（traceability/defect） | ERROR ログ、そのソースをスキップ（全体は継続） |
| 日時列が見つからない | ERROR ログ、そのソースをスキップ |
| CSV 読込例外（破損・エンコーディング不正） | ERROR ログ＋ファイル単位でスキップ。**マニフェストに記録しない**（次回再試行される） |
| 日時パース失敗率 > 1% | WARN、`date="unknown"` パーティションへ |
| 同一ソース内のヘッダ差異 | WARN＋レポート記録、継続 |
| 列名正規化の衝突 | `__2` 付与＋WARN、継続 |
| `merge(validate="one_to_one")` 違反 | 例外を捕まえず中断（設計前提の破れなので落とすべき） |
| pivot 後の列数 > `max_columns_per_source` | `ValueError` で中断 |
| trend マッチ率 0 | `on_no_overlap` に従う（既定 WARN＋全 NaN 列） |
| `0 <` trend マッチ率 `< min_match_rate` | WARN のみ |
| `require_sources` のソースが 0 行 | `ValueError` で中断 |
| レイクが空の状態で `assemble` 実行 | `ValueError`「先に convert を実行してください」 |

---

## 11. 実装順序（coder 用タスク分解）

各タスクは独立にレビュー・テスト可能な単位。上から順に実施する。
実行は必ず `.venv/bin/python`（`uv run` は壊れている）。
**追加インストールは不要**（`pyarrow 25.0.0` / `pandas 3.0.5` を実測確認済み）。

### T0: facts 補正（先にやる。以降の実装が依存する）
- 対象: `docs/real_data_facts.md`
- 内容: 本書 §1.1 の4点を「2026-07-30 追記（実測補正）」として追記。特に
  「`strip()` では不十分・全空白除去が必要」「サフィックスは a/b/c（ダミーは d/e）」
  「ダミー実値は `DUMMY-YOSHIKI` と `EMPTY`」「VIN 2形式混在」。
- 完了条件: 既存記述と矛盾しない形で追記され、既存の該当箇所に「※ 追記で補正」と注記されている。

### T1: `naming.py`
- 内容: §5 の `normalize_name` / `normalize_columns` / `prefixed` / `source_key`。
- 完了条件: §5 の対応表 7 例がすべて一致。冪等（`normalize_name(normalize_name(x)) == normalize_name(x)`）。

### T2: `vin_key.py`
- 内容: §6 の `VinPolicy` / `normalize_vin` / `join_key`。
- 完了条件: `"HE93S-122023     a"` → `vin="HE93S-122023a"`, `vin_base="HE93S-122023"`, `vin_pass_no=2`;
  `"JS3JB74V8V5106401 "` → `vin="JS3JB74V8V5106401"`, `vin_pass_no=1`;
  `"DUMMY-YOSHIKId"` / `"EMPTY"` / `""` → `vin_is_dummy=True`。

### T3: `raw_sources.py`
- 内容: §4 の `probe_header` / `discover_sources`。
- 完了条件: 実 `data/raw/` に対して traceability 11・trend 9・defect 2・repair 0 ソースを返し、
  各ソースの `time_column` が §4-5 の実測結果と一致する。**CSV 本体は読まない（`nrows=0` のみ）**。

### T4: `raw_convert.py`（変換）
- 内容: §7 の `convert_all` / `convert_file` / マニフェスト、`reports/column_name_mapping.csv` 出力。
- 完了条件: `.venv/bin/python main.py convert` が `data/lake/` に
  `{kind}/{source}/date=2026-07-24/` と `date=2026-07-25/`（trend は 07-29/07-30）を作る。
  2回目の実行で全ファイル `skipped=True`。`--force` で再変換。

### T5: `raw_convert.read_source`（読取）
- 内容: 列プルーニング＋日付フィルタ付き読取。
- 完了条件: `columns=[...]` で指定列のみが返る。存在しないソースで空 DataFrame。

### T6: `assemble.py` — traceability / defect（trend 以外）
- 内容: §8.1〜8.3、`reports/ingest_quality.csv` / `vin_panel_dictionary.csv` 出力。
- 完了条件: パネルが約 1,300 行、`present__*` が 11 列、`defect_*` 列が全て `defect_` で始まる、
  ダミー行が除外されている、`validate="one_to_one"` を通る。

### T7: `assemble.py` — trend 時刻結合
- 内容: §8.4、`reports/trend_anchor_map.csv` / `trend_join_report.csv` 出力。
- 完了条件: 実データで WARN「trend と anchor の期間が重複しません」が出て、trend 列が生成され全 NaN。
  `on_no_overlap: skip` で trend 列が消え、`error` で中断する。

### T8: CLI・config・README
- 内容: `cli.py` に `convert`（`--force`）/ `assemble`（`--date-from` / `--date-to`）を追加。
  `config.yaml` に §9 を追記。README に実データ経路の章を追加し、旧経路を「合成データ用（廃止候補）」と明記、
  p ≫ n の注意も記載。
- 完了条件: `python main.py convert && python main.py assemble` が通る。
  **`STAGES` / `ALL_ORDER` は未変更**で既存 89 テストが緑。

---

## 12. tester への申し送り（テスト観点）

原則: 実データ（41MB）を各テストで読み直さない。**小さな自作 fixture を主とし、実データは smoke 1本**。
trend 結合の正当性検証には期間が重複する fixture を自作する必要がある（実データでは検証不能）。
これは「テストを通すための細工」ではなく、実データに存在しない条件を補うための正当な fixture である。

### 12.1 `naming.py`
- 半角カナが全角に畳まれる（`ｿﾞｰﾝ#1 通過日時` → `ゾーン_1_通過日時`）。
- `#` 単独末尾が消える（`VIN#` → `VIN`、`ﾛﾎﾞｯﾄ#` → `ロボット`）。
- `(大)` / `PA-ON` / `&` の置換、連続 `_` の畳み込み、前後 `_` の除去。
- 衝突時に `__2` が付き、元名対応が両方保持される。
- 冪等性。

### 12.2 `vin_key.py`
- **内部空白を含むサフィックス VIN が正しく分解される**（`strip()` 実装なら落ちるケースを必ず 1 本置く）。
- `a`/`b`/`c` が `vin_pass_no` 2/3/4 になる。
- `DUMMY-YOSHIKId` / `EMPTY` / 空文字が `vin_is_dummy=True`。
- `suffix_policy="merge"` で結合キーが `vin_base` になる。
- 大文字化されない（末尾 `a` が `A` にならない）。

### 12.3 `raw_sources.py`
- `シーラー炉_202607.csv` / `シーラー炉_20260724.csv` / `シーラー炉.csv` が**同一ソース**にまとまる。
- `前処理・電着_202607.csv` → ソース名 `前処理_電着`。
- `time_column` 決定規則: `DATETIME` 優先。無ければ `通過日時` を含む**最初**の列
  （`入口` が存在しない `各工程滞在時間` 相当の fixture で `前処理_通過日時` が選ばれる）。
- VIN 列なしの traceability fixture がスキップされ ERROR ログが出る。

### 12.4 `raw_convert.py`
- **日付境界をまたぐ 1 ファイル**が 2 パーティションに分割される（`06:00` と翌 `02:00` の 2 行 fixture）。
- `chunksize=1` でも結果が同じ（チャンク分割の不変性）。
- 2回目実行で `skipped=True` / `mtime` を変えると再変換 / `force=True` で必ず再変換。
- 行数を減らして再変換したとき、古い part ファイルが残っていない。
- `積算値` で終わる列が float64、それ以外の float が float32。
- 日時パース不能行が `date=unknown` に入り WARN が出る。
- `read_source(columns=[...])` が指定列のみ返す / 存在しないソースで空 DataFrame。

### 12.5 `assemble.py`
- 1行/VIN ソースが `{source}__{col}` にプレフィクスされる。
- 複数行/VIN ソースが `mean/min/max/std` と `n_rows` に集約される（3行 fixture で値を手計算検証）。
- `pivot_by` 指定時に `{source}__{pivot値}__{col}` になり、`max_columns_per_source` 超過で `ValueError`。
- defect: 種類別カウントが `crosstab` と一致、`検査箇所` が落ちる、列が全て `defect_` で始まる。
- 台帳が全ソースの VIN 和集合になり `present__*` が正しい（A のみの VIN / B のみの VIN を含む fixture）。
- ダミー行が除外され、除外件数が `ingest_quality.csv` に記録される。
- **trend 結合（期間が重複する自作 fixture）**: 1分グリッド 20 行 ＋ アンカー 2 件で、
  `mode=window, window_minutes=3` の結果が手計算の中心移動平均と一致する。
  `tolerance` 外のアンカーが NaN になる。`mode=point` では窓平均されない。
- **trend 期間非重複 fixture**: `warn_empty` で列が全 NaN ＋ WARN、`skip` で列なし、`error` で例外。
- アンカー解決: `ブース_1_...` → `ブース`、`シーラー炉_...` → `シーラー炉`、
  `コンベア_...` → fallback、`anchor_map` の明示指定が規則より優先される。

### 12.6 統合 smoke（1本、`slow` マーク推奨）
`data/raw/` が存在する場合のみ実行。`convert` → `assemble` が例外なく完走し、
パネル行数 > 1000、trend 列が存在し `trend_match_rate == 0`、レポート 5 種が生成されること。

### 12.7 既存テストへの回帰条件
`cli.STAGES` / `ALL_ORDER` を変更しないので、既存 89 本
（`tests/test_transforms.py::test_ingest_writes_all_interim_tables` を含む）は無改修で緑であること。
**これを本タスクの回帰条件とする。**

---

## 13. 未確定事項（設計上そう扱う）

1. **【確定・2026-07-31】VIN サフィックス `a`/`b`/`c` の業務的意味** — ユーザー確認により
   「同一車体の2回目以降の通過」と判明（別車体ではない）。既定 `suffix_policy: keep`
   （upstream 工程の値を2回目通過の行に複製しない）は、この意味を前提にした §6 の根拠と整合するため
   **変更不要**。`vin_base` / `vin_pass_no` を常に保持する設計もそのまま活きる。
2. **【確定・2026-07-31】trend 結合の基準** — ユーザー判断により「trend は DATETIME 基準とする」
   と確定。設備の滞在時間を使った区間平均への変更は不要。現行の実装（trend 側で
   `rolling(center=True)` を DATETIME 軸に適用したうえで、traceability の入口通過時刻に
   `merge_asof(direction="nearest")` で最近傍点参照する方式。§8.4(3)）を**そのまま採用**する。
   窓幅（既定 ±5分）は今回の判断の対象外のため config 既定のまま据え置く。
3. **【確定・2026-07-31】1年分の配置形式** — ユーザー依頼により提案を提示（§15 新設）。
   結論: **日次ファイル・追記禁止（書いたら二度と変更しない）を推奨**。
   `source_key` が `_YYYY` / `_YYYYMM` / `_YYYYMMDD` / サフィックス無しを吸収し、
   日付はファイル名ではなく**中身の日時列**から決めるため、この推奨に対する**実装変更は不要**
   （現行コードのまま日次ファイルを投入できる）。詳細と根拠は §15。
4. **`閾値判定ﾌﾗｸﾞ` / `判定結果_3Bit` の値の意味**（実測値に `2` がある。OK/NG ではない）。
   → 数値としてそのまま持つ。フラグ解釈は行わない。
5. **上塗ロボットの測定値 0 の意味**（当該ロボットが塗装していない＝欠測 か、実測 0 か）。
   → 現設計は 0 を実測値として mean に含める。`n_rows` を併記して後から判断できるようにする。
6. **不良の「重大度」定義** — 実データに `severity` は無い。`不良サイズ` が代替になり得るが閾値は不明。
   → 本書では閾値を設けず統計量のみ出す。
7. **【確定・2026-07-31】ブツ検に行が無い VIN の意味** — ユーザー判断により「未検査」と確定。
   `assemble.py` を改修し、`defect_{source}__count`/`__has`/`__kind__*`/`__part__*` は
   defect ソースに一切登場しない VIN では **0 埋めせず NaN のまま**残すよう変更した
   （その VIN が defect ソースに登場するが特定の種類が無いだけの場合の 0 埋めは従来どおり）。

---

## 14. ユーザー確認事項（要回答）

**回答済み（2026-07-31）**:

- VIN サフィックス `a`/`b`/`c` → 同一車体の2回目以降の通過（§13-1 参照。実装変更不要）。
- ブツ検に行が無い VIN → 未検査（§13-7 参照。`assemble.py` を改修し NaN 化済み）。
- repair データ → `data/raw/repair/defect.csv` として提供された（[docs/real_data_repair_design.md](real_data_repair_design.md) で対応済み）。`PB-ON` は塗装工場投入時刻、`PB-OK` は出荷時刻と確認。
- **VIN の2形式**（型式ベース / 17桁フル VIN）→ 国内/国外仕様の差であり、同一車体を指す別表記ではない。
  つまりキー統合は不要（型式ベースと17桁フルVINが同じ車体を指して重複することはない）。`vin_format`
  列（`型式`/`full17`/`other`）は情報として保持するのみで、`vin_key` の結合ロジックは変更不要。
- **trend を VIN に紐付ける基準** → DATETIME 基準で確定（§13-2）。滞在時間区間平均への変更は不要。
- **工場共通ユーティリティ系 trend の紐付け先** → 「データ結合だけ行い、分析（どの工程に意味的に
  紐付くか）は後で検討する」という回答。現行の既定挙動（`fallback_anchor_source: ブース` へ
  フォールバックして機械的に結合する。§8.4(2) 規則4）をそのまま維持し、ユーティリティ系列の
  解釈・分析は本設計のスコープ外として先送りする。
- **1年分のデータの配置形式** → こちらから提案を提示（§15）。日次ファイル・追記禁止を推奨。
  実装変更は不要。

**未回答（引き続き要回答）**:

1. **`閾値判定ﾌﾗｸﾞ`（値に `2` がある）と `判定結果_3Bit` の値定義**を教えてください。
2. **上塗ロボットの測定値が 0 の行は「そのロボットは塗装していない」という意味ですか。**
3. **不良の重大度に相当する指標はありますか。** `不良サイズ` に運用上の閾値があれば教えてください。
4. **trend と traceability の期間が重複するサンプルを頂けますか。** 現サンプルでは trend 結合が
   一切検証できません（trend 07/29-30 / 他 07/24-25）。同一日の 1 日分でも構いません。

---

## 15. 1年分データの配置形式（提案・2026-07-31）

### 結論

**日次ファイル・書いたら二度と変更しない（append-only）方式を推奨する。**

```text
data/raw/traceability/{設備名}_YYYYMMDD.csv     例: ブース_20260724.csv
data/raw/trend/{設備名}_YYYYMMDD.csv            例: コンベア_20260724.csv
data/raw/defect/{検査工程名}_YYYYMMDD.csv       例: 上塗ブツ検_20260724.csv
data/raw/repair/{既存の命名（例: defect.csv）}   ※ 日次で洗い替えなら別名にするか要相談（下記注意）
```

1日分のデータが確定したら、その日付のファイルを1回だけ書き出し、以後は内容もファイル名も変更しない。
翌日分は新しいファイル（`_20260725.csv` 等）として追加する。

### 根拠

1. **今回提供されたサンプルの実体と一致する**。ファイル名は `_202607`（月）だが中身は実際には
   7/24 1日分だった。データの生成粒度が「1日単位」であることは既に実測済みであり、
   ファイル名もその粒度に合わせるのが最も誤解が少ない。
2. **増分変換（convert のマニフェスト判定）と相性が良い**。`raw_convert.py` は
   ファイルの `(size, mtime_ns)` が前回と一致すれば再変換をスキップする（§7.2 手順1）。
   日次ファイルは一度書いたら変更されないため、2日目以降は「新しく増えたファイルだけ」が
   変換対象になり、変換コストは**その日の増分のみ**で済む。
   - 対して「月次1ファイルに毎日追記」する方式だと、月の後半になるほどファイルが肥大化し、
     かつ追記のたびに `size`/`mtime` が変わるため**その月のファイル全体が毎回再変換対象になる**
     （増分の意味が薄れ、月末には約30日分を毎日読み直すことになる）。
   - 「月次1ファイルを月末に確定」する方式も検討しうるが、月内はデータが分析に使えず
     鮮度が落ちるため推奨しない。
3. **実装変更が不要**。`naming.source_key()` は既に `_YYYY` / `_YYYYMM` / `_YYYYMMDD` /
   サフィックス無しの4パターンを吸収し（`_DATE_SUFFIX_RE = r"_(\d{4}|\d{6}|\d{8})$"`）、
   同一 `(kind, ソース名)` の全ファイルを1つの `RawSource` にまとめる。日付パーティションは
   ファイル名からではなく**各行の日時列の実際の値**から決まる（§7.2 手順3-5）ため、
   日次ファイルをそのまま `data/raw/{kind}/` に置くだけで、コード側の変更なく正しく
   日付パーティション分割される。
4. **ファイル数の増加は許容範囲**。1年 ≈ 250稼働日 × 22ソース ≈ 5,500 ファイル。
   Parquet レイク側も同数程度のパーティションになるが、`read_source` は
   hive パーティションの `filters` で日付を絞ってから読むため、ファイル数の増加は
   読み取り性能にほぼ影響しない。

### 注意点（repair のみ別扱いが必要）

repair（`data/raw/repair/defect.csv`）は現在**単一ファイル・日付サフィックス無し**で提供されており、
`real_ingest.source_aliases` の既定 `repair/defect: 修正` はこの命名を前提にしている
（[docs/real_data_repair_design.md](real_data_repair_design.md) §8-7 参照）。
repair も日次ファイルに切り替える場合は、ファイル名が変わることで
`source_aliases` の対象外になり、ソース名が変わってレイクのディレクトリが分かれてしまう。
**repair を日次化する場合は、その時点で `source_aliases` の設定を追記する運用とする**
（設計変更は不要、config 追記のみ）。単一ファイルを洗い替え運用する場合は、増分変換の効果が
薄れる点（上記2と同じ理由）に注意されたい。
