#!/usr/bin/env bash
# 跑全部離線測試（不打 Garmin / OpenAI / Telegram）。都用資料庫複本，不會動到 data/garmin_coach.db。
set -eo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
echo "== pipeline（合成資料：normalize / store / context / brief）"; $PY tests/test_pipeline.py | tail -1
echo "== coach（假 LLM：工具迴圈 / 記憶寫回 / 結束萃取）";        $PY tests/test_coach.py 2>/dev/null | tail -1
echo "== telegram（假 LLM + 假 Telegram：指令流程）";             $PY tests/test_telegram.py 2>/dev/null | tail -1
echo "== router（假 LLM：分流規則 / 跨模型上下文 / 模型選擇）";      $PY tests/test_router.py 2>/dev/null | tail -1
echo "== reuse（假 LLM + 假向量：相似問題重用 / 重新 / 不相關）";       $PY tests/test_reuse.py 2>/dev/null | tail -1
echo "== web（假子模型：網路搜尋閘門 / 快取 / 完整紀錄 / API 解析）";     $PY tests/test_web.py 2>/dev/null | tail -1
