"""合成資料庫：沒有真實 data/garmin_coach.db（例如剛 clone 公開 repo）時給離線測試用。

ensure_test_db(dst)：有真實資料庫就複製一份到 dst（原本的行為，不動真實記憶）；沒有就用合成資料建一個。
設 TEST_SYNTH=1 可強制用合成資料（用來確認測試不依賴作者的資料）。

合成內容（日期都相對於今天）：45 天每日恢復、一年內每週一次跑步、近 60 天每 10 天一次騎車、近 10 天另加 2 次跑步，
一筆體能快照（LT HR 167）、心率/功率區間、一個賽事與一條筆記；最近 6 筆活動有原始 JSON（含逐秒資料）。
原始 JSON 放在暫存目錄並把 config.RAW_DIR 指過去，所以不會碰到真實的 data/。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "data" / "garmin_coach.db"


def ensure_test_db(dst: str) -> str:
    if REAL_DB.exists() and not os.environ.get("TEST_SYNTH"):
        shutil.copy(REAL_DB, dst)
        return dst
    print("[synth] 沒有真實資料庫（或 TEST_SYNTH=1），改用合成資料")
    build_synthetic(dst)
    return dst


# ---------------------------------------------------------------- 原始 JSON 樣板（Garmin API 的形狀）
def daily_raw(d: date, rhr: int, hrv: int, readiness: int = 71) -> dict:
    ds = d.isoformat()
    return {
        "user_summary": {"restingHeartRate": rhr + 1, "lastSevenDaysAvgRestingHeartRate": rhr + 2, "minHeartRate": rhr - 3, "maxHeartRate": 172,
                         "averageStressLevel": 27, "maxStressLevel": 88, "stressQualifier": "BALANCED", "restStressDuration": 30000,
                         "lowStressDuration": 20000, "mediumStressDuration": 5000, "highStressDuration": 1200,
                         "bodyBatteryHighestValue": 92, "bodyBatteryLowestValue": 21, "bodyBatteryChargedValue": 70, "bodyBatteryDrainedValue": 71,
                         "bodyBatteryMostRecentValue": 35, "totalSteps": 12345, "activeKilocalories": 800, "moderateIntensityMinutes": 20,
                         "vigorousIntensityMinutes": 45, "averageSpo2": 96.0},
        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 25200, "deepSleepSeconds": 5400, "lightSleepSeconds": 13800, "remSleepSeconds": 6000,
                                    "awakeSleepSeconds": 600, "sleepStartTimestampLocal": 1760828400000, "sleepEndTimestampLocal": 1760853600000,
                                    "averageSpO2Value": 95.5, "averageRespirationValue": 14.2, "avgOvernightHrv": float(hrv), "hrvStatus": "BALANCED",
                                    "bodyBatteryChange": 68, "restingHeartRate": rhr, "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}}}},
        "hrv": {"hrvSummary": {"weeklyAvg": hrv - 2, "lastNightAvg": hrv, "lastNight5MinHigh": hrv + 20, "status": "BALANCED",
                               "baseline": {"lowUpper": 48, "balancedLow": 52, "balancedUpper": 68}}},
        "rhr": {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": rhr, "calendarDate": ds}]}}},
        "stress": None,
        "training_readiness": [{"timestamp": f"{ds}T06:00:00.0", "score": readiness, "level": "MODERATE", "feedbackShort": "GOOD_RECOVERY", "recoveryTime": 14}],
        "training_status": {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 52.3, "vo2MaxValue": 52, "fitnessAge": 29}, "cycling": None},
                            "mostRecentTrainingStatus": {"latestTrainingStatusData": {"1": {"trainingStatus": 7, "trainingStatusFeedbackPhrase": "PRODUCTIVE",
                                "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 620, "dailyTrainingLoadChronic": 540, "dailyAcuteChronicWorkloadRatio": 1.15, "acwrStatus": "OPTIMAL"}}}},
                            "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {"1": {"monthlyLoadAerobicLow": 900, "monthlyLoadAerobicHigh": 450,
                                "monthlyLoadAnaerobic": 120, "trainingBalanceFeedbackPhrase": "AEROBIC_HIGH_SHORTAGE"}}}},
        "max_metrics": [{"generic": {"vo2MaxPreciseValue": 52.4, "fitnessAge": 28.5}, "cycling": None}],
    }


def _details(seconds: int, hr0: float, speed: float, power: float) -> dict:
    descs = [{"key": "directTimestamp", "metricsIndex": 0}, {"key": "sumDuration", "metricsIndex": 1},
             {"key": "directHeartRate", "metricsIndex": 2}, {"key": "directSpeed", "metricsIndex": 3}, {"key": "directPower", "metricsIndex": 4}]
    rows = []
    for t in range(0, seconds, 5):      # 每 5 秒一筆就夠（降採樣工具不在乎間距）
        hr = hr0 + (8 * t / seconds) + (0 if t > 300 else -20 * (1 - t / 300))
        rows.append({"metrics": [1760828400000 + t * 1000, t, round(hr, 1), speed, power]})
    return {"metricDescriptors": descs, "activityDetailMetrics": rows}


def activity_raw(aid: int, sport: str, km: float, minutes: int, start: datetime, with_details: bool) -> tuple[dict, dict]:
    dur = minutes * 60.0
    speed = km * 1000 / dur
    run = sport == "running"
    item = {"activityId": aid, "activityName": "Morning Run" if run else "Morning Ride", "activityType": {"typeKey": sport},
            "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"), "startTimeGMT": (start - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
            "distance": km * 1000, "duration": dur, "elapsedDuration": dur + 50, "movingDuration": dur - 20,
            "averageSpeed": round(speed, 3), "maxSpeed": round(speed * 1.3, 3), "averageHR": 148 if run else 135, "maxHR": 171,
            "avgPower": 281 if run else 190, "maxPower": 350, "normPower": 285 if run else 200, "aerobicTrainingEffect": 3.4, "anaerobicTrainingEffect": 0.8,
            "trainingEffectLabel": "AEROBIC_BASE", "activityTrainingLoad": 142.0 if run else 110.0, "vO2MaxValue": 52.0, "calories": 820,
            "elevationGain": 85.0, "elevationLoss": 80.0, "minElevation": 10.0, "maxElevation": 60.0,
            "minTemperature": 24.0, "maxTemperature": 28.0, "locationName": "Taipei", "startLatitude": 25.03, "startLongitude": 121.56,
            "hrTimeInZone_1": 100.0, "hrTimeInZone_2": dur * 0.6, "hrTimeInZone_3": dur * 0.3, "hrTimeInZone_4": 100.0,
            "trainingStressScore": 78.0, "intensityFactor": 0.88}
    if run:
        item.update({"averageRunningCadenceInStepsPerMinute": 176.0, "avgStrideLength": 113.6, "avgVerticalOscillation": 8.1,
                     "avgGroundContactTime": 245.0, "avgVerticalRatio": 7.2, "avgGradeAdjustedSpeed": round(speed * 1.01, 3)})
    laps = []
    n_laps = max(2, int(km))
    for i in range(n_laps):
        laps.append({"lapIndex": i + 1, "intensityType": "ACTIVE", "distance": km * 1000 / n_laps, "duration": dur / n_laps,
                     "averageSpeed": round(speed, 3), "averageHR": (140 if run else 128) + i, "maxHR": 155 + i,
                     "averagePower": (270 if run else 185) + i, "elevationGain": 5, "elevationLoss": 3, "averageRunCadence": 175})
    bundle = {
        "activity": {"activityName": item["activityName"], "timeZoneUnitDTO": {"timeZone": "Asia/Taipei"}, "summaryDTO": {},
                     "metadataDTO": {"selfEvaluationFeeling": "GOOD", "selfEvaluationPerceivedEffort": 6}},
        "splits": {"lapDTOs": laps},
        "weather": {"temp": 77, "apparentTemp": 82, "dewPoint": 70, "relativeHumidity": 80, "windSpeed": 6, "windDirectionCompassPoint": "NE",
                    "weatherTypeDTO": {"desc": "Partly Cloudy"}},
        "hr_zones": [{"zoneNumber": 1, "secsInZone": 100.0}, {"zoneNumber": 2, "secsInZone": dur * 0.6}, {"zoneNumber": 3, "secsInZone": dur * 0.3}],
        "power_zones": [{"zoneNumber": 2, "secsInZone": dur * 0.8}],
        "details": _details(int(dur), 145 if run else 128, speed, 280 if run else 190) if with_details else {},
    }
    return item, bundle


def physio_raw() -> dict:
    return {"lactate_threshold": {"speed_and_heart_rate": {"heartRate": 167, "speed": 3.9}, "power": {"functionalThresholdPower": 310}},
            "cycling_ftp": {"functionalThresholdPower": 250},
            "race_predictions": {"time5K": 1260, "time10K": 2640, "timeHalfMarathon": 5900, "timeMarathon": 12600},
            "endurance_score": {"overallScore": 6200}, "hill_score": None}


def profile_raw() -> dict:
    return {"profile": {"userData": {"gender": "MALE", "birthDate": "1988-05-01", "height": 175.0, "weight": 68500.0,
                                     "vo2MaxRunning": 52.0, "vo2MaxCycling": 50.0, "lactateThresholdHeartRate": 167, "lactateThresholdSpeed": 0.39,
                                     "thresholdHeartRateAutoDetected": True, "ftpAutoDetected": True},
                        "userSleep": {"sleepTime": 82800, "wakeTime": 21600}},
            "hr_zones": [{"sport": "RUNNING", "trainingMethod": "HR_RESERVE", "maxHeartRate": 185, "maxHeartRateUsed": 185, "restingHeartRateUsed": 46,
                          "lactateThresholdHeartRateUsed": 167, "zone1Floor": 93, "zone2Floor": 130, "zone3Floor": 148, "zone4Floor": 160, "zone5Floor": 170},
                         {"sport": "CYCLING", "trainingMethod": "HR_RESERVE", "maxHeartRateUsed": 180, "restingHeartRateUsed": 46,
                          "lactateThresholdHeartRateUsed": 160, "zone1Floor": 90, "zone2Floor": 125, "zone3Floor": 142, "zone4Floor": 155, "zone5Floor": 165}],
            "power_zones": [{"sport": "RUNNING", "functionalThresholdPower": 310, "zone1Floor": 0, "zone2Floor": 250, "zone3Floor": 280, "zone4Floor": 300,
                             "zone5Floor": 320, "zone6Floor": 350, "zone7Floor": 400},
                            {"sport": "CYCLING", "functionalThresholdPower": 250, "zone1Floor": 0, "zone2Floor": 140, "zone3Floor": 190, "zone4Floor": 225,
                             "zone5Floor": 265, "zone6Floor": 300, "zone7Floor": 375}]}


# ---------------------------------------------------------------- 建庫
def build_synthetic(dst: str) -> None:
    from garmin_coach import config
    from garmin_coach.context import Context
    from garmin_coach.normalize import normalize_activity, normalize_daily, normalize_physio, normalize_profile
    from garmin_coach.store import Store

    data = Path(tempfile.mkdtemp(prefix="garmin_coach_synth_"))
    config.DATA_DIR = data
    config.RAW_DIR = data / "raw"
    config.ensure_dirs()
    if os.path.exists(dst):
        os.remove(dst)
    today = date.today()
    with Store(dst) as store:
        for i in range(45):
            d = today - timedelta(days=i)
            store.upsert_daily(normalize_daily(d, daily_raw(d, rhr=46 + (i % 5), hrv=55 + (i % 7), readiness=60 + (i % 4) * 8)))
        sessions = [(days_ago + 1, "running", 10 + (days_ago % 3) * 2, 55) for days_ago in range(0, 365, 7)]
        sessions += [(days_ago, "cycling", 40, 90) for days_ago in range(2, 60, 10)]
        sessions += [(3, "running", 6, 35), (5, "running", 8, 45)]
        sessions.sort(key=lambda s: -s[0])          # 舊的先放，最後 6 筆（最近的）附逐秒資料
        aid = 90000
        for n, (days_ago, sport, km, minutes) in enumerate(sessions):
            aid += 1
            start = datetime.combine(today - timedelta(days=days_ago), datetime.min.time()).replace(hour=6, minute=30)
            with_details = n >= len(sessions) - 6
            item, bundle = activity_raw(aid, sport, km, minutes, start, with_details)
            store.upsert_activity(normalize_activity(item, bundle))
            if with_details:
                (config.RAW_DIR / "activities" / f"{aid}.json").write_text(json.dumps({"list_item": item, **bundle}), encoding="utf-8")
        store.upsert_physio(normalize_physio(today, physio_raw()))
        ctx = Context(store)
        for k, v in normalize_profile(profile_raw()).items():
            ctx.set_profile(k, v)
        ctx.add_event("race", "半程馬拉松", (today + timedelta(days=60)).isoformat(), priority="A", notes="目標 1:45")
        ctx.add_note("status", "最近工作忙，平日只能早上練", source="user")
