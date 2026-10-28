"""外部資料：Open-Meteo 空氣品質（免金鑰）。失敗時回傳 None，不影響同步。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger(__name__)
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_aqi(lat: float, lon: float, when: datetime, timeout: float = 10) -> Optional[dict[str, float]]:
    """回傳活動開始時刻最近一小時的 {"aqi_us": .., "pm25": ..}。"""
    day = when.date().isoformat()
    try:
        r = requests.get(
            AQ_URL,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "us_aqi,pm2_5",
                "start_date": day, "end_date": day,
                "timezone": "auto",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        h = r.json().get("hourly") or {}
        times, aqi, pm = h.get("time") or [], h.get("us_aqi") or [], h.get("pm2_5") or []
        if not times:
            return None
        target = when.strftime("%Y-%m-%dT%H:00")
        idx = times.index(target) if target in times else min(range(len(times)), key=lambda i: abs(
            (datetime.fromisoformat(times[i]) - when).total_seconds()))
        out: dict[str, float] = {}
        if idx < len(aqi) and aqi[idx] is not None:
            out["aqi_us"] = aqi[idx]
        if idx < len(pm) and pm[idx] is not None:
            out["pm25"] = pm[idx]
        return out or None
    except Exception as e:  # noqa: BLE001
        log.warning("AQI 取得失敗: %s", e)
        return None


def fetch_weather_outlook(lat: float, lon: float, past_days: int = 3, forecast_days: int = 3, timeout: float = 10) -> Optional[dict]:
    """最近幾天 + 未來幾天的氣溫 / 濕度 / 降雨機率 / 日出日落，並附早上 5-7 點與傍晚 17-19 點的氣溫。"""
    try:
        r = requests.get(
            FORECAST_URL,
            params={
                "latitude": lat, "longitude": lon, "timezone": "auto",
                "past_days": past_days, "forecast_days": forecast_days,
                "daily": "temperature_2m_max,temperature_2m_min,apparent_temperature_max,relative_humidity_2m_mean,"
                         "precipitation_probability_max,precipitation_sum,sunrise,sunset,wind_speed_10m_max",
                "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        j = r.json()
        d, h = j.get("daily") or {}, j.get("hourly") or {}
        hourly_by_day: dict[str, dict[str, list]] = {}
        for i, t in enumerate(h.get("time") or []):
            day, hour = t[:10], int(t[11:13])
            slot = "morning_5_7" if 5 <= hour <= 7 else "evening_17_19" if 17 <= hour <= 19 else None
            if slot:
                hourly_by_day.setdefault(day, {}).setdefault(slot, []).append(
                    (h["temperature_2m"][i], h["apparent_temperature"][i], h["relative_humidity_2m"][i]))
        days = []
        for i, day in enumerate(d.get("time") or []):
            row = {
                "date": day,
                "temp_min": d["temperature_2m_min"][i], "temp_max": d["temperature_2m_max"][i],
                "apparent_max": d["apparent_temperature_max"][i], "humidity_mean": d["relative_humidity_2m_mean"][i],
                "rain_prob_max": d["precipitation_probability_max"][i], "rain_mm": d["precipitation_sum"][i],
                "wind_max_kph": d["wind_speed_10m_max"][i],
                "sunrise": (d["sunrise"][i] or "")[11:16], "sunset": (d["sunset"][i] or "")[11:16],
            }
            for slot, vals in hourly_by_day.get(day, {}).items():
                vals = [v for v in vals if v[0] is not None]
                if vals:
                    row[slot] = {"temp": round(sum(v[0] for v in vals) / len(vals), 1),
                                 "apparent": round(sum(v[1] for v in vals) / len(vals), 1),
                                 "humidity": round(sum(v[2] for v in vals) / len(vals))}
            days.append(row)
        return {"timezone": j.get("timezone"), "lat": lat, "lon": lon, "days": days}
    except Exception as e:  # noqa: BLE001
        log.warning("天氣預報取得失敗: %s", e)
        return None
