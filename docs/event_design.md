# イベント台帳テンプレート（4M: Man / Machine / Material / Method）

## 1. 目的

データに現れない変化点（人・設備・材料・方法）を日単位で記録し、不良分析・予測モデルの説明変数として利用する。

---

## 2. 記録粒度

- 基本粒度: **1行 = 1イベント**

- 日付粒度: 日単位（必要に応じて時刻も入力）

- 紐付けキー: 日付 + 工場 + ライン + 設備（可能な範囲）

---

## 3. CSVカラム定義（推奨）

| カラム名 | 型 | 必須 | 説明 | 例 |

|---|---|---|---|---|

| event_id | string | 必須 | イベント一意ID | EVT-20260724-001 |

| event_date | date | 必須 | 発生日 | 2026-07-24 |

| event_time | string | 任意 | 発生時刻（HH:MM） | 13:40 |

| plant_code | string | 必須 | 工場コード | P01 |

| line_code | string | 必須 | ラインコード | L-A |

| equipment_id | string | 任意 | 設備ID（対象設備がある場合） | EQ-12 |

| category_4m | string | 必須 | `Man/Machine/Material/Method` | Machine |

| event_type | string | 必須 | イベント種別（下表） | maintenance |

| event_subtype | string | 任意 | 詳細種別 | 定期点検 |

| severity | int | 必須 | 重大度（1=軽微,2=中,3=重大） | 2 |

| start_ts | datetime | 任意 | 開始日時 | 2026-07-24 13:40:00 |

| end_ts | datetime | 任意 | 終了日時 | 2026-07-24 15:10:00 |

| duration_min | int | 任意 | 継続時間（分） | 90 |

| impact_scope | string | 任意 | 影響範囲（ライン全体/設備のみ/工程のみ） | ライン全体 |

| planned_flag | int | 必須 | 計画イベントか（1=計画,0=突発） | 1 |

| change_flag | int | 必須 | 変更イベントか（1/0） | 1 |

| recovery_action | string | 任意 | 実施した対処 | ベルト交換 |

| status | string | 必須 | `open/closed` | closed |

| suspected_quality_impact | int | 必須 | 品質影響見込み（0/1） | 1 |

| related_lot | string | 任意 | 関連ロット | LOT-8891 |

| related_vin_count | int | 任意 | 影響VIN台数（概算） | 120 |

| recorder | string | 必須 | 記録者 | sato |

| approver | string | 任意 | 確認者 | tanaka |

| note | string | 任意 | 自由記述 | 交換後に振動低下 |

---

## 4. event_type マスタ（推奨値）

### Man

- `man_replacement`（人の入れ替え）

- `man_shortage`（欠員）

- `process_assignment_change`（担当工程変更）

### Machine

- `inspection`（点検）

- `maintenance`（整備）

- `parts_replacement`（部品交換）

- `line_trouble`（ライントラブル）

### Material

- `material_change`（材料交換）

- `material_deterioration`（材料劣化）

### Method

- `work_method_change`（作業方法変更）

- `jig_change`（治具変更）

---

## 5. CSVサンプル

```csv

event_id,event_date,event_time,plant_code,line_code,equipment_id,category_4m,event_type,event_subtype,severity,start_ts,end_ts,duration_min,impact_scope,planned_flag,change_flag,recovery_action,status,suspected_quality_impact,related_lot,related_vin_count,recorder,approver,note

EVT-20260724-001,2026-07-24,08:50,P01,L-A,EQ-12,Machine,inspection,定期点検,1,2026-07-24 08:50:00,2026-07-24 09:20:00,30,設備のみ,1,0,点検のみ,closed,0,,0,sato,tanaka,異常なし

EVT-20260724-002,2026-07-24,10:10,P01,L-A,EQ-08,Machine,line_trouble,センサー通信断,3,2026-07-24 10:10:00,2026-07-24 11:05:00,55,ライン全体,0,1,通信ケーブル交換,closed,1,LOT-8891,120,sato,tanaka,停止中に滞留増加

EVT-20260725-001,2026-07-25,13:30,P01,L-A,,Man,man_shortage,欠員補充遅れ,2,2026-07-25 13:30:00,2026-07-25 17:00:00,210,工程のみ,0,1,応援者配置,closed,1,,80,suzuki,,作業遅延あり

EVT-20260726-001,2026-07-26,09:00,P01,L-B,EQ-03,Material,material_change,サプライヤ切替,2,2026-07-26 09:00:00,2026-07-26 09:40:00,40,ライン全体,1,1,条件再調整,closed,1,LOT-9002,200,yamada,ito,初期ロット監視要

EVT-20260727-001,2026-07-27,14:15,P01,L-B,EQ-03,Method,jig_change,位置決め治具変更,2,2026-07-27 14:15:00,2026-07-27 15:00:00,45,工程のみ,1,1,作業手順書改訂,closed,1,,150,yamada,ito,変更後の寸法ばらつき確認

 

6. 入力ルール（運用）

event_id は必ず一意

event_date は必須（時刻不明でも日付だけ記録）

severity は 1/2/3 の3段階で統一

planned_flag, change_flag, suspected_quality_impact は 0/1

note は原因仮説・現場所見を簡潔に残す（後でカテゴリ化可能）

7. 分析用に作る派生列（後処理）

event_flag_day（当日イベント有無）

event_count_day（日次イベント件数）

severity_sum_day（日次重大度合計）

machine_trouble_flag_day（line_trouble有無）

material_change_flag_day

method_change_flag_day

days_since_last_event（最終イベントからの経過日数）

impact_window_3d/7d（イベント後3日/7日影響フラグ）