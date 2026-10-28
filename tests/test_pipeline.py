import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
def _tmp(env, suffix):
    p = os.environ.get(env)
    return p or tempfile.mkstemp(prefix="garmin_coach_", suffix=suffix)[1]
import json, math, os, sys
from datetime import date, datetime, timedelta
from garmin_coach import config
from garmin_coach.normalize import normalize_daily, normalize_activity, normalize_physio, compute_decoupling
from garmin_coach.store import Store
from garmin_coach.context import Context
from garmin_coach.brief import build_brief, to_markdown
from garmin_coach.models import SessionFeedback
import sqlite3; print("sqlite", sqlite3.sqlite_version)

day = date(2026, 10, 19)
raw_daily = {
  "user_summary": {"restingHeartRate": 48, "lastSevenDaysAvgRestingHeartRate": 50, "minHeartRate": 44, "maxHeartRate": 172,
    "averageStressLevel": 27, "maxStressLevel": 88, "stressQualifier": "BALANCED", "restStressDuration": 30000,
    "lowStressDuration": 20000, "mediumStressDuration": 5000, "highStressDuration": 1200,
    "bodyBatteryHighestValue": 92, "bodyBatteryLowestValue": 21, "bodyBatteryChargedValue": 70, "bodyBatteryDrainedValue": 71,
    "bodyBatteryMostRecentValue": 35, "totalSteps": 12345, "activeKilocalories": 800, "moderateIntensityMinutes": 20,
    "vigorousIntensityMinutes": 45, "averageSpo2": 96.0},
  "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 25200, "deepSleepSeconds": 5400, "lightSleepSeconds": 13800, "remSleepSeconds": 6000,
    "awakeSleepSeconds": 600, "sleepStartTimestampLocal": 1760828400000, "sleepEndTimestampLocal": 1760853600000,
    "averageSpO2Value": 95.5, "averageRespirationValue": 14.2, "avgOvernightHrv": 61.0, "hrvStatus": "BALANCED",
    "bodyBatteryChange": 68, "restingHeartRate": 48, "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}}}},
  "hrv": {"hrvSummary": {"weeklyAvg": 58, "lastNightAvg": 61, "lastNight5MinHigh": 84, "status": "BALANCED",
    "baseline": {"lowUpper": 48, "balancedLow": 52, "balancedUpper": 68}}},
  "rhr": {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 47, "calendarDate": "2026-10-19"}]}}},
  "stress": None,
  "training_readiness": [
    {"timestamp": "2026-10-19T06:00:00.0", "score": 71, "level": "MODERATE", "feedbackShort": "GOOD_RECOVERY", "recoveryTime": 14},
    {"timestamp": "2026-10-19T09:00:00.0", "score": 40, "level": "LOW", "feedbackShort": "TRAINING_LOAD_HIGH", "recoveryTime": 36}],
  "training_status": {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 52.3, "vo2MaxValue": 52, "fitnessAge": 29}, "cycling": None},
    "mostRecentTrainingStatus": {"latestTrainingStatusData": {"12345": {"trainingStatus": 7, "trainingStatusFeedbackPhrase": "PRODUCTIVE",
      "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 620, "dailyTrainingLoadChronic": 540, "dailyAcuteChronicWorkloadRatio": 1.15, "acwrStatus": "OPTIMAL"}}}},
    "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {"12345": {"monthlyLoadAerobicLow": 900, "monthlyLoadAerobicHigh": 450,
      "monthlyLoadAnaerobic": 120, "trainingBalanceFeedbackPhrase": "AEROBIC_HIGH_SHORTAGE"}}}},
  "max_metrics": [{"generic": {"vo2MaxPreciseValue": 52.4, "fitnessAge": 28.5}, "cycling": None}],
}
rec = normalize_daily(day, raw_daily)
assert rec.resting_hr == 47, rec.resting_hr                    # rhr endpoint 優先
assert rec.readiness_score == 40 and rec.recovery_time_hours == 0.6
assert rec.sleep_total_min == 420 and rec.sleep_score == 82
assert rec.vo2max_run == 52.4 and rec.acwr == 1.15 and rec.training_status == "PRODUCTIVE"
assert rec.stress_rest_min == 500.0
print("daily ok", rec.sources)

# 活動：60 分鐘穩定跑，後半心率緩升 → 正 drift
n = 3600
descs = [{"key": "directTimestamp", "metricsIndex": 0}, {"key": "sumDuration", "metricsIndex": 1},
         {"key": "directHeartRate", "metricsIndex": 2}, {"key": "directSpeed", "metricsIndex": 3}, {"key": "directPower", "metricsIndex": 4}]
rows = []
for t in range(n):
    hr = 145 + (8 * t / n) + (0 if t > 300 else -20 * (1 - t / 300))
    rows.append({"metrics": [1760828400000 + t * 1000, t, hr, 3.33, 280]})
details = {"metricDescriptors": descs, "activityDetailMetrics": rows}
dec = compute_decoupling(details)
print("decoupling", dec)
assert dec["hr_drift_pct"] and 2 < dec["hr_drift_pct"] < 6
assert abs(dec["pa_hr_decoupling_pct"] - dec["hr_drift_pct"]) < 0.2   # 等速下 Pa:Hr == HR drift

item = {"activityId": 20001, "activityName": "Morning Run", "activityType": {"typeKey": "running"},
  "startTimeLocal": "2026-10-19 06:12:00", "startTimeGMT": "2026-10-18 22:12:00", "distance": 12000.0, "duration": 3600.0,
  "elapsedDuration": 3650.0, "movingDuration": 3580.0, "averageSpeed": 3.333, "maxSpeed": 4.5, "averageHR": 148, "maxHR": 171,
  "avgPower": 281, "maxPower": 350, "normPower": 285, "aerobicTrainingEffect": 3.4, "anaerobicTrainingEffect": 0.8,
  "trainingEffectLabel": "AEROBIC_BASE", "activityTrainingLoad": 142.0, "vO2MaxValue": 52.0, "calories": 820,
  "averageRunningCadenceInStepsPerMinute": 176.0, "avgStrideLength": 113.6, "avgVerticalOscillation": 8.1, "avgGroundContactTime": 245.0,
  "avgVerticalRatio": 7.2, "elevationGain": 85.0, "elevationLoss": 80.0, "minElevation": 10.0, "maxElevation": 60.0,
  "minTemperature": 24.0, "maxTemperature": 28.0, "locationName": "Taipei", "startLatitude": 25.03, "startLongitude": 121.56,
  "avgGradeAdjustedSpeed": 3.36, "hrTimeInZone_1": 100.0, "hrTimeInZone_2": 2400.0, "hrTimeInZone_3": 1000.0, "hrTimeInZone_4": 100.0,
  "trainingStressScore": 78.0, "intensityFactor": 0.88}
bundle = {
  "activity": {"activityName": "Morning Run", "timeZoneUnitDTO": {"timeZone": "Asia/Taipei"}, "summaryDTO": {},
               "metadataDTO": {"selfEvaluationFeeling": "GOOD", "selfEvaluationPerceivedEffort": 6}},
  "splits": {"lapDTOs": [{"lapIndex": 1, "intensityType": "ACTIVE", "distance": 1000, "duration": 300, "averageSpeed": 3.33, "averageHR": 140,
                          "maxHR": 150, "averagePower": 270, "elevationGain": 5, "elevationLoss": 3, "averageRunCadence": 175},
                         {"lapIndex": 2, "intensityType": "ACTIVE", "distance": 1000, "duration": 295, "averageSpeed": 3.39, "averageHR": 148,
                          "maxHR": 155, "averagePower": 282, "elevationGain": 8, "elevationLoss": 10, "averageRunCadence": 177}]},
  "weather": {"temp": 77, "apparentTemp": 82, "dewPoint": 70, "relativeHumidity": 80, "windSpeed": 6, "windDirectionCompassPoint": "NE",
              "weatherTypeDTO": {"desc": "Partly Cloudy"}},
  "hr_zones": [{"zoneNumber": 1, "secsInZone": 100.0}, {"zoneNumber": 2, "secsInZone": 2400.0}, {"zoneNumber": 3, "secsInZone": 1000.0}],
  "power_zones": [{"zoneNumber": 2, "secsInZone": 3000.0}],
  "details": details,
}
sess = normalize_activity(item, bundle)
assert sess.temp_c == 25.0 and sess.wind_kph == 9.7 and sess.humidity_pct == 80
assert sess.avg_pace_s_per_km == 300.0 and sess.timezone == "Asia/Taipei"
assert sess.garmin_feeling == "GOOD" and sess.garmin_perceived_effort == 6
assert sess.hr_zone_s == {"z1": 100, "z2": 2400, "z3": 1000} and sess.power_zone_s == {"z2": 3000}
assert len(sess.splits) == 2 and sess.splits[1].avg_hr == 148
assert sess.avg_stride_len_m == 1.14
print("activity ok", sess.sources, "drift", sess.hr_drift_pct, sess.pa_hr_decoupling_pct, sess.pw_hr_decoupling_pct)

physio = normalize_physio(day, {
  "lactate_threshold": {"speed_and_heart_rate": {"heartRate": 168, "speed": 3.9}, "power": {"functionalThresholdPower": 310}},
  "cycling_ftp": {"functionalThresholdPower": 250}, "race_predictions": {"time5K": 1260, "time10K": 2640, "timeHalfMarathon": 5900, "timeMarathon": 12600},
  "endurance_score": {"overallScore": 6200}, "hill_score": None})
assert physio.lt_hr == 168 and physio.lt_pace_s_per_km == 25.6 and physio.pred_half_s == 5900 and physio.cycling_ftp_w == 250

# ---- store round trip ----
dbp = _tmp("TEST_DB", ".db")
if os.path.exists(dbp): os.remove(dbp)
with Store(dbp) as store:
    for i in range(7):
        d = day - timedelta(days=i)
        r = normalize_daily(d, raw_daily); r.resting_hr = 47 + i
        store.upsert_daily(r)
    store.upsert_daily(rec)      # 再 upsert 一次，驗證 ON CONFLICT
    store.upsert_activity(sess)
    # 第二次 upsert 更新 aqi
    sess.aqi_us = 42; sess.pm25 = 9.5; store.upsert_activity(sess)
    older = sess.model_copy(update={"activity_id": 19999, "start_time": datetime(2026, 10, 15, 6, 0), "distance_m": 8000.0, "name": "Easy"})
    store.upsert_activity(older)
    store.upsert_physio(physio)
    store.upsert_feedback(SessionFeedback(activity_id=20001, rpe=6, feeling="腿有點重"))
    store.upsert_feedback(SessionFeedback(activity_id=20001, pain="左腳踝輕微痠"))   # 合併
    fb = store.get_feedback(20001); assert fb.rpe == 6 and fb.pain and fb.feeling, fb

    back = store.get_activity(20001)
    assert back.hr_zone_s == sess.hr_zone_s and back.splits[0].distance_m == 1000 and back.aqi_us == 42 and back.start_time == sess.start_time
    dback = store.get_daily(day); assert dback.resting_hr == 47 and dback.sources == rec.sources and dback.sleep_start == rec.sleep_start
    assert store.has_activity(20001, with_details=True) and not store.has_activity(1)
    assert len(store.daily_range(day - timedelta(days=6), day)) == 7
    print("weekly", store.weekly_load(day, 4))

    ctx = Context(store)
    ctx.set_profile("age", 38); ctx.set_profile("weight_kg", 68.5); ctx.set_profile("goal", "全馬 sub 3:30")
    ctx.add_event("race", "台北馬拉松", "2026-12-20", priority="A", notes="目標 3:30")
    ctx.add_event("travel", "出差東京", "2026-11-02", "2026-11-05", notes="只能跑飯店跑步機")
    ctx.add_note("injury", "左腳踝 10/19 開始有輕微痠痛，下坡時明顯", source="user")
    ctx.add_note("race_progress", "進入 10 週馬拉松專項期，第 2 週", source="ai")
    for t in ["400m 配速", "LT1", "400m 配速", "400m 配速"]:
        ctx.touch_interest(t)
    ctx.touch_interest("LT1", definition="第一乳酸閾值（有氧閾值），約 Z2 上緣")
    snap = ctx.snapshot(day)
    assert snap["upcoming_races"][0]["days_to_go"] == 62 and snap["interests"][0] == {"term": "400m 配速", "count": 3, "definition": None}
    assert snap["interests"][1]["definition"].startswith("第一")
    print("context ok", store.counts())

    b = build_brief(store, 20001)
    md = to_markdown(b)
    assert "Pa:Hr" in md and "台北馬拉松" in md and "400m 配速(x3)" in md and "左腳踝" in md and "AQI (US): 42" in md
    assert b["recent_sessions"][0]["activity_id"] == 19999
    json.dumps(b, ensure_ascii=False, default=str)
    open(_tmp("TEST_MD", ".md"), "w").write(md)
print("ALL OK")
