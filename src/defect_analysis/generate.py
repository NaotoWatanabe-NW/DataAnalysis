"""合成データ生成（実データ導入時はこの工程を差し替える）。

生成する 4 データと VIN に対する基数:
    traceability : VIN 1:1（設備ごと・月ごとの分割 CSV）
    trend        : VIN 1:1（設備ごと・月ごとの分割 CSV）
    defect       : VIN 1:N（不良の無い VIN は行なし）
    repair       : VIN 0..1（修正の無い VIN は行なし）

設備トレンドのドライバ測定値と不良の間に因果（信号）を注入するため、
後段の統計検定・機械学習で実際に検出可能な関係が生まれる。
生データは実運用を模して常に CSV（storage.format は中間/処理済のみに適用）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config

logger = logging.getLogger(__name__)

TREND_MEASURES = ["torque", "temperature", "pressure", "vibration"]

# generate() が書き込むサブディレクトリ（High-2: raw_dir 共有時の実データ混入ガード対象）
_WRITE_KINDS = ("traceability", "trend", "defect", "repair")
# このマーカーが raw_dir 直下にあれば「過去に generate が書き込んだ場所」とみなす。
_GENERATE_MARKER_FILENAME = ".generate_marker.json"

# 不良カテゴリ -> 不良コード / 修正アクション
_CATEGORY_CODE = {
    "寸法不良": "D001",
    "組付不良": "D002",
    "外観不良": "D003",
    "機能不良": "D004",
    "その他": "D009",
}
_REPAIR_ACTION = {
    "寸法不良": "寸法調整",
    "組付不良": "組付やり直し",
    "外観不良": "再塗装",
    "機能不良": "部品交換",
    "その他": "点検調整",
}


def _guard_raw_dir_before_write(raw_dir: Path, *, force: bool) -> None:
    """書き込み前ガード（High-2）。

    `paths.raw_dir`（generate の書き込み先）と `real_ingest.raw_dir`（convert の読込元）が
    同一ディレクトリに設定されていると、generate の合成 CSV が実データに混入し、
    discover_sources が別ソースとして誤認識してしまう。
    raw_dir に generate 由来のマーカーが無いのに kind サブディレクトリへ既に CSV が
    置かれている場合は、実データが置かれている可能性があるとみなして中断する。
    """
    if force:
        return
    marker = raw_dir / _GENERATE_MARKER_FILENAME
    if marker.exists():
        return
    for kind in _WRITE_KINDS:
        kind_dir = raw_dir / kind
        if kind_dir.exists() and any(kind_dir.glob("*.csv")):
            raise ValueError(
                f"{raw_dir} には generate（合成データ）由来でない CSV が既に存在する可能性があります"
                f"（{kind_dir}）。実データが置かれている可能性があるため書き込みを中断しました。"
                " config の paths.raw_dir を別ディレクトリに変更するか、対象ディレクトリを空にしてください。"
                " 強制的に上書きする場合は generate(cfg, force=True) "
                "（CLI: `python main.py generate --force`）を使ってください。"
            )


def _write_generate_marker(raw_dir: Path) -> None:
    """generate() が正常に書き込んだ後、raw_dir がこのモジュール由来であることを記録する。"""
    marker = raw_dir / _GENERATE_MARKER_FILENAME
    marker.write_text(
        json.dumps(
            {
                "generated_by": "defect_analysis.generate",
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _zscore(x: np.ndarray) -> np.ndarray:
    std = x.std()
    if std == 0:
        return np.zeros_like(x)
    return (x - x.mean()) / std


def generate(cfg: Config, *, force: bool = False) -> dict[str, int]:
    """設定に基づき 4 データの分割 CSV を data/raw 配下へ書き出す。

    force=True でなければ、書き込み前に `_guard_raw_dir_before_write` で
    実データ混入の可能性をチェックする（High-2）。
    """
    s = cfg.get("synthesize")
    seed = int(cfg.get("project.random_seed", 42))
    rng = np.random.default_rng(seed)

    n = int(s["n_vin"])
    months: list[str] = list(s["months"])
    plant = s["plant"]
    lines = list(s["lines"])
    operators = list(s["operators"])
    rel_std = float(s["trend_rel_std"])
    driver_gain = float(s["driver_gain"])
    equipments = list(s["equipments"])
    drivers = list(s["defect_drivers"])
    random_defect_rate = float(s["random_defect_rate"])
    repair_rate = float(s["repair_rate_given_defect"])

    raw_dir = cfg.path("paths.raw_dir", create=True)
    _guard_raw_dir_before_write(raw_dir, force=force)

    # ---- VIN 台帳（1:1 の基準）----------------------------------------
    vins = np.array([f"VIN{i:07d}" for i in range(n)])
    month_of_vin = rng.choice(months, size=n)
    line_of_vin = rng.choice(lines, size=n)
    op_of_vin = rng.choice(operators, size=n)
    lot_of_vin = np.array(
        [f"LOT-{m}-{rng.integers(1, 21):02d}" for m in month_of_vin]
    )
    # VIN ごとの潜在品質 q（大きいほどドライバ測定値が上振れ→不良が出やすい）
    q = rng.standard_normal(n)

    # 生産開始時刻: 各 VIN の割当月内のランダムな日時
    month_start = pd.to_datetime(pd.Series(month_of_vin) + "-01")
    day_offset = rng.integers(0, 27, size=n)
    sec_offset = rng.integers(6 * 3600, 22 * 3600, size=n)  # 6:00〜22:00
    cur_in = month_start + pd.to_timedelta(day_offset, unit="D") + pd.to_timedelta(sec_offset, unit="s")

    # ---- 設備を工程順に通過させて trace / trend を生成 ------------------
    driver_keys = {(d["equipment_id"], d["measure"]) for d in drivers}
    driver_values: dict[tuple[str, str], np.ndarray] = {}

    trace_frames: list[pd.DataFrame] = []
    trend_frames: list[pd.DataFrame] = []
    last_out: pd.Series | None = None

    for pos, eq in enumerate(equipments):
        eq_id = eq["id"]
        means = eq["trend_means"]

        # トレンド測定値（非ドライバは純ノイズ、ドライバは q に連動）
        measure_cols: dict[str, np.ndarray] = {}
        for m in TREND_MEASURES:
            base = float(means[m])
            vals = base * (1.0 + rel_std * rng.standard_normal(n))
            if (eq_id, m) in driver_keys:
                vals = vals + base * rel_std * driver_gain * q
                driver_values[(eq_id, m)] = vals
            measure_cols[m] = vals

        # サイクルタイムと設備 IN/OUT
        cycle_mean = 45.0 + 15.0 * pos
        cycle = np.clip(rng.normal(cycle_mean, cycle_mean * 0.15, size=n), 5.0, None)
        in_ts = cur_in.copy()
        out_ts = in_ts + pd.to_timedelta(cycle, unit="s")
        # 次工程までの搬送/滞留（30〜300 秒）
        gap = rng.integers(30, 300, size=n)
        cur_in = out_ts + pd.to_timedelta(gap, unit="s")
        last_out = out_ts

        # 設備判定 OK/NG: ドライバ設備はドライバ測定値の外れで NG になりやすい
        if any(k[0] == eq_id for k in driver_keys):
            drv_measure = next(k[1] for k in driver_keys if k[0] == eq_id)
            z = _zscore(measure_cols[drv_measure])
            ng_prob = _sigmoid(-3.2 + 1.2 * z)
        else:
            ng_prob = np.full(n, 0.003)
        judgment = np.where(rng.random(n) < ng_prob, "NG", "OK")

        trace_frames.append(
            pd.DataFrame(
                {
                    "vin": vins,
                    "equipment_id": eq_id,
                    "equipment_name": eq["name"],
                    "plant_code": plant,
                    "line_code": line_of_vin,
                    "process_month": month_of_vin,
                    "operator": op_of_vin,
                    "lot_no": lot_of_vin,
                    "in_ts": in_ts.dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "out_ts": out_ts.dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "cycle_time_sec": np.round(cycle, 1),
                    "judgment": judgment,
                }
            )
        )
        trend_frames.append(
            pd.DataFrame(
                {
                    "vin": vins,
                    "equipment_id": eq_id,
                    "process_month": month_of_vin,
                    **{m: np.round(measure_cols[m], 4) for m in TREND_MEASURES},
                }
            )
        )

    assert last_out is not None

    # ---- 不良データ（VIN 1:N）-----------------------------------------
    defect_records: list[dict] = []
    defect_seq = 0

    def _append_defect(idx: int, category: str, severity: int, eq_id: str) -> None:
        nonlocal defect_seq
        defect_seq += 1
        base = last_out.iloc[idx]
        ddate = base + pd.Timedelta(days=int(rng.integers(0, 15)), hours=int(rng.integers(0, 24)))
        defect_records.append(
            {
                "vin": vins[idx],
                "defect_id": f"DFT-{ddate:%Y%m}-{defect_seq:06d}",
                "defect_date": ddate.strftime("%Y-%m-%d %H:%M:%S"),
                "detected_equipment_id": eq_id,
                "defect_code": _CATEGORY_CODE[category],
                "defect_category": category,
                "severity": severity,
            }
        )

    for d in drivers:
        key = (d["equipment_id"], d["measure"])
        vals = driver_values[key]
        prob = _sigmoid(float(d["intercept"]) + float(d["slope"]) * _zscore(vals))
        hit_idx = np.nonzero(rng.random(n) < prob)[0]
        sev_w = np.asarray(d["severity_weights"], dtype=float)
        sev_w = sev_w / sev_w.sum()
        for idx in hit_idx:
            severity = int(rng.choice([1, 2, 3], p=sev_w))
            _append_defect(int(idx), d["category"], severity, d["equipment_id"])

    # ドライバに依らない偶発不良（その他）
    other_idx = np.nonzero(rng.random(n) < random_defect_rate)[0]
    for idx in other_idx:
        severity = int(rng.choice([1, 2, 3], p=[0.7, 0.25, 0.05]))
        det_eq = str(rng.choice([e["id"] for e in equipments]))
        _append_defect(int(idx), "その他", severity, det_eq)

    defect_df = pd.DataFrame(defect_records)

    # ---- 修正データ（VIN 0..1、不良ありVINの一部のみ）------------------
    repair_records: list[dict] = []
    if not defect_df.empty:
        parsed_date = pd.to_datetime(defect_df["defect_date"])
        for vin, grp in defect_df.groupby("vin", sort=False):
            if rng.random() >= repair_rate:
                continue
            # 最重篤の不良を修正対象にする
            worst = grp.loc[grp["severity"].idxmax()]
            base = parsed_date.loc[worst.name]
            rdate = base + pd.Timedelta(days=int(rng.integers(0, 8)), hours=int(rng.integers(0, 24)))
            severity = int(worst["severity"])
            rtime = float(np.clip(rng.normal(30 * severity, 8 * severity), 5, None))
            repair_records.append(
                {
                    "vin": vin,
                    "repair_date": rdate.strftime("%Y-%m-%d %H:%M:%S"),
                    "repaired_defect_code": worst["defect_code"],
                    "repair_action": _REPAIR_ACTION[worst["defect_category"]],
                    "repair_time_min": round(rtime, 1),
                }
            )
    repair_df = pd.DataFrame(repair_records)

    # ---- 書き出し（生データは常に CSV、分割単位で保存）------------------
    counts = _write_all(cfg, raw_dir, trace_frames, trend_frames, defect_df, repair_df)
    _write_generate_marker(raw_dir)

    realized_defect_rate = defect_df["vin"].nunique() / n if not defect_df.empty else 0.0
    logger.info(
        "合成データ生成完了: VIN=%d, 不良行=%d(不良ありVIN率=%.1f%%), 修正行=%d",
        n,
        counts["defect_rows"],
        realized_defect_rate * 100,
        counts["repair_rows"],
    )
    return counts


def _write_all(
    cfg: Config,
    raw_dir,
    trace_frames: list[pd.DataFrame],
    trend_frames: list[pd.DataFrame],
    defect_df: pd.DataFrame,
    repair_df: pd.DataFrame,
) -> dict[str, int]:
    trace = pd.concat(trace_frames, ignore_index=True)
    trend = pd.concat(trend_frames, ignore_index=True)

    n_trace_files = _write_grouped_by(
        trace, ["equipment_id", "process_month"], raw_dir / "traceability",
        lambda k: f"{k[0]}_{k[1]}.csv",
    )
    n_trend_files = _write_grouped_by(
        trend, ["equipment_id", "process_month"], raw_dir / "trend",
        lambda k: f"{k[0]}_{k[1]}.csv",
    )

    n_defect_files = 0
    if not defect_df.empty:
        tmp = defect_df.assign(_m=pd.to_datetime(defect_df["defect_date"]).dt.strftime("%Y-%m"))
        n_defect_files = _write_grouped_by(
            tmp, ["_m"], raw_dir / "defect", lambda k: f"defect_{k[0]}.csv", drop=["_m"],
        )

    n_repair_files = 0
    if not repair_df.empty:
        tmp = repair_df.assign(_m=pd.to_datetime(repair_df["repair_date"]).dt.strftime("%Y-%m"))
        n_repair_files = _write_grouped_by(
            tmp, ["_m"], raw_dir / "repair", lambda k: f"repair_{k[0]}.csv", drop=["_m"],
        )

    return {
        "trace_rows": len(trace),
        "trend_rows": len(trend),
        "defect_rows": len(defect_df),
        "repair_rows": len(repair_df),
        "trace_files": n_trace_files,
        "trend_files": n_trend_files,
        "defect_files": n_defect_files,
        "repair_files": n_repair_files,
    }


def _write_grouped_by(df, keys, directory, name_fn, drop: list[str] | None = None) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    for key, grp in df.groupby(keys, sort=True):
        key_tuple = key if isinstance(key, tuple) else (key,)
        out = grp.drop(columns=drop) if drop else grp
        out.to_csv(directory / name_fn(key_tuple), index=False)
        count += 1
    return count
