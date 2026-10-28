"""AI 教練可呼叫的資料工具。

三層資料：
* 基礎資料層  get_athlete_profile / get_health_status / get_daily_recovery / get_weather_outlook / get_athlete_context
* 過去訓練層  get_recent_training / get_sport_history / get_fitness_metrics
* 這次訓練    get_activity_detail / get_activity_timeseries
寫回：record_note / record_event / record_interest / log_feedback / update_event
知識：search_knowledge / get_paper（論文知識庫）、search_web（網路搜尋：閘門 + 快取，見 websearch.py）

每個函式回傳可 JSON 化的 dict；TOOL_SPECS 是給 OpenAI function calling 的 schema。
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Optional

from . import config
from .context import Context
from .external import fetch_weather_outlook
from .models import DailyRecovery, SessionFeedback, TrainingSession, fmt_duration, fmt_pace, mps_to_pace
from .normalize import dig
from .pricing import estimate_cost
from .store import Store

log = logging.getLogger(__name__)


def _compact_session(s: TrainingSession) -> dict[str, Any]:
    d = {
        "activity_id": s.activity_id, "date": s.start_time.strftime("%Y-%m-%d %H:%M") if s.start_time else None,
        "sport": s.sport, "name": s.name,
        "km": round(s.distance_m / 1000, 2) if s.distance_m else None, "duration": fmt_duration(s.duration_s),
        "pace": fmt_pace(s.avg_pace_s_per_km) if s.sport and "running" in s.sport else None,
        "speed_kph": round(s.avg_speed_mps * 3.6, 1) if s.avg_speed_mps and s.sport and "cycl" in s.sport else None,
        "avg_hr": s.avg_hr, "max_hr": s.max_hr, "avg_power": s.avg_power, "np": s.normalized_power,
        "load": s.training_load, "aerobic_te": s.aerobic_te, "anaerobic_te": s.anaerobic_te, "te_label": s.te_label,
        "pa_hr_decoupling_pct": s.pa_hr_decoupling_pct, "temp_c": s.temp_c, "elev_gain_m": s.elevation_gain_m,
        "detail_level": "full" if "activity" in s.sources else "list",
    }
    return {k: v for k, v in d.items() if v is not None}


def _recovery_row(r: DailyRecovery) -> dict[str, Any]:
    d = {
        "date": r.date.isoformat(), "rhr": r.resting_hr, "hrv": r.hrv_last_night, "hrv_status": r.hrv_status,
        "sleep_h": round(r.sleep_total_min / 60, 1) if r.sleep_total_min else None, "sleep_score": r.sleep_score,
        "deep_min": r.sleep_deep_min, "rem_min": r.sleep_rem_min,
        "bed": r.sleep_start.strftime("%H:%M") if r.sleep_start else None, "wake": r.sleep_end.strftime("%H:%M") if r.sleep_end else None,
        "stress": r.stress_avg, "bb_high": r.bb_high, "bb_low": r.bb_low, "readiness": r.readiness_score,
        "readiness_level": r.readiness_level, "recovery_h": r.recovery_time_hours,
        "acute_load": r.acute_load, "chronic_load": r.chronic_load, "acwr": r.acwr, "steps": r.steps,
    }
    return {k: v for k, v in d.items() if v is not None}


def _stats(vals: list[float]) -> Optional[dict[str, float]]:
    vals = [v for v in vals if v is not None]
    if len(vals) < 3:
        return None
    return {"mean": round(statistics.mean(vals), 1), "sd": round(statistics.pstdev(vals), 1),
            "min": round(min(vals), 1), "max": round(max(vals), 1), "n": len(vals)}


class Tools:
    def __init__(self, store: Store, today: Optional[date] = None) -> None:
        self.store = store
        self.ctx = Context(store)
        self.today = today or date.today()
        self.conversation_id: Optional[int] = None      # Coach 會設；llm_calls / web_searches 用來對應對話
        self._web: Any = None                           # websearch.WebSearcher（延遲建立；測試可換假的）

    # ============ 基礎資料層 ============
    def get_athlete_profile(self) -> dict[str, Any]:
        """身體資料、心率/功率區間、體重、使用者自填資料。"""
        prof = self.ctx.get_profile()
        garmin_keys = {"garmin_body", "hr_zones", "power_zones", "latest_weight", "sleep_window", "devices", "home_location"}
        out = {k: prof[k] for k in garmin_keys if k in prof}
        out["user_set"] = {k: v for k, v in prof.items() if k not in garmin_keys}
        return out

    def get_health_status(self, day: Optional[str] = None, baseline_days: int = 30) -> dict[str, Any]:
        """當天 RHR / HRV / 睡眠 / Body Battery / 準備度 與近 N 天基準比較，判斷身體狀況是否正常。"""
        d = date.fromisoformat(day) if day else self.today
        today_rec = self.store.get_daily(d)
        base = [r for r in self.store.daily_range(d - timedelta(days=baseline_days), d - timedelta(days=1))]
        baseline = {
            "rhr": _stats([r.resting_hr for r in base]),
            "hrv": _stats([r.hrv_last_night for r in base]),
            "sleep_h": _stats([r.sleep_total_min / 60 for r in base if r.sleep_total_min]),
            "sleep_score": _stats([r.sleep_score for r in base]),
            "stress": _stats([r.stress_avg for r in base]),
            "bb_high": _stats([r.bb_high for r in base]),
            "readiness": _stats([r.readiness_score for r in base]),
        }
        flags: list[str] = []
        if today_rec:
            t = today_rec
            b = baseline
            if t.resting_hr and b["rhr"] and t.resting_hr - b["rhr"]["mean"] >= max(4, 2 * b["rhr"]["sd"]):
                flags.append(f"RHR {t.resting_hr} 高於基準 {b['rhr']['mean']}（+{round(t.resting_hr - b['rhr']['mean'], 1)}），可能疲勞/生病/脫水")
            if t.hrv_last_night and t.hrv_baseline_low and t.hrv_last_night < t.hrv_baseline_low:
                flags.append(f"HRV {t.hrv_last_night} 低於 Garmin 平衡區間下限 {t.hrv_baseline_low}")
            elif t.hrv_last_night and b["hrv"] and t.hrv_last_night < b["hrv"]["mean"] - b["hrv"]["sd"]:
                flags.append(f"HRV {t.hrv_last_night} 低於 30 天平均 {b['hrv']['mean']} 一個標準差")
            if t.hrv_status and t.hrv_status not in ("BALANCED",):
                flags.append(f"Garmin HRV 狀態 {t.hrv_status}")
            if t.sleep_total_min and t.sleep_total_min < 360:
                flags.append(f"睡眠只有 {round(t.sleep_total_min / 60, 1)} 小時")
            elif t.sleep_total_min and b["sleep_h"] and t.sleep_total_min / 60 < b["sleep_h"]["mean"] - 1:
                flags.append(f"睡眠 {round(t.sleep_total_min / 60, 1)}h 比平均少 1 小時以上")
            if t.bb_high is not None and t.bb_high < 60:
                flags.append(f"Body Battery 最高只充到 {t.bb_high}")
            if t.readiness_score is not None and t.readiness_score < 35:
                flags.append(f"訓練準備度 {t.readiness_score}（{t.readiness_level}）")
            if t.acwr and t.acwr > 1.4:
                flags.append(f"ACWR {t.acwr} 偏高，負荷增加太快")
            if t.stress_avg and b["stress"] and t.stress_avg > b["stress"]["mean"] + 10:
                flags.append(f"壓力 {t.stress_avg} 高於平均 {b['stress']['mean']}")
        level = "normal" if not flags else ("attention" if len(flags) <= 2 else "poor")
        return {
            "date": d.isoformat(),
            "today": _recovery_row(today_rec) if today_rec else None,
            "baseline_days": baseline_days, "baseline": {k: v for k, v in baseline.items() if v},
            "assessment": level, "flags": flags,
            "last_7_days": [_recovery_row(r) for r in self.store.daily_range(d - timedelta(days=6), d)],
        }

    def get_daily_recovery(self, start: str, end: Optional[str] = None) -> dict[str, Any]:
        """指定日期範圍的每日恢復資料。"""
        s = date.fromisoformat(start)
        e = date.fromisoformat(end) if end else self.today
        return {"rows": [_recovery_row(r) for r in self.store.daily_range(s, e)]}

    def _home_location(self) -> Optional[tuple[float, float, str]]:
        prof = self.ctx.get_profile()
        loc = prof.get("home_location")
        if isinstance(loc, dict) and loc.get("lat") is not None:
            return float(loc["lat"]), float(loc["lon"]), str(loc.get("name") or "home")
        for s in self.store.recent_activities(30):
            if s.lat is not None and s.lon is not None:
                return s.lat, s.lon, s.location_name or "最近戶外活動地點"
        return None

    def get_weather_outlook(self, past_days: int = 3, forecast_days: int = 3) -> dict[str, Any]:
        """最近幾天與未來幾天的氣溫、濕度、降雨、日出日落（Open-Meteo，以家或最近活動地點為準）。"""
        loc = self._home_location()
        if not loc:
            return {"error": "沒有位置資訊，請用 context set home_location '{\"lat\":..,\"lon\":..}' 設定"}
        lat, lon, name = loc
        w = fetch_weather_outlook(lat, lon, past_days, forecast_days)
        if not w:
            return {"error": "天氣服務無回應"}
        w["location"] = name
        return w

    def get_athlete_context(self) -> dict[str, Any]:
        """目標賽事、行程/限制、傷病與近況筆記、常關注主題。"""
        return self.ctx.snapshot(self.today)

    # ============ 過去訓練層 ============
    def get_recent_training(self, days: int = 10) -> dict[str, Any]:
        """最近 N 天所有訓練 + 負荷總計，用來判斷負荷與強度是否合適。"""
        start = self.today - timedelta(days=days - 1)
        sessions = self.store.activities_between(start, self.today)
        by_sport: dict[str, dict[str, float]] = {}
        for s in sessions:
            b = by_sport.setdefault(s.sport or "other", {"sessions": 0, "km": 0.0, "hours": 0.0, "load": 0.0})
            b["sessions"] += 1
            b["km"] += (s.distance_m or 0) / 1000
            b["hours"] += (s.duration_s or 0) / 3600
            b["load"] += s.training_load or 0
        for b in by_sport.values():
            for k in ("km", "hours", "load"):
                b[k] = round(b[k], 1)
        daily = self.store.daily_range(start, self.today)
        return {
            "window": f"{start.isoformat()}~{self.today.isoformat()}",
            "sessions": [_compact_session(s) for s in sessions],
            "totals": {"sessions": len(sessions), "km": round(sum((s.distance_m or 0) for s in sessions) / 1000, 1),
                       "hours": round(sum((s.duration_s or 0) for s in sessions) / 3600, 1),
                       "load": round(sum((s.training_load or 0) for s in sessions), 0)},
            "by_sport": by_sport,
            "load_trend": [{"date": r.date.isoformat(), "acute": r.acute_load, "chronic": r.chronic_load, "acwr": r.acwr,
                            "readiness": r.readiness_score} for r in daily if r.acute_load is not None],
            "weekly": self.store.weekly_load(self.today, 6),
            "note": "sessions 只是摘要；某一次的分段/區間/天氣用 get_activity_detail(activity_id)，逐秒變化用 get_activity_timeseries(activity_id)",
        }

    def get_sport_history(self, sport: str = "running", days: int = 365, recent_n: int = 8) -> dict[str, Any]:
        """同一項目一年內的訓練：每月彙總、最佳表現、最近幾次，用來看變化與建立基準。"""
        start = self.today - timedelta(days=days)
        rows = [s for s in self.store.activities_between(start, self.today) if s.sport and sport.lower() in s.sport.lower()]
        monthly: dict[str, dict[str, Any]] = {}
        for s in rows:
            m = s.start_time.strftime("%Y-%m") if s.start_time else "?"
            b = monthly.setdefault(m, {"sessions": 0, "km": 0.0, "hours": 0.0, "load": 0.0, "_hr": [], "_pace": [], "_pw": [], "longest_km": 0.0})
            b["sessions"] += 1
            b["km"] += (s.distance_m or 0) / 1000
            b["hours"] += (s.duration_s or 0) / 3600
            b["load"] += s.training_load or 0
            b["longest_km"] = max(b["longest_km"], (s.distance_m or 0) / 1000)
            if s.avg_hr:
                b["_hr"].append(s.avg_hr)
            if s.avg_pace_s_per_km:
                b["_pace"].append(s.avg_pace_s_per_km)
            if s.avg_power:
                b["_pw"].append(s.avg_power)
        out_monthly = []
        for m in sorted(monthly):
            b = monthly[m]
            out_monthly.append({
                "month": m, "sessions": b["sessions"], "km": round(b["km"], 1), "hours": round(b["hours"], 1),
                "load": round(b["load"]), "longest_km": round(b["longest_km"], 1),
                "avg_hr": round(statistics.mean(b["_hr"])) if b["_hr"] else None,
                "avg_pace": fmt_pace(statistics.mean(b["_pace"])) if b["_pace"] and "run" in sport else None,
                "avg_power": round(statistics.mean(b["_pw"])) if b["_pw"] else None,
            })
        # 基準：心率 vs 配速/功率的效率（aerobic efficiency）
        eff = []
        for s in rows:
            if s.avg_hr and s.avg_speed_mps and s.duration_s and s.duration_s > 1200:
                eff.append({"date": s.start_time.date().isoformat() if s.start_time else None, "activity_id": s.activity_id,
                            "pace": fmt_pace(s.avg_pace_s_per_km) if "run" in sport else None,
                            "speed_kph": round(s.avg_speed_mps * 3.6, 1), "avg_hr": s.avg_hr,
                            "speed_per_hr": round(s.avg_speed_mps * 60 / s.avg_hr, 3),   # m/min per bpm
                            "power_per_hr": round(s.avg_power / s.avg_hr, 2) if s.avg_power else None, "km": round(s.distance_m / 1000, 1) if s.distance_m else None})
        bests = {}
        if rows:
            longest = max(rows, key=lambda s: s.distance_m or 0)
            bests["longest"] = _compact_session(longest)
            steady = [s for s in rows if (s.distance_m or 0) >= 5000 and s.avg_pace_s_per_km]
            if steady and "run" in sport:
                bests["fastest_avg_pace_over_5km"] = _compact_session(min(steady, key=lambda s: s.avg_pace_s_per_km))
            hi_load = max(rows, key=lambda s: s.training_load or 0)
            bests["highest_load"] = _compact_session(hi_load)
        return {
            "sport": sport, "window": f"{start.isoformat()}~{self.today.isoformat()}", "total_sessions": len(rows),
            "monthly": out_monthly, "bests": bests,
            "efficiency_trend": eff[-12:],
            "recent": [_compact_session(s) for s in rows[-min(int(recent_n or 8), 20):]],
        }

    def get_fitness_metrics(self, history_days: int = 365, max_points: int = 4, include_zones: bool = True) -> dict[str, Any]:
        """乳酸閾值、FTP、比賽預測、VO2max、跑步耐受度、耐力/爬坡分數：最新值 + 一年趨勢。"""
        p = self.store.latest_physio()
        latest: dict[str, Any] = {}
        if p:
            latest = {k: v for k, v in p.model_dump(mode="json").items() if v is not None and k not in ("sources", "synced_at")}
            latest["lt_pace"] = fmt_pace(p.lt_pace_s_per_km)
            for k in ("pred_5k_s", "pred_10k_s", "pred_half_s", "pred_marathon_s"):
                if latest.get(k):
                    latest[k.replace("_s", "")] = fmt_duration(latest[k])
        start = self.today - timedelta(days=history_days)
        history: dict[str, list[dict[str, Any]]] = {}
        for m in self.store.metric_names():
            series = self.store.metric_series(m, start, self.today)
            if not series:
                continue
            if len(series) > max_points:          # 均勻取樣，保留頭尾
                step = len(series) / max_points
                series = [series[int(i * step)] for i in range(max_points - 1)] + [series[-1]]
            for r in series:
                if m == "lt_pace_s_per_km":
                    r["pace"] = fmt_pace(r["value"])
                elif m.startswith("pred_"):
                    r["time"] = fmt_duration(r["value"])
            history[m] = series
        prof = self.ctx.get_profile()
        out = {"latest": latest, "history": history}
        if include_zones:
            out["hr_zones"], out["power_zones"] = prof.get("hr_zones"), prof.get("power_zones")
        return out

    # ============ 這次訓練 ============
    def get_activity_detail(self, activity_id: Optional[int] = None) -> dict[str, Any]:
        """單次訓練完整資料：摘要、分段、心率/功率區間（含區間邊界）、天氣、主觀回饋。不給 id 則取最新。"""
        s = self.store.get_activity(int(activity_id)) if activity_id else self.store.latest_activity()
        if not s:
            return {"error": "找不到活動"}
        d = s.model_dump(mode="json")
        d = {k: v for k, v in d.items() if v is not None and k not in ("sources", "synced_at", "lat", "lon", "start_time_utc")}
        d["avg_pace"] = fmt_pace(s.avg_pace_s_per_km)
        d["avg_gap_pace"] = fmt_pace(s.avg_gap_pace_s_per_km) if s.avg_gap_pace_s_per_km else None
        d["duration"] = fmt_duration(s.duration_s)
        d["km"] = round(s.distance_m / 1000, 2) if s.distance_m else None
        d["hr_zone_time"] = {k: fmt_duration(v) for k, v in s.hr_zone_s.items()}
        d["power_zone_time"] = {k: fmt_duration(v) for k, v in s.power_zone_s.items()}
        d["splits"] = [{**{k: v for k, v in sp.model_dump().items() if v is not None}, "pace": fmt_pace(sp.avg_pace_s_per_km)} for sp in s.splits]
        prof = self.ctx.get_profile()
        zone_sport = "cycling" if s.sport and "cycl" in s.sport else "default"
        d["hr_zone_bounds"] = (prof.get("hr_zones") or {}).get(zone_sport)
        pz_sport = "cycling" if zone_sport == "cycling" else "running"
        d["power_zone_bounds"] = (prof.get("power_zones") or {}).get(pz_sport)
        fb = self.store.get_feedback(s.activity_id)
        d["athlete_feedback"] = {k: v for k, v in fb.model_dump(mode="json").items() if v is not None} if fb else None
        d["detail_level"] = "full" if "activity" in s.sources else "list_only（可用 sync 補細節）"
        return d

    def get_activity_timeseries(self, activity_id: Optional[int] = None, step_s: int = 60) -> dict[str, Any]:
        """逐秒資料降採樣（預設每 60 秒一筆）：心率、配速、功率、步頻、海拔、溫度、體力、表現狀況。"""
        s = self.store.get_activity(int(activity_id)) if activity_id else self.store.latest_activity()
        if not s:
            return {"error": "找不到活動"}
        path = config.RAW_DIR / "activities" / f"{s.activity_id}.json"
        if not path.exists():
            return {"error": "沒有原始檔，請先 sync"}
        raw = json.loads(path.read_text(encoding="utf-8"))
        details = raw.get("details") or {}
        descs = details.get("metricDescriptors") or []
        rows = details.get("activityDetailMetrics") or []
        if not descs or not rows:
            return {"error": "此活動沒有逐秒資料（list_only），用 sync --force 補抓"}
        idx = {d.get("key"): d.get("metricsIndex") for d in descs}
        want = {
            "hr": "directHeartRate", "speed": "directSpeed", "gap_speed": "directGradeAdjustedSpeed", "power": "directPower",
            "cadence": "directRunCadence", "elev": "directElevation", "temp": "directAirTemperature",
            "stamina": "directAvailableStamina", "perf": "directPerformanceCondition", "dist": "sumDistance",
        }
        i_t = idx.get("sumDuration")
        buckets: dict[int, dict[str, list[float]]] = {}
        for n, r in enumerate(rows):
            m = r.get("metrics") or []
            t = m[i_t] if i_t is not None and i_t < len(m) and m[i_t] is not None else n
            b = buckets.setdefault(int(t // step_s), {})
            for k, key in want.items():
                i = idx.get(key)
                if i is not None and i < len(m) and m[i] is not None:
                    b.setdefault(k, []).append(float(m[i]))
        out = []
        for bi in sorted(buckets):
            b = buckets[bi]
            avg = lambda k: round(statistics.mean(b[k]), 1) if b.get(k) else None  # noqa: E731
            row: dict[str, Any] = {"t": fmt_duration(bi * step_s), "t_s": bi * step_s}
            if b.get("dist"):
                row["km"] = round(max(b["dist"]) / 1000, 2)
            if avg("hr"): row["hr"] = round(avg("hr"))
            if avg("speed"):
                row["pace"] = fmt_pace(mps_to_pace(avg("speed"))) if s.sport and "run" in s.sport else None
                row["kph"] = round(avg("speed") * 3.6, 1)
            if avg("gap_speed") and s.sport and "run" in s.sport: row["gap_pace"] = fmt_pace(mps_to_pace(avg("gap_speed")))
            if avg("power"): row["power"] = round(avg("power"))
            if avg("cadence"): row["cadence"] = round(avg("cadence"))
            if avg("elev") is not None: row["elev"] = avg("elev")
            if avg("temp") is not None: row["temp"] = avg("temp")
            if avg("stamina") is not None: row["stamina"] = round(avg("stamina"))
            if avg("perf") is not None: row["perf_cond"] = round(avg("perf"))
            out.append({k: v for k, v in row.items() if v is not None})
        return {"activity_id": s.activity_id, "step_s": step_s, "points": len(out), "rows": out}

    # ============ 寫回 ============
    def record_note(self, category: str, content: str, body_part: Optional[str] = None, severity: Optional[str] = None,
                    expires_at: Optional[str] = None, source: str = "ai") -> dict[str, Any]:
        """記錄對話中得知的重要資訊（傷痛、生病、近況、比賽進度、偏好）。傷痛請寫明部位、程度、何時開始、什麼情況會痛。"""
        expires_at = _valid_date(expires_at)
        parts = []
        body_part, severity = _clean(body_part), _clean(severity)
        if body_part:
            parts.append(f"部位: {body_part}")
        if severity:
            parts.append(f"程度: {severity}")
        full = (" | ".join(parts) + " | " if parts else "") + content
        for n in self.ctx.list_notes(limit=200):          # 避免重複
            if n["content"] == full:
                return {"note_id": n["id"], "duplicate": True}
        nid = self.ctx.add_note(category, full, source=source, expires_at=expires_at)
        return {"note_id": nid, "recorded": full}

    def record_event(self, kind: str, title: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                     priority: Optional[str] = None, notes: Optional[str] = None) -> dict[str, Any]:
        """記錄比賽目標、家庭/工作行程、時間限制、生病或受傷期間。"""
        for e in self.ctx.list_events(active_only=False):
            if e["title"] == title and e["start_date"] == start_date:
                return {"event_id": e["id"], "duplicate": True}
        sd = _valid_date(start_date)
        if sd:
            for e in self.ctx.list_events(active_only=True):
                if e.get("kind") == kind and e.get("start_date") == sd:          # 同類別同一天 = 同一件事，補資訊不重複建
                    merged = (e.get("notes") or "")
                    if notes and notes not in merged:
                        merged = (merged + "｜" if merged else "") + notes
                    self.ctx.update_event(int(e["id"]), notes=merged or None, priority=_valid_priority(priority))
                    return {"event_id": e["id"], "duplicate": True, "updated": True}
        eid = self.ctx.add_event(kind, title, sd, _valid_date(end_date), _valid_priority(priority), notes or None)
        return {"event_id": eid}

    def update_event(self, event_id: int, status: Optional[str] = None, notes: Optional[str] = None,
                     start_date: Optional[str] = None, end_date: Optional[str] = None,
                     priority: Optional[str] = None, title: Optional[str] = None) -> dict[str, Any]:
        """更新事件狀態（done / cancelled）、優先順序（A/B/C）、備註、日期或標題。"""
        self.ctx.update_event(int(event_id), status=status, notes=notes, start_date=_valid_date(start_date),
                              end_date=_valid_date(end_date), priority=_valid_priority(priority), title=(title or None))
        return {"event_id": event_id, "updated": True}

    def record_interest(self, term: str, definition: Optional[str] = None, notes: Optional[str] = None) -> dict[str, Any]:
        """使用者問到的專業詞彙或常關注主題（LT1、400m 配速、步頻…），累計次數。"""
        parts = [x.strip() for x in re.split(r"[／/、]", term) if x.strip()]
        if len(parts) > 1 and all(len(x) <= 8 for x in parts):           # 「LT1／LT2」→ 分別記 LT1、LT2
            for x in parts:
                self.record_interest(x, None, notes)
            return {"term": parts, "ok": True}
        key = term.split("（")[0].split("(")[0].strip() or term.strip()   # "LT1（第一乳酸閾值）" → "LT1"，次數才會累計在同一個詞
        if key != term.strip() and not definition:
            definition = term.strip()
        self.ctx.touch_interest(key, definition, notes)
        return {"term": key, "ok": True}

    def log_feedback(self, activity_id: Optional[int] = None, rpe: Optional[int] = None, feeling: Optional[str] = None,
                     pain: Optional[str] = None, sick: Optional[bool] = None, notes: Optional[str] = None) -> dict[str, Any]:
        """記錄使用者對這次訓練的主觀回饋：RPE(1-10)、體感、疼痛、生病。"""
        rpe = _parse_rpe(rpe)
        clear_rpe = rpe == 0                                  # 0 = 清除先前誤記的 RPE
        if rpe is not None and not (1 <= int(rpe) <= 10):
            rpe = None                                   # 超出範圍代表沒提供
        feeling, pain, notes = feeling or None, pain or None, notes or None
        sick = True if sick else None                    # 只記錄明確生病，false 視為未提供
        if not activity_id:
            latest = self.store.latest_activity()
            if not latest:
                return {"error": "沒有活動"}
            activity_id = latest.activity_id
        self.store.upsert_feedback(SessionFeedback(activity_id=int(activity_id), rpe=rpe, feeling=feeling, pain=pain, sick=sick, notes=notes))
        if clear_rpe:
            self.store.conn.execute("UPDATE session_feedback SET rpe=NULL WHERE activity_id=?", (int(activity_id),))
            self.store.conn.commit()
        fb = self.store.get_feedback(int(activity_id))
        return {k: v for k, v in fb.model_dump(mode="json").items() if v is not None} if fb else {}

    # ============ 背景知識庫 ============
    SNIPPET_CHARS = 900     # 每筆檢索結果最多送這麼多字給模型（原本整段 2400 字）；要全文用 get_paper

    @staticmethod
    def _snippet(text: str, limit: int) -> str:
        """保留開頭（標題/期刊/目的）與結尾（結果/結論），中間略過。"""
        if len(text) <= limit:
            return text
        head = limit // 3
        return text[:head].rstrip() + " … " + text[-(limit - head):].lstrip()

    def _kb_ready(self) -> Optional[dict[str, Any]]:
        from .knowledge import KB_DB, Embedder, KnowledgeBase
        if not KB_DB.exists():
            return {"error": "知識庫尚未建立，請先執行 kb build"}
        if not hasattr(self, "_kb"):
            self._kb = KnowledgeBase()
            self._embedder = Embedder() if config.OPENAI_API_KEY else None
        return None

    def search_knowledge(self, query: str, k: int = 4, topic: Optional[str] = None, max_chars: Optional[int] = None) -> dict[str, Any]:
        """搜尋運動科學論文知識庫（近 10 年，PubMed）。query 請用英文關鍵字。回傳精簡段落與引用資訊；要看完整內容用 get_paper。"""
        err = self._kb_ready()
        if err:
            return err
        limit = int(max_chars or self.SNIPPET_CHARS)
        vec = self._embed_query(query)
        hits = self._kb.search(query, k=min(int(k or 4), 8), topic=topic, embedder=self._embedder, qvec=vec)
        out = []
        for h in hits:
            d = {k2: v for k2, v in h.items() if k2 not in ("score", "doi")}
            d["text"] = self._snippet(h.get("text") or "", limit)
            out.append(d)
        res: dict[str, Any] = {"query": query, "results": out, "note": "段落已截短；需要某篇的完整摘要或全文段落時呼叫 get_paper(pmid)"}
        web = self._web_cached(vec, min_sim=0.55, limit=2)
        if web:
            res["web_cached"] = web
            res["note"] += "；web_cached 是之前上網查過的資料（快取），完整內容用 search_web(cache_id=N)，不花錢"
        return res

    def get_paper(self, pmid: str, max_chars: int = 6000) -> dict[str, Any]:
        """取某篇論文（pmid）的完整摘要與主要全文段落，給需要細節（數字、方法、劑量）時用。"""
        err = self._kb_ready()
        if err:
            return err
        c = self._kb.conn
        r = c.execute("SELECT pmid, title, journal, year, authors, abstract, pub_types FROM kb_papers WHERE pmid=?", (str(pmid),)).fetchone()
        if not r:
            return {"error": f"找不到 pmid {pmid}"}
        authors = json.loads(r["authors"] or "[]")
        first = authors[0].split()[0] if authors else "?"
        citation = f"{first} et al. ({r['year']})" if len(authors) > 1 else f"{first} ({r['year']})"
        chunks = [row["text"] for row in c.execute(
            "SELECT text FROM kb_chunks WHERE pmid=? AND section != 'abstract' ORDER BY ord", (str(pmid),))]
        body, used = [], 0
        for ch in chunks:
            if used + len(ch) > int(max_chars):
                break
            body.append(ch); used += len(ch)
        return {"pmid": r["pmid"], "citation": citation, "title": r["title"], "journal": r["journal"], "year": r["year"],
                "pub_types": json.loads(r["pub_types"] or "[]"), "abstract": r["abstract"],
                "fulltext_sections": body, "fulltext_truncated": used < sum(len(ch) for ch in chunks)}

    # ============ 網路搜尋（閘門 + 快取；細節見 websearch.py） ============
    WEB_SUMMARY_CHARS = 5000    # 回給主模型的整理最多這麼多字（資料庫存完整；用 cache_id 取可到 12000）

    def _embed_query(self, text: str) -> Optional[Any]:
        """查詢向量（knowledge.Embedder，已正規化）；沒有 API key 或失敗回 None。"""
        if self._kb_ready():
            return None
        if not getattr(self, "_embedder", None):
            return None
        try:
            return self._embedder.embed([text])[0]
        except Exception as e:  # noqa: BLE001
            log.warning("查詢向量化失敗：%s", e)
            return None

    def _searcher(self) -> Any:
        if self._web is None:
            from .websearch import WebSearcher
            self._web = WebSearcher()
        return self._web

    def _log_llm(self, model: str, purpose: str, delta: dict[str, int], extra_cost: float = 0.0) -> float:
        """工具內部的模型呼叫也記進 llm_calls（納入每日預算與 /cost）。"""
        cost = round((estimate_cost(delta, model) or 0.0) + extra_cost, 4)
        self.store.conn.execute(
            "INSERT INTO llm_calls(conversation_id, ts, model, purpose, input, cached_input, output, cost_usd) VALUES(?,?,?,?,?,?,?,?)",
            (self.conversation_id, datetime.now().isoformat(timespec="seconds"), model, purpose,
             delta.get("input", 0), delta.get("cached_input", 0), delta.get("output", 0), cost))
        self.store.conn.commit()
        return cost

    def _judge(self, prompt: str) -> Optional[bool]:
        w = self._searcher()
        ans, delta = w.judge(prompt)
        self._log_llm(w.judge_model, "web_gate", delta)
        return ans

    def _kb_coverage(self, vec: Optional[Any]) -> dict[str, Any]:
        """查詢與論文知識庫的最高餘弦相似度與最接近的三段。"""
        if vec is None or self._kb_ready():
            return {"max_sim": 0.0, "hits": []}
        hits = self._kb.nearest(vec, k=3)
        return {"max_sim": hits[0]["sim"] if hits else 0.0, "hits": hits}

    def _web_cached(self, vec: Optional[Any], min_sim: float, limit: int) -> list[dict[str, Any]]:
        from .websearch import WebCache
        out = []
        for r in WebCache(self.store).similar(vec, min_sim, limit=limit):
            out.append({"cache_id": r["id"], "fetched_at": (r["ts"] or "")[:10], "query": r["query"], "similarity": r["sim"],
                        "summary": self._snippet(r["summary"] or "", 500)})
        return out

    def _web_result(self, rec: dict[str, Any], cached: bool, cost: float = 0.0, vec: Optional[Any] = None, full: bool = False) -> dict[str, Any]:
        from .websearch import WebCache
        summary = rec.get("summary") or ""
        searches = json.loads(rec.get("searches") or "[]")
        limit = 12000 if full else self.WEB_SUMMARY_CHARS
        out: dict[str, Any] = {
            "cache_id": rec["id"], "cached": cached, "fetched_at": (rec.get("ts") or "")[:16], "query": rec["query"],
            "summary": summary[:limit], "truncated": len(summary) > limit,
            "citations": json.loads(rec.get("citations") or "[]")[:10],
            "searches_run": [s.get("query") for s in searches if s.get("query")],
        }
        # 其他相關的舊查詢（同一賽事的另一個面向等）：先看這些，不要再拆新查詢上網
        related = WebCache(self.store).similar(vec, 0.55, limit=3, exclude_id=rec["id"]) if vec is not None else []
        if related:
            out["related_cached"] = [{"cache_id": r["id"], "fetched_at": (r["ts"] or "")[:10], "query": r["query"],
                                      "summary_head": (r["summary"] or "")[:300]} for r in related]
            out["related_note"] = "related_cached 是之前查過的相關資料；需要完整內容用 search_web(cache_id=N)（不花錢），不要再拆成新查詢上網。"
        if cached:
            try:
                age = (date.today() - date.fromisoformat((rec.get("ts") or "")[:10])).days
            except ValueError:
                age = None
            out["note"] = (f"這是 {age} 天前查過的結果（快取，不花錢）。回答時註明資料日期（{out['fetched_at'][:10]}）；"
                           "只有使用者明確要最新資訊而這份明顯過時，才用 ignore_cache=true 重查。")
        else:
            out["note"] = f"已上網查並存入快取 #{rec['id']}（費用 US${cost:.4f}）；之後同樣的問題會直接回傳快取。回答時標明來源與查詢日期。"
        return out

    def search_web(self, query: str, reason: str = "", ignore_cache: bool = False, cache_id: Optional[int] = None) -> dict[str, Any]:
        """網路搜尋（費用高）。先查快取，再確認本地知識庫拿不到，再看每日上限，最後才真的上網；結果完整記錄。"""
        from .websearch import CACHE_JUDGE_PROMPT, KB_JUDGE_PROMPT, WebCache, format_kb_hits, normalize_query
        cache = WebCache(self.store)
        if cache_id:
            rec = cache.get(int(cache_id))
            if not rec:
                return {"error": f"找不到網路快取 #{cache_id}"}
            cache.touch(rec["id"])
            return self._web_result(rec, cached=True, vec=cache.embedding(rec["id"]), full=True)
        q = (query or "").strip()
        if not q:
            return {"error": "query 不可為空"}
        if not config.WEB_SEARCH_ENABLED:
            return {"denied": True, "reason": "網路搜尋未啟用（.env WEB_SEARCH_ENABLED=0）"}
        vec = self._embed_query(q)
        if vec is None:
            return {"error": "網路搜尋需要 OPENAI_API_KEY 與知識庫（向量比對）"}
        norm = normalize_query(q)
        reason = (reason or "").strip()
        # 1) 快取：完全相同 → 相近（高相似度直接用；中間地帶由便宜模型確認）
        if not ignore_cache:
            rec = cache.find_exact(norm)
            if not rec:
                for cand in cache.similar(vec, config.WEB_CACHE_MAYBE, limit=3):
                    if cand["sim"] >= config.WEB_CACHE_SIMILARITY:
                        rec = cand
                        break
                    if self._judge(CACHE_JUDGE_PROMPT.format(old=cand["query"], old_summary=(cand["summary"] or "")[:400], new=q)):
                        rec = cand
                        break
            if rec:
                cache.touch(rec["id"])
                log.info("web 快取命中 #%s（%s）← %s", rec["id"], rec["query"][:40], q[:40])
                return self._web_result(rec, cached=True, vec=vec)
        # 2) 閘門：知識庫拿得到就不放行
        cov = self._kb_coverage(vec)
        judged = "skip"
        if cov["max_sim"] >= config.WEB_KB_FLOOR:
            ans = self._judge(KB_JUDGE_PROMPT.format(query=q, reason=reason or "未說明", snippets=format_kb_hits(cov["hits"])))
            if ans is None:
                ans = cov["max_sim"] >= 0.6          # 判斷失敗時保守：相似度高就當知識庫有
            judged = "covered" if ans else "not_covered"
            if ans:
                cache.save_attempt("denied", q, norm, reason, "知識庫已有相關資料", cov["max_sim"], judged, self.conversation_id)
                log.info("web 拒絕（知識庫可回答，sim=%.2f）：%s", cov["max_sim"], q[:60])
                return {"denied": True, "reason": "論文知識庫已有相關資料，不放行網路搜尋。請直接用下面的 kb_results 回答（要更多用 search_knowledge / get_paper）。",
                        "kb_max_similarity": cov["max_sim"],
                        "kb_results": [{"pmid": h["pmid"], "citation": h["citation"], "title": h["title"], "pub_types": h["pub_types"],
                                        "text": self._snippet(h["text"], self.SNIPPET_CHARS)} for h in cov["hits"]]}
        # 3) 上限：每天 / 每段對話
        related = cache.similar(vec, 0.55, limit=3)
        n = cache.count_today()
        if n >= config.WEB_SEARCH_DAILY_MAX:
            cache.save_attempt("denied", q, norm, reason, f"每日上限 {config.WEB_SEARCH_DAILY_MAX}", cov["max_sim"], judged, self.conversation_id)
            return self._denied_limit(f"今天的網路搜尋已達上限（{n}/{config.WEB_SEARCH_DAILY_MAX} 次）", related)
        nc = cache.count_conversation(self.conversation_id)
        if nc >= config.WEB_SEARCH_PER_CONVERSATION:
            cache.save_attempt("denied", q, norm, reason, f"每段對話上限 {config.WEB_SEARCH_PER_CONVERSATION}", cov["max_sim"], judged, self.conversation_id)
            return self._denied_limit(f"這段對話的網路搜尋已達上限（{nc}/{config.WEB_SEARCH_PER_CONVERSATION} 次）", related)
        # 4) 執行
        w = self._searcher()
        try:
            res = w.run(q, reason)
        except Exception as e:  # noqa: BLE001
            cache.save_attempt("failed", q, norm, reason, f"{type(e).__name__}: {e}"[:300], cov["max_sim"], judged, self.conversation_id)
            log.warning("web 搜尋失敗：%s", e)
            return {"error": f"網路搜尋失敗：{type(e).__name__}: {e}"}
        cost = self._log_llm(w.model, "web", res["usage"], extra_cost=res["calls"] * config.WEB_SEARCH_CALL_USD)
        rec = cache.save_done(q, norm, reason, cov["max_sim"], judged, res, cost, vec, self.conversation_id)
        log.info("web 放行 #%s：%s（%d 次搜尋，US$%.4f）", rec["id"], q[:60], res["calls"], cost)
        return self._web_result(rec, cached=False, cost=cost, vec=vec)

    @staticmethod
    def _denied_limit(reason: str, related: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"denied": True, "reason": reason + "。明說拿不到最新資料，改用本地資料或請使用者提供；不要換個問法再試。"}
        if related:
            out["related_cached"] = [{"cache_id": r["id"], "fetched_at": (r["ts"] or "")[:10], "query": r["query"], "summary_head": (r["summary"] or "")[:300]} for r in related]
            out["related_note"] = "之前查過的相關資料，完整內容用 search_web(cache_id=N)（不花錢）"
        return out

    # ============ 訓練前資料包 ============
    def preflight(self, activity_id: Optional[int] = None, include_weather: bool = True) -> dict[str, Any]:
        """對話開始前一次打包：三層資料的核心部分。"""
        detail = self.get_activity_detail(activity_id)
        day = None
        if detail.get("start_time"):
            day = detail["start_time"][:10]
        pkg = {
            "generated_at": datetime.now().isoformat(timespec="minutes"),
            "today": self.today.isoformat(),
            "athlete_profile": self.get_athlete_profile(),
            "health_status": self.get_health_status(),  # 永遠是今天
            "athlete_context": self.get_athlete_context(),
            "recent_training_10d": self.get_recent_training(10),
            "fitness_metrics": self.get_fitness_metrics(max_points=4, include_zones=False),   # 區間已在 athlete_profile
            "this_session": detail,
        }
        if day and day != self.today.isoformat():
            # 這次訓練不是今天：另附訓練當天的身體狀態（精簡版），避免把舊的準備度當成今天的
            hs = self.get_health_status(day)
            pkg["session_day_health"] = {k: hs.get(k) for k in ("date", "today", "assessment", "flags")}
        if include_weather:
            pkg["weather_outlook"] = self.get_weather_outlook()
        return pkg

    # ============ dispatch ============
    def call(self, name: str, **kwargs: Any) -> Any:
        if name not in {t["function"]["name"] for t in TOOL_SPECS}:      # 只允許公開給模型的工具
            return {"error": f"unknown tool {name}"}
        fn = getattr(self, name, None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**kwargs)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}


def _kb_topics() -> list[str]:
    try:
        from .knowledge import TOPICS
        return list(TOPICS)
    except Exception:  # noqa: BLE001
        return ["zone2", "lt1_lt2", "interval", "endurance", "strength", "running", "cycling", "marathon", "nutrition", "recovery_monitoring"]


_KB_TOPICS = _kb_topics()

_PLACEHOLDERS = {"", "未提供", "未知", "不明", "不詳", "無", "沒有", "none", "null", "n/a", "na", "unknown", "未指定", "不適用"}


def _clean(v: Optional[str]) -> Optional[str]:
    """模型常把沒有的欄位填成「未提供」之類的字串，一律視為沒填。"""
    if v is None:
        return None
    v = str(v).strip()
    return None if v.lower() in _PLACEHOLDERS else v


def _parse_rpe(v: Any) -> Optional[int]:
    """RPE 可能是 6、'6'、'5–6/10'、'7/10'、'約 6'：取第一個數字（範圍取平均）；解析不出來就當沒填。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(v).replace("／", "/"))]
    if not nums:
        return None
    s = str(v)
    if "/10" in s.replace(" ", "") or s.strip().endswith("/10"):
        nums = nums[:-1] if len(nums) > 1 and nums[-1] == 10 else nums
    if len(nums) >= 2 and re.search(r"\d\s*[–\-~到至]\s*\d", s):
        return int(round((nums[0] + nums[1]) / 2))
    return int(round(nums[0]))


def _valid_priority(p: Optional[str]) -> Optional[str]:
    """只接受 A/B/C；模型常填「未指定」「無」之類的字串，一律視為沒有。"""
    p = (p or "").strip().upper()
    return p if p in ("A", "B", "C") else None


def _valid_date(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        return None


def _p(**props: Any) -> dict[str, Any]:
    required = [k for k, v in props.items() if v.pop("required", False)]
    return {"type": "object", "properties": props, "required": required}


TOOL_SPECS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "get_athlete_profile", "description": "身體資料（年齡、身高、體重、VO2max、乳酸閾值）、心率區間與功率區間設定、使用者自填資料（目標等）。", "parameters": _p()}},
    {"type": "function", "function": {"name": "get_health_status", "description": "指定日期（預設今天）的 RHR、HRV、睡眠、壓力、Body Battery、訓練準備度，與近 30 天基準比較並給出 normal/attention/poor 判斷與原因。", "parameters": _p(day={"type": "string", "description": "YYYY-MM-DD"}, baseline_days={"type": "integer", "default": 30})}},
    {"type": "function", "function": {"name": "get_daily_recovery", "description": "日期範圍內每天的恢復資料（RHR、HRV、睡眠、壓力、Body Battery、準備度、負荷）。", "parameters": _p(start={"type": "string", "required": True}, end={"type": "string"})}},
    {"type": "function", "function": {"name": "get_weather_outlook", "description": "最近幾天與未來幾天的氣溫、體感、濕度、降雨機率、日出日落時間，含早上 5-7 點與傍晚 17-19 點氣溫。", "parameters": _p(past_days={"type": "integer", "default": 3}, forecast_days={"type": "integer", "default": 3})}},
    {"type": "function", "function": {"name": "get_athlete_context", "description": "目標賽事（含倒數天數）、行程與時間限制、傷病/近況筆記、常關注主題。", "parameters": _p()}},
    {"type": "function", "function": {"name": "get_recent_training", "description": "最近 N 天（預設 10）所有訓練與負荷總計、每日急慢性負荷/ACWR 趨勢、週統計，用來判斷負荷與強度。", "parameters": _p(days={"type": "integer", "default": 10})}},
    {"type": "function", "function": {"name": "get_sport_history", "description": "同一項目（running/cycling/swimming）一年內的訓練：每月彙總、最佳表現、心率效率趨勢、最近幾次，用來看變化與建立基準。", "parameters": _p(sport={"type": "string", "default": "running"}, days={"type": "integer", "default": 365}, recent_n={"type": "integer", "default": 15})}},
    {"type": "function", "function": {"name": "get_fitness_metrics", "description": "乳酸閾值心率/配速、跑步與自行車 FTP、比賽預測、VO2max、跑步耐受度、耐力與爬坡分數：最新值與一年趨勢（預設 4 個取樣點，要更細給 max_points）。心率/功率區間在 get_athlete_profile。", "parameters": _p(history_days={"type": "integer", "default": 365}, max_points={"type": "integer", "default": 4})}},
    {"type": "function", "function": {"name": "get_paper", "description": "取某篇論文的完整摘要與主要全文段落（search_knowledge 回的是截短段落）。需要細節如數字、劑量、方法時用，給 pmid。", "parameters": _p(pmid={"type": "string", "required": True}, max_chars={"type": "integer", "default": 6000})}},
    {"type": "function", "function": {"name": "search_web", "description": "網路搜尋（費用高、每天有上限）。只在 Garmin 資料和 search_knowledge 都拿不到時用：賽事資訊（日期、路線、爬升、關門時間、補給、轉換區）、場地或路線資料、規則、裝備規格、公告。不要拿來查訓練方法、生理原理、營養原則（那些查 search_knowledge），也不要查使用者自己的數據。工具會先查快取（之前查過的同一件事直接回傳、不花錢），再檢查知識庫（知識庫已有相關資料會拒絕並直接回傳知識庫結果）。query 用最可能找到來源的語言（台灣賽事用中文、國際資料用英文），一次一個明確的問題。", "parameters": _p(query={"type": "string", "required": True}, reason={"type": "string", "required": True, "description": "為什麼要上網：本地資料缺什麼"}, ignore_cache={"type": "boolean", "default": False, "description": "只有使用者明確要最新資料、而快取結果明顯過時才設 true"}, cache_id={"type": "integer", "description": "直接取某筆網路快取的完整內容（search_knowledge 回的 web_cached 有 cache_id），不花錢"})}},
    {"type": "function", "function": {"name": "get_activity_detail", "description": "單次訓練完整資料：摘要、每圈/每公里分段（配速、心率、功率、步頻、爬升）、心率/功率區間時間與邊界、HR drift、天氣、AQI、主觀回饋。分析資料包 this_session 以外的任何一次訓練都要先呼叫這個；不給 id 則取最新一筆。", "parameters": _p(activity_id={"type": "integer"})}},
    {"type": "function", "function": {"name": "get_activity_timeseries", "description": "單次訓練的逐秒資料降採樣（心率、配速、GAP、功率、步頻、海拔、溫度、體力、表現狀況）。使用者問到訓練中的某一段（前幾分鐘、轉換期、後半段、最後幾公里、某趟間歇）或要看變化/衰退時一定要呼叫，不要用摘要回答。", "parameters": _p(activity_id={"type": "integer"}, step_s={"type": "integer", "default": 60, "description": "每幾秒一筆，間歇課表建議 30"})}},
    {"type": "function", "function": {"name": "search_knowledge", "description": "搜尋近 10 年運動科學與運動醫學論文。主題：zone2、lt1_lt2、interval、endurance、strength、running、cycling、marathon、nutrition、recovery_monitoring；運動醫學：injury_running（跑步傷害）、injury_cycling（自行車傷害）、injury_prevention（傷害預防/負荷管理/回場）、sports_medicine（熱傷害、低血鈉、過度訓練/RED-S、心臟、感染）。給訓練建議、解釋原理、處理疼痛傷害時先查，把結論融進回答；不要在回答裡標注作者年份或論文出處。query 用英文關鍵字，例如 'iliotibial band syndrome runners risk factors'。", "parameters": _p(query={"type": "string", "required": True}, k={"type": "integer", "default": 4}, topic={"type": "string", "enum": _KB_TOPICS})}},
    {"type": "function", "function": {"name": "record_note", "description": "記錄對話中得知、之後評估訓練會用到的資訊。身體不適/疼痛/生病一定要記，寫明部位、程度、開始時間、什麼情況會痛。category: injury / illness / status / recovery / race_progress / preference / insight。", "parameters": _p(category={"type": "string", "required": True, "enum": ["injury", "illness", "status", "recovery", "race_progress", "preference", "insight", "other"]}, content={"type": "string", "required": True}, body_part={"type": "string"}, severity={"type": "string", "description": "輕微 / 中等 / 嚴重，或 1-10"}, expires_at={"type": "string", "description": "YYYY-MM-DD，短期狀態可設過期日；不確定就不要帶這個欄位"})}},
    {"type": "function", "function": {"name": "record_event", "description": "記錄比賽目標、家庭/工作行程、時間限制、生病或受傷期間（有日期的情境）。", "parameters": _p(kind={"type": "string", "required": True, "enum": ["race", "travel", "family", "work", "illness", "injury", "constraint", "other"]}, title={"type": "string", "required": True}, start_date={"type": "string"}, end_date={"type": "string"}, priority={"type": "string", "enum": ["A", "B", "C"], "description": "A 主要目標賽 / B 次要 / C 練習賽；不知道就不要填"}, notes={"type": "string"})}},
    {"type": "function", "function": {"name": "update_event", "description": "更新已記錄的事件：狀態（done/cancelled）、優先順序（A=主要目標賽、B=次要、C=練習賽）、日期、備註或標題。使用者說哪場是主要目標時用這個設 priority。", "parameters": _p(event_id={"type": "integer", "required": True}, status={"type": "string"}, priority={"type": "string", "enum": ["A", "B", "C"]}, notes={"type": "string"}, start_date={"type": "string"}, end_date={"type": "string"}, title={"type": "string"})}},
    {"type": "function", "function": {"name": "record_interest", "description": "使用者問到的專業詞彙或反覆關注的主題，每次問到都呼叫一次以累計次數。term 只放簡短詞彙本身（如 LT1、400m 配速、步頻、HR drift），說明放 definition。", "parameters": _p(term={"type": "string", "required": True}, definition={"type": "string"}, notes={"type": "string"})}},
    {"type": "function", "function": {"name": "log_feedback", "description": "記錄使用者對這次訓練的主觀回饋：RPE(1-10)、體感、疼痛、是否生病、備註。只填使用者明確說出的欄位，沒說的不要帶（不要用 false 代表未提供）。rpe=0 代表清除先前誤記的 RPE。", "parameters": _p(activity_id={"type": "integer"}, rpe={"type": "integer"}, feeling={"type": "string"}, pain={"type": "string"}, sick={"type": "boolean"}, notes={"type": "string"})}},
]
