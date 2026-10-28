"""訓練後整理包（brief）：把一次訓練 + 近期恢復 + 體能 + 個人情境組成一份可直接丟給 AI 的資料。

輸出兩種格式：
* dict   – 給程式 / function calling 用
* markdown – 直接貼到 ChatGPT
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from .context import Context
from .models import DailyRecovery, TrainingSession, fmt_duration, fmt_pace
from .store import Store

_DROP = {"sources", "synced_at", "lat", "lon", "start_time_utc"}


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and k not in _DROP and v != {} and v != []}


def recovery_row(r: DailyRecovery) -> dict[str, Any]:
    return _clean({
        "date": r.date.isoformat(), "rhr": r.resting_hr, "hrv": r.hrv_last_night, "hrv_status": r.hrv_status,
        "sleep_score": r.sleep_score, "sleep_h": round(r.sleep_total_min / 60, 1) if r.sleep_total_min else None,
        "stress": r.stress_avg, "bb_high": r.bb_high, "bb_low": r.bb_low,
        "readiness": r.readiness_score, "recovery_h": r.recovery_time_hours, "acwr": r.acwr, "heat_accl": r.heat_acclimation_pct,
    })


def build_brief(store: Store, activity_id: Optional[int] = None, trend_days: int = 7, weeks: int = 4) -> dict[str, Any]:
    sess = store.get_activity(activity_id) if activity_id else store.latest_activity()
    if sess is None:
        raise ValueError("資料庫裡沒有訓練活動，請先執行 sync")
    day = sess.start_time.date() if sess.start_time else date.today()
    ctx = Context(store)

    today_rec = store.get_daily(day)
    trend = store.daily_range(day - timedelta(days=trend_days - 1), day)
    physio = store.latest_physio()
    fb = store.get_feedback(sess.activity_id)
    recent = [s for s in store.activities_between(day - timedelta(days=13), day) if s.activity_id != sess.activity_id]

    session = _clean(sess.model_dump(mode="json"))
    session["avg_pace"] = fmt_pace(sess.avg_pace_s_per_km)
    session["avg_gap_pace"] = fmt_pace(sess.avg_gap_pace_s_per_km) if sess.avg_gap_pace_s_per_km else None
    session["duration"] = fmt_duration(sess.duration_s)
    session["distance_km"] = round(sess.distance_m / 1000, 2) if sess.distance_m else None
    session["hr_zones"] = ", ".join(f"{k} {fmt_duration(v)}" for k, v in sess.hr_zone_s.items()) or None
    session["power_zones"] = ", ".join(f"{k} {fmt_duration(v)}" for k, v in sess.power_zone_s.items()) or None
    session.pop("hr_zone_s", None); session.pop("power_zone_s", None)
    session["splits"] = [
        _clean({**sp.model_dump(), "pace": fmt_pace(sp.avg_pace_s_per_km)}) for sp in sess.splits
    ]

    return {
        "session": session,
        "athlete_feedback": _clean(fb.model_dump(mode="json")) if fb else {},
        "recovery": {
            "on_day": recovery_row(today_rec) if today_rec else {},
            "trend": [recovery_row(r) for r in trend],
        },
        "fitness": _clean({
            **(physio.model_dump(mode="json") if physio else {}),
            "lt_pace": fmt_pace(physio.lt_pace_s_per_km) if physio else None,
            "vo2max_run": today_rec.vo2max_run if today_rec else None,
            "heat_acclimation": f"{today_rec.heat_acclimation_pct}% ({today_rec.heat_trend})" if today_rec and today_rec.heat_acclimation_pct is not None else None,
            "training_status": today_rec.training_status if today_rec else None,
            "acute_load": today_rec.acute_load if today_rec else None,
            "chronic_load": today_rec.chronic_load if today_rec else None,
            "load_balance": (
                f"低有氧 {today_rec.load_aerobic_low} / 高有氧 {today_rec.load_aerobic_high} / 無氧 {today_rec.load_anaerobic}"
                f"（{today_rec.load_balance_feedback}）"
            ) if today_rec and today_rec.load_aerobic_low is not None else None,
        }),
        "recent_sessions": [
            _clean({
                "activity_id": s.activity_id, "date": s.start_time.date().isoformat() if s.start_time else None,
                "sport": s.sport, "name": s.name, "km": round(s.distance_m / 1000, 1) if s.distance_m else None,
                "duration": fmt_duration(s.duration_s), "pace": fmt_pace(s.avg_pace_s_per_km), "avg_hr": s.avg_hr,
                "load": s.training_load, "aerobic_te": s.aerobic_te,
            }) for s in recent
        ],
        "weekly_load": store.weekly_load(day, weeks),
        "athlete_context": ctx.snapshot(day),
    }


# ---------- Markdown ----------

def _kv_lines(d: dict[str, Any], keys: list[tuple[str, str]]) -> list[str]:
    out = []
    for k, label in keys:
        v = d.get(k)
        if v is not None:
            out.append(f"- {label}: {v}")
    return out


def to_markdown(b: dict[str, Any]) -> str:
    s = b["session"]
    lines = [f"# 訓練分析資料：{s.get('name', '')}（{s.get('start_time', '')[:16]}）", ""]
    lines.append("## 本次訓練")
    lines += _kv_lines(s, [
        ("sport", "運動類型"), ("distance_km", "距離 (km)"), ("duration", "時間"), ("avg_pace", "平均配速"),
        ("avg_gap_pace", "GAP 配速"), ("avg_hr", "平均心率"), ("max_hr", "最大心率"),
        ("hr_zones", "心率區間時間"), ("hr_drift_pct", "HR drift (%)"), ("pa_hr_decoupling_pct", "Pa:Hr 脫鉤 (%)"),
        ("pw_hr_decoupling_pct", "Pw:Hr 脫鉤 (%)"),
        ("power_zones", "功率區間時間"), ("avg_power", "平均功率 (W)"), ("normalized_power", "NP (W)"), ("tss", "TSS"), ("intensity_factor", "IF"),
        ("avg_cadence", "步頻"), ("avg_stride_len_m", "步幅 (m)"), ("avg_gct_ms", "觸地時間 (ms)"), ("avg_vertical_osc_cm", "垂直振幅 (cm)"),
        ("aerobic_te", "有氧 TE"), ("anaerobic_te", "無氧 TE"), ("te_label", "訓練效果"), ("training_load", "訓練負荷"),
        ("vo2max", "VO2max（本次）"), ("garmin_feeling", "手錶自評感受"), ("garmin_perceived_effort", "手錶自評費力度"),
    ])
    lines.append("")
    lines.append("### 外部環境")
    lines += _kv_lines(s, [
        ("temp_c", "氣溫 (°C)"), ("apparent_temp_c", "體感溫度 (°C)"), ("humidity_pct", "濕度 (%)"), ("dew_point_c", "露點 (°C)"),
        ("wind_kph", "風速 (km/h)"), ("wind_dir", "風向"), ("weather_desc", "天氣"), ("device_temp_max_c", "手錶溫度 (°C)"),
        ("aqi_us", "AQI (US)"), ("pm25", "PM2.5"), ("elevation_gain_m", "爬升 (m)"), ("elevation_loss_m", "下降 (m)"),
        ("location_name", "地點"),
    ])
    if s.get("splits"):
        lines += ["", "### 分段", "| # | 類型 | 距離 m | 時間 | 配速 | 平均HR | 功率 | 爬升 |", "|---|---|---|---|---|---|---|---|"]
        for sp in s["splits"]:
            lines.append(f"| {sp.get('index')} | {sp.get('intensity', '')} | {sp.get('distance_m', '')} | "
                         f"{fmt_duration(sp.get('duration_s'))} | {sp.get('pace')} | {sp.get('avg_hr', '')} | "
                         f"{sp.get('avg_power', '')} | {sp.get('elevation_gain_m', '')} |")
    fb = b.get("athlete_feedback") or {}
    if fb:
        lines += ["", "## 我的主觀回饋"]
        lines += _kv_lines(fb, [("rpe", "RPE (1-10)"), ("feeling", "體感"), ("pain", "疼痛"), ("sick", "生病"), ("notes", "備註")])

    rec = b["recovery"]
    lines += ["", "## 生理恢復"]
    if rec.get("on_day"):
        lines.append("當天： " + ", ".join(f"{k}={v}" for k, v in rec["on_day"].items() if k != "date"))
    if rec.get("trend"):
        lines += ["", "| 日期 | RHR | HRV | HRV狀態 | 睡眠分 | 睡眠h | 壓力 | BB高/低 | 準備度 | ACWR |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in rec["trend"]:
            lines.append(f"| {r.get('date')} | {r.get('rhr', '')} | {r.get('hrv', '')} | {r.get('hrv_status', '')} | "
                         f"{r.get('sleep_score', '')} | {r.get('sleep_h', '')} | {r.get('stress', '')} | "
                         f"{r.get('bb_high', '')}/{r.get('bb_low', '')} | {r.get('readiness', '')} | {r.get('acwr', '')} |")

    fit = b.get("fitness") or {}
    if fit:
        lines += ["", "## 體能指標"]
        lines += _kv_lines(fit, [
            ("vo2max_run", "VO2max"), ("lt_hr", "乳酸閾值心率"), ("lt_pace", "乳酸閾值配速"), ("run_ftp_w", "跑步 FTP (W)"),
            ("cycling_ftp_w", "自行車 FTP (W)"), ("training_status", "訓練狀態"), ("heat_acclimation", "熱適應 %"), ("acute_load", "急性負荷"),
            ("chronic_load", "慢性負荷"), ("load_balance", "負荷平衡"), ("endurance_score", "耐力分數"), ("hill_score", "爬坡分數"),
        ])
        preds = [(k, fit.get(k)) for k in ("pred_5k_s", "pred_10k_s", "pred_half_s", "pred_marathon_s") if fit.get(k)]
        if preds:
            lines.append("- 比賽預測: " + ", ".join(f"{k[5:-2]}={fmt_duration(v)}" for k, v in preds))

    if b.get("weekly_load"):
        lines += ["", "## 近幾週負荷", "| 週 | 次數 | km | 小時 | 負荷 |", "|---|---|---|---|---|"]
        for w in b["weekly_load"]:
            lines.append(f"| {w['week']} | {w['sessions']} | {w['km']} | {w['hours']} | {w['load']} |")
    if b.get("recent_sessions"):
        lines += ["", "## 近 14 天其他訓練"]
        for r in b["recent_sessions"]:
            lines.append(f"- {r.get('date')} {r.get('sport')} {r.get('name', '')}: {r.get('km', '')}km {r.get('duration')} "
                         f"{r.get('pace')} HR{r.get('avg_hr', '')} 負荷{r.get('load', '')}")

    ctx = b.get("athlete_context") or {}
    lines += ["", "## 個人情境"]
    if ctx.get("profile"):
        lines.append("- 基本資料: " + ", ".join(f"{k}={v}" for k, v in ctx["profile"].items()))
    for e in ctx.get("upcoming_races", []):
        lines.append(f"- 目標賽事: {e['title']} {e.get('start_date')}（剩 {e.get('days_to_go')} 天，優先級 {e.get('priority') or '-'}）{e.get('notes') or ''}")
    for e in ctx.get("active_events", []):
        lines.append(f"- [{e['kind']}] {e['title']} {e.get('start_date') or ''}~{e.get('end_date') or ''} {e.get('notes') or ''}")
    for n in ctx.get("notes", []):
        lines.append(f"- [{n['category']} {n['created_at']}] {n['content']}")
    if ctx.get("interests"):
        lines.append("- 我常關注的主題: " + ", ".join(f"{i['term']}(x{i['count']})" for i in ctx["interests"]))
    return "\n".join(lines) + "\n"
