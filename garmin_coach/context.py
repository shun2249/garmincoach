"""個人情境層：基本資料、事件（比賽/行程/受傷/生病）、記憶筆記、常關注詞彙。

這一層是 AI 教練對話後要「寫回」的地方：
* notes      – 對話中學到、對基礎資料有幫助的內容（比賽進度、近況、休息程度、是否受傷）
* interests  – 常問的專業詞彙 / 主題（400m 配速、LT1 ...），累計次數
* events     – 有日期的情境（比賽目標、家庭行程、時間限制、生病、疼痛）
* profile    – key/value 基本資料（年齡、體重、最大心率、目標、週可訓練時數 ...）
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional

from .store import Store

EVENT_KINDS = ("race", "travel", "family", "work", "illness", "injury", "constraint", "other")
NOTE_CATEGORIES = ("status", "recovery", "injury", "illness", "race_progress", "preference", "insight", "other")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Context:
    def __init__(self, store: Store) -> None:
        self.db = store.conn

    # ---------- profile ----------
    def set_profile(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO profile(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), _now()),
        )
        self.db.commit()

    def get_profile(self) -> dict[str, Any]:
        rows = self.db.execute("SELECT key, value FROM profile ORDER BY key").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def delete_profile(self, key: str) -> None:
        self.db.execute("DELETE FROM profile WHERE key=?", (key,))
        self.db.commit()

    # ---------- events ----------
    def add_event(self, kind: str, title: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                  priority: Optional[str] = None, notes: Optional[str] = None) -> int:
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind 必須是 {EVENT_KINDS}")
        cur = self.db.execute(
            "INSERT INTO events(kind,title,start_date,end_date,status,priority,notes,created_at,updated_at) "
            "VALUES(?,?,?,?,'active',?,?,?,?)",
            (kind, title, start_date, end_date or start_date, priority, notes, _now(), _now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def update_event(self, event_id: int, **fields: Any) -> None:
        allowed = {"kind", "title", "start_date", "end_date", "status", "priority", "notes"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return
        sets["updated_at"] = _now()
        sql = "UPDATE events SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
        self.db.execute(sql, [*sets.values(), event_id])
        self.db.commit()

    def list_events(self, active_only: bool = True, kind: Optional[str] = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM events", []
        conds = []
        if active_only:
            conds.append("status='active'")
        if kind:
            conds.append("kind=?")
            params.append(kind)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY COALESCE(start_date, '9999') , id"
        return [dict(r) for r in self.db.execute(sql, params)]

    def upcoming_races(self, today: Optional[date] = None) -> list[dict[str, Any]]:
        today = today or date.today()
        out = []
        for e in self.list_events(kind="race"):
            if e["start_date"] and e["start_date"] >= today.isoformat():
                e["days_to_go"] = (date.fromisoformat(e["start_date"]) - today).days
                out.append(e)
        return out

    # ---------- notes（記憶）----------
    def add_note(self, category: str, content: str, source: str = "user", expires_at: Optional[str] = None) -> int:
        if category not in NOTE_CATEGORIES:
            category = "other"
        cur = self.db.execute(
            "INSERT INTO notes(category,content,source,active,created_at,expires_at) VALUES(?,?,?,1,?,?)",
            (category, content, source, _now(), expires_at),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def deactivate_note(self, note_id: int) -> None:
        self.db.execute("UPDATE notes SET active=0 WHERE id=?", (note_id,))
        self.db.commit()

    def list_notes(self, active_only: bool = True, category: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM notes", []
        conds = []
        if active_only:
            conds.append("active=1 AND (expires_at IS NULL OR expires_at >= date('now'))")
        if category:
            conds.append("category=?")
            params.append(category)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(sql, params)]

    # ---------- interests（常關注詞彙）----------
    def touch_interest(self, term: str, definition: Optional[str] = None, notes: Optional[str] = None) -> None:
        term = term.strip()
        self.db.execute(
            "INSERT INTO interests(term,count,definition,notes,first_seen,last_seen) VALUES(?,1,?,?,?,?) "
            "ON CONFLICT(term) DO UPDATE SET count=count+1, last_seen=excluded.last_seen, "
            "definition=COALESCE(excluded.definition, interests.definition), "
            "notes=COALESCE(excluded.notes, interests.notes)",
            (term, definition, notes, _now(), _now()),
        )
        self.db.commit()

    def list_interests(self, limit: int = 30) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM interests ORDER BY count DESC, last_seen DESC LIMIT ?", (limit,))]

    # ---------- 給 AI 的彙整 ----------
    def snapshot(self, today: Optional[date] = None) -> dict[str, Any]:
        today = today or date.today()
        return {
            "profile": self.get_profile(),
            "upcoming_races": self.upcoming_races(today),
            "active_events": [e for e in self.list_events() if e["kind"] != "race"],
            "notes": [{"id": n["id"], "category": n["category"], "content": n["content"], "created_at": n["created_at"][:10]}
                      for n in self.list_notes(limit=30)],
            "interests": [{"term": i["term"], "count": i["count"], "definition": i["definition"]} for i in self.list_interests(15)],
        }
