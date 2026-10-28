"""命令列介面：python -m garmin_coach <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .brief import build_brief, to_markdown
from .context import EVENT_KINDS, NOTE_CATEGORIES, Context
from .models import SessionFeedback, fmt_duration, fmt_pace
from .store import Store


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _client():
    from .client import GarminClient
    c = GarminClient()
    c.login()
    return c


# ---------- commands ----------

def cmd_login(args: argparse.Namespace) -> None:
    c = _client()
    print(f"登入成功：{c.api.full_name} ({c.api.display_name})，單位制 {c.api.unit_system}")
    print(f"token 存放於 {config.TOKEN_DIR}")


def cmd_sync(args: argparse.Namespace) -> None:
    from . import sync as S
    c = _client()
    with Store() as store:
        end = args.to or date.today()
        start = args.frm or (end - timedelta(days=args.days - 1))
        args.days_given = "--days" in sys.argv
        if args.what in ("all", "daily"):
            n = S.sync_daily(c, store, start, end, force=args.force)
            print(f"daily: {n} 天已更新")
        if args.what in ("all", "activities"):
            n = S.sync_activities(c, store, start, end, with_details=not args.no_details, force=args.force,
                                  external=None if not args.no_external else False, list_only=args.list_only)
            print(f"activities: {n} 筆已更新")
        if args.what == "details":
            n = S.fill_details(c, store, min_km=args.min_km, days=args.days if args.days_given else None, limit=args.limit,
                               external=None if not args.no_external else False)
            print(f"details: {n} 筆補齊細節")
        if args.what in ("all", "physio"):
            S.sync_physio(c, store, end)
            print("physio: 已更新")
        if args.what in ("all", "profile"):
            prof = S.sync_profile(c, store)
            print("profile: " + ", ".join(prof))
        if args.what in ("all", "history"):
            n = S.sync_history(c, store, days=max(args.days, 365))
            print(f"history: {n} 筆指標")
        print("資料庫統計:", store.counts())


def cmd_reprocess(args: argparse.Namespace) -> None:
    from .sync import reprocess
    with Store() as store:
        print(reprocess(store))


def cmd_activities(args: argparse.Namespace) -> None:
    with Store() as store:
        for s in store.recent_activities(args.limit, args.sport):
            print(f"{s.activity_id}  {s.start_time:%Y-%m-%d %H:%M}  {s.sport:<16} {(s.distance_m or 0)/1000:6.2f}km "
                  f"{fmt_duration(s.duration_s):>8} {fmt_pace(s.avg_pace_s_per_km):>9}  HR {s.avg_hr or '-':>3}  "
                  f"load {s.training_load or '-'}  drift {s.pa_hr_decoupling_pct if s.pa_hr_decoupling_pct is not None else '-'}%  {s.name}")


def cmd_show(args: argparse.Namespace) -> None:
    with Store() as store:
        if args.kind == "day":
            rec = store.get_daily(_date(args.key))
            _print_json(rec.model_dump(mode="json") if rec else {"error": "no data"})
        elif args.kind == "activity":
            s = store.get_activity(int(args.key))
            _print_json(s.model_dump(mode="json") if s else {"error": "no data"})
        elif args.kind == "physio":
            p = store.latest_physio()
            _print_json(p.model_dump(mode="json") if p else {"error": "no data"})


def cmd_log(args: argparse.Namespace) -> None:
    with Store() as store:
        aid = int(args.activity_id) if args.activity_id != "latest" else store.latest_activity().activity_id
        fb = SessionFeedback(activity_id=aid, rpe=args.rpe, feeling=args.feeling, pain=args.pain,
                             sick=True if args.sick else None, notes=args.notes)
        store.upsert_feedback(fb)
        _print_json(store.get_feedback(aid).model_dump(mode="json"))


def cmd_brief(args: argparse.Namespace) -> None:
    config.ensure_dirs()
    with Store() as store:
        aid = None if args.activity_id in (None, "latest") else int(args.activity_id)
        b = build_brief(store, aid, trend_days=args.trend_days)
    if args.json:
        text = json.dumps(b, ensure_ascii=False, indent=2, default=str)
    else:
        text = to_markdown(b)
    if args.out or args.save:
        out = Path(args.out) if args.out else config.BRIEF_DIR / f"{b['session']['activity_id']}.{'json' if args.json else 'md'}"
        out.write_text(text, encoding="utf-8")
        print(f"已寫入 {out}")
    else:
        print(text)


def cmd_context(args: argparse.Namespace) -> None:
    with Store() as store:
        ctx = Context(store)
        sub = args.ctx_cmd
        if sub == "show":
            _print_json(ctx.snapshot())
        elif sub == "set":
            try:
                value: Any = json.loads(args.value)
            except json.JSONDecodeError:
                value = args.value
            ctx.set_profile(args.key, value)
            _print_json(ctx.get_profile())
        elif sub == "unset":
            ctx.delete_profile(args.key)
        elif sub == "event":
            if args.event_cmd == "add":
                eid = ctx.add_event(args.kind, args.title, args.date, args.end, args.priority, args.notes)
                print(f"event #{eid} 已新增")
            elif args.event_cmd == "list":
                for e in ctx.list_events(active_only=not args.all):
                    print(f"#{e['id']:<3} [{e['kind']}] {e['title']}  {e['start_date'] or ''}~{e['end_date'] or ''}  "
                          f"{e['status']} {e['priority'] or ''}  {e['notes'] or ''}")
            elif args.event_cmd == "done":
                ctx.update_event(args.id, status="done")
            elif args.event_cmd == "update":
                ctx.update_event(args.id, title=args.title, start_date=args.date, end_date=args.end,
                                 priority=args.priority, notes=args.notes, status=args.status)
        elif sub == "note":
            if args.note_cmd == "add":
                nid = ctx.add_note(args.category, args.content, source="user", expires_at=args.expires)
                print(f"note #{nid} 已新增")
            elif args.note_cmd == "list":
                for n in ctx.list_notes(active_only=not args.all, category=args.category):
                    print(f"#{n['id']:<3} [{n['category']}] {n['created_at'][:10]} ({n['source']}) {n['content']}")
            elif args.note_cmd == "rm":
                ctx.deactivate_note(args.id)
        elif sub == "interest":
            if args.interest_cmd == "add":
                ctx.touch_interest(args.term, args.definition, args.notes)
            for i in ctx.list_interests():
                print(f"{i['term']:<20} x{i['count']:<3} 最近 {i['last_seen'][:10]}  {i['definition'] or ''}")


def cmd_cost(args: argparse.Namespace) -> None:
    from .telegram_bot import CoachService
    print(CoachService().cost())


def cmd_web(args: argparse.Namespace) -> None:
    from .telegram_bot import CoachService
    from .tools import Tools
    from .store import Store
    if args.web_cmd == "list":
        print(CoachService().web())
    elif args.web_cmd == "show":
        print(CoachService().web(str(args.id)))
    elif args.web_cmd == "search":
        with Store() as store:
            r = Tools(store).search_web(args.query, reason=args.reason or "手動查詢", ignore_cache=args.ignore_cache)
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))


def cmd_telegram(args: argparse.Namespace) -> None:
    from .telegram_bot import main
    main()


def cmd_chat(args: argparse.Namespace) -> None:
    from .coach import OpenAILLM, run_cli_chat
    llm = OpenAILLM(model=args.model)
    extract = OpenAILLM(model=config.OPENAI_EXTRACT_MODEL) if config.OPENAI_EXTRACT_MODEL else None
    mode = args.router or config.OPENAI_ROUTER
    deep = OpenAILLM(model=args.deep_model or config.OPENAI_DEEP_MODEL) if (args.deep_model or config.OPENAI_DEEP_MODEL) and mode != "simple" else None
    with Store() as store:
        aid = None if args.activity_id in (None, "latest") else int(args.activity_id)
        cont = None
        if args.cont is not None:
            if args.cont == "last":
                row = store.conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
                if not row:
                    raise SystemExit("還沒有任何對話可以接續")
                cont = int(row[0])
            else:
                cont = int(args.cont)
        run_cli_chat(store, llm, aid, include_weather=not args.no_weather, extract_llm=extract, continue_from=cont,
                     deep_llm=deep, mode=mode)


def cmd_tool(args: argparse.Namespace) -> None:
    """直接呼叫某個 AI 工具，看 AI 會拿到什麼。例：tool get_health_status '{"day":"2026-10-18"}'"""
    from .tools import TOOL_SPECS, Tools
    if args.name == "list":
        for t in TOOL_SPECS:
            f = t["function"]
            print(f"{f['name']:<26} {f['description'][:80]}")
        return
    kwargs = json.loads(args.args) if args.args else {}
    with Store() as store:
        t = Tools(store)
        result = t.preflight(**kwargs) if args.name == "preflight" else t.call(args.name, **kwargs)
    _print_json(result)


def cmd_kb(args: argparse.Namespace) -> None:
    from .knowledge import TOPICS, Embedder, KnowledgeBase, build
    kb = KnowledgeBase()
    try:
        if args.kb_cmd == "topics":
            for k, v in TOPICS.items():
                print(f"{k:<20} {v['label']}")
        elif args.kb_cmd == "build":
            topics = args.topics.split(",") if args.topics else ([] if args.retry_errors else None)   # 只給 --retry-errors 就不重搜
            r = build(kb, topics, per_topic_hq=args.hq, per_topic_general=args.general, since=args.since,
                      fulltext=not args.no_fulltext, embed=not args.no_embed, retry_errors=args.retry_errors)
            _print_json(r)
        elif args.kb_cmd == "embed":
            print("embedded:", kb.embed_missing(Embedder()))
        elif args.kb_cmd == "search":
            emb = Embedder() if not args.bm25_only else None
            for h in kb.search(args.query, k=args.k, topic=args.topic, embedder=emb):
                print(f"\n[{h['citation']}]  PMID {h['pmid']}  {', '.join(h['pub_types'][:2])}\n{h['title']}\n  ({h['section']}) {h['text'][:400].replace(chr(10), ' ')}...")
        elif args.kb_cmd == "stats":
            _print_json(kb.stats())
    finally:
        kb.close()


def cmd_stats(args: argparse.Namespace) -> None:
    with Store() as store:
        _print_json({"counts": store.counts(), "weekly_load": store.weekly_load(date.today(), args.weeks)})


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="garmin_coach", description="Garmin AI 教練資料層")
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("login", help="登入 Garmin 並快取 token").set_defaults(fn=cmd_login)

    s = sp.add_parser("sync", help="同步資料到本地 SQLite")
    s.add_argument("what", nargs="?", choices=["all", "daily", "activities", "physio", "profile", "history", "details"], default="all",
                   help="details = 替只有摘要的活動補抓分段/逐秒（配 --min-km / --days / --limit）")
    s.add_argument("--min-km", type=float, default=0, help="details：只補至少這麼長的活動")
    s.add_argument("--limit", type=int, default=None, help="details：最多補幾筆")
    s.add_argument("--days", type=int, default=14, help="往前幾天（預設 14）")
    s.add_argument("--from", dest="frm", type=_date, help="起始日 YYYY-MM-DD")
    s.add_argument("--to", type=_date, help="結束日 YYYY-MM-DD")
    s.add_argument("--force", action="store_true", help="已存在也重抓")
    s.add_argument("--no-details", action="store_true", help="不抓逐秒資料（較快，但沒有 HR drift）")
    s.add_argument("--no-external", action="store_true", help="不抓 Open-Meteo AQI")
    s.add_argument("--list-only", action="store_true", help="活動只存清單欄位，不抓細節（回補一年歷史用）")
    s.set_defaults(fn=cmd_sync)

    sp.add_parser("reprocess", help="用本地原始 JSON 重新整理（不打 API）").set_defaults(fn=cmd_reprocess)

    a = sp.add_parser("activities", help="列出最近活動")
    a.add_argument("--limit", type=int, default=15)
    a.add_argument("--sport")
    a.set_defaults(fn=cmd_activities)

    sh = sp.add_parser("show", help="顯示單筆結構化資料")
    sh.add_argument("kind", choices=["day", "activity", "physio"])
    sh.add_argument("key", nargs="?", default="")
    sh.set_defaults(fn=cmd_show)

    lg = sp.add_parser("log", help="記錄主觀回饋（RPE / 體感 / 疼痛 / 生病）")
    lg.add_argument("activity_id", help="活動 ID 或 latest")
    lg.add_argument("--rpe", type=int)
    lg.add_argument("--feeling")
    lg.add_argument("--pain")
    lg.add_argument("--sick", action="store_true")
    lg.add_argument("--notes")
    lg.set_defaults(fn=cmd_log)

    b = sp.add_parser("brief", help="產生訓練後整理包（丟給 AI 用）")
    b.add_argument("activity_id", nargs="?", default="latest")
    b.add_argument("--json", action="store_true")
    b.add_argument("--trend-days", type=int, default=7)
    b.add_argument("--out", help="輸出檔案路徑")
    b.add_argument("--save", action="store_true", help="存到 briefs/<id>.md")
    b.set_defaults(fn=cmd_brief)

    c = sp.add_parser("context", help="個人情境：基本資料 / 事件 / 筆記 / 關注詞彙")
    csp = c.add_subparsers(dest="ctx_cmd", required=True)
    csp.add_parser("show")
    cs = csp.add_parser("set"); cs.add_argument("key"); cs.add_argument("value")
    cu = csp.add_parser("unset"); cu.add_argument("key")
    ce = csp.add_parser("event"); esp = ce.add_subparsers(dest="event_cmd", required=True)
    ea = esp.add_parser("add"); ea.add_argument("kind", choices=EVENT_KINDS); ea.add_argument("title")
    ea.add_argument("--date"); ea.add_argument("--end"); ea.add_argument("--priority"); ea.add_argument("--notes")
    el = esp.add_parser("list"); el.add_argument("--all", action="store_true")
    ed = esp.add_parser("done"); ed.add_argument("id", type=int)
    eu = esp.add_parser("update"); eu.add_argument("id", type=int); eu.add_argument("--title"); eu.add_argument("--date")
    eu.add_argument("--end"); eu.add_argument("--priority"); eu.add_argument("--notes"); eu.add_argument("--status")
    cn = csp.add_parser("note"); nsp = cn.add_subparsers(dest="note_cmd", required=True)
    na = nsp.add_parser("add"); na.add_argument("category", choices=NOTE_CATEGORIES); na.add_argument("content"); na.add_argument("--expires")
    nl = nsp.add_parser("list"); nl.add_argument("--all", action="store_true"); nl.add_argument("--category")
    nr = nsp.add_parser("rm"); nr.add_argument("id", type=int)
    ci = csp.add_parser("interest"); isp = ci.add_subparsers(dest="interest_cmd", required=True)
    ia = isp.add_parser("add"); ia.add_argument("term"); ia.add_argument("--definition"); ia.add_argument("--notes")
    isp.add_parser("list")
    c.set_defaults(fn=cmd_context)

    ch = sp.add_parser("chat", help="與 AI 教練對話（需 OPENAI_API_KEY）")
    ch.add_argument("activity_id", nargs="?", default="latest")
    ch.add_argument("--model", help="簡單問題用的模型（預設 OPENAI_MODEL）")
    ch.add_argument("--deep-model", help="深入問題用的模型（預設 OPENAI_DEEP_MODEL）")
    ch.add_argument("--router", choices=["auto", "simple", "deep"], help="auto 分流 / simple 全用 --model / deep 全用 --deep-model")
    ch.add_argument("--no-weather", action="store_true")
    ch.add_argument("--continue", dest="cont", nargs="?", const="last", metavar="ID",
                    help="接續之前的對話（不給 ID 就接最後一段）")
    ch.set_defaults(fn=cmd_chat)

    co = sp.add_parser("cost", help="今天/本月 OpenAI 對話費用（llm_calls）")
    co.set_defaults(fn=cmd_cost)

    wb = sp.add_parser("web", help="網路搜尋紀錄/快取（web list | web show ID | web search \"...\" --reason ...）")
    wsp = wb.add_subparsers(dest="web_cmd", required=True)
    wsp.add_parser("list")
    wsh = wsp.add_parser("show"); wsh.add_argument("id", type=int)
    wse = wsp.add_parser("search", help="走完整閘門（快取→知識庫→上限）後才真的上網，會寫進真實資料庫")
    wse.add_argument("query"); wse.add_argument("--reason"); wse.add_argument("--ignore-cache", action="store_true")
    wb.set_defaults(fn=cmd_web)

    tg = sp.add_parser("telegram", help="啟動 Telegram bot（需 TELEGRAM_BOT_TOKEN）")
    tg.set_defaults(fn=cmd_telegram)

    tl = sp.add_parser("tool", help="直接呼叫 AI 工具看結果（tool list 列出全部；tool preflight 看訓練前資料包）")
    tl.add_argument("name")
    tl.add_argument("args", nargs="?", help="JSON 參數")
    tl.set_defaults(fn=cmd_tool)

    kb = sp.add_parser("kb", help="背景知識庫（論文 RAG）")
    ksp = kb.add_subparsers(dest="kb_cmd", required=True)
    ksp.add_parser("topics")
    kbld = ksp.add_parser("build"); kbld.add_argument("--topics", help="逗號分隔，預設全部")
    kbld.add_argument("--hq", type=int, default=120, help="每主題高品質（回顧/統合/RCT）篇數")
    kbld.add_argument("--general", type=int, default=60, help="每主題一般篇數")
    kbld.add_argument("--since", type=int, default=2016)
    kbld.add_argument("--no-fulltext", action="store_true"); kbld.add_argument("--no-embed", action="store_true")
    kbld.add_argument("--retry-errors", action="store_true", help="重試之前抓全文失敗（500）的論文")
    ksp.add_parser("embed")
    ks = ksp.add_parser("search"); ks.add_argument("query"); ks.add_argument("-k", type=int, default=5)
    ks.add_argument("--topic"); ks.add_argument("--bm25-only", action="store_true")
    ksp.add_parser("stats")
    kb.set_defaults(fn=cmd_kb)

    st = sp.add_parser("stats", help="資料庫統計與週負荷")
    st.add_argument("--weeks", type=int, default=6)
    st.set_defaults(fn=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("garminconnect").setLevel(logging.ERROR if not args.verbose else logging.DEBUG)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
