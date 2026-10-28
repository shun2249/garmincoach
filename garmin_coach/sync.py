"""同步流程：Garmin → 原始 JSON 檔 → 結構化 SQLite。"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Iterator, Optional

from . import config
from .client import GarminClient, dump_raw
from .external import fetch_aqi
from .context import Context
from .normalize import normalize_activity, normalize_daily, normalize_history, normalize_physio, normalize_profile
from .store import Store

log = logging.getLogger(__name__)


def daterange(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def sync_daily(client: GarminClient, store: Store, start: date, end: date, force: bool = False) -> int:
    """抓每日生理恢復。今天與昨天永遠重抓（資料會持續更新），其他天已存在就略過。"""
    config.ensure_dirs()
    n = 0
    today = date.today()
    for day in daterange(start, end):
        if not force and store.has_daily(day) and (today - day).days > 1:
            continue
        raw = client.fetch_daily(day)
        raw_path = config.RAW_DIR / "daily" / f"{day.isoformat()}.json"
        # 某個端點暫時逾時/失敗時，不要用空值蓋掉上次抓到的資料
        if raw_path.exists():
            try:
                old = json.loads(raw_path.read_text())
            except (OSError, ValueError):
                old = {}
            for k, v in (old or {}).items():
                if raw.get(k) in (None, {}, []) and v not in (None, {}, []):
                    raw[k] = v
                    log.warning("daily %s: %s 這次沒抓到，沿用上次的資料", day, k)
        dump_raw(raw_path, raw)
        rec = normalize_daily(day, raw)
        store.upsert_daily(rec)
        n += 1
        log.info("daily %s  RHR=%s HRV=%s sleep=%s BB=%s readiness=%s  [%s]",
                 day, rec.resting_hr, rec.hrv_last_night, rec.sleep_score, rec.bb_high, rec.readiness_score,
                 ",".join(rec.sources))
    return n


def sync_activities(client: GarminClient, store: Store, start: Optional[date] = None, end: Optional[date] = None,
                    limit: Optional[int] = None, with_details: bool = True, external: Optional[bool] = None,
                    force: bool = False, list_only: bool = False) -> int:
    """抓訓練活動。給日期範圍就用範圍，否則抓最近 limit 筆。

    list_only=True 只存清單欄位（0 次額外呼叫，適合回補一年歷史）；之後對重要活動再補細節。
    """
    config.ensure_dirs()
    external = config.EXTERNAL_AQI if external is None else external
    if start:
        items = client.fetch_activity_list(start, end or date.today())
    else:
        items = client.fetch_recent_activities(limit or 20)
    n = 0
    for item in items:
        aid = int(item["activityId"])
        if not force and store.has_activity(aid, with_details=(with_details and not list_only)):
            continue
        if list_only:
            raw_path = config.RAW_DIR / "activities" / f"{aid}.json"
            if not raw_path.exists():
                dump_raw(raw_path, {"list_item": item})
            store.upsert_activity(normalize_activity(item, None))
            n += 1
            continue
        bundle = client.fetch_activity_bundle(aid, with_details=with_details)
        dump_raw(config.RAW_DIR / "activities" / f"{aid}.json", {"list_item": item, **bundle})
        sess = normalize_activity(item, bundle)
        if external and sess.lat is not None and sess.lon is not None and sess.start_time is not None:
            aq = fetch_aqi(sess.lat, sess.lon, sess.start_time)
            if aq:
                sess.aqi_us = int(aq["aqi_us"]) if "aqi_us" in aq else None
                sess.pm25 = aq.get("pm25")
                sess.sources.append("open_meteo_aqi")
        store.upsert_activity(sess)
        n += 1
        log.info("activity %s %s %s %.1fkm HR=%s drift=%s%%  [%s]",
                 aid, sess.start_time, sess.sport, (sess.distance_m or 0) / 1000, sess.avg_hr,
                 sess.pa_hr_decoupling_pct, ",".join(sess.sources))
    return n


def fill_details(client: GarminClient, store: Store, min_km: float = 0, days: Optional[int] = None,
                 limit: Optional[int] = None, external: Optional[bool] = None) -> int:
    """替只有清單摘要（sources=["list"]）的活動補抓分段/區間/天氣/逐秒細節。
    min_km：只補至少這麼長的；days：只補最近 N 天的；兩者都給就是交集。由新到舊。"""
    config.ensure_dirs()
    external = config.EXTERNAL_AQI if external is None else external
    sql = "SELECT activity_id, start_time FROM activities WHERE sources = '[\"list\"]'"
    params: list[Any] = []
    if min_km:
        sql += " AND distance_m >= ?"; params.append(min_km * 1000)
    if days:
        sql += " AND start_time >= ?"; params.append((date.today() - timedelta(days=days)).isoformat())
    sql += " ORDER BY start_time DESC"
    rows = store.conn.execute(sql, params).fetchall()
    if limit:
        rows = rows[:limit]
    log.info("fill_details: %d 筆要補", len(rows))
    n = 0
    for r in rows:
        aid = int(r[0])
        raw_path = config.RAW_DIR / "activities" / f"{aid}.json"
        try:
            item = json.loads(raw_path.read_text()).get("list_item") if raw_path.exists() else None
        except (OSError, ValueError):
            item = None
        if not item:
            log.warning("activity %s 沒有清單原始資料，略過", aid)
            continue
        try:
            bundle = client.fetch_activity_bundle(aid, with_details=True)
        except Exception as e:  # noqa: BLE001
            log.warning("activity %s 細節抓取失敗：%s", aid, e)
            continue
        dump_raw(raw_path, {"list_item": item, **bundle})
        sess = normalize_activity(item, bundle)
        if external and sess.lat is not None and sess.lon is not None and sess.start_time is not None:
            aq = fetch_aqi(sess.lat, sess.lon, sess.start_time)
            if aq:
                sess.aqi_us = int(aq["aqi_us"]) if "aqi_us" in aq else None
                sess.pm25 = aq.get("pm25")
                sess.sources.append("open_meteo_aqi")
        store.upsert_activity(sess)
        n += 1
        log.info("details %s %s %s %.1fkm HR=%s drift=%s%%  [%s]", aid, sess.start_time, sess.sport,
                 (sess.distance_m or 0) / 1000, sess.avg_hr, sess.pa_hr_decoupling_pct, ",".join(sess.sources))
    return n


def sync_physio(client: GarminClient, store: Store, day: Optional[date] = None) -> None:
    config.ensure_dirs()
    day = day or date.today()
    raw = client.fetch_physio(day)
    dump_raw(config.RAW_DIR / "physio" / f"{day.isoformat()}.json", raw)
    snap = normalize_physio(day, raw)
    store.upsert_physio(snap)
    log.info("physio %s LT-HR=%s LT-pace=%s FTP=%s  [%s]", day, snap.lt_hr, snap.lt_pace_s_per_km, snap.run_ftp_w,
             ",".join(snap.sources))


def sync_profile(client: GarminClient, store: Store) -> dict[str, Any]:
    """身體資料、心率/功率區間、體重 → profile 表。"""
    config.ensure_dirs()
    raw = client.fetch_profile()
    dump_raw(config.RAW_DIR / "physio" / "profile.json", raw)
    prof = normalize_profile(raw)
    ctx = Context(store)
    for k, v in prof.items():
        ctx.set_profile(k, v)
    log.info("profile 已更新: %s", ", ".join(prof))
    return prof


def sync_history(client: GarminClient, store: Store, days: int = 365) -> int:
    """體能指標一年歷史（VO2max、LT、比賽預測、耐力、爬坡、跑步耐受度）。"""
    config.ensure_dirs()
    end = date.today()
    start = end - timedelta(days=days)
    raw = client.fetch_history(start, end)
    dump_raw(config.RAW_DIR / "physio" / f"history_{end.isoformat()}.json", raw)
    n = store.upsert_metrics(normalize_history(raw))
    log.info("history: %d 筆指標", n)
    return n


def sync_all(client: GarminClient, store: Store, days: int = 14, force: bool = False, with_details: bool = True) -> dict[str, int]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    nd = sync_daily(client, store, start, end, force=force)
    na = sync_activities(client, store, start, end, with_details=with_details, force=force)
    sync_physio(client, store, end)
    sync_profile(client, store)
    return {"daily": nd, "activities": na}


def reprocess(store: Store) -> dict[str, int]:
    """不打 Garmin API，直接用 data/raw/ 裡的原始 JSON 重新整理（修改欄位對應後使用）。"""
    counts = {"daily": 0, "activities": 0, "physio": 0}
    for f in sorted((config.RAW_DIR / "daily").glob("*.json")):
        store.upsert_daily(normalize_daily(date.fromisoformat(f.stem), json.loads(f.read_text(encoding="utf-8"))))
        counts["daily"] += 1
    for f in sorted((config.RAW_DIR / "activities").glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        item = raw.pop("list_item", None)
        if not item:
            continue
        sess = normalize_activity(item, raw)
        old = store.get_activity(sess.activity_id)
        if old:                                   # AQI 不在原始檔裡，保留舊值
            sess.aqi_us, sess.pm25 = old.aqi_us, old.pm25
            if old.aqi_us is not None and "open_meteo_aqi" not in sess.sources:
                sess.sources.append("open_meteo_aqi")
        store.upsert_activity(sess)
        counts["activities"] += 1
    for f in sorted((config.RAW_DIR / "physio").glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        if f.stem == "profile":
            for k, v in normalize_profile(raw).items():
                Context(store).set_profile(k, v)
        elif f.stem.startswith("history_"):
            store.upsert_metrics(normalize_history(raw))
        else:
            store.upsert_physio(normalize_physio(date.fromisoformat(f.stem), raw))
            counts["physio"] += 1
    log.info("reprocess 完成 %s", counts)
    return counts
