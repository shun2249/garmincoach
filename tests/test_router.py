"""分流器與跨模型上下文：假 LLM，不打 API。"""
import json, os, shutil, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach.coach import Coach, OpenAILLM, classify_depth
from garmin_coach.store import Store

class FakeLLM:
    def __init__(self, model, answer="deep"):
        self.model = model; self.answer = answer; self.usage = {"calls": 0, "input": 0, "cached_input": 0, "output": 0}; self.seen = []
    def chat(self, messages, tools=None):
        self.usage["calls"] += 1; self.usage["input"] += 10; self.usage["output"] += 5
        last = messages[-1]["content"]
        self.seen.append(last[:30])
        if last.startswith("你是分流器"):
            return {"role": "assistant", "content": self.answer}
        if last.startswith("請根據以上整段對話"):
            return {"role": "assistant", "content": json.dumps({"summary": "s", "notes": [], "interests": [], "events": [], "feedback": {}})}
        return {"role": "assistant", "content": f"answer from {self.model}", "_model": self.model,
                "_raw_items": [{"id": "rs_1", "type": "reasoning", "summary": []},
                               {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"answer from {self.model}"}]}]}

# 1) 規則
router = FakeLLM("luna", "deep")
assert classify_depth("好", "", router) == "simple"                      # 極短句
assert classify_depth("那第二週呢", "", router, last_depth="deep") == "deep"   # deep 後短追問沿用
assert classify_depth("幫我排賽前兩週的課表", "", router) == "deep"
router2 = FakeLLM("luna", "simple"); assert classify_depth("今天 HRV 96 是什麼意思", "", router2) == "simple"
print("rules ok")

# 2) 跨模型 raw items 轉換：reasoning 丟掉、function_call 去 id、message 變文字
raw = [{"id": "rs_1", "type": "reasoning", "summary": []},
       {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "get_health_status", "arguments": "{}", "status": "completed"},
       {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]
conv = OpenAILLM._foreign_items(raw)
assert conv == [{"type": "function_call", "call_id": "call_1", "name": "get_health_status", "arguments": "{}"}, {"role": "assistant", "content": "hi"}], conv
print("foreign items ok")

# 3) Coach 依分流選模型；simple → luna、deep → sol；mode=simple 不分流
db = os.environ.get("TEST_DB") or tempfile.mkstemp(prefix="garmin_coach_", suffix=".db")[1]
from tests.synth import ensure_test_db; ensure_test_db(db)
with Store(db) as store:
    luna, sol = FakeLLM("luna", "deep"), FakeLLM("sol")
    c = Coach(store, luna, deep_llm=sol, mode="auto")
    r = c.ask("幫我排賽前兩週的課表"); assert r == "answer from sol" and c.last_model == "sol" and c.last_depth == "deep", (r, c.last_model)
    luna.answer = "simple"
    r = c.ask("今天 HRV 96 是什麼意思"); assert r == "answer from luna" and c.last_depth == "simple"
    # 上下文裡有 sol 的 raw items，luna 的 _to_input 不會原樣送 → 用 OpenAILLM 的轉換邏輯檢查
    fake_openai = OpenAILLM.__new__(OpenAILLM); fake_openai.model = "luna"
    items = fake_openai._to_input(c.messages)
    n_reason = sum(1 for i in items if i.get("type") == "reasoning")
    n_own = sum(1 for m in c.messages if m["role"] == "assistant" and m.get("_raw_items") and m.get("_model") == "luna")
    assert n_reason == n_own, (n_reason, n_own, "只能送 luna 自己的 reasoning，sol 的要被轉掉")
    rows = store.conn.execute("select purpose, model, count(*) from llm_calls group by purpose, model order by 1,2").fetchall()
    print("llm_calls:", [tuple(r) for r in rows])
    assert ("route", "luna", 2) in [tuple(r) for r in rows] and ("chat", "sol", 1) in [tuple(r) for r in rows]
    c2 = Coach(store, luna, deep_llm=sol, mode="simple"); assert c2.ask("幫我排賽前兩週的課表") == "answer from luna"
    c3 = Coach(store, luna, deep_llm=sol, mode="deep"); assert c3.ask("好") == "answer from sol"
    u = c.finish()["usage"]; assert u and u.get("deep", {}).get("model") == "sol", u
print("ROUTER TEST OK")
