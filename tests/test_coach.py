"""用假 LLM 測試 Coach：工具呼叫迴圈、記憶寫回、結束萃取。用真實 DB 的複本。"""
import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
def _tmp(env, suffix):
    p = os.environ.get(env)
    return p or tempfile.mkstemp(prefix="garmin_coach_", suffix=suffix)[1]
import json, os, shutil, sys
from tests.synth import ensure_test_db
dst = ensure_test_db(_tmp("TEST_DB", ".db"))
from garmin_coach.store import Store
from garmin_coach.coach import Coach
from garmin_coach.tools import Tools, TOOL_SPECS

class FakeLLM:
    model = "fake"
    def __init__(self): self.n = 0
    def chat(self, messages, tools=None):
        self.n += 1
        last = messages[-1]
        if last["role"] == "user" and last["content"].startswith("請根據以上整段對話"):
            return {"role": "assistant", "content": '```json\n' + json.dumps({
                "summary": "輕鬆跑 5.7km，HR 偏高，建議下次放慢。", "notes": [{"category": "status", "content": "最近工作壓力大，睡眠 6 小時"}],
                "interests": [{"term": "HR drift", "definition": "心率漂移"}], "events": [], "feedback": {"rpe": 6, "feeling": "腿有點重"}}, ensure_ascii=False) + '\n```'}
        if last["role"] == "user" and tools:
            # 第一輪：同時呼叫三個工具（含記錄左膝疼痛、詞彙）
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_activity_timeseries", "arguments": json.dumps({"step_s": 60})}},
                {"id": "c2", "type": "function", "function": {"name": "record_note", "arguments": json.dumps({"category": "injury", "content": "跑到 4km 開始痛，下樓梯明顯", "body_part": "左膝外側", "severity": "輕微"})}},
                {"id": "c3", "type": "function", "function": {"name": "record_interest", "arguments": json.dumps({"term": "LT1"})}},
                {"id": "c4", "type": "function", "function": {"name": "get_sport_history", "arguments": json.dumps({"sport": "running", "days": 365})}},
            ]}
        if last["role"] == "tool":
            # 檢查工具結果有被餵回來
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            ts = json.loads(tool_msgs[0]["content"]); assert ts["points"] > 5 and "hr" in ts["rows"][1], ts
            note = json.loads(tool_msgs[1]["content"]); assert note["note_id"]
            hist = json.loads(tool_msgs[3]["content"]); assert hist["total_sessions"] >= 5 and hist["monthly"], hist
            return {"role": "assistant", "content": f"看過逐秒資料（{ts['points']} 點）與一年歷史（{hist['total_sessions']} 次），左膝已記錄。"}
        return {"role": "assistant", "content": "ok"}

with Store(dst) as store:
    t = Tools(store)
    pf = t.preflight(include_weather=True)
    assert pf["this_session"]["activity_id"] and pf["health_status"]["assessment"] in ("normal", "attention", "poor")
    assert pf["recent_training_10d"]["totals"]["sessions"] >= 3
    assert isinstance(pf["fitness_metrics"]["latest"]["lt_hr"], (int, float))
    print("preflight keys:", list(pf)); print("health:", pf["health_status"]["assessment"], pf["health_status"]["flags"])
    print("weather:", (pf.get("weather_outlook") or {}).get("location"), [(d["date"], d["temp_min"], d["temp_max"], d["sunrise"], d["sunset"]) for d in (pf.get("weather_outlook") or {}).get("days", [])][:4])
    print("preflight size:", len(json.dumps(pf, ensure_ascii=False, default=str)), "chars")
    names = {s["function"]["name"] for s in TOOL_SPECS}
    for n in names: assert callable(getattr(t, n)), n

    llm = FakeLLM()
    coach = Coach(store, llm, include_weather=False)
    reply = coach.ask("今天跑完左膝外側有點痛，LT1 是什麼？")
    print("reply:", reply)
    assert "左膝" in reply and len(coach.tool_log) == 4
    r = coach.finish(); print("finish:", r)
    assert r["written"]["notes"] == 1 and r["written"]["feedback"] == 1
    notes = t.ctx.list_notes(); assert any("左膝外側" in n["content"] for n in notes) and any("對話 #" in n["content"] for n in notes)
    ints = {i["term"]: i["count"] for i in t.ctx.list_interests()}
    assert ints.get("LT1", 0) >= 1 and ints.get("HR drift", 0) >= 1, ints   # 真實 DB 複本裡可能本來就有這些詞
    fb = store.get_feedback(coach.activity_id); assert fb.rpe == 6
    rows = store.conn.execute("SELECT role, count(*) FROM messages WHERE conversation_id=? GROUP BY role", (coach.conversation_id,)).fetchall()
    print("messages:", [tuple(r) for r in rows])
    assert store.conn.execute("SELECT summary FROM conversations WHERE id=?", (coach.conversation_id,)).fetchone()[0]
print("COACH TEST OK")
