"""從命令列問教練一句話（不進互動模式），方便測試與自動化。

用法：
  python scripts/ask.py "今天騎車的訓練如何"                 # 真實資料庫，針對最近一次訓練，會寫入記憶
  python scripts/ask.py --copy "我如何安排 zone2"           # 用資料庫複本跑，真實記憶不受影響（測試用）
  python scripts/ask.py --continue 1 "我預計完賽 6.5 小時"  # 接續對話 #1
  python scripts/ask.py --activity 24787502307 "這次騎得如何"
  python scripts/ask.py --no-finish "..."                   # 不跑結束萃取（之後可用 --continue 接回）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from garmin_coach import config  # noqa: E402
from garmin_coach.coach import Coach, OpenAILLM  # noqa: E402
from garmin_coach.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--copy", action="store_true", help="用資料庫複本（不動真實記憶）")
    ap.add_argument("--continue", dest="cont", type=int, help="接續的對話 id")
    ap.add_argument("--activity", type=int, help="針對哪次訓練（預設最近一次）")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-finish", action="store_true", help="不跑結束萃取")
    ap.add_argument("--mode", choices=["auto", "simple", "deep"], default=None, help="分流模式（預設 OPENAI_ROUTER）")
    a = ap.parse_args()

    db = config.DB_PATH
    if a.copy:
        db = Path(tempfile.mkstemp(prefix="garmin_coach_copy_", suffix=".db")[1])
        shutil.copy(config.DB_PATH, db)
        print(f"[複本] {db}")
    llm = OpenAILLM(model=a.model) if a.model else OpenAILLM()
    extract = OpenAILLM(model=config.OPENAI_EXTRACT_MODEL) if config.OPENAI_EXTRACT_MODEL else None
    with Store(db) as store:
        mode = a.mode or config.OPENAI_ROUTER
        deep = OpenAILLM(model=config.OPENAI_DEEP_MODEL) if config.OPENAI_DEEP_MODEL and mode != "simple" else None
        coach = Coach(store, llm, a.activity, extract_llm=extract, continue_from=a.cont, deep_llm=deep, mode=mode)
        print(f"對話 #{coach.conversation_id}  activity={coach.activity_id}  分流={coach.mode}")
        t0 = time.time()
        reply = coach.ask(a.question, on_tool=lambda n, args, r: print(f"   ⚙ {n}({json.dumps(args, ensure_ascii=False)[:150]})"))
        ru = f" ♻️ 更新自 {coach.last_reuse['when']}（相似度 {coach.last_reuse['similarity']}）" if coach.last_reuse else ""
        print(f"\n[{time.time() - t0:.1f}s] 教練 [{coach.last_model} / {coach.last_depth}]{ru} 費用 US${coach.last_ask_cost:.4f}:\n{reply}\n")
        if not a.no_finish:
            r = coach.finish()
            print("摘要:", r.get("summary"))
            print("寫入:", r.get("written"))
            print("費用:", (r.get("usage") or {}).get("cost_usd"))


if __name__ == "__main__":
    main()
