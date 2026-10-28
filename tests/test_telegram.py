import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
def _tmp(env, suffix):
    p = os.environ.get(env)
    return p or tempfile.mkstemp(prefix="garmin_coach_", suffix=suffix)[1]
import asyncio, json, sys
import garmin_coach.telegram_bot as tb
from garmin_coach.store import Store

class FakeLLM:
    model = "fake"; usage = {"input": 100, "cached_input": 0, "output": 50}
    def __init__(self, model=None): pass
    def chat(self, messages, tools=None):
        last = messages[-1]
        if last["role"] == "user" and last["content"].startswith("請根據以上整段對話"):
            return {"role": "assistant", "content": json.dumps({"summary": "測試摘要", "notes": [], "interests": [], "events": [], "feedback": {}})}
        if last["role"] == "user" and tools:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "search_knowledge", "arguments": json.dumps({"query": "tapering", "k": 2})}}]}
        return {"role": "assistant", "content": "**結論：** 減量就好\n- 項目一\n### 課表\n- **10/21** 游 1 km"}

tb.OpenAILLM = FakeLLM
SYNCS = []
def fake_sync(self, days=3):      # 開新對話會自動同步 Garmin；測試不打 Garmin
    SYNCS.append(days); self.last_sync = __import__("time").time(); return "同步完成：每日 3 天、訓練 0 筆已更新\n最新：x"
tb.CoachService.sync = fake_sync
from tests.synth import ensure_test_db
DB = ensure_test_db(_tmp("TEST_DB", ".db"))
tb.Store = lambda: Store(DB)

class FakeChat:
    id = 777
    def __init__(self): self.sent = []
    async def send_message(self, text, parse_mode=None): self.sent.append((parse_mode, text))
    async def send_chat_action(self, *a, **k): pass
class FakeUser:
    def __init__(self, uid, username=None): self.id = uid; self.username = username
class FakeUpdate:
    def __init__(self, uid, text=""):
        self.effective_chat = FakeChat(); self.effective_user = FakeUser(uid)
        self.message = type("M", (), {"text": text})()
class Ctx:
    def __init__(self, args=None): self.args = args or []

async def main():
    bot = tb.CoachBot("123456:ABCDEF", {42}, 30)
    assert not hasattr(bot, "cmd_sync"), "/sync 已移除，改為開新對話自動同步"
    # 未授權
    u = FakeUpdate(1, "hi"); await bot.on_text(u, Ctx()); print("unauth:", u.effective_chat.sent[-1][1][:40])
    # 授權：第一句自動開新對話
    u = FakeUpdate(42, "我該怎麼減量"); await bot.on_text(u, Ctx())
    for pm, t in u.effective_chat.sent: print("sent", pm, "|", t.replace("\n", " / ")[:120])
    assert 777 in bot.svc.sessions and bot.svc.tool_log(777)[0]["name"] == "search_knowledge"
    assert SYNCS == [3] and "已同步 Garmin" in u.effective_chat.sent[0][1], "新對話要先自動同步"
    # /status /activities /tools
    for fn in (bot.cmd_status, bot.cmd_activities, bot.cmd_tools):
        u = FakeUpdate(42); await fn(u, Ctx()); print(fn.__name__, "|", u.effective_chat.sent[-1][1].replace("\n", " / ")[:110])
    # /end
    u = FakeUpdate(42); await bot.cmd_end(u, Ctx()); print("end |", u.effective_chat.sent[-1][1].replace("\n", " / ")[:160])
    assert 777 not in bot.svc.sessions
    assert "摘要：測試摘要" in u.effective_chat.sent[-1][1] and "寫入" not in u.effective_chat.sent[-1][1] and "費用" not in u.effective_chat.sent[-1][1]
    # /continue 接最後一段，再 /new
    u = FakeUpdate(42); await bot.cmd_continue(u, Ctx()); print("continue |", u.effective_chat.sent[-1][1].replace("\n", " / ")[:80])
    assert SYNCS == [3], "接續對話不同步"
    u = FakeUpdate(42); await bot.cmd_new(u, Ctx()); print("new |", [t[:30] for _, t in u.effective_chat.sent])
    assert SYNCS == [3] and "剛同步過" in u.effective_chat.sent[-1][1], "冷卻內的 /new 不再打 Garmin"
    bot.svc.last_sync = 0
    u = FakeUpdate(42); await bot.cmd_new(u, Ctx()); assert SYNCS == [3, 3] and "已同步 Garmin" in u.effective_chat.sent[-1][1]
    def boom(self, days=3): raise RuntimeError("Garmin 需要 MFA")
    tb.CoachService.sync = boom; bot.svc.last_sync = 0
    u = FakeUpdate(42); await bot.cmd_new(u, Ctx()); print("new(sync fail) |", u.effective_chat.sent[-1][1].split("\n")[0])
    assert 777 in bot.svc.sessions and "同步失敗" in u.effective_chat.sent[-1][1], "同步失敗不擋對話"
    # 閒置自動結束路徑
    bot.svc.sessions[777]["last"] -= 10_000
    assert bot.svc.idle_chats(30) == [777]
    chat = FakeChat(); await bot._finish_and_report(chat, 777); print("idle finish |", chat.sent[-1][1][:40])
    print("TG TEST OK")
asyncio.run(main())
