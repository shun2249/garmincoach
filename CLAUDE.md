# CLAUDE.md — 給 Claude Code 的專案守則

先讀 `HANDOFF.md`（現況、模組地圖、坑）。README.md 是使用說明；`MANUAL.md` 是完整產品說明書（交接用），改功能後同步更新對應段落。

## 環境
- Python 3.10，一律用 `.venv/bin/python`；指令入口 `python -m garmin_coach ...`。
- 秘密只在 `.env`（gitignore）。**絕對不要**在輸出、commit、文件印出 Garmin 密碼、OpenAI key、Telegram token；要改就直接寫檔。
- `data/`、`briefs/`、`.env` 不進 git。
- 這個 repo 是公開的：文件與測試不要寫進任何個人資料（Telegram user id、bot 帳號、email、住家位置、真實對話內容）；範例用假資料。

## 改程式的規矩
- 介面文字（CLI、教練提示、Telegram 回覆）用繁體中文；程式註解中文即可。
- 改 `models.py` 欄位 → store 會自動 ALTER；改 `normalize.py` → 跑 `python -m garmin_coach reprocess` 回填。
- 改工具（`tools.py`）要同步改 `TOOL_SPECS` schema；模型常亂填欄位，驗證放在工具層。
- 網路搜尋（`search_web`）的閘門門檻（WEB_KB_FLOOR、WEB_CACHE_*）是用真實向量校準過的（HANDOFF §4.5），改 embedding 模型或知識庫大改後要重新校準。
- 改 `garmin_coach/*.py` 後 Telegram bot 要重啟才生效（`scripts/botctl.sh restart` 或 systemctl）。
- 每個功能做完 `tests/run.sh`，通過再 commit；commit 訊息英文一行為主。

## 測試與驗證
- 真實對話測試用 `scripts/ask.py --copy "..."`（資料庫複本），不要污染真實記憶。
- 驗證教練回答時，對照資料庫數字與 `data/knowledge.db` 的引用是否存在。
- Garmin API 有 429 風險：不要重複登入、不要大量重抓；`sync` 已有節流。

## Telegram bot 運維
- 同一 token 只能一個程序 polling。用 `scripts/botctl.sh status` 先看，再 start/restart。
- 不要用 `pkill -f "garmin_coach telegram"`（會殺到自己的 shell），用 pid 檔。
- 重啟期間進行中的對話會自動接回，正在等回覆的那一句會掉。

## 常見任務入口
- 同步：`sync daily` / `sync activities --days N` / `sync all`
- 看 AI 拿到什麼：`tool preflight`、`tool get_health_status`
- 記憶：`context show|note|event|interest`
- 知識庫：`kb search` / `kb build --topics X` / `kb stats`
- 網路搜尋：`web list` / `web show ID`；`web search "..." --reason ...` 會真的上網（花錢）並寫進真實資料庫，測試請用 `scripts/ask.py --copy`
