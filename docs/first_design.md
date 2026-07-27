# データ分析パイプライン全体設計（修正版）

## 0. 目的

設備データ（設備ごと・月ごとCSV）と不良データ、修正データをVINで統合し、可視化・統計・機械学習・必要時の深層学習までを一気通貫で実施する。

---

## 1. 複数の設備データファイルからデータを集計する

### 目的

- 分割保存された設備CSVを自動で収集し、統一スキーマで結合する

### 主な処理

- ファイル探索（設備ID・年月を取得）
- カラム名統一、型統一、日時変換
- 異常ファイルの除外・ログ化

### 必要メソッド

- ファイル操作: `pathlib.Path.rglob`, `glob.glob`, `re.search`
- 読込: `pd.read_csv`（または `pl.read_csv`, `pl.scan_csv`）
- 整形: `rename`, `astype`, `to_datetime`, `concat`
- 品質確認: `isna().sum`, `duplicated`, `nunique`
- ログ: `logging.info/warning/error`

### 出力

- 統合設備テーブル（推奨: `parquet`）

---

## 2. 設備 | 不良 | 修正データを集計・統合する

### 目的

- VIN軸で3データを結合可能な形に整える

### 主な処理

- 設備: VIN 1:1 を基準

- 不良: VIN 1:N をVIN単位に事前集約

- 修正: VIN 0..1 をleft join（欠損=未修正）

### 必要メソッド

- 不良集約: `groupby('vin').agg`, `size`, `nunique`, `min`, `max`

- 結合: `merge(on='vin', how='left', validate=...)`

- 突合検証: `merge(..., indicator=True)`

- クロス確認: `crosstab`, `pivot_table`

### 出力

- VIN単位統合テーブル（分析用マート）

---

## 3. データ加工（新規列作成・特徴量化）

### 目的

- 分析/予測に有効な説明変数を作る

### 主な加工

- 時間特徴: 加工時間、滞留時間、リードタイム

- 修正特徴: `has_repair`（0/1）、修正まで時間

- 不良特徴: 不良件数、重大不良件数、不良種別数

- カテゴリ統合: 細分類を中分類/大分類に集約、低頻度を`OTHER`

### 必要メソッド

- 時間差分: `sort_values`, `groupby().shift`, `dt.total_seconds`

- 条件列: `np.where`, `assign`, `apply`（必要最小限）

- カテゴリ統合: `map(dict)`, `replace`, `where`

- 欠損処理: `fillna`, `dropna`

- 外れ値処理: `clip`, `quantile`

### 出力

- 特徴量テーブル（推奨: `parquet`、版管理付き）

---

## 4. データ型に合わせたグラフ化（EDA）

### 目的

- 分布、群差、関係性、時系列傾向を把握する

### グラフ方針（データ型別）

- 数値: ヒストグラム、箱ひげ

- 数値×数値: 散布図、ペアプロット

- 数値×カテゴリ: 層別箱ひげ、バイオリン

- 時系列: 月次トレンド線

### 必要メソッド

- 可視化: `sns.histplot`, `sns.boxplot`, `sns.pairplot`, `sns.lineplot`, `sns.barplot`

- 集計補助: `groupby`, `resample`, `pivot_table`

- 出力: `plt.savefig`, `plt.close`

### 出力

- 自動生成グラフ（設備別/月別フォルダ保存）

---

## 5. 統計手法で相関・群間差を判断する

### 目的

- 関係性の有無を定量的に把握し、特徴量選別の初期判断に使う

### 主な手法

- 数値×数値: Pearson/Spearman相関

- 数値×2群: t検定 or Mann-Whitney U

- カテゴリ×カテゴリ: カイ二乗検定

- 多群比較: ANOVA/Kruskal-Wallis

必要メソッド

- `scipy.stats.pearsonr`, `spearmanr`

- `ttest_ind`, `mannwhitneyu`

- `chi2_contingency`

- `f_oneway`, `kruskal`

### 判断ルール（推奨）

- 「相関低い=即削除」はしない

- 単変量結果は**候補判断**に留め、後段モデルで再評価

---

## 6. 機械学習で予測精度を判断する（主戦略）

### 目的

- 不良・重大度・修正要否を予測し、実用的な精度を確認する

### タスク例

- 回帰: 不良点数予測

- 分類: 重大不良有無、修正要否

### 推奨モデル順

1. ベースライン（線形/ロジスティック）

2. 木モデル（RandomForest）

3. 本命（LightGBM）

### 必要メソッド

- 分割/検証: `train_test_split`, `KFold`, `StratifiedKFold`, `TimeSeriesSplit`

- 前処理: `Pipeline`, `ColumnTransformer`, `OneHotEncoder`, `StandardScaler`

- 学習: `LinearRegression`, `LogisticRegression`, `RandomForest*`, `LGBMRegressor/Classifier`

- 評価: `MAE`, `RMSE`, `R2`, `ROC-AUC`, `PR-AUC`, `F1`, `Recall`

- 解釈: `feature_importances_`, `permutation_importance`, `shap.TreeExplainer`

### 出力

- モデル性能レポート（CV結果、特徴量重要度、しきい値別性能）

---

## 7. 必要に応じて深層学習（LSTM等）

### 適用条件

- VINごとに時系列イベント列が十分ある

- 単純集約より系列情報が効くと想定される

### 主な手法

- LSTM（工程イベント系列）

- MLP（大規模表形式）

- （必要なら）Transformer系

### 必要メソッド

- 前処理: `pad_sequences`, 系列長統一、マスク処理

- 学習: `keras.Sequential`, `LSTM`, `Dense`, `Dropout`

- 学習制御: `EarlyStopping`, `ModelCheckpoint`

- 評価: 回帰/分類タスク別指標（上記と同様）

### 注意

- まずLightGBM基準を作り、**上回る場合のみ採用**

---

## 8. 運用・自動化（全体共通）

### 必須

- 中間成果物をParquet保存

- 設定ファイル化（パス、対象期間、閾値）

- ログ・エラーハンドリング

- 定期実行（cron/Task Scheduler）

### 必要メソッド

- 入出力: `read_parquet`, `to_parquet`, `to_csv`

- CLI化: `argparse`

- 設定管理: `yaml/json` 読込

- 実行制御: `main()`, `try/except`, `logging`

---

## 9. 全体フロー（最終版）

1. 設備CSV群の自動収集・統合

2. 不良データVIN集約 + 修正データ整形

3. 設備を基準にVIN結合

4. 特徴量作成（時間/不良/修正/カテゴリ統合）

5. データ型別に自動グラフ生成

6. 統計検定で関係性を評価

7. 機械学習で予測性能を評価（主軸: LightGBM）

8. 条件を満たす場合のみ深層学習を追加

9. レポート・成果物を自動保存
