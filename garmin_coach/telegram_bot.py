"""Telegram 介面：同一個 Coach、同一套記錄邏輯，只是輸入窗口換到手機上。

設定（.env）：
  TELEGRAM_BOT_TOKEN=123456:ABC...      # 向 @BotFather 申請
  TELEGRAM_ALLOWED_USERS=12345678       # 允許對話的 Telegram user id，逗號分隔；空白時 bot 只會回你的 id
  TELEGRAM_IDLE_MINUTES=30              # 多久沒講話就自動結束對話並整理記憶
執行：
  python -m garmin_coach telegram
"""
from __future__ import annotations

import asyncio
import json
import functools
import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import config
from .coach import Coach, OpenAILLM
from .store import Store
from .tools import Tools

log = logging.getLogger(__name__)
TG_MAX = 3900          # Telegram 單則上限 4096，留餘裕

HELP = """我是你的 Garmin AI 教練。直接打字就能聊，問題會連同 Garmin 資料一起送給模型。
每次開新對話都會先自動同步 Garmin 最近 3 天的資料，練完直接打字就好。

/new [activity_id]  開新對話（先同步 Garmin，預設針對最近一次訓練）
/continue [對話id]  接續上一段對話
/end  結束對話，整理摘要與記憶
/status  今天的身體狀況
/activities  最近的訓練
/splits [activity_id]  某次訓練的每圈/每公里分段（不經過模型，免費）
/tools  這段對話呼叫過哪些工具
/cost  今天與本月的 AI 費用（含網路搜尋）
/web [id]  網路搜尋紀錄；給 id 看那一筆的完整內容
/model  看或切換分流：auto（自動）/ simple（全用便宜模型）/ deep（全用深入模型）
/help  這份說明

沒講話超過 %d 分鐘會自動結束並整理記憶；之後再打字就自動開新對話（會再同步一次）。"""


# ---------------------------------------------------------------- 文字格式
def _table_to_pre(lines: list[str]) -> str:
    """Markdown 表格 → 等寬對齊文字（手機上比一串 | 好讀）。"""
    rows = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):   # 分隔列
            continue
        rows.append(cells)
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    def w(sx: str) -> int:   # 全形字算 2 格
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in sx)
    widths = [max(w(r[i]) for r in rows) for i in range(ncol)]
    out = []
    for r in rows:
        out.append("  ".join(c + " " * (widths[i] - w(c)) for i, c in enumerate(r)).rstrip())
    return "<pre>" + "\n".join(out) + "</pre>"


def md_to_html(text: str) -> str:
    """把教練的 Markdown 轉成 Telegram 能吃的 HTML（粗體、標題、清單、行內 code、表格）。"""
    out: list[str] = []
    table: list[str] = []
    def flush() -> None:
        if table:
            out.append(_table_to_pre(table)); table.clear()
    for line in html.escape(text, quote=False).split("\n"):
        if line.strip().startswith("|") and line.strip().endswith("|"):
            table.append(re.sub(r"\*\*(.+?)\*\*", r"\1", line))   # 表格內不處理粗體
            continue
        flush()
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if m:
            line = f"<b>{m.group(1).strip()}</b>"
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        out.append(line)
    flush()
    return "\n".join(out)


def split_message(text: str, limit: int = TG_MAX) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def send_chat(chat: Any, text: str, as_html: bool = True) -> None:
    for chunk in split_message(text):
        if as_html:
            try:
                await chat.send_message(md_to_html(chunk), parse_mode=ParseMode.HTML)
                continue
            except BadRequest:
                pass
        await chat.send_message(chunk)


async def send(update: Update, text: str, as_html: bool = True) -> None:
    await send_chat(update.effective_chat, text, as_html)


class Typing:
    """長時間處理中持續送「輸入中…」。"""

    def __init__(self, chat: Any) -> None:
        self.chat = chat.effective_chat if isinstance(chat, Update) else chat
        self.task: Optional[asyncio.Task] = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.chat.send_chat_action(ChatAction.TYPING)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(4)

    async def __aenter__(self) -> "Typing":
        self.task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.task:
            self.task.cancel()


# ---------------------------------------------------------------- 教練服務
class CoachService:
    """所有 Store / Coach / Garmin 操作都在同一條 worker thread 執行（sqlite 連線不能跨執行緒）。"""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coach")
        self.store: Optional[Store] = None
        self.sessions: dict[int, dict[str, Any]] = {}     # chat_id -> {coach, last, tools_before}
        self.unauth_seen: dict[int, float] = {}            # 未授權者上次被回覆的時間（限流）
        self.modes: dict[int, str] = {}                    # chat_id -> auto/simple/deep（/model 設定，跨對話保留）
        self.last_sync: float = 0.0                        # 上次成功同步 Garmin 的 epoch 秒（開新對話時自動同步，冷卻內略過）

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, functools.partial(fn, *args, **kwargs))

    # ---- 以下全部在 worker thread 內執行 ----
    def _store(self) -> Store:
        if self.store is None:
            config.ensure_dirs()
            self.store = Store()
        return self.store

    def _llms(self) -> tuple[OpenAILLM, Optional[OpenAILLM], Optional[OpenAILLM]]:
        extract = OpenAILLM(model=config.OPENAI_EXTRACT_MODEL) if config.OPENAI_EXTRACT_MODEL else None
        deep = OpenAILLM(model=config.OPENAI_DEEP_MODEL) if config.OPENAI_DEEP_MODEL else None
        return OpenAILLM(), extract, deep

    def start(self, chat_id: int, activity_id: Optional[int] = None, continue_from: Optional[int] = None) -> str:
        sync_note = "" if continue_from else self.sync_if_due()
        llm, extract, deep = self._llms()
        mode = self.modes.get(chat_id, config.OPENAI_ROUTER)
        coach = Coach(self._store(), llm, activity_id, extract_llm=extract, continue_from=continue_from, deep_llm=deep, mode=mode)
        self.sessions[chat_id] = {"coach": coach, "last": time.time()}
        log.info("chat %s: %s #%s activity=%s", chat_id, "continue" if continue_from else "new", coach.conversation_id, coach.activity_id)
        s = coach.preflight["this_session"]
        hs = coach.preflight["health_status"]
        head = f"接續對話 #{coach.conversation_id}" if continue_from else f"新對話 #{coach.conversation_id}{sync_note}"
        return (f"{head}\n訓練：{s.get('name')} {str(s.get('start_time', ''))[:16]}  {s.get('km')} km {s.get('duration')}  HR {s.get('avg_hr')}\n"
                f"身體狀況：{hs.get('assessment')}  " + ("; ".join(hs.get("flags") or []) or "無異常"))

    def unfinished(self) -> list[tuple[int, float]]:
        """沒有摘要（沒跑過 /end）的對話：(id, 最後一則訊息的 epoch 秒)。"""
        rows = self._store().conn.execute(
            "SELECT c.id, MAX(m.created_at) FROM conversations c JOIN messages m ON m.conversation_id=c.id "
            "WHERE c.summary IS NULL AND EXISTS (SELECT 1 FROM messages u WHERE u.conversation_id=c.id AND u.role='user') "
            "GROUP BY c.id ORDER BY c.id").fetchall()
        out = []
        for cid, ts in rows:
            try:
                out.append((int(cid), datetime.fromisoformat(ts).timestamp()))
            except (TypeError, ValueError):
                continue
        return out

    def resume_id(self, idle_minutes: int) -> Optional[int]:
        """bot 重啟後：最近一段還沒結束、而且還在閒置時限內的對話，接回去而不是開新的。"""
        cutoff = time.time() - idle_minutes * 60
        cands = [cid for cid, ts in self.unfinished() if ts >= cutoff]
        return cands[-1] if cands else None

    def finish_stale(self, idle_minutes: int) -> list[int]:
        """bot 啟動時：把之前沒結束、已超過閒置時限的對話補跑一次結束萃取。"""
        cutoff = time.time() - idle_minutes * 60
        done = []
        for cid, ts in self.unfinished():
            if ts < cutoff:
                llm, extract, _ = self._llms()
                Coach(self._store(), llm, extract_llm=extract, continue_from=cid).finish()
                done.append(cid)
        return done

    def last_conversation_id(self) -> Optional[int]:
        row = self._store().conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None

    def ask(self, chat_id: int, text: str) -> tuple[str, list[str]]:
        sess = self.sessions[chat_id]
        coach: Coach = sess["coach"]
        before = len(coach.tool_log)
        reply = coach.ask(text)
        sess["last"] = time.time()
        used = [t["name"] for t in coach.tool_log[before:]]
        log.info("chat %s: ask %d 字 -> %d 字, model=%s(%s)%s, tools=%s, cost=US$%.4f, today=US$%.3f", chat_id, len(text), len(reply),
                 coach.last_model, coach.last_depth, f" reuse@{coach.last_reuse['when']}" if coach.last_reuse else "", used,
                 getattr(coach, "last_ask_cost", 0.0), coach.spent_today())
        return reply, used, coach.last_model, coach.last_depth, coach.last_reuse

    def set_mode(self, chat_id: int, mode: str) -> str:
        self.modes[chat_id] = mode
        sess = self.sessions.get(chat_id)
        if sess:
            sess["coach"].mode = mode
        return mode

    def cost(self) -> str:
        c = self._store().conn
        today = date.today().isoformat(); month = today[:7]
        def q(where: str, arg: str) -> tuple[int, float]:
            r = c.execute(f"SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE {where}", (arg,)).fetchone()
            return int(r[0]), float(r[1])
        n_t, c_t = q("ts >= ?", today); n_m, c_m = q("ts >= ?", month + "-01")
        budget = config.OPENAI_DAILY_BUDGET_USD
        lines = [f"今天：{n_t} 次呼叫，US${c_t:.3f}" + (f"（上限 US${budget:.2f}）" if budget else ""),
                 f"本月：{n_m} 次呼叫，US${c_m:.3f}"]
        for row in c.execute("SELECT model, COUNT(*), COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE ts >= ? GROUP BY model", (month + "-01",)):
            lines.append(f"  {row[0]}: {row[1]} 次, US${row[2]:.3f}")
        from .websearch import WebCache
        wc = WebCache(self._store())
        wt, wct, wh_t = wc.stats(today); wm, wcm, wh_m = wc.stats(month + "-01")
        lines.append(f"網路搜尋：今天 {wt} 次（上限 {config.WEB_SEARCH_DAILY_MAX}）US${wct:.3f}、快取命中 {wh_t}；本月 {wm} 次 US${wcm:.3f}、命中 {wh_m}（已含在上面）")
        lines.append("（只含對話、萃取、分流、網路搜尋；知識庫 embedding 另計，見 data/logs）")
        return "\n".join(lines)

    def web(self, arg: Optional[str] = None) -> str:
        """/web：列最近的網路搜尋紀錄；/web ID 看某一筆完整內容。"""
        from .websearch import WebCache
        wc = WebCache(self._store())
        if arg and arg.strip().isdigit():
            r = wc.get(int(arg))
            if not r:
                return f"找不到網路快取 #{arg}"
            cites = json.loads(r.get("citations") or "[]")
            return (f"#{r['id']} {r['ts'][:16]}  {r['query']}\n理由：{r.get('reason') or ''}\n"
                    f"知識庫相似度 {r.get('kb_max_sim')}（{r.get('kb_judged')}）  搜尋 {r.get('calls')} 次  US${r.get('cost_usd') or 0:.4f}  重用 {r.get('hits')} 次\n\n"
                    f"{r.get('summary') or ''}\n\n引用：\n" + "\n".join(f"- {c.get('title') or ''} {c.get('url')}" for c in cites))
        rows = wc.list(15, all_status=True)
        if not rows:
            return "還沒有網路搜尋紀錄。"
        out = []
        for r in rows:
            tag = {"done": "✅", "denied": "⛔", "failed": "❌"}.get(r["status"], "?")
            extra = f"US${r['cost_usd'] or 0:.3f} 重用{r['hits'] or 0}" if r["status"] == "done" else (r.get("deny_reason") or "")
            out.append(f"{tag} #{r['id']} {r['ts'][:10]} {r['query'][:40]}  {extra}")
        return "\n".join(out) + "\n\n/web ID 看完整內容"

    def finish(self, chat_id: int) -> Optional[dict[str, Any]]:
        sess = self.sessions.pop(chat_id, None)
        if not sess:
            return None
        r = sess["coach"].finish()
        log.info("chat %s: finish #%s written=%s cost=%s", chat_id, sess["coach"].conversation_id, r.get("written"), (r.get("usage") or {}).get("cost_usd"))
        return r

    def tool_log(self, chat_id: int) -> list[dict[str, Any]]:
        sess = self.sessions.get(chat_id)
        return list(sess["coach"].tool_log) if sess else []

    def idle_chats(self, minutes: int) -> list[int]:
        cutoff = time.time() - minutes * 60
        return [cid for cid, s in self.sessions.items() if s["last"] < cutoff]

    SYNC_COOLDOWN_S = 300   # 幾秒內已同步過就不再打 Garmin（連續 /new、結束後馬上開新對話）

    def sync_if_due(self) -> str:
        """開新對話前自動同步 Garmin 最近 3 天；失敗不擋對話，只在開頭註明。回傳要接在標題後面的短句。"""
        if time.time() - self.last_sync < self.SYNC_COOLDOWN_S:
            return "（Garmin 剛同步過）"
        try:
            msg = self.sync()
        except Exception as e:  # noqa: BLE001
            log.exception("自動同步失敗")
            return f"（Garmin 同步失敗，先用現有資料：{str(e)[:80]}）"
        return "（" + msg.split("\n", 1)[0].replace("同步完成：", "已同步 Garmin：") + "）"

    def sync(self, days: int = 3) -> str:
        from . import sync as S
        from .client import GarminClient

        def no_mfa() -> str:
            raise RuntimeError("Garmin 需要 MFA 驗證碼，請在電腦上執行 python -m garmin_coach login 後再試")

        client = GarminClient(prompt_mfa=no_mfa)
        client.login()
        store = self._store()
        end = date.today()
        start = end - timedelta(days=days - 1)
        nd = S.sync_daily(client, store, start, end)
        na = S.sync_activities(client, store, start, end)
        self.last_sync = time.time()
        latest = store.latest_activity()
        line = ""
        if latest:
            a = latest.model_dump() if hasattr(latest, "model_dump") else dict(latest)
            line = f"\n最新：{a.get('name')} {str(a.get('start_time', ''))[:16]}  {round((a.get('distance_m') or 0) / 1000, 2)} km  HR {a.get('avg_hr')}"
        return f"同步完成：每日 {nd} 天、訓練 {na} 筆已更新{line}"

    def status(self) -> str:
        hs = Tools(self._store()).get_health_status()
        t = hs.get("today") or {}
        if not t:
            return f"{hs.get('date')} 還沒有資料（開新對話時會自動同步 Garmin；要手動抓請在終端機跑 sync daily）"
        lines = [f"{hs['date']} 身體狀況：{hs['assessment']}",
                 f"RHR {t.get('rhr')}  HRV {t.get('hrv')} ({t.get('hrv_status')})  睡眠 {t.get('sleep_h')}h 分數 {t.get('sleep_score')}",
                 f"壓力 {t.get('stress')}  Body Battery {t.get('bb_low')}→{t.get('bb_high')}  準備度 {t.get('readiness')} ({t.get('readiness_level')})",
                 f"急性/慢性負荷 {t.get('acute_load')}/{t.get('chronic_load')}  ACWR {t.get('acwr')}  恢復 {t.get('recovery_h')}h"]
        if hs.get("flags"):
            lines.append("注意：" + "; ".join(hs["flags"]))
        return "\n".join(lines)

    def splits(self, activity_id: Optional[int] = None) -> str:
        from .models import fmt_pace, fmt_duration
        t = Tools(self._store())
        d = t.get_activity_detail(activity_id)
        if d.get("error"):
            return str(d["error"])
        sp = d.get("splits") or []
        head = f"{d.get('name')} {str(d.get('start_time',''))[:16]}  {d.get('km')} km {d.get('duration')}  HR {d.get('avg_hr')}/{d.get('max_hr')}"
        if not sp:
            return head + "\n（這筆沒有分段資料）"
        running = "run" in str(d.get("sport", ""))
        lines = [head, f"{'#':>2} {'距離':>6} {'時間':>6} {'配速' if running else '時速':>8} {'心率':>7} {'功率':>5}"]
        for x in sp[:40]:
            dist = x.get("distance_m") or 0
            pace = fmt_pace(x.get("avg_pace_s_per_km")) if running and x.get("avg_pace_s_per_km") else (f"{(x.get('avg_speed_mps') or 0)*3.6:.1f}" if x.get("avg_speed_mps") else "-")
            hr = f"{x.get('avg_hr') or '-'}/{x.get('max_hr') or '-'}"
            lines.append(f"{x.get('index', ''):>2} {dist:>5.0f}m {fmt_duration(x.get('duration_s') or 0):>6} {pace:>8} {hr:>7} {str(round(x['avg_power'])) if x.get('avg_power') else '-':>5}")
        if len(sp) > 40:
            lines.append(f"…共 {len(sp)} 段，只列前 40")
        return "<pre>" + html.escape("\n".join(lines)) + "</pre>"

    def activities(self, n: int = 8) -> str:
        rows = self._store().recent_activities(30)[:n]
        if not rows:
            return "沒有訓練紀錄"
        out = []
        for r in rows:
            a = r.model_dump() if hasattr(r, "model_dump") else dict(r)
            km = round((a.get("distance_m") or 0) / 1000, 1)
            out.append(f"{a['activity_id']}  {str(a.get('start_time', ''))[:16]}  {a.get('sport')}  {km} km  HR {a.get('avg_hr')}  {a.get('name') or ''}")
        return "\n".join(out)


# ---------------------------------------------------------------- Bot
class CoachBot:
    def __init__(self, token: str, allowed: set[int], idle_minutes: int, allowed_names: Optional[set[str]] = None) -> None:
        self.allowed = allowed
        self.allowed_names = {n.lower() for n in (allowed_names or set())}
        self.idle_minutes = idle_minutes
        self.svc = CoachService()
        self._idle_task: Optional[asyncio.Task] = None
        self.app = Application.builder().token(token).post_init(self._post_init).post_shutdown(self._post_shutdown).build()
        for cmd, fn in [("start", self.cmd_help), ("help", self.cmd_help), ("new", self.cmd_new),
                        ("continue", self.cmd_continue), ("end", self.cmd_end), ("status", self.cmd_status),
                        ("activities", self.cmd_activities), ("tools", self.cmd_tools), ("cost", self.cmd_cost), ("model", self.cmd_model),
                        ("splits", self.cmd_splits), ("web", self.cmd_web)]:
            self.app.add_handler(CommandHandler(cmd, fn))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    async def _post_init(self, app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("new", "開新對話（先同步 Garmin）"),
            BotCommand("continue", "接續上一段對話"), BotCommand("end", "結束對話並整理記憶"),
            BotCommand("status", "今天身體狀況"), BotCommand("activities", "最近的訓練"),
            BotCommand("tools", "這段對話用過的工具"), BotCommand("cost", "今天/本月 AI 費用"), BotCommand("model", "分流 auto/simple/deep"),
            BotCommand("splits", "某次訓練的分段表（免費）"), BotCommand("web", "網路搜尋紀錄 / 快取"), BotCommand("help", "說明"),
        ])
        self._idle_task = asyncio.create_task(self._idle_loop())   # 保留參考，避免被 GC
        try:
            stale = await self.svc.run(self.svc.finish_stale, self.idle_minutes)
            if stale:
                log.info("啟動時補結束了之前未整理的對話：%s", stale)
        except Exception:  # noqa: BLE001
            log.exception("補結束舊對話失敗")
        log.info("Telegram bot 已啟動，允許的使用者：%s", (sorted(self.allowed) + sorted("@" + n for n in self.allowed_names)) or "（尚未設定）")

    async def _post_shutdown(self, app: Application) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        self.svc.pool.shutdown(wait=False)

    # ---- 權限 ----
    async def _guard(self, update: Update) -> bool:
        user = update.effective_user
        if user and (user.id in self.allowed or (user.username or "").lower() in self.allowed_names):
            if user.id not in self.allowed:
                log.info("使用者 @%s 以 username 通過，數字 id 是 %s（建議改填 id）", user.username, user.id)
            return True
        uid = user.id if user else "?"
        now = time.time()
        if isinstance(uid, int) and now - self.svc.unauth_seen.get(uid, 0) < 3600:   # 同一個陌生人一小時只回一次
            log.warning("拒絕未授權的使用者 %s（已限流，不回覆）", uid)
            return False
        if isinstance(uid, int):
            self.svc.unauth_seen[uid] = now
        if not self.allowed and not self.allowed_names:
            await update.effective_chat.send_message(
                f"你的 Telegram id 是 {uid}。\n請把它寫進 .env 的 TELEGRAM_ALLOWED_USERS={uid}，重新啟動 bot 後就能對話。")
        else:
            await update.effective_chat.send_message(f"這個 bot 只服務設定好的使用者（你的 id：{uid}）。")
        log.warning("拒絕未授權的使用者 %s", uid)
        return False

    # ---- 指令 ----
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await send(update, HELP % self.idle_minutes, as_html=False)

    async def _end_if_active(self, update: Update, note: str) -> None:
        chat_id = update.effective_chat.id
        if chat_id in self.svc.sessions:
            await update.effective_chat.send_message(note)
            await self._finish_and_report(update.effective_chat, chat_id)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        aid = int(context.args[0]) if context.args and context.args[0].isdigit() else None
        await self._end_if_active(update, "先結束目前的對話…")
        try:
            async with Typing(update):
                msg = await self.svc.run(self.svc.start, update.effective_chat.id, aid)
        except Exception as e:  # noqa: BLE001
            log.exception("start 失敗")
            msg = f"開新對話失敗：{e}"
        await send(update, msg, as_html=False)

    async def cmd_continue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        if chat_id in self.svc.sessions and not context.args:
            await update.effective_chat.send_message("目前已經在對話中，直接打字就好。")
            return
        await self._end_if_active(update, "先結束目前的對話…")
        cid = int(context.args[0]) if context.args and context.args[0].isdigit() else await self.svc.run(self.svc.last_conversation_id)
        if not cid:
            await update.effective_chat.send_message("還沒有任何對話可以接續，用 /new 開始。")
            return
        try:
            async with Typing(update):
                msg = await self.svc.run(self.svc.start, chat_id, None, cid)
        except Exception as e:  # noqa: BLE001
            log.exception("continue 失敗")
            msg = f"接續失敗：{e}"
        await send(update, msg, as_html=False)

    async def _finish_and_report(self, chat: Any, chat_id: int) -> None:
        try:
            async with Typing(chat):
                r = await self.svc.run(self.svc.finish, chat_id)
        except Exception as e:  # noqa: BLE001
            log.exception("finish 失敗")
            await chat.send_message(f"整理記憶失敗：{e}")
            return
        if not r:
            await chat.send_message("目前沒有進行中的對話。")
            return
        if not r.get("summary") and not r.get("written"):
            await chat.send_message("對話已結束（這段沒有講到東西，不用整理）。")
            return
        lines = ["對話已結束。"]          # 寫入了什麼、花了多少只進 log 與 /cost，不在這裡顯示
        if r.get("summary"):
            lines.append(f"摘要：{r['summary']}")
        await send_chat(chat, "\n".join(lines), as_html=False)

    async def cmd_end(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await self._finish_and_report(update.effective_chat, update.effective_chat.id)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await send(update, await self.svc.run(self.svc.status), as_html=False)

    async def cmd_activities(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await send(update, await self.svc.run(self.svc.activities), as_html=False)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        cur = self.svc.modes.get(chat_id, config.OPENAI_ROUTER)
        arg = (context.args[0].lower() if context.args else "")
        alias = {"luna": "simple", "sol": "deep", "cheap": "simple", "auto": "auto", "simple": "simple", "deep": "deep"}
        if arg in alias:
            cur = self.svc.set_mode(chat_id, alias[arg])
        await update.effective_chat.send_message(
            f"分流模式：{cur}\n簡單問題 → {config.OPENAI_MODEL}\n深入問題 → {config.OPENAI_DEEP_MODEL or '（未設定，全用簡單模型）'}\n"
            f"改用 /model auto、/model simple（全用便宜）、/model deep（全用深入）")

    async def cmd_splits(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        aid = int(context.args[0]) if context.args and context.args[0].isdigit() else None
        text = await self.svc.run(self.svc.splits, aid)
        for chunk in split_message(text):
            try:
                await update.effective_chat.send_message(chunk, parse_mode=ParseMode.HTML)
            except BadRequest:
                await update.effective_chat.send_message(re.sub(r"</?pre>", "", chunk))

    async def cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await send(update, await self.svc.run(self.svc.cost), as_html=False)

    async def cmd_web(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            arg = context.args[0] if context.args else None
            await send(update, await self.svc.run(self.svc.web, arg), as_html=False)

    async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        log_ = self.svc.tool_log(update.effective_chat.id)
        if not log_:
            await update.effective_chat.send_message("這段對話還沒呼叫過工具。")
            return
        import json
        await send(update, "\n".join(f"{t['name']} {json.dumps(t['args'], ensure_ascii=False)[:150]}" for t in log_), as_html=False)

    # ---- 一般訊息 ----
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()
        if not text:
            return
        try:
            async with Typing(update):
                if chat_id not in self.svc.sessions:
                    rid = await self.svc.run(self.svc.resume_id, self.idle_minutes)
                    head = await self.svc.run(self.svc.start, chat_id, None, rid)
                    await send(update, head + ("\n（上次沒結束，先接回來）" if rid else ""), as_html=False)
                reply, used, model, depth, reuse = await self.svc.run(self.svc.ask, chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.exception("ask 失敗")
            await update.effective_chat.send_message(f"出錯了：{e}")
            return
        parts = []
        if used:
            counts: dict[str, int] = {}
            for u in used:
                counts[u] = counts.get(u, 0) + 1
            parts.append("🔧 " + ", ".join(f"{k}×{v}" if v > 1 else k for k, v in counts.items()))
        if config.OPENAI_DEEP_MODEL and model:
            parts.append(("🧠 " if depth == "deep" else "⚡ ") + str(model).replace("gpt-", ""))
        if reuse:
            parts.append(f"♻️ 更新自 {reuse['when']} 的回答（相似度 {reuse['similarity']:.2f}）；要重排就說「重新規劃」")
        footer = ("\n\n" + "  ".join(parts)) if parts else ""
        await send(update, reply + footer)

    # ---- 閒置自動結束 ----
    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            for chat_id in self.svc.idle_chats(self.idle_minutes):
                try:
                    chat = await self.app.bot.get_chat(chat_id)
                    await chat.send_message(f"超過 {self.idle_minutes} 分鐘沒對話，自動結束並整理記憶。")
                    await self._finish_and_report(chat, chat_id)
                except Exception:  # noqa: BLE001
                    log.exception("閒置結束失敗 chat=%s", chat_id)
                    self.svc.sessions.pop(chat_id, None)

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)   # 重啟期間的訊息不丟


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("請先在 .env 設定 TELEGRAM_BOT_TOKEN（向 @BotFather 申請）")
    if not config.OPENAI_API_KEY:
        raise SystemExit("請先在 .env 設定 OPENAI_API_KEY")
    CoachBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_ALLOWED_USERS, config.TELEGRAM_IDLE_MINUTES,
             allowed_names=config.TELEGRAM_ALLOWED_USERNAMES).run()


if __name__ == "__main__":
    main()
