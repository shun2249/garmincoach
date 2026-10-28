"""結構化資料模型。

兩個核心層：
* DailyRecovery  – 生理恢復（每天一筆）：HRV / RHR / 睡眠 / Stress / Body Battery / 訓練準備度 / 負荷
* TrainingSession – 每一次訓練：配速、功率、HR Drift、訓練負荷、外部環境（氣溫/濕度/風/AQI/地形）
* SessionFeedback – 使用者主觀回饋：RPE、體感、疼痛、生病
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class DailyRecovery(BaseModel):
    date: date

    # --- HRV（夜間）---
    hrv_last_night: Optional[float] = None        # ms，夜間平均
    hrv_5min_high: Optional[float] = None         # ms，夜間 5 分鐘最高
    hrv_weekly_avg: Optional[float] = None
    hrv_status: Optional[str] = None              # BALANCED / LOW / UNBALANCED / POOR
    hrv_baseline_low: Optional[float] = None
    hrv_baseline_high: Optional[float] = None

    # --- 心率 ---
    resting_hr: Optional[int] = None
    resting_hr_7d_avg: Optional[int] = None
    min_hr: Optional[int] = None
    max_hr: Optional[int] = None

    # --- 睡眠 ---
    sleep_score: Optional[int] = None
    sleep_quality: Optional[str] = None
    sleep_total_min: Optional[float] = None
    sleep_deep_min: Optional[float] = None
    sleep_light_min: Optional[float] = None
    sleep_rem_min: Optional[float] = None
    sleep_awake_min: Optional[float] = None
    sleep_start: Optional[datetime] = None
    sleep_end: Optional[datetime] = None
    sleep_avg_spo2: Optional[float] = None
    sleep_avg_respiration: Optional[float] = None
    sleep_body_battery_change: Optional[int] = None

    # --- 壓力 ---
    stress_avg: Optional[int] = None
    stress_max: Optional[int] = None
    stress_qualifier: Optional[str] = None
    stress_rest_min: Optional[float] = None
    stress_low_min: Optional[float] = None
    stress_medium_min: Optional[float] = None
    stress_high_min: Optional[float] = None

    # --- Body Battery ---
    bb_high: Optional[int] = None
    bb_low: Optional[int] = None
    bb_charged: Optional[int] = None
    bb_drained: Optional[int] = None
    bb_latest: Optional[int] = None

    # --- 訓練準備度 / 恢復 ---
    readiness_score: Optional[int] = None
    readiness_level: Optional[str] = None
    readiness_feedback: Optional[str] = None
    recovery_time_hours: Optional[float] = None

    # --- 體能 / 負荷 ---
    vo2max_run: Optional[float] = None
    vo2max_cycle: Optional[float] = None
    fitness_age: Optional[float] = None
    training_status: Optional[str] = None
    acute_load: Optional[float] = None
    chronic_load: Optional[float] = None
    acwr: Optional[float] = None                  # 急慢性負荷比
    acwr_status: Optional[str] = None
    load_aerobic_low: Optional[float] = None
    load_aerobic_high: Optional[float] = None
    load_anaerobic: Optional[float] = None
    load_balance_feedback: Optional[str] = None
    heat_acclimation_pct: Optional[int] = None    # 熱適應 %（台灣夏天很重要）
    heat_trend: Optional[str] = None

    # --- 日常活動量 ---
    steps: Optional[int] = None
    active_kcal: Optional[int] = None
    intensity_min_moderate: Optional[int] = None
    intensity_min_vigorous: Optional[int] = None
    avg_spo2: Optional[float] = None

    sources: list[str] = Field(default_factory=list)   # 成功取得的原始資料來源
    synced_at: Optional[datetime] = None


class Split(BaseModel):
    index: int
    intensity: Optional[str] = None               # ACTIVE / REST / WARMUP / COOLDOWN
    distance_m: Optional[float] = None
    duration_s: Optional[float] = None
    avg_pace_s_per_km: Optional[float] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    avg_power: Optional[float] = None
    avg_cadence: Optional[float] = None
    elevation_gain_m: Optional[float] = None
    elevation_loss_m: Optional[float] = None


class TrainingSession(BaseModel):
    activity_id: int
    name: Optional[str] = None
    sport: Optional[str] = None                   # running / cycling / trail_running ...
    start_time: Optional[datetime] = None         # 當地時間
    start_time_utc: Optional[datetime] = None
    timezone: Optional[str] = None
    duration_s: Optional[float] = None
    moving_s: Optional[float] = None
    elapsed_s: Optional[float] = None
    distance_m: Optional[float] = None
    calories: Optional[int] = None

    # --- 配速 / 速度 ---
    avg_speed_mps: Optional[float] = None
    max_speed_mps: Optional[float] = None
    avg_pace_s_per_km: Optional[float] = None
    avg_gap_pace_s_per_km: Optional[float] = None  # grade adjusted pace

    # --- 心率 ---
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    hr_zone_s: dict[str, float] = Field(default_factory=dict)   # {"z1": sec, ...}
    hr_drift_pct: Optional[float] = None           # 後半 HR / 前半 HR - 1
    pa_hr_decoupling_pct: Optional[float] = None   # 配速/心率 脫鉤（Pa:Hr）
    pw_hr_decoupling_pct: Optional[float] = None   # 功率/心率 脫鉤（Pw:Hr）

    # --- 功率 ---
    avg_power: Optional[float] = None
    max_power: Optional[float] = None
    normalized_power: Optional[float] = None
    power_zone_s: dict[str, float] = Field(default_factory=dict)
    tss: Optional[float] = None
    intensity_factor: Optional[float] = None

    # --- 跑步動態 ---
    avg_cadence: Optional[float] = None
    avg_stride_len_m: Optional[float] = None
    avg_vertical_osc_cm: Optional[float] = None
    avg_gct_ms: Optional[float] = None
    avg_vertical_ratio: Optional[float] = None

    # --- 訓練效果 / 負荷 ---
    aerobic_te: Optional[float] = None
    anaerobic_te: Optional[float] = None
    te_label: Optional[str] = None
    training_load: Optional[float] = None
    vo2max: Optional[float] = None

    # --- 外部環境 ---
    temp_c: Optional[float] = None
    apparent_temp_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    dew_point_c: Optional[float] = None
    wind_kph: Optional[float] = None
    wind_dir: Optional[str] = None
    weather_desc: Optional[str] = None
    device_temp_min_c: Optional[float] = None
    device_temp_max_c: Optional[float] = None
    aqi_us: Optional[int] = None
    pm25: Optional[float] = None

    # --- 地形 ---
    elevation_gain_m: Optional[float] = None
    elevation_loss_m: Optional[float] = None
    elevation_min_m: Optional[float] = None
    elevation_max_m: Optional[float] = None
    location_name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

    # --- 手錶上的自評（訓練後提示）---
    garmin_feeling: Optional[str] = None
    garmin_perceived_effort: Optional[int] = None

    splits: list[Split] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    synced_at: Optional[datetime] = None


class SessionFeedback(BaseModel):
    """使用者對該次訓練的主觀回饋（體感、生病、疼痛）。"""
    activity_id: int
    rpe: Optional[int] = None                     # 1–10
    feeling: Optional[str] = None                 # 體感描述
    pain: Optional[str] = None                    # 疼痛部位 / 程度
    sick: Optional[bool] = None
    notes: Optional[str] = None
    updated_at: Optional[datetime] = None


class PhysioSnapshot(BaseModel):
    """週期性快照：乳酸閾值、FTP、比賽預測等。"""
    date: date
    lt_hr: Optional[int] = None
    lt_speed_mps: Optional[float] = None
    lt_pace_s_per_km: Optional[float] = None
    run_ftp_w: Optional[float] = None
    cycling_ftp_w: Optional[float] = None
    pred_5k_s: Optional[int] = None
    pred_10k_s: Optional[int] = None
    pred_half_s: Optional[int] = None
    pred_marathon_s: Optional[int] = None
    endurance_score: Optional[int] = None
    hill_score: Optional[int] = None
    sources: list[str] = Field(default_factory=list)
    synced_at: Optional[datetime] = None


# ---------- 小工具 ----------

def mps_to_pace(mps: Optional[float]) -> Optional[float]:
    """m/s → 秒/公里。"""
    if not mps or mps <= 0:
        return None
    return round(1000.0 / mps, 1)


def fmt_pace(s_per_km: Optional[float]) -> str:
    if s_per_km is None:
        return "-"
    m, s = divmod(int(round(s_per_km)), 60)
    return f"{m}:{s:02d}/km"


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
