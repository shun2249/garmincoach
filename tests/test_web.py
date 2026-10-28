"""網路搜尋：閘門（快取 → 知識庫 → 每日上限）、完整紀錄、快取重用、Coach 整合、API 回傳解析。假子模型，不打 API。"""
import json, os, shutil, sys, tempfile, types
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garmin_coach import config
from garmin_coach.store import Store
from garmin_coach.tools import Tools
from garmin_coach.websearch import WebCache, WebSearcher, normalize_query, format_kb_hits
from garmin_coach.coach import Coach

db = os.environ.get("TEST_DB") or tempfile.mkstemp(prefix="garmin_coach_", suffix=".db")[1]
from tests.synth import ensure_test_db; ensure_test_db(db)
config.WEB_SEARCH_ENABLED = True; config.WEB_SEARCH_DAILY_MAX = 5; config.WEB_SEARCH_CALL_USD = 0.01

# 0) 正規化
assert normalize_query("  Challenge Taiwan 2026, 113 bike-course!  ") == "challenge taiwan 2026 113 bikecourse", normalize_query("  Challenge Taiwan 2026, 113 bike-course!  ")
assert normalize_query("台北 113 鐵人三項（2026）") == "台北 113 鐵人三項2026"
print("normalize ok")

# 假向量：用固定基底，cos 可控
def unit(v): v = np.asarray(v, dtype=np.float32); return v / np.linalg.norm(v)
E1 = unit([1, 0, 0]); E_SAME = unit([0.95, 0.31, 0]); E_MAYBE = unit([0.75, 0.66, 0]); E_OTHER = unit([0.2, 0.98, 0])
assert round(float(E1 @ E_SAME), 2) == 0.95 and round(float(E1 @ E_MAYBE), 2) == 0.75 and round(float(E1 @ E_OTHER), 2) == 0.2
VECS = {"Challenge Taiwan 2026 113 bike course elevation": E1, "challenge taiwan 113 2026 bike route elevation gain": E_SAME,
        "Challenge Taiwan 2026 113 run course": E_MAYBE, "tapering before half ironman": E_OTHER, "台北馬拉松 2026 報名日期": unit([0, 0, 1])}

class FakeSearcher:
    model = "gpt-6-luna"; judge_model = "gpt-6-luna"
    def __init__(self): self.runs = []; self.judges = []; self.judge_answer = False
    def judge(self, prompt):
        self.judges.append(prompt); return self.judge_answer, {"input": 500, "cached_input": 0, "output": 1}
    def run(self, query, reason=""):
        self.runs.append((query, reason))
        return {"model": self.model, "summary": f"結論：{query} 的資料。\n事實：\n- 日期 2026-04-25 [1]\n- 爬升 850 m [2]\n來源：\n[1] 官網 — https://example.com/a\n[2] 路線 — https://example.com/b",
                "searches": [{"type": "search", "query": query, "sources": ["https://example.com/a", "https://example.com/b"], "status": "completed"}],
                "citations": [{"url": "https://example.com/a", "title": "官網"}], "raw": [{"type": "web_search_call", "id": "ws_1"}],
                "usage": {"input": 12000, "cached_input": 0, "output": 600}, "calls": 1, "cost_usd": 0.0}

state = {"cov": {"max_sim": 0.3, "hits": []}}
KB_HITS = [{"sim": 0.66, "pmid": "1", "title": "Tapering review", "year": 2023, "citation": "Wang et al. (2023) SM", "pub_types": ["Meta-Analysis"], "section": "abstract", "text": "Taper 2-3 weeks reduce volume 40-60%..."}]

with Store(db) as store:
    t = Tools(store); t.conversation_id = 999
    fake = FakeSearcher(); t._web = fake
    t._embed_query = lambda q: VECS[q]
    t._kb_coverage = lambda vec: state["cov"]
    cache = WebCache(store)

    # 1) 知識庫可回答 → 拒絕、回知識庫結果、不上網；denied 有紀錄、閘門呼叫記進 llm_calls
    state["cov"] = {"max_sim": 0.66, "hits": KB_HITS}; fake.judge_answer = True
    r = t.search_web("tapering before half ironman", reason="想知道減量")
    assert r["denied"] and r["kb_results"][0]["citation"].startswith("Wang") and not fake.runs, r
    assert "tapering before half ironman" in fake.judges[-1] and "Tapering review" in fake.judges[-1]
    row = store.conn.execute("select status, deny_reason, kb_max_sim, kb_judged, conversation_id from web_searches order by id desc limit 1").fetchone()
    assert tuple(row) == ("denied", "知識庫已有相關資料", 0.66, "covered", 999), tuple(row)
    assert store.conn.execute("select count(*) from llm_calls where purpose='web_gate' and conversation_id=999").fetchone()[0] == 1
    print("gate: kb covered → denied ok")

    # 2) 知識庫相似度低於 floor → 不問判斷、直接放行；完整紀錄
    state["cov"] = {"max_sim": 0.3, "hits": []}
    n_judge = len(fake.judges)
    r = t.search_web("Challenge Taiwan 2026 113 bike course elevation", reason="賽事路線本地沒有")
    assert r["cached"] is False and r["cache_id"] and "850 m" in r["summary"] and r["citations"][0]["url"] == "https://example.com/a", r
    assert len(fake.runs) == 1 and len(fake.judges) == n_judge
    rec = store.conn.execute("select * from web_searches where id=?", (r["cache_id"],)).fetchone()
    assert rec["status"] == "done" and rec["kb_judged"] == "skip" and rec["calls"] == 1 and rec["embedding"] is not None and rec["reason"] == "賽事路線本地沒有"
    assert json.loads(rec["searches"])[0]["sources"] == ["https://example.com/a", "https://example.com/b"] and json.loads(rec["raw"])[0]["id"] == "ws_1"
    assert rec["input"] == 12000 and rec["output"] == 600
    exp_cost = round(12000 * 0.1 / 1e6 + 600 * 0.5 / 1e6 + 0.01, 4)
    assert abs(rec["cost_usd"] - exp_cost) < 1e-6, (rec["cost_usd"], exp_cost)
    lc = store.conn.execute("select purpose, model, cost_usd, conversation_id from llm_calls order by id desc limit 1").fetchone()
    assert tuple(lc) == ("web", "gpt-6-luna", exp_cost, 999), tuple(lc)
    first_id = r["cache_id"]
    print("allow + full record ok, cost", exp_cost)

    # 3) 同一個查詢（大小寫/標點不同）→ 快取命中、不上網、hits+1
    r = t.search_web("Challenge Taiwan 2026 113 bike course elevation", reason="x")
    assert r["cached"] is True and r["cache_id"] == first_id and len(fake.runs) == 1 and "快取" in r["note"], r
    assert store.conn.execute("select hits from web_searches where id=?", (first_id,)).fetchone()[0] == 1
    # 4) 相近查詢（cos 0.95 ≥ 0.85）→ 直接命中，不問判斷
    n_judge = len(fake.judges)
    r = t.search_web("challenge taiwan 113 2026 bike route elevation gain", reason="x")
    assert r["cached"] is True and r["cache_id"] == first_id and len(fake.judges) == n_judge and len(fake.runs) == 1
    # 5) 中間地帶（cos 0.75）：判斷 no → 不是同一件事 → 走閘門放行上網；判斷 yes → 命中
    fake.judge_answer = False
    r = t.search_web("Challenge Taiwan 2026 113 run course", reason="跑步路線")
    assert r["cached"] is False and len(fake.runs) == 2 and "run course" in fake.judges[-1], r
    second_id = r["cache_id"]
    fake.judge_answer = True
    r = t.search_web("Challenge Taiwan 2026 113 run course", reason="跑步路線")   # 完全相同 → exact 命中
    assert r["cached"] and r["cache_id"] == second_id and len(fake.runs) == 2
    print("cache exact / similar / maybe ok")

    # 6) ignore_cache → 重新上網
    r = t.search_web("Challenge Taiwan 2026 113 bike course elevation", reason="使用者要最新", ignore_cache=True)
    assert r["cached"] is False and len(fake.runs) == 3 and r["cache_id"] != first_id
    # 7) cache_id 直接取
    r = t.search_web("whatever", cache_id=first_id); assert r["cached"] and r["cache_id"] == first_id and "850 m" in r["summary"]
    assert "error" in t.search_web("x", cache_id=99999)
    # 8) search_knowledge 的 web_cached 來源：相似 ≥ 0.55
    wc = t._web_cached(E_SAME, 0.55, 2); assert wc and wc[0]["cache_id"] in (first_id, second_id) and "850 m" in wc[0]["summary"], wc
    assert not t._web_cached(unit([0, 0, 1]), 0.55, 2)
    # 8b) 相關的舊查詢會附在結果裡（related_cached），cache_id 取回是完整內容
    r = t.search_web("Challenge Taiwan 2026 113 run course", reason="x")    # exact 命中 #second_id
    assert r["cached"] and r["cache_id"] == second_id and {x["cache_id"] for x in r["related_cached"]} >= {first_id}, r
    r = t.search_web("x", cache_id=second_id); assert r["related_cached"] and r["truncated"] is False
    # 8c) 「未找到」的結果只保留 WEB_CACHE_DAYS_NOTFOUND 天
    from garmin_coach.websearch import is_not_found
    assert is_not_found("未找到：2026 官方路線圖") and not is_not_found("結論：日期 2026-04-25")
    store.conn.execute("update web_searches set summary='未找到：官方尚未公布', ts=? where id=?", ("2026-09-01T00:00:00", second_id)); store.conn.commit()
    assert cache.find_exact("challenge taiwan 2026 113 run course") is None and not cache.similar(E_MAYBE, 0.9)
    from datetime import datetime
    store.conn.execute("update web_searches set ts=? where id=?", (datetime.now().isoformat(timespec="seconds"), second_id)); store.conn.commit()
    assert cache.find_exact("challenge taiwan 2026 113 run course")["id"] == second_id
    # 9) 每日上限（今天已放行 3 次）
    config.WEB_SEARCH_DAILY_MAX = 3
    r = t.search_web("台北馬拉松 2026 報名日期", reason="報名")
    assert r["denied"] and "上限" in r["reason"] and len(fake.runs) == 3, r
    assert store.conn.execute("select deny_reason from web_searches order by id desc limit 1").fetchone()[0].startswith("每日上限")
    config.WEB_SEARCH_DAILY_MAX = 5
    # 9b) 每段對話上限（conversation 999 已放行 3 次）
    config.WEB_SEARCH_PER_CONVERSATION = 3
    r = t.search_web("台北馬拉松 2026 報名日期", reason="報名")
    assert r["denied"] and "這段對話" in r["reason"] and len(fake.runs) == 3, r
    t.conversation_id = 1000
    config.WEB_SEARCH_PER_CONVERSATION = 3
    # 10) 關閉功能
    config.WEB_SEARCH_ENABLED = False
    assert "未啟用" in t.search_web("台北馬拉松 2026 報名日期", reason="x")["reason"]; config.WEB_SEARCH_ENABLED = True
    # 11) 子模型失敗 → error、failed 紀錄
    def boom(q, reason=""): raise RuntimeError("api down")
    fake.run = boom
    r = t.search_web("台北馬拉松 2026 報名日期", reason="x"); assert "error" in r and "api down" in r["error"]
    assert store.conn.execute("select status from web_searches order by id desc limit 1").fetchone()[0] == "failed"
    n_done, cost_sum, hits = cache.stats("2000-01-01"); assert n_done == 3 and hits >= 4, (n_done, cost_sum, hits)
    print("ignore_cache / cache_id / web_cached / daily cap / disabled / failure ok")

    # 12) Coach 整合：假 LLM 呼叫 search_web；conversation_id 接上；denied 後模型照常作答
    class FakeLLM:
        model = "fake"; usage = {"calls": 0, "input": 0, "cached_input": 0, "output": 0}
        def chat(self, messages, tools=None):
            last = messages[-1]
            if last["role"] == "user" and tools:
                return {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "search_web", "arguments": json.dumps({"query": "Challenge Taiwan 2026 113 bike course elevation", "reason": "本地無賽事路線"})}}]}
            if last["role"] == "tool":
                d = json.loads(last["content"]); assert d["cached"] is True and "850 m" in d["summary"], d
                return {"role": "assistant", "content": "路線爬升 850 m（來源：官網，快取 2026）"}
            return {"role": "assistant", "content": "ok"}
    c = Coach(store, FakeLLM())
    c.tools._web = FakeSearcher(); c.tools._embed_query = lambda q: VECS[q]; c.tools._kb_coverage = lambda vec: state["cov"]
    assert c.tools.conversation_id == c.conversation_id
    out = c.ask("幫我查 Challenge Taiwan 113 的自行車路線爬升")
    assert "850 m" in out and c.tool_log[-1]["name"] == "search_web" and not c.tools._web.runs   # 命中快取，沒上網
    print("coach integration ok")

# 13) WebSearcher.run 解析真實 API 形狀（假 client）
class Obj:
    def __init__(self, **kw): self.__dict__.update(kw)
    def model_dump(self, exclude_none=True):
        def conv(v):
            if isinstance(v, Obj): return v.model_dump()
            if isinstance(v, list): return [conv(x) for x in v]
            return v
        return {k: conv(v) for k, v in self.__dict__.items() if v is not None}
ann = Obj(type="url_citation", url="https://race.example/info", title="賽事資訊", start_index=0, end_index=5)
resp = Obj(output=[
    Obj(type="reasoning", id="rs_1", summary=[]),
    Obj(type="web_search_call", id="ws_1", status="completed", action=Obj(type="search", query="Challenge Taiwan 2026 date", sources=[Obj(type="url", url="https://race.example/info")])),
    Obj(type="web_search_call", id="ws_2", status="completed", action=Obj(type="open_page", url="https://race.example/info")),
    Obj(type="message", id="msg_1", content=[Obj(type="output_text", text="結論：2026-04-25 舉行。", annotations=[ann, ann])]),
], usage=Obj(input_tokens=9000, output_tokens=300, input_tokens_details=Obj(cached_tokens=1000)))
ws = WebSearcher.__new__(WebSearcher); ws.model = "gpt-6-luna"; ws.judge_model = "gpt-6-luna"; ws.usage = {"calls": 0, "input": 0, "cached_input": 0, "output": 0}
captured = {}
ws.client = Obj(responses=Obj(create=lambda **kw: captured.update(kw) or resp))
res = ws.run("Challenge Taiwan 2026 date", "賽事日期")
assert captured["tools"][0]["type"] == "web_search" and captured["max_tool_calls"] == config.WEB_SEARCH_MAX_CALLS and captured["store"] is False
assert captured["include"] == ["web_search_call.action.sources"] and "Challenge Taiwan 2026 date" in captured["input"][1]["content"]
assert res["summary"] == "結論：2026-04-25 舉行。" and res["calls"] == 2 and len(res["citations"]) == 1 and res["citations"][0]["url"] == "https://race.example/info"
assert res["searches"][0]["query"] == "Challenge Taiwan 2026 date" and res["searches"][0]["sources"] == ["https://race.example/info"] and res["searches"][1]["url"] == "https://race.example/info"
assert res["usage"] == {"input": 8000, "cached_input": 1000, "output": 300} and [d["type"] for d in res["raw"]] == ["reasoning", "web_search_call", "web_search_call", "message"]
assert abs(res["cost_usd"] - round(8000 * 0.1 / 1e6 + 1000 * 0.01 / 1e6 + 300 * 0.5 / 1e6 + 2 * 0.01, 4)) < 1e-6, res["cost_usd"]
# judge 解析
ws.client = Obj(responses=Obj(create=lambda **kw: Obj(output_text=" Yes.", usage=Obj(input_tokens=50, output_tokens=1, input_tokens_details=None))))
assert ws.judge("q?") == (True, {"input": 50, "cached_input": 0, "output": 1})
ws.client = Obj(responses=Obj(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("x"))))
assert ws.judge("q?")[0] is None
assert "Wang" in format_kb_hits(KB_HITS) and format_kb_hits([]).startswith("（")
print("parse ok")
print("OK test_web")
