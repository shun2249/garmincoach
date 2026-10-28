"""AI 教練對話：OpenAI function calling + 對話記憶。

流程：
1. 對話開始前先用 Tools.preflight() 打包三層資料（身體/健康/天氣、過去 10 天 + 一年同項目、這次訓練）。
2. 對話中 AI 需要更多資料時呼叫工具（tools.py），例如逐秒資料、一年同項目歷史。
3. 使用者提到不適、疼痛、比賽、行程、專業詞彙時，AI 直接用 record_* 工具寫回。
4. 結束時再做一次萃取：摘要 + 漏記的筆記/詞彙，寫進 conversations / notes / interests。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional, Protocol

from . import config
from .pricing import PRICES, estimate_cost  # noqa: F401  （價目表在 pricing.py，這裡保留匯出給舊程式用）
from .store import Store
from .tools import TOOL_SPECS, Tools

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是使用者的個人耐力運動教練（跑步、自行車、游泳、鐵人三項），講繁體中文。

你手上有使用者 Garmin 的完整資料，對話一開始會附上「訓練前資料包」，裡面有：
- 基礎資料層：身體資料、心率/功率區間、今天的 RHR/HRV/睡眠/壓力/Body Battery 與 30 天基準的比較（health_status 永遠是今天 today 的狀態；若 this_session 不是今天的訓練，session_day_health 才是訓練當天的狀態，兩者不要混用）、天氣與日出日落、目標賽事、傷病與近況筆記
- 過去訓練層：最近 10 天所有訓練與負荷、乳酸閾值/FTP/比賽預測/VO2max/跑步耐受度與一年趨勢
- 這次訓練：完整摘要、分段、心率/功率區間時間與邊界、HR drift

工作原則：
0. 範圍限制：你只處理運動訓練、運動生理、恢復與睡眠、運動營養與補給、傷痛與健康狀況、賽事規劃與配速、裝備與訓練環境這些主題。其他任何請求（寫小說、寫程式、翻譯、閒聊、時事、一般知識、作業、文案等）一律不做：用一句話說明你是訓練教練只回答運動與健康相關問題，然後停止，不要提供任何部分內容，也不要把這類請求記進任何工具。使用者堅持也一樣拒絕。
1. 先看資料再下結論。數字要具體（配速、心率、區間、百分比），不要空泛鼓勵。
2. 判斷身體狀況：用 RHR、HRV、睡眠時數與基準比較；異常時要先問使用者感受再建議調整。
3. 判斷強度：用使用者自己的心率區間 / 功率區間 / 乳酸閾值，不要用通用公式。
4. 判斷負荷：看最近 10 天的量與 ACWR，並對照一年同項目的歷史（需要時呼叫 get_sport_history）。
5. 需要更多資料就呼叫工具，不要猜測、也不要說「沒有資料」：資料包裡 recent_training_10d 的 sessions 只是摘要，每一次訓練都另外存有每圈/每公里分段與逐秒資料。
   - 使用者要分析某一次訓練（不是資料包的 this_session）：先呼叫 get_activity_detail(activity_id)，裡面有分段、區間時間、漂移、天氣；同一天有兩筆（例如騎後接跑）兩筆都要拉。
   - 使用者提到訓練中的某一段（前幾分鐘、轉換期、後半段、最後幾公里、某個爬坡、間歇的某一趟）：呼叫 get_activity_timeseries(activity_id, step_s=30 或 60)，用那一段的心率、配速、功率、步頻實際數字回答。
   - 看趨勢用 get_sport_history，看更長的恢復趨勢用 get_daily_recovery。
6. 使用者提到任何身體不適、疼痛、生病：立刻呼叫 record_note（category=injury 或 illness），寫明部位、程度、何時開始、什麼情況會痛，之後用來評估是否該降量或是否已影響表現。
7. 使用者提到比賽目標、家庭/工作行程、時間限制：呼叫 record_event。
8. 使用者問到專業詞彙或反覆關心的主題（LT1、400m 配速、步頻、HR drift…）：呼叫 record_interest，並用他能理解的方式解釋。
9. 使用者描述這次訓練的感受或 RPE：呼叫 log_feedback。
10. 對話中得知的近況（睡不好的原因、最近工作壓力、恢復狀況、比賽準備進度）：呼叫 record_note（status / recovery / race_progress）。
11. 回答結構：先講結論（這次訓練如何、身體狀況如何），再講依據，最後給下一步建議。
12. 給訓練建議、解釋訓練原理或營養策略時，先用 search_knowledge（英文關鍵字）查論文知識庫，把研究結論用自己的話融進建議裡；回答裡不要標注論文出處或作者年份（例如「（Wang 2023）」、「根據 Smith 等人的研究」這類寫法一律省略），也不要列參考文獻；找不到就明說是經驗判斷。使用者提到疼痛、傷害、生病或問傷害預防時，用 topic=injury_running / injury_cycling / injury_prevention / sports_medicine 查運動醫學文獻；涉及紅旗症狀（胸痛、暈厥、嚴重腫脹、無法承重、發燒合併心悸）要明確建議就醫，不要只給訓練建議。search_knowledge 回來的結果只要主題相關（系統性回顧、共識聲明、RCT 尤佳），就要拿來當依據，即使只拿到摘要段落；不要在有相關結果時說「找不到研究」。
13. 篇幅：抓重點，一次回覆以 300–1500 字為原則（分段表格不計入）：已經讀到的資料要用上，數字、依據、建議寫完整，不要為了精簡省略；需要追問時一次只問一到兩個問題。
14. 分析某一次訓練時，回答裡要附一張分段表，不要只給範圍或平均：穩定跑/騎列每公里或每 5 公里（距離、配速或功率、平均/最高心率）；間歇課列每一趟工作段（距離、時間、配速、功率、平均/最高心率），恢復段合併成一行說明；超過 15 列就列重點趟次並說明省略了哪些。表格用 Markdown 表格語法。
15. 網路搜尋 search_web 是最後手段：先確認 Garmin 資料（工具）和論文知識庫（search_knowledge）都拿不到，才用它查賽事資訊（日期、路線、爬升、關門時間）、場地、規則、裝備規格這類外部事實；訓練方法與生理原理一律查 search_knowledge，不要上網。拿到的網路資料只是參考，回答時標「（來源：網站名，查詢日期）」；回傳 cached=true 代表是之前查過的快取，要註明資料日期，不要為了重查而改寫問題或設 ignore_cache。被拒絕（denied）就用回傳的 kb_results 或本地資料回答，不要換個說法再試。"""

EXTRACT_PROMPT = """請根據以上整段對話，輸出 JSON（不要其他文字）：
{
  "summary": "三到五句話的對話摘要，包含對這次訓練的評估與建議",
  "notes": [{"category": "injury|illness|status|recovery|race_progress|preference|insight", "content": "..."}],
  "interests": [{"term": "...", "definition": "..."}],
  "events": [{"kind": "race|travel|family|work|illness|injury|constraint", "title": "...", "start_date": "YYYY-MM-DD", "end_date": null, "priority": null, "notes": null}],
  "feedback": {"rpe": null, "feeling": null, "pain": null, "sick": null}
}
只列出對話中明確出現、而且對之後訓練評估有幫助的內容。下面「已記錄」清單裡的項目（或意思相同的）不要再列。沒有就給空陣列或 null。
與運動、訓練、健康無關的內容（例如被拒絕的寫作/程式/閒聊請求）完全不要納入 summary、notes、interests、events。
notes 只記 Garmin 資料裡「沒有」的東西：使用者自述的體感、不適、傷痛、生病、生活與工作情境、訓練決定或偏好、教練做出的重要判斷。
不要把心率、功率、睡眠、HRV、RHR、負荷、天氣這類 Garmin 已有的數值重抄成 note，這些資料庫裡本來就有。"""


class LLM(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]: ...


class OpenAILLM:
    """OpenAI Responses API。內部仍用 chat-completions 風格的 messages，呼叫時轉換。

    assistant 回覆會夾帶 _raw_items（含 reasoning / function_call 原始項目），
    下一輪轉換時原樣送回，推理模型在工具呼叫之間才不會失憶。
    """

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None, reasoning: Optional[str] = None) -> None:
        from openai import OpenAI
        key = api_key or config.OPENAI_API_KEY
        if not key:
            raise RuntimeError("未設定 OPENAI_API_KEY（.env）")
        self.client = OpenAI(api_key=key)
        self.model = model or config.OPENAI_MODEL
        self.reasoning = reasoning or config.OPENAI_REASONING
        self.usage = {"calls": 0, "input": 0, "cached_input": 0, "output": 0}

    @staticmethod
    def _foreign_items(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """別的模型產生的原始項目：丟掉 reasoning、去掉 id，只留文字與 function_call（call_id 要保留以對應輸出）。"""
        out: list[dict[str, Any]] = []
        for it in raw:
            t = it.get("type")
            if t == "function_call":
                out.append({"type": "function_call", "call_id": it["call_id"], "name": it["name"], "arguments": it.get("arguments") or "{}"})
            elif t == "message":
                text = "".join(c.get("text", "") for c in it.get("content") or [] if c.get("type") == "output_text")
                if text:
                    out.append({"role": "assistant", "content": text})
        return out

    def _to_input(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role in ("system", "user"):
                items.append({"role": "developer" if role == "system" else "user", "content": m.get("content") or ""})
            elif role == "assistant":
                if m.get("_raw_items"):
                    same = m.get("_model") in (None, self.model)
                    items.extend(m["_raw_items"] if same else self._foreign_items(m["_raw_items"]))
                    continue
                if m.get("content"):
                    items.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]})
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m.get("content") or ""})
        return items

    @staticmethod
    def _to_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"type": "function", "name": t["function"]["name"], "description": t["function"]["description"],
                 "parameters": t["function"]["parameters"]} for t in tools]

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.model, "input": self._to_input(messages), "store": False}
        if tools:
            kwargs["tools"] = self._to_tools(tools)
            kwargs["tool_choice"] = "auto"
        if self.reasoning:
            kwargs["reasoning"] = {"effort": self.reasoning}
        resp = self.client.responses.create(**kwargs)
        u = getattr(resp, "usage", None)
        if u:
            cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
            self.usage["calls"] += 1
            self.usage["input"] += (u.input_tokens or 0) - cached
            self.usage["cached_input"] += cached
            self.usage["output"] += u.output_tokens or 0
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        raw_items: list[dict[str, Any]] = []
        for item in resp.output:
            raw_items.append(item.model_dump(exclude_none=True))
            if item.type == "message":
                texts.extend(c.text for c in item.content if getattr(c, "type", "") == "output_text")
            elif item.type == "function_call":
                calls.append({"id": item.call_id, "type": "function", "function": {"name": item.name, "arguments": item.arguments}})
        out: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts), "_raw_items": raw_items, "_model": self.model}
        if calls:
            out["tool_calls"] = calls
        return out


ROUTER_PROMPT = """你是分流器。判斷使用者這句話需要哪種程度的回答，只輸出一個詞：deep 或 simple。
deep：需要多因素權衡或較長推理——安排課表/週期/賽前減量、賽事配速與策略、傷痛或生病的處置與能不能練、比較不同訓練方法、分析多週趨勢並給計畫、營養或補給計畫、「我該怎麼辦」類的決策。
simple：解釋某個數據或名詞、看今天/這次訓練的狀況、單一事實查詢（例如「平均功率多少」）、簡短的確認、記錄事情（RPE、行程、賽事、目標時間）。就算上一句教練在做深入規劃，只要這句只是查一個數據或名詞，仍是 simple；但如果是在修改或追問那份規劃（「那游泳可以少一點嗎」），是 deep。
上一句教練回覆的開頭：{prev}
使用者這句：{text}
只回 deep 或 simple。"""


_FOLLOWUP = re.compile(r"^(那|也|還是|可以|可不可以|改|換成|如果|不然|要是|或者|再)|(呢|嗎|好嗎|可以嗎)$")


def classify_depth(text: str, prev_reply: str, router_llm: LLM, last_depth: Optional[str] = None) -> str:
    """便宜模型分流：回 'deep' 或 'simple'。極短句直接 simple；上一題是 deep 的短追問沿用 deep。"""
    t = text.strip()
    if last_depth == "deep" and 4 <= len(t) <= 15 and _FOLLOWUP.search(t):   # 「那游泳可以少一點嗎」這種接著上一題的修改
        return "deep"
    if len(t) <= 6:
        return "simple"
    try:
        r = router_llm.chat([{"role": "user", "content": ROUTER_PROMPT.format(prev=(prev_reply or "")[:120], text=t[:500])}])
        return "deep" if "deep" in (r.get("content") or "").lower() else "simple"
    except Exception as e:  # noqa: BLE001
        log.warning("分流失敗，用 simple：%s", e)
        return "simple"


REUSE_PROMPT = """【系統提示：你在 {when} 回答過類似的問題「{prev_q}」，當時的回答附在下面。
請用「訓練前資料包」與工具裡的最新資料更新那份回答：修正已過期的內容（日期、距離比賽的天數、恢復指標、負荷、已完成或已取消的訓練、新的傷痛或行程），保留原本的結構、結論與語氣，不要從頭重寫。
若情況有重大改變（受傷、生病、賽事變動、使用者的新要求），說明改了哪裡。回答開頭請註明「（依 {when} 的回答更新）」。】
=== {when} 的回答 ===
{prev_a}
=== 回答結束 ==="""

_FRESH = re.compile(r"重新|重排|重做|重寫|從頭|不要用之前|換個|另外排")

SAME_PROMPT = """判斷兩個問題是不是在問同一件事（舊問題的回答只要用最新資料更新，就能直接回答新問題）。只輸出 yes 或 no。
注意：同一主題但不同請求算 no（例如「排課表」與「補給怎麼準備」；「膝蓋痛怎麼處理」與「膝蓋痛是什麼原因」）。
舊問題：{old}
新問題：{new}"""


class Coach:
    def __init__(self, store: Store, llm: LLM, activity_id: Optional[int] = None, include_weather: bool = True,
                 max_tool_rounds: int = 8, extract_llm: Optional[LLM] = None,
                 continue_from: Optional[int] = None, deep_llm: Optional[LLM] = None, mode: str = "auto") -> None:
        """continue_from=對話 id：接續那段對話（沿用同一筆 conversation，之前的問答放進上下文）。
        deep_llm：深入問題用的模型；mode = auto（分流）/ simple（全用 llm）/ deep（全用 deep_llm）。"""
        self.store = store
        self.llm = llm
        self.deep_llm = deep_llm
        self.mode = mode if mode in ("auto", "simple", "deep") else "auto"
        self.last_depth: Optional[str] = None
        self.last_model: Optional[str] = getattr(llm, "model", None)
        self.last_reuse: Optional[dict[str, Any]] = None     # 這一句是否重用了舊回答：{id, when, question, similarity}
        self._embedder: Any = None
        self.extract_llm = extract_llm or llm
        self.tools = Tools(store)
        self.max_tool_rounds = max_tool_rounds
        prev = self._load_conversation(continue_from) if continue_from else None
        if prev and activity_id is None:
            activity_id = prev["activity_id"]
        self.preflight = self.tools.preflight(activity_id, include_weather=include_weather)
        self.activity_id = self.preflight["this_session"].get("activity_id")
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": "【訓練前資料包】\n" + json.dumps(self.preflight, ensure_ascii=False, default=str)},
        ]
        if prev:
            self.messages.extend(prev["messages"])          # 之前的問答（純文字），不再重存
            self.conversation_id = prev["id"]
        else:
            self.conversation_id = self._new_conversation()
        self.tools.conversation_id = self.conversation_id   # 工具內部的模型呼叫（網路搜尋/閘門）記帳用
        self.tool_log: list[dict[str, Any]] = []

    def _load_conversation(self, cid: int) -> Optional[dict[str, Any]]:
        row = self.store.conn.execute("SELECT id, activity_id FROM conversations WHERE id=?", (cid,)).fetchone()
        if not row:
            raise ValueError(f"找不到對話 #{cid}")
        msgs = [{"role": r[0], "content": r[1], "_seed": True} for r in self.store.conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND role IN ('user','assistant') "
            "AND content IS NOT NULL AND content != '' ORDER BY id", (cid,))]
        return {"id": int(row[0]), "activity_id": row[1], "messages": msgs}

    # ---------- 持久化 ----------
    def _new_conversation(self) -> int:
        cur = self.store.conn.execute(
            "INSERT INTO conversations(activity_id, started_at, model) VALUES(?,?,?)",
            (self.activity_id, datetime.now().isoformat(timespec="seconds"), getattr(self.llm, "model", None)),
        )
        self.store.conn.commit()
        return int(cur.lastrowid)

    def _save(self, msg: dict[str, Any]) -> None:
        self.store.conn.execute(
            "INSERT INTO messages(conversation_id, role, content, tool_calls, created_at) VALUES(?,?,?,?,?)",
            (self.conversation_id, msg["role"], msg.get("content"),
             json.dumps(msg.get("tool_calls"), ensure_ascii=False) if msg.get("tool_calls") else None,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.store.conn.commit()

    # ---------- 用量 / 預算 ----------
    TOOL_OUTPUT_KEEP = 400          # 前幾輪的工具輸出只留這麼多字，避免每次都重送整包逐秒資料/論文

    def _snapshot(self, llm: LLM) -> dict[str, int]:
        return dict(getattr(llm, "usage", None) or {})

    def _log_call(self, llm: LLM, before: dict[str, int], purpose: str) -> float:
        """把這一次模型呼叫的用量差額寫進 llm_calls，回傳費用。"""
        after = getattr(llm, "usage", None)
        if not after:
            return 0.0
        delta = {k: after.get(k, 0) - before.get(k, 0) for k in ("input", "cached_input", "output")}
        cost = estimate_cost(delta, getattr(llm, "model", "")) or 0.0
        self.store.conn.execute(
            "INSERT INTO llm_calls(conversation_id, ts, model, purpose, input, cached_input, output, cost_usd) VALUES(?,?,?,?,?,?,?,?)",
            (self.conversation_id, datetime.now().isoformat(timespec="seconds"), getattr(llm, "model", None), purpose,
             delta["input"], delta["cached_input"], delta["output"], cost))
        self.store.conn.commit()
        return cost

    def spent_today(self) -> float:
        row = self.store.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE ts >= ?",
                                      (date.today().isoformat(),)).fetchone()
        return float(row[0] or 0)

    def over_budget(self) -> Optional[str]:
        budget = config.OPENAI_DAILY_BUDGET_USD
        if budget and self.spent_today() >= budget:
            return (f"今天的 AI 費用已達上限 US${budget:.3f}（已用 US${self.spent_today():.3f}），暫停回答。"
                    f"明天會自動恢復；要調整上限改 .env 的 OPENAI_DAILY_BUDGET_USD。")
        return None

    def _prune_old_tool_outputs(self) -> None:
        """上一輪以前的工具輸出截短（之後模型要用再呼叫一次就好）。"""
        last_user = max((i for i, m in enumerate(self.messages) if m["role"] == "user"), default=-1)
        for m in self.messages[:last_user]:
            if m["role"] == "tool" and len(m.get("content") or "") > self.TOOL_OUTPUT_KEEP and not m.get("_pruned"):
                m["content"] = m["content"][: self.TOOL_OUTPUT_KEEP] + " …（舊的工具輸出已截斷，需要完整資料請再呼叫）"
                m["_pruned"] = True

    def _pick_llm(self, user_text: str) -> LLM:
        """依 mode 與分流結果選這一句用哪個模型。"""
        if not self.deep_llm or self.mode == "simple":
            depth = "simple"
        elif self.mode == "deep":
            depth = "deep"
        else:
            prev = next((m.get("content") or "" for m in reversed(self.messages) if m["role"] == "assistant" and m.get("content")), "")
            before = self._snapshot(self.llm)
            depth = classify_depth(user_text, prev, self.llm, self.last_depth)
            self.last_ask_cost += self._log_call(self.llm, before, "route")
        llm = self.deep_llm if depth == "deep" else self.llm
        self.last_depth, self.last_model = depth, getattr(llm, "model", None)
        log.info("分流 %s → %s", depth, self.last_model)
        return llm

    # ---------- 對話 ----------
    def ask(self, user_text: str, on_tool: Optional[Any] = None) -> str:
        """送出一句話，處理工具呼叫迴圈，回傳 AI 最終回覆。"""
        self.last_ask_cost = 0.0
        stop = self.over_budget()
        if stop:
            log.warning("超過每日預算，拒絕呼叫模型")
            return stop
        t = user_text.strip()
        followup = self.last_depth in ("deep", "reuse") and 4 <= len(t) <= 15 and bool(_FOLLOWUP.search(t))
        llm = self._pick_llm(user_text)
        self.last_reuse = None
        model_text = user_text
        if self.last_depth == "deep" and not followup and not _FRESH.search(t):
            hit = self._find_similar(t)
            if hit:
                self.last_reuse = hit
                llm = self.llm                                     # 改用便宜模型做「更新」
                self.last_depth, self.last_model = "reuse", getattr(llm, "model", None)
                model_text = user_text + "\n\n" + REUSE_PROMPT.format(when=hit["when"], prev_q=hit["question"][:80], prev_a=hit["answer"])
                log.info("重用 %s 的回答（相似度 %.2f）→ %s 更新", hit["when"], hit["similarity"], self.last_model)
        user_msg = {"role": "user", "content": model_text}
        self.messages.append(user_msg)
        self._save({"role": "user", "content": user_text})
        self._prune_old_tool_outputs()
        for _ in range(self.max_tool_rounds):
            before = self._snapshot(llm)
            reply = llm.chat(self.messages, TOOL_SPECS)
            self.last_ask_cost += self._log_call(llm, before, "chat")
            self.messages.append(reply)
            self._save(reply)
            if self.over_budget() and reply.get("tool_calls"):
                note = {"role": "assistant", "content": "（已達今日費用上限，先停在這裡。）"}
                self.messages.append(note)
                return note["content"]
            if not reply.get("tool_calls"):
                self._remember_answer(t, reply.get("content", ""))
                return reply.get("content", "")
            for tc in reply["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self.tools.call(name, **args)
                self.tool_log.append({"name": name, "args": args})
                if on_tool:
                    on_tool(name, args, result)
                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False, default=str)}
                self.messages.append(tool_msg)
                self._save({"role": "tool", "content": f"{name}({json.dumps(args, ensure_ascii=False)}) -> {len(tool_msg['content'])} chars"})
        before = self._snapshot(llm)
        final = llm.chat(self.messages)                # 超過回合數：強制不用工具作答
        self.last_ask_cost += self._log_call(llm, before, "chat")
        self.messages.append(final)
        self._save(final)
        self._remember_answer(t, final.get("content", ""))
        return final.get("content", "")

    # ---------- 問答重用 ----------
    def _embed(self, text: str) -> Optional[bytes]:
        if not config.REUSE_SIMILARITY:
            return None
        try:
            if self._embedder is None:
                from .knowledge import Embedder
                self._embedder = Embedder()
            return self._embedder.embed([text])[0].astype("float32").tobytes()
        except Exception as e:  # noqa: BLE001
            log.warning("問題向量化失敗，略過重用：%s", e)
            return None

    def _find_similar(self, question: str) -> Optional[dict[str, Any]]:
        """最近 REUSE_DAYS 天內、相似度 ≥ 門檻的舊回答；多筆命中取最新的一筆。"""
        if not config.REUSE_SIMILARITY:
            return None
        rows = self.store.conn.execute(
            "SELECT id, ts, question, answer, embedding FROM qa_cache WHERE embedding IS NOT NULL AND ts >= ? ORDER BY id",
            ((date.today() - timedelta(days=config.REUSE_DAYS)).isoformat(),)).fetchall()
        if not rows:
            return None
        vec = self._embed(question)
        if vec is None:
            return None
        import numpy as np
        q = np.frombuffer(vec, dtype=np.float32)
        cands: list[dict[str, Any]] = []
        for r in rows:
            sim = float(np.dot(q, np.frombuffer(r["embedding"], dtype=np.float32)))
            if sim >= config.REUSE_SIMILARITY:
                cands.append({"id": r["id"], "when": str(r["ts"])[:10], "question": r["question"], "answer": r["answer"], "similarity": round(sim, 3)})
        # 中文短句的向量相似度不高（換句話說約 0.5–0.8），門檻只當初篩；最後用便宜模型確認是不是同一個請求
        cands.sort(key=lambda c: (c["similarity"], c["id"]), reverse=True)
        for cand in cands[:2]:
            if cand["question"].strip() == question.strip() or self._same_request(cand["question"], question):
                return cand
        return None

    def _same_request(self, old_q: str, new_q: str) -> bool:
        try:
            before = self._snapshot(self.llm)
            r = self.llm.chat([{"role": "user", "content": SAME_PROMPT.format(old=old_q[:200], new=new_q[:200])}])
            self.last_ask_cost += self._log_call(self.llm, before, "route")
            return (r.get("content") or "").strip().lower().startswith("yes")
        except Exception as e:  # noqa: BLE001
            log.warning("重用確認失敗：%s", e)
            return False

    def _remember_answer(self, question: str, answer: str) -> None:
        """深入題（含更新版）的最終回答存進快取。"""
        if self.last_depth not in ("deep", "reuse") or not answer or len(answer) < 80:
            return
        vec = self._embed(question)
        self.store.conn.execute(
            "INSERT INTO qa_cache(conversation_id, ts, question, answer, model, depth, embedding) VALUES(?,?,?,?,?,?,?)",
            (self.conversation_id, datetime.now().isoformat(timespec="seconds"), question, answer, self.last_model, self.last_depth, vec))
        self.store.conn.commit()

    # ---------- 結束萃取 ----------
    def finish(self) -> dict[str, Any]:
        """對話結束：摘要 + 補記漏掉的筆記/詞彙/事件/回饋。"""
        if not any(m["role"] == "user" for m in self.messages):
            return {"summary": None, "written": {}}
        msgs = [{"role": m["role"], "content": m["content"]} for m in self.messages
                if m["role"] in ("system", "user", "assistant") and m.get("content")
                and not str(m.get("content", "")).startswith("【訓練前資料包】")]
        recorded = [t for t in self.tool_log if t["name"] in ("record_note", "record_event", "record_interest", "log_feedback")]
        lines = [f"- {t['name']}: {json.dumps(t['args'], ensure_ascii=False)}" for t in recorded]
        lines += [f"- 既有事件: {e.get('kind')} {e.get('title')} {e.get('start_date')} (priority {e.get('priority')})"
                  for e in self.tools.ctx.list_events(active_only=True)]
        already = ("\n已記錄：\n" + "\n".join(lines)) if lines else "\n已記錄：無"
        before = self._snapshot(self.extract_llm)
        reply = self.extract_llm.chat(msgs + [{"role": "user", "content": EXTRACT_PROMPT + already}])
        self._log_call(self.extract_llm, before, "extract")
        text = reply.get("content", "").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0] if "```" in text else text.strip("`")
            text = text.replace("json\n", "", 1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("萃取結果不是 JSON: %s", text[:200])
            data = {"summary": text[:500]}
        written: dict[str, int] = {"notes": 0, "interests": 0, "events": 0}
        # 每一項各自包起來：模型填錯一個欄位不能讓整個 finish（摘要、其他筆記）失敗
        for n in data.get("notes") or []:
            try:
                if n.get("content"):
                    r = self.tools.record_note(n.get("category") or "other", n["content"], source=f"conversation:{self.conversation_id}")
                    written["notes"] += 0 if r.get("duplicate") else 1
            except Exception as e:  # noqa: BLE001
                log.warning("萃取筆記寫入失敗：%s (%s)", e, n)
        touched = {t["args"].get("term", "").strip().lower() for t in recorded if t["name"] == "record_interest"}
        for i in data.get("interests") or []:
            try:
                if i.get("term") and i["term"].strip().lower() not in touched:
                    self.tools.record_interest(i["term"], i.get("definition"))
                    written["interests"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("萃取名詞寫入失敗：%s (%s)", e, i)
        for e in data.get("events") or []:
            try:
                if e.get("title") and e.get("kind"):
                    r = self.tools.record_event(e["kind"], e["title"], e.get("start_date"), e.get("end_date"), e.get("priority"), e.get("notes"))
                    written["events"] += 0 if r.get("duplicate") else 1
            except Exception as ex:  # noqa: BLE001
                log.warning("萃取事件寫入失敗：%s (%s)", ex, e)
        fb = data.get("feedback") or {}
        fb_done = any(t["name"] == "log_feedback" for t in recorded)
        if not fb_done and any(fb.get(k) is not None for k in ("rpe", "feeling", "pain", "sick")) and self.activity_id:
            try:
                self.tools.log_feedback(self.activity_id, rpe=fb.get("rpe"), feeling=fb.get("feeling"), pain=fb.get("pain"), sick=fb.get("sick"))
                written["feedback"] = 1
            except Exception as e:  # noqa: BLE001
                log.warning("萃取回饋寫入失敗：%s (%s)", e, fb)
        summary = data.get("summary")
        usage = getattr(self.llm, "usage", None)
        if usage:
            usage = {**usage, "cost_usd": estimate_cost(usage, getattr(self.llm, "model", ""))}
            du = getattr(self.deep_llm, "usage", None) if self.deep_llm else None
            if du and du.get("calls"):
                dc = estimate_cost(du, getattr(self.deep_llm, "model", ""))
                usage["deep"] = {**du, "model": getattr(self.deep_llm, "model", None), "cost_usd": dc}
                if usage["cost_usd"] is not None and dc is not None:
                    usage["cost_usd"] = round(usage["cost_usd"] + dc, 4)
            eu = getattr(self.extract_llm, "usage", None)
            if self.extract_llm is not self.llm and eu:
                ec = estimate_cost(eu, getattr(self.extract_llm, "model", ""))
                usage["extract"] = {**eu, "cost_usd": ec}
                if usage["cost_usd"] is not None and ec is not None:
                    usage["cost_usd"] = round(usage["cost_usd"] + ec, 4)
        self.store.conn.execute("UPDATE conversations SET summary=?, usage=? WHERE id=?",
                                (summary, json.dumps(usage) if usage else None, self.conversation_id))
        self.store.conn.commit()
        if summary:
            self.store.conn.execute("UPDATE notes SET active=0 WHERE source=? AND content LIKE ?",
                                    (f"conversation:{self.conversation_id}", f"[對話 #{self.conversation_id} 摘要]%"))
            self.tools.record_note("insight", f"[對話 #{self.conversation_id} 摘要] {summary}", source=f"conversation:{self.conversation_id}")
        return {"summary": summary, "written": written, "usage": usage}


def run_cli_chat(store: Store, llm: LLM, activity_id: Optional[int] = None, include_weather: bool = True,
                 extract_llm: Optional[LLM] = None, continue_from: Optional[int] = None,
                 deep_llm: Optional[LLM] = None, mode: str = "auto") -> None:
    coach = Coach(store, llm, activity_id, include_weather=include_weather, extract_llm=extract_llm, continue_from=continue_from,
                  deep_llm=deep_llm, mode=mode)
    if deep_llm:
        print(f"模型：{getattr(llm, 'model', '?')}（簡單）/ {getattr(deep_llm, 'model', '?')}（深入），分流 {coach.mode}")
    if continue_from:
        print(f"接續對話 #{coach.conversation_id}（之前 {sum(1 for m in coach.messages if m.get('_seed'))} 則訊息已載入）")
    s = coach.preflight["this_session"]
    hs = coach.preflight["health_status"]
    print(f"\n[AI 教練] 這次訓練：{s.get('name')} {s.get('start_time', '')[:16]}  {s.get('km')}km {s.get('duration')}  HR {s.get('avg_hr')}")
    print(f"[AI 教練] 身體狀況：{hs.get('assessment')}  " + ("; ".join(hs.get("flags") or []) or "無異常"))
    print("[AI 教練] 輸入問題開始對話；/end 結束並萃取記憶；/tools 看工具呼叫紀錄\n")
    while True:
        try:
            text = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            text = "/end"
        if not text:
            continue
        if text in ("/end", "/quit", "/exit"):
            print("\n[AI 教練] 整理對話記憶中...")
            r = coach.finish()
            print(f"[AI 教練] 摘要：{r.get('summary')}")
            print(f"[AI 教練] 已寫入：{r.get('written')}")
            if r.get("usage"):
                u = r["usage"]
                print(f"[AI 教練] token：input {u['input']:,} + cached {u['cached_input']:,} / output {u['output']:,}  "
                      f"≈ US${u.get('cost_usd')}")
            break
        if text == "/tools":
            for t in coach.tool_log:
                print("  ", t["name"], json.dumps(t["args"], ensure_ascii=False))
            continue
        reply = coach.ask(text, on_tool=lambda n, a, r: print(f"   ⚙ {n}({json.dumps(a, ensure_ascii=False)})"))
        tag = f"[{coach.last_model}] " if deep_llm else ""
        print(f"\n教練 {tag}: {reply}\n")
