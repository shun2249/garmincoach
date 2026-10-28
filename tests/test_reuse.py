"""問答重用：假 LLM + 假向量，不打 API。"""
import json, os, shutil, sys, tempfile
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach import config
from garmin_coach.coach import Coach
from garmin_coach.store import Store

class FakeLLM:
    def __init__(self, model, route="deep"):
        self.model = model; self.route = route; self.usage = {"calls": 0, "input": 0, "cached_input": 0, "output": 0}; self.last_input = ""
    def chat(self, messages, tools=None):
        self.usage["calls"] += 1; self.usage["input"] += 10; self.usage["output"] += 5
        last = messages[-1]["content"]; self.last_input = last
        if last.startswith("你是分流器"): return {"role": "assistant", "content": self.route}
        if last.startswith("判斷兩個問題"): return {"role": "assistant", "content": "yes" if "課表" in last.split("新問題：")[-1] else "no"}
        return {"role": "assistant", "content": f"answer from {self.model}: " + "x" * 100, "_model": self.model}

class FakeEmbedder:
    """問題裡含 '課表' 的都給同一個向量，其他給正交向量。"""
    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(1536, dtype=np.float32); v[0 if "課表" in t else 1] = 1.0; out.append(v)
        return np.stack(out)

db = os.environ.get("TEST_DB") or tempfile.mkstemp(prefix="garmin_coach_", suffix=".db")[1]
from tests.synth import ensure_test_db; ensure_test_db(db)
config.REUSE_SIMILARITY = 0.5
with Store(db) as st:
    st.conn.execute("DELETE FROM qa_cache"); st.conn.commit()
    luna, sol = FakeLLM("luna"), FakeLLM("sol")
    c = Coach(st, luna, deep_llm=sol, mode="auto"); c._embedder = FakeEmbedder()
    r = c.ask("幫我排賽前兩週的課表"); assert c.last_depth == "deep" and r.startswith("answer from sol"), r
    assert st.conn.execute("select count(*) from qa_cache").fetchone()[0] == 1
    # 相似問題 → reuse，用 luna，且 prompt 夾帶舊回答
    c = Coach(st, luna, deep_llm=sol, mode="auto"); c._embedder = FakeEmbedder()
    r = c.ask("幫我重看一下賽前的課表安排"); assert c.last_depth == "reuse" and c.last_model == "luna", (c.last_depth, c.last_model)
    assert "=== " in luna.last_input and "answer from sol" in luna.last_input and c.last_reuse["similarity"] >= 0.99
    assert st.conn.execute("select count(*) from qa_cache where depth='reuse'").fetchone()[0] == 1
    # 存進 messages 表的是原問題，不含夾帶的舊回答
    saved = st.conn.execute("select content from messages where role='user' order by id desc limit 1").fetchone()[0]
    assert saved == "幫我重看一下賽前的課表安排", saved
    # 「重新」→ 不重用
    c = Coach(st, luna, deep_llm=sol, mode="auto"); c._embedder = FakeEmbedder()
    c.ask("請重新規劃賽前課表"); assert c.last_depth == "deep" and c.last_reuse is None
    # 不相關的深入題 → 不重用
    c = Coach(st, luna, deep_llm=sol, mode="auto"); c._embedder = FakeEmbedder()
    c.ask("右膝痛要怎麼處理"); assert c.last_depth == "deep" and c.last_reuse is None
    # simple 題不進快取
    luna.route = "simple"; c = Coach(st, luna, deep_llm=sol, mode="auto"); c._embedder = FakeEmbedder()
    n = st.conn.execute("select count(*) from qa_cache").fetchone()[0]; c.ask("今天 HRV 是多少")
    assert st.conn.execute("select count(*) from qa_cache").fetchone()[0] == n
    # 關閉重用
    config.REUSE_SIMILARITY = 0; luna.route = "deep"
    c = Coach(st, luna, deep_llm=sol, mode="auto"); c._embedder = FakeEmbedder(); c.ask("幫我重新排一份完整的賽前課表"); assert c.last_reuse is None and c.last_depth == "deep", (c.last_reuse, c.last_depth)
print("REUSE TEST OK")
