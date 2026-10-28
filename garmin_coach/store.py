"""SQLite 儲存層。

* 結構化資料表由 pydantic 模型自動產生欄位（巢狀欄位存成 JSON 文字）。
* 個人情境 / 記憶 / 關注詞彙 另有固定表，見 context.py。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Type, TypeVar, get_args, get_origin

from pydantic import BaseModel

from . import config
from .models import DailyRecovery, PhysioSnapshot, SessionFeedback, TrainingSession

M = TypeVar("M", bound=BaseModel)

_SCALARS = {int: "INTEGER", float: "REAL", bool: "INTEGER", str: "TEXT", date: "TEXT", datetime: "TEXT"}


def _col_type(annotation: Any) -> tuple[str, bool]:
    """回傳 (SQLite 型別, 是否為 JSON 欄位)。"""
    origin = get_origin(annotation)
    if origin is not None and type(None) in get_args(annotation):  # Optional[X]
        inner = [a for a in get_args(annotation) if a is not type(None)]
        return _col_type(inner[0]) if len(inner) == 1 else ("TEXT", True)
    if annotation in _SCALARS:
        return _SCALARS[annotation], False
    return "TEXT", True


def _columns(model: Type[BaseModel]) -> dict[str, tuple[str, bool]]:
    return {name: _col_type(f.annotation) for name, f in model.model_fields.items()}


def _to_row(obj: BaseModel) -> dict[str, Any]:
    cols = _columns(type(obj))
    data = obj.model_dump(mode="json")
    return {k: (json.dumps(v, ensure_ascii=False) if is_json and v is not None else v) for k, (_, is_json) in cols.items() for v in [data.get(k)]}


def _from_row(model: Type[M], row: sqlite3.Row) -> M:
    cols = _columns(model)
    data: dict[str, Any] = {}
    for k, (_, is_json) in cols.items():
        if k not in row.keys():
            continue
        v = row[k]
        if is_json and isinstance(v, str):
            v = json.loads(v)
        data[k] = v
    return model.model_validate(data)


TABLES: dict[str, tuple[Type[BaseModel], str]] = {
    "daily_recovery": (DailyRecovery, "date"),
    "activities": (TrainingSession, "activity_id"),
    "session_feedback": (SessionFeedback, "activity_id"),
    "physio_snapshots": (PhysioSnapshot, "date"),
}

CONTEXT_DDL = """
CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- race / travel / family / illness / injury / constraint / other
    title TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    status TEXT DEFAULT 'active',  -- active / done / cancelled
    priority TEXT,                 -- A / B / C（比賽用）
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,        -- status / recovery / injury / illness / race_progress / preference / insight / other
    content TEXT NOT NULL,
    source TEXT,                   -- user / ai / conversation:<id>
    active INTEGER DEFAULT 1,
    created_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS interests (
    term TEXT PRIMARY KEY,         -- 常問的詞彙或主題，例如 "400m 配速"、"LT1"
    count INTEGER DEFAULT 1,
    definition TEXT,
    notes TEXT,
    first_seen TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS metric_history (
    date TEXT NOT NULL,
    metric TEXT NOT NULL,          -- vo2max_run / vo2max_cycle / lt_hr / lt_pace / run_ftp / pred_5k ... / endurance / hill / run_tolerance ...
    value REAL,
    extra TEXT,
    PRIMARY KEY (date, metric)
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER,
    started_at TEXT,
    model TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (          -- 每一次模型呼叫的用量與費用（預算控管、審計用）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    ts TEXT,
    model TEXT,
    purpose TEXT,                                 -- chat / extract
    input INTEGER, cached_input INTEGER, output INTEGER,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);
CREATE TABLE IF NOT EXISTS qa_cache (           -- 深入題的問答快取（問題向量 + 回答），供相似問題重用
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    ts TEXT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    model TEXT,
    depth TEXT,                                   -- deep（新鮮回答）/ reuse（更新過的回答）
    embedding BLOB
);
CREATE TABLE IF NOT EXISTS web_searches (       -- 網路搜尋（OpenAI web_search）完整紀錄 + 快取（websearch.py）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    conversation_id INTEGER,
    query TEXT NOT NULL,
    query_norm TEXT,
    reason TEXT,                                  -- 模型說明為什麼本地資料沒有
    status TEXT,                                  -- done / denied / failed
    deny_reason TEXT,
    kb_max_sim REAL,                              -- 查詢與論文知識庫的最高相似度（閘門依據）
    kb_judged TEXT,                               -- 閘門判斷：covered / not_covered / skip（低於 floor）
    model TEXT,
    summary TEXT,                                 -- 子模型整理的完整結果（事實 + 來源），之後重用
    searches TEXT,                                -- JSON：實際執行的搜尋（查詢字串、來源 URL、開啟的頁面）
    citations TEXT,                               -- JSON：結果引用的 URL/標題
    raw TEXT,                                     -- JSON：API 回傳的原始 output 項目（完整紀錄）
    input INTEGER, cached_input INTEGER, output INTEGER, calls INTEGER, cost_usd REAL,
    embedding BLOB,                               -- 查詢向量（找相近的舊查詢）
    hits INTEGER DEFAULT 0,                       -- 被重用幾次
    last_used TEXT
);
CREATE INDEX IF NOT EXISTS idx_web_searches_ts ON web_searches(ts);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    created_at TEXT
);
"""


class Store:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- schema ----------
    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        for table, (model, pk) in TABLES.items():
            cols = _columns(model)
            defs = ", ".join(f'"{k}" {t}' + (" PRIMARY KEY" if k == pk else "") for k, (t, _) in cols.items())
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({defs})')
            existing = {r["name"] for r in cur.execute(f'PRAGMA table_info("{table}")')}
            for k, (t, _) in cols.items():          # 模型新增欄位時自動補上
                if k not in existing:
                    cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{k}" {t}')
        cur.executescript(CONTEXT_DDL)
        if "usage" not in {r["name"] for r in cur.execute("PRAGMA table_info(conversations)")}:
            cur.execute("ALTER TABLE conversations ADD COLUMN usage TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time)")
        self.conn.commit()

    # ---------- 通用 upsert / get ----------
    def upsert(self, table: str, obj: BaseModel) -> None:
        row = _to_row(obj)
        keys = list(row)
        sql = (
            f'INSERT INTO "{table}" ({", ".join(f"{chr(34)}{k}{chr(34)}" for k in keys)}) '
            f'VALUES ({", ".join("?" for _ in keys)}) '
            f'ON CONFLICT DO UPDATE SET {", ".join(f"{chr(34)}{k}{chr(34)}=excluded.{chr(34)}{k}{chr(34)}" for k in keys)}'
        )
        self.conn.execute(sql, [row[k] for k in keys])
        self.conn.commit()

    def get(self, table: str, key: Any) -> Optional[BaseModel]:
        model, pk = TABLES[table]
        r = self.conn.execute(f'SELECT * FROM "{table}" WHERE "{pk}" = ?', (str(key) if pk == "date" else key,)).fetchone()
        return _from_row(model, r) if r else None

    # ---------- 便利方法 ----------
    def upsert_daily(self, rec: DailyRecovery) -> None:
        self.upsert("daily_recovery", rec)

    def get_daily(self, day: date) -> Optional[DailyRecovery]:
        return self.get("daily_recovery", day.isoformat())  # type: ignore[return-value]

    def daily_range(self, start: date, end: date) -> list[DailyRecovery]:
        rows = self.conn.execute(
            'SELECT * FROM daily_recovery WHERE date BETWEEN ? AND ? ORDER BY date', (start.isoformat(), end.isoformat())
        ).fetchall()
        return [_from_row(DailyRecovery, r) for r in rows]

    def has_daily(self, day: date) -> bool:
        return self.conn.execute("SELECT 1 FROM daily_recovery WHERE date = ?", (day.isoformat(),)).fetchone() is not None

    def upsert_activity(self, s: TrainingSession) -> None:
        self.upsert("activities", s)

    def get_activity(self, activity_id: int) -> Optional[TrainingSession]:
        return self.get("activities", activity_id)  # type: ignore[return-value]

    def has_activity(self, activity_id: int, with_details: bool = False) -> bool:
        r = self.conn.execute("SELECT sources FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
        if not r:
            return False
        if not with_details:
            return True
        return "activity" in (json.loads(r["sources"] or "[]"))

    def recent_activities(self, limit: int = 10, sport: Optional[str] = None) -> list[TrainingSession]:
        sql = "SELECT * FROM activities"
        params: list[Any] = []
        if sport:
            sql += " WHERE sport LIKE ?"
            params.append(f"%{sport}%")
        sql += " ORDER BY start_time DESC LIMIT ?"
        params.append(limit)
        return [_from_row(TrainingSession, r) for r in self.conn.execute(sql, params)]

    def activities_between(self, start: date, end: date) -> list[TrainingSession]:
        rows = self.conn.execute(
            "SELECT * FROM activities WHERE date(start_time) BETWEEN ? AND ? ORDER BY start_time",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [_from_row(TrainingSession, r) for r in rows]

    def latest_activity(self) -> Optional[TrainingSession]:
        acts = self.recent_activities(1)
        return acts[0] if acts else None

    def upsert_feedback(self, fb: SessionFeedback) -> None:
        fb.updated_at = datetime.now()
        existing = self.get_feedback(fb.activity_id)
        if existing:   # 只覆蓋有給值的欄位
            merged = existing.model_dump()
            merged.update({k: v for k, v in fb.model_dump().items() if v is not None})
            fb = SessionFeedback.model_validate(merged)
        self.upsert("session_feedback", fb)

    def get_feedback(self, activity_id: int) -> Optional[SessionFeedback]:
        return self.get("session_feedback", activity_id)  # type: ignore[return-value]

    def upsert_physio(self, p: PhysioSnapshot) -> None:
        self.upsert("physio_snapshots", p)

    def latest_physio(self) -> Optional[PhysioSnapshot]:
        r = self.conn.execute("SELECT * FROM physio_snapshots ORDER BY date DESC LIMIT 1").fetchone()
        return _from_row(PhysioSnapshot, r) if r else None

    # ---------- 體能指標歷史 ----------
    def upsert_metrics(self, rows: Iterable[tuple[str, str, Optional[float], Optional[dict[str, Any]]]]) -> int:
        n = 0
        for d, metric, value, extra in rows:
            self.conn.execute(
                "INSERT INTO metric_history(date, metric, value, extra) VALUES(?,?,?,?) "
                "ON CONFLICT(date, metric) DO UPDATE SET value=excluded.value, extra=excluded.extra",
                (d, metric, value, json.dumps(extra, ensure_ascii=False) if extra else None),
            )
            n += 1
        self.conn.commit()
        return n

    def metric_series(self, metric: str, start: Optional[date] = None, end: Optional[date] = None) -> list[dict[str, Any]]:
        sql, params = "SELECT date, value, extra FROM metric_history WHERE metric=?", [metric]
        if start:
            sql += " AND date>=?"; params.append(start.isoformat())
        if end:
            sql += " AND date<=?"; params.append(end.isoformat())
        sql += " ORDER BY date"
        out = []
        for r in self.conn.execute(sql, params):
            row = {"date": r["date"], "value": r["value"]}
            if r["extra"]:
                row.update(json.loads(r["extra"]))
            out.append(row)
        return out

    def metric_names(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT metric FROM metric_history ORDER BY metric")]

    # ---------- 統計 ----------
    def weekly_load(self, end: date, weeks: int = 4) -> list[dict[str, Any]]:
        """每週距離 / 時間 / 訓練負荷（往前 N 週）。"""
        rows = self.conn.execute(
            """
            SELECT strftime('%Y-W%W', start_time) AS week,
                   COUNT(*) AS sessions,
                   ROUND(SUM(distance_m)/1000.0, 1) AS km,
                   ROUND(SUM(duration_s)/3600.0, 1) AS hours,
                   ROUND(SUM(training_load), 0) AS load
            FROM activities
            WHERE date(start_time) BETWEEN date(?, ?) AND ?
            GROUP BY week ORDER BY week
            """,
            (end.isoformat(), f"-{weeks * 7} days", end.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {}
        for t in list(TABLES) + ["metric_history", "events", "notes", "interests", "conversations"]:
            out[t] = self.conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        return out
