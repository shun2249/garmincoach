"""把 Garmin 原始 JSON 整理成結構化模型。

所有欄位都用防禦式取值：Garmin 回傳格式偶爾改變或缺欄位時，只會得到 None，
原始 JSON 另外完整保存在 data/raw/，不會遺失資料。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from . import config
from .models import DailyRecovery, PhysioSnapshot, Split, TrainingSession, mps_to_pace

log = logging.getLogger(__name__)


# ---------- 通用小工具 ----------

def dig(obj: Any, *keys: Any, default: Any = None) -> Any:
    """安全地沿著 dict/list 路徑取值。"""
    cur = obj
    for k in keys:
        if cur is None:
            return default
        if isinstance(k, int):
            if isinstance(cur, list) and -len(cur) <= k < len(cur):
                cur = cur[k]
            else:
                return default
        elif isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return default if cur is None else cur


def find_key(obj: Any, key: str) -> Any:
    """遞迴搜尋第一個出現的 key（用在不確定巢狀位置的欄位）。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return None if f is None else int(round(f))


def _minutes(seconds: Any) -> Optional[float]:
    f = _num(seconds)
    return None if f is None else round(f / 60.0, 1)


def _dt(v: Any) -> Optional[datetime]:
    """支援 ISO 字串、'YYYY-MM-DD HH:MM:SS'、epoch 毫秒。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = v / 1000.0 if v > 1e11 else v
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(v, str):
        s = v.replace("Z", "").replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[: len("2000-01-01 00:00:00.000")], fmt)
            except ValueError:
                continue
    return None


def f_to_c(v: Optional[float]) -> Optional[float]:
    return None if v is None else round((v - 32) * 5 / 9, 1)


def mph_to_kph(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v * 1.609344, 1)


def round_floats(obj: Any, ndigits: int = 2, keep: tuple[str, ...] = ("lat", "lon")) -> Any:
    """把模型裡的 float 欄位四捨五入（Garmin 常回傳 177.828125 這類值），lat/lon 保留原精度。"""
    from pydantic import BaseModel
    if isinstance(obj, BaseModel):
        for name in obj.model_fields:
            v = getattr(obj, name)
            if isinstance(v, float) and name not in keep:
                setattr(obj, name, round(v, ndigits))
            elif isinstance(v, list):
                setattr(obj, name, [round_floats(x, ndigits, keep) for x in v])
            elif isinstance(v, dict):
                setattr(obj, name, {k: (round(x, ndigits) if isinstance(x, float) else x) for k, x in v.items()})
    return obj


# ---------- 每日生理恢復 ----------

def normalize_daily(day: date, raw: dict[str, Any]) -> DailyRecovery:
    rec = DailyRecovery(date=day, synced_at=datetime.now())
    sources: list[str] = []

    us = raw.get("user_summary")
    if isinstance(us, dict):
        sources.append("user_summary")
        rec.resting_hr = _int(us.get("restingHeartRate"))
        rec.resting_hr_7d_avg = _int(us.get("lastSevenDaysAvgRestingHeartRate"))
        rec.min_hr = _int(us.get("minHeartRate"))
        rec.max_hr = _int(us.get("maxHeartRate"))
        rec.stress_avg = _int(us.get("averageStressLevel"))
        rec.stress_max = _int(us.get("maxStressLevel"))
        rec.stress_qualifier = us.get("stressQualifier")
        rec.stress_rest_min = _minutes(us.get("restStressDuration"))
        rec.stress_low_min = _minutes(us.get("lowStressDuration"))
        rec.stress_medium_min = _minutes(us.get("mediumStressDuration"))
        rec.stress_high_min = _minutes(us.get("highStressDuration"))
        rec.bb_high = _int(us.get("bodyBatteryHighestValue"))
        rec.bb_low = _int(us.get("bodyBatteryLowestValue"))
        rec.bb_charged = _int(us.get("bodyBatteryChargedValue"))
        rec.bb_drained = _int(us.get("bodyBatteryDrainedValue"))
        rec.bb_latest = _int(us.get("bodyBatteryMostRecentValue"))
        rec.steps = _int(us.get("totalSteps"))
        rec.active_kcal = _int(us.get("activeKilocalories"))
        rec.intensity_min_moderate = _int(us.get("moderateIntensityMinutes"))
        rec.intensity_min_vigorous = _int(us.get("vigorousIntensityMinutes"))
        rec.avg_spo2 = _num(us.get("averageSpo2"))

    # RHR 專用 endpoint 比 user_summary 準（user_summary 當天可能還沒結算）
    rhr_val = dig(raw.get("rhr"), "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", 0, "value")
    if rhr_val is not None:
        sources.append("rhr")
        rec.resting_hr = _int(rhr_val)

    hrv = dig(raw.get("hrv"), "hrvSummary")
    if isinstance(hrv, dict):
        sources.append("hrv")
        rec.hrv_last_night = _num(hrv.get("lastNightAvg"))
        rec.hrv_5min_high = _num(hrv.get("lastNight5MinHigh"))
        rec.hrv_weekly_avg = _num(hrv.get("weeklyAvg"))
        rec.hrv_status = hrv.get("status")
        rec.hrv_baseline_low = _num(dig(hrv, "baseline", "balancedLow"))
        rec.hrv_baseline_high = _num(dig(hrv, "baseline", "balancedUpper"))

    sl = dig(raw.get("sleep"), "dailySleepDTO")
    if isinstance(sl, dict) and sl.get("sleepTimeSeconds"):
        sources.append("sleep")
        rec.sleep_total_min = _minutes(sl.get("sleepTimeSeconds"))
        rec.sleep_deep_min = _minutes(sl.get("deepSleepSeconds"))
        rec.sleep_light_min = _minutes(sl.get("lightSleepSeconds"))
        rec.sleep_rem_min = _minutes(sl.get("remSleepSeconds"))
        rec.sleep_awake_min = _minutes(sl.get("awakeSleepSeconds"))
        rec.sleep_start = _dt(sl.get("sleepStartTimestampLocal"))
        rec.sleep_end = _dt(sl.get("sleepEndTimestampLocal"))
        rec.sleep_avg_spo2 = _num(sl.get("averageSpO2Value"))
        rec.sleep_avg_respiration = _num(sl.get("averageRespirationValue"))
        rec.sleep_body_battery_change = _int(sl.get("bodyBatteryChange"))
        rec.sleep_score = _int(dig(sl, "sleepScores", "overall", "value"))
        rec.sleep_quality = dig(sl, "sleepScores", "overall", "qualifierKey")
        if rec.hrv_last_night is None:
            rec.hrv_last_night = _num(sl.get("avgOvernightHrv"))
        if rec.hrv_status is None:
            rec.hrv_status = sl.get("hrvStatus")
        if rec.resting_hr is None:
            rec.resting_hr = _int(sl.get("restingHeartRate"))

    st = raw.get("stress")
    if isinstance(st, dict) and rec.stress_avg is None:
        sources.append("stress")
        rec.stress_avg = _int(st.get("avgStressLevel"))
        rec.stress_max = _int(st.get("maxStressLevel"))

    tr = raw.get("training_readiness")
    if isinstance(tr, list) and tr:
        # 取最新一筆（同一天可能有多筆：起床、訓練後更新）
        latest = max((t for t in tr if isinstance(t, dict)), key=lambda t: str(t.get("timestamp", "")), default=None)
        if latest:
            sources.append("training_readiness")
            rec.readiness_score = _int(latest.get("score"))
            rec.readiness_level = latest.get("level")
            rec.readiness_feedback = latest.get("feedbackShort")
            rt = _num(latest.get("recoveryTime"))          # Garmin 給的是分鐘
            rec.recovery_time_hours = round(rt / 60.0, 1) if rt is not None else None
    elif isinstance(tr, dict):
        sources.append("training_readiness")
        rec.readiness_score = _int(tr.get("score"))
        rec.readiness_level = tr.get("level")
        rec.readiness_feedback = tr.get("feedbackShort")

    ts = raw.get("training_status")
    if isinstance(ts, dict):
        sources.append("training_status")
        rec.vo2max_run = _num(dig(ts, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue")) or _num(
            dig(ts, "mostRecentVO2Max", "generic", "vo2MaxValue"))
        rec.vo2max_cycle = _num(dig(ts, "mostRecentVO2Max", "cycling", "vo2MaxPreciseValue"))
        rec.fitness_age = _num(dig(ts, "mostRecentVO2Max", "generic", "fitnessAge"))
        status_map = dig(ts, "mostRecentTrainingStatus", "latestTrainingStatusData")
        if isinstance(status_map, dict) and status_map:
            dev = next(iter(status_map.values()))
            rec.training_status = dev.get("trainingStatusFeedbackPhrase") or str(dev.get("trainingStatus"))
            atl = dev.get("acuteTrainingLoadDTO") or {}
            rec.acute_load = _num(atl.get("dailyTrainingLoadAcute"))
            rec.chronic_load = _num(atl.get("dailyTrainingLoadChronic"))
            rec.acwr = _num(atl.get("dailyAcuteChronicWorkloadRatio"))
            rec.acwr_status = atl.get("acwrStatus")
        bal_map = dig(ts, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap")
        if isinstance(bal_map, dict) and bal_map:
            bal = next(iter(bal_map.values()))
            rec.load_aerobic_low = _num(bal.get("monthlyLoadAerobicLow"))
            rec.load_aerobic_high = _num(bal.get("monthlyLoadAerobicHigh"))
            rec.load_anaerobic = _num(bal.get("monthlyLoadAnaerobic"))
            rec.load_balance_feedback = bal.get("trainingBalanceFeedbackPhrase")
        heat = dig(ts, "mostRecentVO2Max", "heatAltitudeAcclimation") or {}
        rec.heat_acclimation_pct = _int(heat.get("heatAcclimationPercentage"))
        rec.heat_trend = heat.get("heatTrend")

    mm = raw.get("max_metrics")
    if isinstance(mm, list) and mm:
        sources.append("max_metrics")
        g = dig(mm, 0, "generic") or {}
        rec.vo2max_run = _num(g.get("vo2MaxPreciseValue")) or rec.vo2max_run
        rec.fitness_age = _num(g.get("fitnessAge")) or rec.fitness_age
        c = dig(mm, 0, "cycling") or {}
        rec.vo2max_cycle = _num(c.get("vo2MaxPreciseValue")) or rec.vo2max_cycle

    rec.sources = sources
    return round_floats(rec)


# ---------- 訓練活動 ----------

def _zone_seconds(zones: Any, prefix: str = "z") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(zones, list):
        for z in zones:
            if not isinstance(z, dict):
                continue
            n = z.get("zoneNumber")
            secs = _num(z.get("secsInZone"))
            if n is not None and secs is not None:
                out[f"{prefix}{n}"] = round(secs, 0)
    return out


def _zone_seconds_from_summary(item: dict[str, Any], field_prefix: str, prefix: str = "z") -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(1, 8):
        v = _num(item.get(f"{field_prefix}{i}"))
        if v is not None:
            out[f"{prefix}{i}"] = round(v, 0)
    return out


def compute_decoupling(details: Any) -> dict[str, Optional[float]]:
    """從逐秒資料算心率漂移。

    hr_drift_pct       = 後半平均 HR / 前半平均 HR - 1
    pa_hr_decoupling   = (後半 HR/速度) / (前半 HR/速度) - 1   （Pa:Hr）
    pw_hr_decoupling   = (後半 HR/功率) / (前半 HR/功率) - 1   （Pw:Hr）
    正值代表同樣輸出下心率上升（脫水、熱、耐力不足等）。
    """
    out: dict[str, Optional[float]] = {"hr_drift_pct": None, "pa_hr_decoupling_pct": None, "pw_hr_decoupling_pct": None}
    descs = dig(details, "metricDescriptors")
    rows = dig(details, "activityDetailMetrics")
    if not isinstance(descs, list) or not isinstance(rows, list) or len(rows) < 20:
        return out
    idx = {d.get("key"): d.get("metricsIndex") for d in descs if isinstance(d, dict)}
    i_hr, i_spd, i_pw = idx.get("directHeartRate"), idx.get("directSpeed"), idx.get("directPower")
    i_t = idx.get("sumDuration") if idx.get("sumDuration") is not None else idx.get("directTimestamp")
    if i_hr is None:
        return out

    samples: list[tuple[float, float, Optional[float], Optional[float]]] = []
    for n, r in enumerate(rows):
        m = r.get("metrics") if isinstance(r, dict) else None
        if not isinstance(m, list):
            continue
        hr = _num(m[i_hr]) if i_hr < len(m) else None
        if hr is None or hr < 60:
            continue
        t = _num(m[i_t]) if (i_t is not None and i_t < len(m)) else float(n)
        spd = _num(m[i_spd]) if (i_spd is not None and i_spd < len(m)) else None
        pw = _num(m[i_pw]) if (i_pw is not None and i_pw < len(m)) else None
        samples.append((t if t is not None else float(n), hr, spd, pw))
    if len(samples) < 20:
        return out

    t0, t1 = samples[0][0], samples[-1][0]
    mid = (t0 + t1) / 2
    first = [s for s in samples if s[0] <= mid]
    second = [s for s in samples if s[0] > mid]
    if len(first) < 10 or len(second) < 10:
        return out

    def avg(vals: Iterable[Optional[float]]) -> Optional[float]:
        v = [x for x in vals if x is not None and x > 0]
        return sum(v) / len(v) if v else None

    hr1, hr2 = avg(s[1] for s in first), avg(s[1] for s in second)
    if hr1 and hr2:
        out["hr_drift_pct"] = round((hr2 / hr1 - 1) * 100, 1)
    spd1, spd2 = avg(s[2] for s in first), avg(s[2] for s in second)
    if hr1 and hr2 and spd1 and spd2 and spd1 > 0.5 and spd2 > 0.5:
        out["pa_hr_decoupling_pct"] = round(((hr2 / spd2) / (hr1 / spd1) - 1) * 100, 1)
    pw1, pw2 = avg(s[3] for s in first), avg(s[3] for s in second)
    if hr1 and hr2 and pw1 and pw2:
        out["pw_hr_decoupling_pct"] = round(((hr2 / pw2) / (hr1 / pw1) - 1) * 100, 1)
    return out


def normalize_splits(splits_raw: Any) -> list[Split]:
    laps = dig(splits_raw, "lapDTOs")
    out: list[Split] = []
    if not isinstance(laps, list):
        return out
    for i, lap in enumerate(laps):
        if not isinstance(lap, dict):
            continue
        out.append(Split(
            index=_int(lap.get("lapIndex")) or i + 1,
            intensity=lap.get("intensityType"),
            distance_m=_num(lap.get("distance")),
            duration_s=_num(lap.get("duration")),
            avg_pace_s_per_km=mps_to_pace(_num(lap.get("averageSpeed"))),
            avg_hr=_int(lap.get("averageHR")),
            max_hr=_int(lap.get("maxHR")),
            avg_power=_num(lap.get("averagePower")),
            avg_cadence=_num(lap.get("averageRunCadence")) or _num(lap.get("averageBikeCadence")),
            elevation_gain_m=_num(lap.get("elevationGain")),
            elevation_loss_m=_num(lap.get("elevationLoss")),
        ))
    return out


def normalize_activity(item: dict[str, Any], bundle: Optional[dict[str, Any]] = None) -> TrainingSession:
    """item = get_activities 清單裡的一筆；bundle = fetch_activity_bundle 的結果（可為 None）。"""
    bundle = bundle or {}
    detail = bundle.get("activity") if isinstance(bundle.get("activity"), dict) else {}
    summ = detail.get("summaryDTO") or {}

    def pick(list_key: str, detail_key: Optional[str] = None) -> Any:
        v = item.get(list_key)
        if v is None and detail_key:
            v = summ.get(detail_key)
        return v

    s = TrainingSession(activity_id=int(item["activityId"]), synced_at=datetime.now())
    sources = ["list"]
    s.name = item.get("activityName") or detail.get("activityName")
    s.sport = dig(item, "activityType", "typeKey") or dig(detail, "activityTypeDTO", "typeKey")
    s.start_time = _dt(item.get("startTimeLocal") or summ.get("startTimeLocal"))
    s.start_time_utc = _dt(item.get("startTimeGMT") or summ.get("startTimeGMT"))
    s.timezone = dig(detail, "timeZoneUnitDTO", "timeZone")
    s.duration_s = _num(pick("duration", "duration"))
    s.moving_s = _num(pick("movingDuration", "movingDuration"))
    s.elapsed_s = _num(pick("elapsedDuration", "elapsedDuration"))
    s.distance_m = _num(pick("distance", "distance"))
    s.calories = _int(pick("calories", "calories"))

    s.avg_speed_mps = _num(pick("averageSpeed", "averageSpeed"))
    s.max_speed_mps = _num(pick("maxSpeed", "maxSpeed"))
    s.avg_pace_s_per_km = mps_to_pace(s.avg_speed_mps)
    s.avg_gap_pace_s_per_km = mps_to_pace(_num(pick("avgGradeAdjustedSpeed", "avgGradeAdjustedSpeed")))

    s.avg_hr = _int(pick("averageHR", "averageHR"))
    s.max_hr = _int(pick("maxHR", "maxHR"))
    s.hr_zone_s = _zone_seconds(bundle.get("hr_zones")) or _zone_seconds_from_summary(item, "hrTimeInZone_")

    s.avg_power = _num(pick("avgPower", "averagePower"))
    s.max_power = _num(pick("maxPower", "maxPower"))
    s.normalized_power = _num(pick("normPower", "normalizedPower"))
    s.power_zone_s = _zone_seconds(bundle.get("power_zones")) or _zone_seconds_from_summary(item, "powerTimeInZone_")
    s.tss = _num(pick("trainingStressScore", "trainingStressScore"))
    s.intensity_factor = _num(pick("intensityFactor", "intensityFactor"))

    s.avg_cadence = _num(pick("averageRunningCadenceInStepsPerMinute", "averageRunCadence")) or _num(
        pick("averageBikingCadenceInRevPerMinute", "averageBikeCadence"))
    stride_cm = _num(pick("avgStrideLength", "avgStrideLength"))
    s.avg_stride_len_m = round(stride_cm / 100.0, 2) if stride_cm else None
    s.avg_vertical_osc_cm = _num(pick("avgVerticalOscillation", "avgVerticalOscillation"))
    s.avg_gct_ms = _num(pick("avgGroundContactTime", "avgGroundContactTime"))
    s.avg_vertical_ratio = _num(pick("avgVerticalRatio", "avgVerticalRatio"))

    s.aerobic_te = _num(pick("aerobicTrainingEffect", "trainingEffect"))
    s.anaerobic_te = _num(pick("anaerobicTrainingEffect", "anaerobicTrainingEffect"))
    s.te_label = pick("trainingEffectLabel", "trainingEffectLabel")
    s.training_load = _num(pick("activityTrainingLoad", "activityTrainingLoad"))
    s.vo2max = _num(pick("vO2MaxValue", "vO2MaxValue"))

    s.device_temp_min_c = _num(pick("minTemperature", "minTemperature"))
    s.device_temp_max_c = _num(pick("maxTemperature", "maxTemperature"))
    s.elevation_gain_m = _num(pick("elevationGain", "elevationGain"))
    s.elevation_loss_m = _num(pick("elevationLoss", "elevationLoss"))
    s.elevation_min_m = _num(pick("minElevation", "minElevation"))
    s.elevation_max_m = _num(pick("maxElevation", "maxElevation"))
    s.location_name = item.get("locationName") or detail.get("locationName")
    s.lat = _num(pick("startLatitude", "startLatitude"))
    s.lon = _num(pick("startLongitude", "startLongitude"))

    # 手錶自評（位置不固定，遞迴找）
    feeling = find_key(detail, "selfEvaluationFeeling") or find_key(item, "selfEvaluationFeeling")
    effort = find_key(detail, "selfEvaluationPerceivedEffort") or find_key(item, "selfEvaluationPerceivedEffort")
    s.garmin_feeling = str(feeling) if feeling is not None else None
    s.garmin_perceived_effort = _int(effort)

    if detail:
        sources.append("activity")

    w = bundle.get("weather")
    if isinstance(w, dict) and w.get("temp") is not None:
        sources.append("weather")
        imperial = config.WEATHER_UNITS == "imperial"
        temp, app, dew, wind = _num(w.get("temp")), _num(w.get("apparentTemp")), _num(w.get("dewPoint")), _num(w.get("windSpeed"))
        s.temp_c = f_to_c(temp) if imperial else temp
        s.apparent_temp_c = f_to_c(app) if imperial else app
        s.dew_point_c = f_to_c(dew) if imperial else dew
        s.wind_kph = mph_to_kph(wind) if imperial else wind
        s.humidity_pct = _num(w.get("relativeHumidity"))
        s.wind_dir = w.get("windDirectionCompassPoint")
        s.weather_desc = dig(w, "weatherTypeDTO", "desc")

    if bundle.get("splits"):
        s.splits = normalize_splits(bundle["splits"])
        if s.splits:
            sources.append("splits")

    if bundle.get("details"):
        dec = compute_decoupling(bundle["details"])
        s.hr_drift_pct = dec["hr_drift_pct"]
        s.pa_hr_decoupling_pct = dec["pa_hr_decoupling_pct"]
        s.pw_hr_decoupling_pct = dec["pw_hr_decoupling_pct"]
        if s.hr_drift_pct is not None:
            sources.append("details")

    s.sources = sources
    return round_floats(s)


# ---------- 體能快照 ----------

def _pred(preds: Any, key: str) -> Optional[int]:
    v = find_key(preds, key)
    return _int(v)


def normalize_physio(day: date, raw: dict[str, Any]) -> PhysioSnapshot:
    p = PhysioSnapshot(date=day, synced_at=datetime.now())
    sources: list[str] = []
    lt = raw.get("lactate_threshold")
    if isinstance(lt, dict):
        shr = lt.get("speed_and_heart_rate") or {}
        p.lt_hr = _int(shr.get("heartRate"))
        spd = _num(shr.get("speed"))
        p.lt_speed_mps = round(spd * 10, 3) if spd is not None else None   # Garmin 回傳 m/s ÷ 10（實測 0.3556 → 3.556 m/s = 4:41/km）
        p.lt_pace_s_per_km = mps_to_pace(p.lt_speed_mps)
        p.run_ftp_w = _num(dig(lt, "power", "functionalThresholdPower")) or _num(find_key(lt.get("power"), "functionalThresholdPower"))
        if p.lt_hr or p.lt_speed_mps or p.run_ftp_w:
            sources.append("lactate_threshold")
    ftp = raw.get("cycling_ftp")
    if ftp:
        p.cycling_ftp_w = _num(find_key(ftp, "functionalThresholdPower"))
        if p.cycling_ftp_w:
            sources.append("cycling_ftp")
    rp = raw.get("race_predictions")
    if rp:
        p.pred_5k_s = _pred(rp, "time5K")
        p.pred_10k_s = _pred(rp, "time10K")
        p.pred_half_s = _pred(rp, "timeHalfMarathon")
        p.pred_marathon_s = _pred(rp, "timeMarathon")
        if p.pred_5k_s:
            sources.append("race_predictions")
    es = raw.get("endurance_score")
    if es:
        p.endurance_score = _int(find_key(es, "overallScore"))
        if p.endurance_score:
            sources.append("endurance_score")
    hs = raw.get("hill_score")
    if hs:
        p.hill_score = _int(find_key(hs, "overallScore"))
        if p.hill_score:
            sources.append("hill_score")
    p.sources = sources
    return round_floats(p)


# ---------- 個人身體資料 / 區間設定 ----------

def normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    """回傳要存進 profile 表的 {key: value}。"""
    ud = dig(raw.get("profile"), "userData") or {}
    out: dict[str, Any] = {}
    if ud:
        weight_g = _num(ud.get("weight"))
        out["garmin_body"] = _clean_dict({
            "gender": ud.get("gender"), "birth_date": ud.get("birthDate"),
            "height_cm": _num(ud.get("height")),
            "weight_kg": round(weight_g / 1000, 1) if weight_g else None,
            "vo2max_running": _num(ud.get("vo2MaxRunning")), "vo2max_cycling": _num(ud.get("vo2MaxCycling")),
            "lt_hr": _int(ud.get("lactateThresholdHeartRate")),
            "lt_pace_s_per_km": mps_to_pace((_num(ud.get("lactateThresholdSpeed")) or 0) * 10),
            "lt_auto_detected": ud.get("thresholdHeartRateAutoDetected"),
            "ftp_auto_detected": ud.get("ftpAutoDetected"),
        })
        if ud.get("birthDate"):
            try:
                b = date.fromisoformat(ud["birthDate"]); t = date.today()
                out["garmin_body"]["age"] = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
            except ValueError:
                pass
    us = dig(raw.get("profile"), "userSleep") or {}
    if us.get("sleepTime") is not None:
        out["sleep_window"] = {"bed": _secs_to_hhmm(us.get("sleepTime")), "wake": _secs_to_hhmm(us.get("wakeTime"))}

    hz = raw.get("hr_zones")
    if isinstance(hz, list):
        zones = {}
        for z in hz:
            if not isinstance(z, dict):
                continue
            sport = (z.get("sport") or "DEFAULT").lower()
            floors = [_int(z.get(f"zone{i}Floor")) for i in range(1, 6)]
            mx = _int(z.get("maxHeartRateUsed"))
            zones[sport] = {
                "method": z.get("trainingMethod"), "max_hr": mx, "resting_hr": _int(z.get("restingHeartRateUsed")),
                "lt_hr": _int(z.get("lactateThresholdHeartRateUsed")),
                "zones": {f"z{i+1}": [floors[i], (floors[i+1] - 1) if i < 4 else mx] for i in range(5)},
            }
        if zones:
            out["hr_zones"] = zones
    pz = raw.get("power_zones")
    if isinstance(pz, list):
        zones = {}
        for z in pz:
            if not isinstance(z, dict) or z.get("sport") not in ("RUNNING", "CYCLING"):
                continue
            floors = [_int(z.get(f"zone{i}Floor")) for i in range(1, 8)]
            floors = [f for f in floors if f is not None]
            zd = {}
            for i, f in enumerate(floors):
                if i > 0 and f == 0:
                    break
                hi = floors[i + 1] - 1 if i + 1 < len(floors) and floors[i + 1] else None
                zd[f"z{i+1}"] = [f, hi]
            zones[z["sport"].lower()] = {"ftp": _num(z.get("functionalThresholdPower")), "zones": zd}
        if zones:
            out["power_zones"] = zones
    wi = dig(raw.get("weigh_ins"), "dailyWeightSummaries")
    if isinstance(wi, list) and wi:
        latest = max(wi, key=lambda w: w.get("summaryDate", ""))
        lw = latest.get("latestWeight") or {}
        if lw.get("weight"):
            out["latest_weight"] = _clean_dict({
                "date": latest.get("summaryDate"), "weight_kg": round(lw["weight"] / 1000, 1),
                "body_fat_pct": _num(lw.get("bodyFat")), "muscle_mass_kg": (_num(lw.get("muscleMass")) or 0) / 1000 or None,
                "source": lw.get("sourceType"),
            })
    devs = raw.get("devices")
    if isinstance(devs, list):
        out["devices"] = [d.get("productDisplayName") for d in devs if isinstance(d, dict) and d.get("productDisplayName")]
    return out


def _secs_to_hhmm(secs: Any) -> Optional[str]:
    v = _int(secs)
    if v is None:
        return None
    return f"{v // 3600:02d}:{(v % 3600) // 60:02d}"


def _clean_dict(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ---------- 體能指標歷史序列 ----------

def normalize_history(raw: dict[str, Any]) -> list[tuple[str, str, Optional[float], Optional[dict[str, Any]]]]:
    rows: list[tuple[str, str, Optional[float], Optional[dict[str, Any]]]] = []
    for r in raw.get("race_predictions") or []:
        if isinstance(r, dict) and r.get("calendarDate"):
            d = r["calendarDate"]
            for k, m in (("time5K", "pred_5k_s"), ("time10K", "pred_10k_s"), ("timeHalfMarathon", "pred_half_s"), ("timeMarathon", "pred_marathon_s")):
                if r.get(k) is not None:
                    rows.append((d, m, _num(r[k]), None))
    lt = raw.get("lactate_threshold") or {}
    for key, metric, conv in (("speed", "lt_pace_s_per_km", lambda v: mps_to_pace(v * 10)), ("heartRate", "lt_hr", lambda v: v),
                              ("heart_rate", "lt_hr", lambda v: v), ("power", "run_ftp_w", lambda v: v)):
        for r in lt.get(key) or []:
            if isinstance(r, dict) and r.get("value") is not None and r.get("until"):
                rows.append((r["until"], metric, conv(_num(r["value"])), {"from": r.get("from")}))
    for r in raw.get("vo2max") or []:
        if not isinstance(r, dict):
            continue
        g, c = r.get("generic") or {}, r.get("cycling") or {}
        if g.get("calendarDate") and g.get("vo2MaxPreciseValue") is not None:
            rows.append((g["calendarDate"], "vo2max_run", _num(g["vo2MaxPreciseValue"]), None))
        if c.get("calendarDate") and c.get("vo2MaxPreciseValue") is not None:
            rows.append((c["calendarDate"], "vo2max_cycle", _num(c["vo2MaxPreciseValue"]), None))
        h = r.get("heatAltitudeAcclimation") or {}
        if h.get("calendarDate") and h.get("heatAcclimationPercentage") is not None:
            rows.append((h["calendarDate"], "heat_acclimation_pct", _num(h["heatAcclimationPercentage"]), {"trend": h.get("heatTrend")}))
    for d, g in (dig(raw.get("endurance"), "groupMap") or {}).items():
        if isinstance(g, dict) and g.get("groupAverage") is not None:
            rows.append((d, "endurance_score", _num(g["groupAverage"]), {"max": g.get("groupMax")}))
    for r in dig(raw.get("hill"), "hillScoreDTOList") or []:
        if isinstance(r, dict) and r.get("calendarDate") and r.get("overallScore") is not None:
            rows.append((r["calendarDate"], "hill_score", _num(r["overallScore"]), {"strength": r.get("strengthScore"), "endurance": r.get("enduranceScore")}))
    for r in raw.get("running_tolerance") or []:
        if isinstance(r, dict) and r.get("calendarDate"):
            rows.append((r["calendarDate"], "run_tolerance", _num(r.get("tolerance")),
                         {"impact_load": r.get("totalImpactLoad"), "distance_m": r.get("totalDistance"),
                          "week": f"{r.get('startOfWeek')}~{r.get('endOfWeek')}"}))
    return rows
