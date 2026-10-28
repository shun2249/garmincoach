"""集中管理路徑與環境變數。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.getenv("GARMIN_COACH_DATA", PROJECT_ROOT / "data")).expanduser()
RAW_DIR = DATA_DIR / "raw"
BRIEF_DIR = PROJECT_ROOT / "briefs"
DB_PATH = DATA_DIR / "garmin_coach.db"

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")
GARMIN_IS_CN = os.getenv("GARMIN_IS_CN", "0") == "1"
TOKEN_DIR = Path(os.getenv("GARMINTOKENS", "~/.garminconnect")).expanduser()

WEATHER_UNITS = os.getenv("GARMIN_WEATHER_UNITS", "imperial").lower()
EXTERNAL_AQI = os.getenv("EXTERNAL_AQI", "1") == "1"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
OPENAI_REASONING = os.getenv("OPENAI_REASONING") or None
OPENAI_EXTRACT_MODEL = os.getenv("OPENAI_EXTRACT_MODEL") or None   # 結束萃取用的便宜模型，空白沿用主模型   # none / low / medium / high；空白用模型預設

# 分流：簡單問題用 OPENAI_MODEL，深入問題（課表/策略/傷痛/多因素）用 OPENAI_DEEP_MODEL；空白 = 不分流
OPENAI_DEEP_MODEL = os.getenv("OPENAI_DEEP_MODEL", "gpt-6.1-sol") or None
OPENAI_ROUTER = (os.getenv("OPENAI_ROUTER", "auto") or "auto").lower()   # auto / simple / deep

# 問答重用：深入題的回答會存起來；之後向量相似度 ≥ REUSE_SIMILARITY 的舊問題（初篩）再由便宜模型確認是同一個請求，
# 就不送深入模型，改由便宜模型拿舊回答對照最新資料更新。中文短句相似度偏低，0.5 是初篩門檻。0 = 關閉
REUSE_SIMILARITY = float(os.getenv("REUSE_SIMILARITY", "0"))   # 2026-10-20 決定先不用，預設關閉
REUSE_DAYS = int(os.getenv("REUSE_DAYS", "30"))

# 每日 OpenAI 費用上限（美元）：超過就拒絕再呼叫模型，防止失控。0 = 不限制
OPENAI_DAILY_BUDGET_USD = float(os.getenv("OPENAI_DAILY_BUDGET_USD", "2"))
# PubMed E-utilities 禮貌參數用的聯絡 email（可空白；不要用 Garmin 帳號）
PUBMED_EMAIL = os.getenv("PUBMED_EMAIL", "")
# 預建知識庫（kb download）的下載位置：GitHub Release 資產 knowledge.db.xz（旁邊有 .sha256）；也可以填本機檔案路徑
KB_PACK_URL = os.getenv("KB_PACK_URL", "https://github.com/shun2249/garmincoach/releases/download/kb-2026-10-28/knowledge.db.xz")
# 預設發布包的 sha256（釘在程式裡，網址旁邊沒有 .sha256 檔也能驗）；自己換網址時可改或留空（留空就只看網址旁的 .sha256）
KB_PACK_SHA256 = os.getenv("KB_PACK_SHA256", "0eb3b01dec0a6428ac9486f504f1775af035d4099df4c2b4e95dd85c37b94491").strip().lower()

# 網路搜尋（OpenAI Responses API 內建 web_search）：費用高，只在本地資料（Garmin + 論文知識庫）拿不到時放行，結果完整記錄並快取
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "1") == "1"
WEB_SEARCH_MODEL = os.getenv("WEB_SEARCH_MODEL") or OPENAI_MODEL          # 執行搜尋並整理結果的子模型（便宜的就好）
WEB_SEARCH_CONTEXT = (os.getenv("WEB_SEARCH_CONTEXT", "medium") or "medium").lower()   # low / medium / high：搜尋內容佔多少 context
WEB_SEARCH_MAX_CALLS = int(os.getenv("WEB_SEARCH_MAX_CALLS", "3"))        # 一次查詢內最多幾次 web_search 呼叫
WEB_SEARCH_DAILY_MAX = int(os.getenv("WEB_SEARCH_DAILY_MAX", "5"))        # 每天最多放行幾次（快取命中不算）
WEB_SEARCH_PER_CONVERSATION = int(os.getenv("WEB_SEARCH_PER_CONVERSATION", "3"))   # 同一段對話最多放行幾次（防止模型拆成一堆小查詢）
WEB_SEARCH_CALL_USD = float(os.getenv("WEB_SEARCH_CALL_USD", "0.01"))     # 每次 web_search 呼叫的工具費（USD），token 另計
WEB_CACHE_DAYS = int(os.getenv("WEB_CACHE_DAYS", "90"))                   # 快取有效天數
WEB_CACHE_DAYS_NOTFOUND = int(os.getenv("WEB_CACHE_DAYS_NOTFOUND", "14"))  # 結果是「未找到」的快取只保留這麼久（之後可能已公布）
WEB_CACHE_SIMILARITY = float(os.getenv("WEB_CACHE_SIMILARITY", "0.85"))   # 查詢向量相似度 ≥ 此值直接命中快取
WEB_CACHE_MAYBE = float(os.getenv("WEB_CACHE_MAYBE", "0.70"))             # 介於 MAYBE~SIMILARITY：由便宜模型確認是不是同一件事
WEB_SEARCH_COUNTRY = os.getenv("WEB_SEARCH_COUNTRY", "TW")                  # 搜尋結果偏向哪個國家（ISO 3166-1 兩碼）
WEB_SEARCH_TIMEZONE = os.getenv("WEB_SEARCH_TIMEZONE", "Asia/Taipei")       # 搜尋時的時區（IANA）
WEB_KB_FLOOR = float(os.getenv("WEB_KB_FLOOR", "0.45"))                   # 查詢與知識庫最高相似度低於此 → 知識庫顯然沒有，直接放行；以上 → 便宜模型判斷知識庫能否回答

# Telegram 介面（python -m garmin_coach telegram）
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or None
_tg_users = [x.strip().lstrip("@") for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").replace(";", ",").split(",") if x.strip()]
TELEGRAM_ALLOWED_USERS = {int(x) for x in _tg_users if x.isdigit()}                 # 數字 user id（最可靠）
TELEGRAM_ALLOWED_USERNAMES = {x.lower() for x in _tg_users if not x.isdigit()}      # 或 @username（可改名，較不嚴格）
TELEGRAM_IDLE_MINUTES = int(os.getenv("TELEGRAM_IDLE_MINUTES", "30"))

# Garmin 對短時間大量請求很敏感，每次呼叫間隔秒數
API_PAUSE_SECONDS = float(os.getenv("GARMIN_API_PAUSE", "0.4"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR / "daily", RAW_DIR / "activities", RAW_DIR / "physio", BRIEF_DIR):
        d.mkdir(parents=True, exist_ok=True)
