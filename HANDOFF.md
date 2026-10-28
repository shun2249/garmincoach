# Garmin AI Coach — 交接文件

> 這是作者自己那套部署的維護筆記（現況數字、踩過的坑、決策紀錄），公開 repo 一併保留給想改程式的人參考。要從零架一套請先看 `README.md` 的快速開始與 `MANUAL.md`。

最後更新：2026-10-28（commit 見 `git log -1`）。這份文件的目的：任何人（或下一個 Claude Code 工作階段）拿到這個 repo，不用翻對話紀錄就能繼續維護與修改。README.md 是使用說明；這份是「為什麼這樣做、哪裡有坑、現在跑到哪」。

---

## 1. 專案一句話

把 Garmin Connect 的資料抓到本地 SQLite，整理成「生理恢復 / 個人情境 / 每次訓練」三層結構，讓 OpenAI 模型扮演教練：對話時自己用工具取資料、查論文知識庫、把有用的資訊（賽事、傷痛、體感、常問名詞）寫回記憶。輸入窗口有兩個：終端機 `chat` 與 Telegram bot，用的是同一個 `Coach` 類別。

## 2. 目前狀態（2026-10-20）

| 項目 | 狀態 |
|---|---|
| Garmin 同步 | 正常。token 快取在 `~/.garminconnect`，資料從 2025-04-22 起（fenix 8）。本地 60 天每日資料、341 筆活動（其中 65 筆有分段/區間/天氣/逐秒細節：10/7 之後全部、一年內 ≥15 km、近 90 天全部；其餘 276 筆只有清單摘要，`sync details` 可補）、783 筆指標歷史 |
| AI 教練 | 正常。分流：簡單題 `gpt-6-luna`、深入題 `gpt-6.1-sol`（`classify_depth` 用 luna 分類，`OPENAI_ROUTER` 可關），萃取 luna。簡單題約 US$0.003、深入題 US$0.02–0.07 |
| 知識庫 | 建好。2,285 篇論文（2016–2026，含運動醫學 4 主題）、664 篇有全文、21,543 段全部向量化，`data/knowledge.db` 217 MB。全文抓取失敗 142 篇（Europe PMC 500），`kb build --retry-errors` 可再補。公開版：`kb export` 打包成 69 MB 的 `knowledge.db.xz`，預設抓 GitHub Release（tag `kb-2026-10-28`，2026-10-28 尚未建）、404 就抓備援 `KB_PACK_FALLBACK_URL`（作者 Dropbox `garmincoach-kb/` 的分享連結，已實測），別人 `kb download`；論文清單 `kb/papers.tsv` 進 git |
| Telegram bot | 已上線（bot 帳號與白名單 user id 只在 `.env`）。由 `scripts/botctl.sh` 跑著；systemd 服務檔備好但未安裝（見 §7） |
| 記憶內容 | 賽事數筆（A/B/C 優先級）、筆記若干、名詞數個、對話十餘段；內容只在本機 `data/garmin_coach.db` |
| 網路搜尋 | 已做（2026-10-21）。`search_web` 工具：OpenAI Responses API 內建 `web_search` 的子請求（luna），閘門順序 快取 → 論文知識庫能否回答 → 每日 5 次 / 每段對話 3 次；結果（整理全文、搜尋動作、來源、引用、原始項目、用量、費用、向量）全部記在 `web_searches`。實測一次上網 US$0.04–0.05（4 次 web_search 呼叫，按 US$0.01/次估）、快取命中免費 |
| 測試 | `tests/run.sh` 七支離線測試全部通過 |

## 3. 目錄與模組地圖

```
garmin_project/
├── garmin_coach/            主程式（python -m garmin_coach ...）
│   ├── config.py            路徑、.env 讀取、所有環境變數
│   ├── client.py            GarminClient：登入（token→帳密→MFA）、safe() 包裝、fetch_* 各端點、dump_raw
│   ├── models.py            Pydantic v2 模型：DailyRecovery / TrainingSession / Split / SessionFeedback / PhysioSnapshot
│   ├── normalize.py         Garmin 原始 JSON → 模型。單位換算、心率漂移/解耦計算、profile/history 整理
│   ├── store.py             SQLite。資料表由模型自動產生欄位（新增欄位自動 ALTER）；記憶表在 CONTEXT_DDL
│   ├── sync.py              sync_daily / activities / physio / profile / history / all；reprocess()
│   ├── external.py          Open-Meteo：AQI/PM2.5、天氣展望（含日出日落）
│   ├── context.py           記憶層 API：profile(kv) / events / notes / interests
│   ├── tools.py             AI 可呼叫的 18 個工具 + TOOL_SPECS（function schema）+ preflight() 訓練前資料包；search_web 的閘門流程在這裡
│   ├── websearch.py         網路搜尋：WebSearcher（web_search 子請求、yes/no 判斷）、WebCache（web_searches 紀錄/快取/相似查詢）、提示詞
│   ├── pricing.py           PRICES 價目表與 estimate_cost（coach / websearch 共用）
│   ├── coach.py             SYSTEM_PROMPT、EXTRACT_PROMPT、OpenAILLM（Responses API、用量/費用）、Coach（對話迴圈、記憶寫回、finish 萃取、continue_from、分流）、run_cli_chat
│   ├── telegram_bot.py      Telegram 介面：CoachService（單一 worker thread）、CoachBot（指令、白名單、閒置自動結束、重啟接續）
│   ├── knowledge.py         論文知識庫：PubMed/Europe PMC 抓取、切段、embedding、FTS5+向量 RRF 檢索、build()
│   ├── brief.py             不用 API 時的「訓練後整理包」Markdown（貼到 ChatGPT 網頁用）
│   └── cli.py               argparse 指令入口
├── tests/                   離線測試（都用資料庫複本）：run.sh / test_pipeline / test_coach / test_telegram / test_router / test_reuse / test_web
├── scripts/                 ask.py（命令列問一句）、botctl.sh（bot 啟停）、mfa_login.py（非互動 MFA 登入）
├── deploy/                  garmin-coach-telegram.service（systemd user unit）
├── data/                    （gitignore）garmin_coach.db、knowledge.db、raw/（Garmin 原始 JSON）、knowledge_raw/、logs/
├── briefs/                  （gitignore）brief 輸出
├── .env                     （gitignore，chmod 600）所有秘密；.env.example 是範本
├── README.md                使用說明
└── HANDOFF.md               本文件
```

行數（2026-10-21）：normalize 626、tools 740、telegram_bot 620、coach 560、knowledge 520、cli 390、store 340、websearch 250。

## 4. 資料流

### 4.1 同步（Garmin → SQLite）
1. `client.fetch_daily(day)` 打 7 個端點（user_summary、sleep、hrv、rhr、stress、training_readiness、training_status、max_metrics），每個用 `safe()` 包，失敗回 None 並記 warning。
2. 原始 JSON 存 `data/raw/daily/YYYY-MM-DD.json`、`data/raw/activities/<id>/*.json`。**sync_daily 會先把舊 raw 裡有、這次沒抓到的區塊合併回來**（端點暫時逾時不會把好資料蓋成空值，2026-10-20 修）。
3. `normalize_*` 轉成模型 → `store.upsert_*`。今天與昨天永遠重抓，其他天已存在就略過（`--force` 全抓）。
4. 活動另外抓 splits、weather、hr/power zones、逐秒 details（算心率漂移與 Pa:Hr / Pw:Hr 解耦）、Open-Meteo AQI。
5. `reprocess` 用本地 raw 重新 normalize，不打 API（改了 normalize 之後用這個回填）。

### 4.2 對話（Coach）
1. `Coach.__init__` → `tools.preflight(activity_id)` 打包：athlete_profile、**health_status（永遠是今天）**、session_day_health（訓練不是今天才有）、athlete_context（賽事/筆記/名詞）、recent_training_10d、fitness_metrics、this_session、weather_outlook。這包以 system 訊息塞進對話開頭。
2. `ask()`：送 messages + TOOL_SPECS 給模型；有 tool_calls 就執行 `tools.call()` 回填，最多 8 輪；每則訊息即時寫入 `messages` 表。
3. 模型在對話中自己呼叫 record_note / record_event / update_event / record_interest / log_feedback 寫記憶（SYSTEM_PROMPT 規則）。
4. `finish()`：用 EXTRACT_PROMPT 跑一次萃取（摘要 + 補記漏掉的 notes/interests/events/feedback），把對話中已經用工具記過的傳給模型避免重複；**notes 只記 Garmin 沒有的東西**（體感、傷痛、情境、決定、教練判斷），不重抄數值。摘要存 conversations.summary，並寫一條 `[對話 #N 摘要]` insight 筆記（重跑 finish 會先停用舊的那條）。
5. `continue_from=id`：把該對話的 user/assistant 純文字載回上下文，沿用同一筆 conversation。

### 4.3 Telegram
- 所有 Store/Coach/Garmin 操作排進 `CoachService.pool`（單一 worker thread），因為 sqlite 連線不能跨執行緒。
- 每個 chat 一個 session（記憶體）。打字時沒有 session → 先看有沒有「未結束且在閒置時限內」的對話可接回（`resume_id`），否則開新對話。
- 開新對話（`start` 且非 continue）前 `sync_if_due()` 自動同步 Garmin 最近 3 天：`SYNC_COOLDOWN_S`=300 秒內同步過就略過；失敗只在標題註明，不擋對話。`/sync` 指令已移除（2026-10-28）。
- 結束訊息只回「對話已結束 + 摘要」；寫入筆數與費用只進 log 與 `/cost`。
- 閒置超過 `TELEGRAM_IDLE_MINUTES`（預設 30）自動 finish。啟動時 `finish_stale` 把之前沒結束、已超時的對話補跑萃取。
- 未授權使用者只會收到自己的 id。白名單支援數字 id 或 @username（建議用 id）。
- Markdown → Telegram HTML 只處理 `**粗體**`、`#` 標題、`-` 清單、行內 code；轉換失敗退回純文字。單則超過 3900 字會切段。

### 4.4 知識庫（RAG）
- `TOPICS`（knowledge.py）14 個主題各一組 PubMed 查詢（10 個訓練/營養/恢復 + 4 個運動醫學：injury_running / injury_cycling / injury_prevention / sports_medicine，2026-10-20 加）；`kb build` 依主題抓 HQ（review/meta/RCT/systematic）與一般各 N 篇，有 PMCID 的從 Europe PMC 抓 JATS 全文。
- 切段 2400 字/重疊 250；`text-embedding-3-small` 1536 維正規化 float32 存 BLOB；FTS5 做 BM25；查詢用 RRF 合併，每篇只回一段。
- 發布（2026-10-28）：`export_pack` 把複本的向量轉 float16（餘弦誤差 < 2e-4，top-5 幾乎不變）、`kb_fts` delete-all、VACUUM、xz → 69 MB；`import_pack` 下載（單檔或 `.001`… 分片接起來）、對 sha256（預設來源用程式裡釘住的 `KB_PACK_SHA256`，其他來源看網址旁的 `.sha256`）、解壓、`kb_fts` rebuild（3 秒）、`os.replace` 換檔。`_vec()` 依 BLOB 長度判斷 float16 / float32，所以下載版之後 `kb build` 新增的 float32 段可以混用。`write_manifest` / `build_from_manifest`：`kb/papers.tsv` 只有書目；重建時 efetch 每批 100 篇，清單標 `fulltext=none` 的不再向 Europe PMC 試。
- 模型透過 `search_knowledge(query, k, topic)` 查，把結論融進回答；2026-10-28 起規則 12 要求**不要**標作者年份或論文出處（之前是標「（第一作者 年份）」）。工具仍回傳 citation/pmid 給 get_paper 用。

### 4.5 網路搜尋（search_web，2026-10-21）
主模型只看得到 function tool `search_web(query, reason, ignore_cache, cache_id)`；真正的 web_search 在一個**獨立的子請求**裡（`WebSearcher.run`：WEB_SEARCH_MODEL + `{"type":"web_search"}`，`max_tool_calls=WEB_SEARCH_MAX_CALLS`，`include=web_search_call.action.sources`，不帶對話上下文），要求輸出可重用的資料整理（結論、逐條事實 [n]、資料日期、來源 URL）。`Tools.search_web` 的順序：
1. `cache_id` 給了就直接回那筆（完整內容，免費）。
2. 快取：`query_norm` 完全相同 → 命中；否則向量相似度 ≥ `WEB_CACHE_SIMILARITY`（0.85）命中，0.70–0.85 之間由 luna 用 CACHE_JUDGE_PROMPT 確認是不是同一件事。「未找到」的結果只算 `WEB_CACHE_DAYS_NOTFOUND`（14）天有效，其餘 `WEB_CACHE_DAYS`（90）天。
3. 閘門：查詢向量對論文知識庫的最高餘弦（`KnowledgeBase.nearest`）。< `WEB_KB_FLOOR`（0.45）→ 知識庫顯然沒有，放行；≥ 0.45 → 把最接近的三段給 luna（KB_JUDGE_PROMPT）判斷知識庫能不能回答，能 → 拒絕並把知識庫段落直接回給主模型（kb_results），主模型不必再查一次。判斷失敗時 ≥ 0.6 當有。
4. 上限：每天 `WEB_SEARCH_DAILY_MAX`（5）、每段對話 `WEB_SEARCH_PER_CONVERSATION`（3），超過回 denied 並附 related_cached。
5. 放行：子請求 → `web_searches` 寫一筆 status=done（summary、searches、citations、raw、用量、費用、embedding）；`llm_calls` 記 purpose=web（token 費 + 呼叫次數 × `WEB_SEARCH_CALL_USD`），閘門判斷記 purpose=web_gate。被拒絕/失敗的也寫 status=denied/failed（deny_reason），方便審計。
每個回傳都附 `related_cached`（相似度 ≥ 0.55 的其他舊查詢，含 cache_id），`search_knowledge` 的結果也會附 `web_cached`，讓主模型先用舊資料、不要拆新查詢。SYSTEM_PROMPT 規則 15 規定 search_web 是最後手段、回答要標來源與查詢日期、denied 不得換說法再試。
校準（2026-10-21，text-embedding-3-small 對 21,543 段）：訓練科學問題最高餘弦 0.55–0.72；賽事/產品/中文查詢 0.19–0.55；灰色地帶（穿戴裝置準確度、膠體成分、防寒衣規則）0.52–0.58 → 所以 floor 0.45 之上一律交 luna 判斷。同一需求不同寫法 0.86–0.89，同賽事不同面向 0.75–0.81，中文同義 0.75。

## 5. 資料庫

### data/garmin_coach.db
| 表 | 來源 | 重點欄位 |
|---|---|---|
| daily_recovery (60 欄) | 模型 DailyRecovery | hrv_*、resting_hr、sleep_*、stress_*、bb_*、readiness_*、recovery_time_hours、vo2max_*、acute/chronic_load、acwr、heat_acclimation_pct、steps… |
| activities (60 欄) | 模型 TrainingSession | 配速/功率/心率、hr_zone_s/power_zone_s（JSON）、hr_drift_pct、pa/pw_hr_decoupling_pct、跑步動態、TE、training_load、天氣（temp_c, apparent_temp_c…）、aqi_us、pm25、海拔、splits（JSON） |
| session_feedback | 使用者主觀 | activity_id, rpe, feeling, pain, sick, notes |
| physio_snapshots | 生理指標快照 | lt_hr, lt_pace_s_per_km, run_ftp_w, cycling_ftp_w, pred_*_s, endurance_score, hill_score |
| metric_history | 一年指標序列 | date, metric, value, extra（pred_5k_s、lt_pace_s_per_km、vo2max_run、heat_acclimation_pct…） |
| profile | kv | garmin_body、sleep_window、hr_zones、power_zones、latest_weight、devices、home_location（可手動 set） |
| events | 記憶 | kind(race/travel/family/work/illness/injury/constraint), title, start/end_date, status(active/done/cancelled), priority(A/B/C 或 NULL), notes |
| notes | 記憶 | category(injury/illness/status/recovery/race_progress/preference/insight), content, source, active, expires_at |
| interests | 記憶 | term, count, definition, notes, first/last_seen |
| conversations / messages | 對話紀錄 | model, summary, usage(JSON 含 cost_usd)；messages 含 tool 呼叫摘要 |
| llm_calls | 每次模型呼叫 | conversation_id, ts, model, purpose(chat/route/extract/web/web_gate), input/cached_input/output, cost_usd；`/cost` 與每日預算用 |
| qa_cache | 問答重用（預設關閉） | question, answer, depth, embedding |
| web_searches | 網路搜尋紀錄 + 快取 | ts, conversation_id, query, query_norm, reason, status(done/denied/failed), deny_reason, kb_max_sim, kb_judged, model, summary（整理全文）, searches(JSON 搜尋動作與來源), citations(JSON), raw(JSON 原始 output), input/cached_input/output/calls/cost_usd, embedding, hits, last_used |

欄位由 Pydantic 模型自動產生；**在 models.py 加欄位，store 啟動時會自動 ALTER TABLE**。巢狀資料存 JSON 字串。

### data/knowledge.db
`kb_papers`（pmid, title, abstract, authors(JSON), journal, year, doi, pmcid, pub_types, mesh, topics, fulltext_status）、`kb_chunks`（pmid, section, ord, text, embedding BLOB：自建 float32、下載版 float16，依長度判斷）、`kb_fts`（FTS5 外部內容表 + 觸發器；下載時重建）。

## 6. 設定（.env）

| 變數 | 說明 |
|---|---|
| GARMIN_EMAIL / GARMIN_PASSWORD | Garmin 帳密。只在 token 失效時用 |
| GARMIN_IS_CN | 0 |
| GARMINTOKENS | token 目錄，預設 ~/.garminconnect |
| GARMIN_WEATHER_UNITS | imperial（Garmin 天氣端點回 °F/mph，normalize 會轉） |
| EXTERNAL_AQI | 1 = 活動同步時打 Open-Meteo 抓 AQI |
| OPENAI_API_KEY | |
| OPENAI_MODEL | 預設 gpt-6-luna |
| OPENAI_REASONING | none/low/medium/high，空白用模型預設 |
| OPENAI_EXTRACT_MODEL | 結束萃取用的模型，空白沿用主模型 |
| OPENAI_DEEP_MODEL / OPENAI_ROUTER | 深入題模型（預設 gpt-6.1-sol）/ 分流 auto、simple、deep |
| OPENAI_DAILY_BUDGET_USD | 每日費用上限（預設 2），含對話、萃取、分流、網路搜尋 |
| REUSE_SIMILARITY / REUSE_DAYS | 問答重用（0 = 關閉，目前關閉） |
| PUBMED_EMAIL | PubMed 禮貌參數，可空白 |
| WEB_SEARCH_ENABLED | 1/0，網路搜尋總開關（預設 1） |
| WEB_SEARCH_MODEL | 執行 web_search 子請求的模型，預設同 OPENAI_MODEL（luna） |
| WEB_SEARCH_CONTEXT | low/medium/high，搜尋內容佔多少 context（預設 medium） |
| WEB_SEARCH_MAX_CALLS | 一次子請求最多幾次 web_search 呼叫（預設 3；實測回傳 4 個 web_search_call 項目，其中含 open_page） |
| WEB_SEARCH_DAILY_MAX / WEB_SEARCH_PER_CONVERSATION | 每天 / 每段對話最多放行幾次（預設 5 / 3；快取命中不算） |
| WEB_SEARCH_CALL_USD | 每個 web_search_call 的工具費估算（預設 0.01；OpenAI 實際計價要對帳單） |
| WEB_CACHE_DAYS / WEB_CACHE_DAYS_NOTFOUND | 快取有效天數（90）/「未找到」結果的有效天數（14） |
| WEB_CACHE_SIMILARITY / WEB_CACHE_MAYBE | 相似查詢直接命中的門檻（0.85）/ 交 luna 確認的下限（0.70） |
| WEB_KB_FLOOR | 知識庫最高相似度低於此直接放行（0.45），以上交 luna 判斷知識庫能否回答 |
| TELEGRAM_BOT_TOKEN | @BotFather 給的 |
| TELEGRAM_ALLOWED_USERS | 逗號分隔，數字 id 或 @username |
| TELEGRAM_IDLE_MINUTES | 預設 30 |

秘密只放 `.env`（gitignore、chmod 600）。**不要在任何輸出、commit、文件裡印出密碼、API key、bot token。**

## 7. 操作手冊

### 日常
```bash
cd ~/garmin_project
.venv/bin/python -m garmin_coach sync daily && .venv/bin/python -m garmin_coach sync activities --days 2
.venv/bin/python -m garmin_coach chat                 # 針對最近一次訓練；/end 結束
.venv/bin/python -m garmin_coach chat --continue      # 接上一段
.venv/bin/python scripts/ask.py "問題"                # 不進互動模式問一句（--copy 用複本測試）
.venv/bin/python -m garmin_coach context show         # 看記憶
.venv/bin/python -m garmin_coach context note list / event list / interest list
.venv/bin/python -m garmin_coach tool preflight       # 看 AI 對話前拿到的整包資料
.venv/bin/python -m garmin_coach kb search "tapering" -k 5
.venv/bin/python -m garmin_coach web list                 # 網路搜尋紀錄（✅ 放行 ⛔ 拒絕 ❌ 失敗）
.venv/bin/python -m garmin_coach web show 3               # 某一筆的完整整理與引用
.venv/bin/python -m garmin_coach web search "2026 IRONMAN 70.3 Kenting bike course" --reason "測試"   # 走完整閘門，會真的上網並寫進真實資料庫
```

### Telegram bot
- 沒裝 systemd：`scripts/botctl.sh start|stop|restart|status|log`（pid/log 在 `data/logs/`）。
- 裝 systemd（建議）：`deploy/garmin-coach-telegram.service` 檔頭 5 行指令；之後用 `systemctl --user restart garmin-coach-telegram`、`journalctl --user -u garmin-coach-telegram -f`。
- **同一個 token 只能有一個程序在 polling**，兩個會互搶（Telegram 回 Conflict）。botctl 會檢查。
- 改了 `garmin_coach/*.py` 要重啟 bot 才生效。重啟是安全的：進行中的對話會在下一句自動接回；只有「正在等模型回覆」那一句會掉，使用者重送即可。
- Garmin token 失效需要 MFA 時，bot 做不到，要回電腦跑 `python -m garmin_coach login`（互動）或 `scripts/mfa_login.py`（非互動）。

### 補舊活動的細節
```bash
python -m garmin_coach sync details --min-km 15 --days 365    # 一年內 15 km 以上
python -m garmin_coach sync details --days 90                 # 近 90 天全部
python -m garmin_coach sync details --limit 50                # 剩下的分批補（每筆約 6 次 Garmin 呼叫，小心 429）
```
一年歷史當初用 `--list-only` 回補，所以舊活動只有摘要；教練分析舊比賽或一年趨勢要分段時就要先補。

### 改了 normalize 之後
`python -m garmin_coach reprocess` 用本地 raw 重算，不打 API。

### 備份
`data/garmin_coach.db`（記憶都在這）定期備份；`knowledge.db` 可重建（`kb build`，embedding 約 US$0.11）或 `kb download --force` 抓回發布版。

### 發布知識庫（公開版）
知識庫有更新（加主題、增量 build）後：
1. `python -m garmin_coach kb export` → `data/export/knowledge.db.xz` + `.sha256` + 分片 `.001`、`.002`、`.003`（25 MiB 一片；約 25 秒，69 MB）。
2. GitHub 建 Release，tag 用 `kb-YYYY-MM-DD`，上傳 `.sha256` 加上單檔或全部分片（檔名不要改；`import_pack` 先抓單檔，404 就逐片抓到 404 為止再接起來）。分片是因為對話介面傳檔上限 30 MiB，沒有這個限制就傳單檔。
3. `config.py` 的 `KB_PACK_URL` 預設值改成新 tag、`KB_PACK_SHA256` 改成 export 印出的 sha256，`.env.example` 的範例跟著改。
4. `python -m garmin_coach kb manifest` 重寫 `kb/papers.tsv`，連同上面的改動一起 commit。
5. 備援：同一個檔也放到 Dropbox `~/Dropbox/garmincoach-kb/`，`dropbox sharelink` 產生連結、`dl=0` 改 `dl=1` 填進 `KB_PACK_FALLBACK_URL`（Release 還沒建或資產被刪時 `kb download` 會改抓這裡）。Dropbox 用戶端卡在「編列索引」時 `dropbox stop; dropbox start`。
別人更新：`kb download --force`，再重啟 bot。

## 8. 測試

```bash
tests/run.sh          # 三支全部跑，約 1–2 分鐘，不打任何外部 API
```
| 測試 | 內容 |
|---|---|
| tests/test_pipeline.py | 合成 Garmin JSON → normalize → store → context → brief；單位換算、解耦計算 |
| tests/test_coach.py | 假 LLM 走 Coach：多工具同時呼叫、工具結果回填、記憶寫回、finish 萃取 |
| tests/test_telegram.py | 假 LLM + 假 Telegram 物件走全部指令：白名單、自動開新對話、/status /activities /tools /end /continue /new、閒置結束 |
| tests/test_router.py | 分流規則、跨模型 raw items 轉換、模型選擇與 llm_calls 記帳 |
| tests/test_reuse.py | 問答重用（假向量）：相似命中、「重新」、不相關 |
| tests/test_web.py | 網路搜尋（假子模型 + 假向量）：知識庫擋下、放行並完整紀錄與費用、exact/相似/中間地帶快取、ignore_cache、cache_id、related_cached、未找到 TTL、每日與每段對話上限、關閉、失敗、Coach 整合、真實 API 回傳形狀解析 |
| tests/test_kb.py | 知識庫發布（假論文 + 假向量）：float16 載入與混用、export↔download 往返、sha256 驗證、FTS 重建、覆蓋保護、論文清單寫讀與照清單重建 |

都用 `data/garmin_coach.db` 的複本（TEST_DB 環境變數可指定）。**真實對話測試一律 `scripts/ask.py --copy`**，不要污染真實記憶。

## 9. 已知問題與限制

1. 知識庫約七成只有摘要（無開放全文）；142 篇全文抓取遇 Europe PMC 500，`kb build --retry-errors` 可補（只重試，不重搜）。注意 `kb build` 不帶 `--topics` 會重搜全部主題，PubMed 相關性排序會變，每次都可能多加幾百篇（2026-10-20 一次就 +566 篇，embedding 約 US$0.04）。
2. （已修 2026-10-20）教練講 taper 一直引 Furrer 2023 大型綜述：原因是 search_knowledge 的 topic 是硬過濾，減量論文被標在 recovery_monitoring，用 topic=endurance 查就只剩那篇 171 段的綜述。現在 topic 改為加分（×1.25）不排除，另依證據等級加權（統合分析/系統性回顧 ×1.3、RCT/共識 ×1.15），taper 查詢改回 Wang 2023 統合分析第一。
3. Garmin 沒有 LT1，只有乳酸閾值（LT2）；Zone 2 上限目前靠 Garmin 心率區間，之後可用心率漂移估。
4. Garmin 對短時間大量請求敏感（429）；`API_PAUSE_SECONDS=0.4`，不要重複登入。
5. Telegram 開新對話自動同步固定抓 3 天（`/sync` 已移除）；更久要回終端機。
6. 活動若 Garmin 沒給天氣（室內、部分舊活動），weather 欄位為空；Open-Meteo 歷史天氣回填還沒做。
7. 記憶筆記沒有自動過期清理（expires_at 有欄位，萃取很少填）；久了要手動 `context note rm`。
8. 兩個輸入窗口（終端機與 Telegram）的 session 是分開的，同時聊會各開一段對話。

## 10. 設計決策與踩過的坑（不要再踩）

- **Garmin 單位**：天氣 °F/mph；`recoveryTime` 是分鐘（曾誤當小時）；`lactateThresholdSpeed` 是 m/s ÷ 10（要 ×10，驗證 4:41/km 正確）；體重公克；步幅 cm。
- **MFA**：非互動環境 `input()` 會 EOF，用 `scripts/mfa_login.py` 等驗證碼檔案。
- **分流與跨模型上下文**：assistant 訊息帶 `_model`；`_to_input` 遇到別的模型產生的 `_raw_items` 會用 `_foreign_items` 轉換（丟 reasoning、function_call 去 id 留 call_id、message 轉文字），否則 API 會拒收。分流規則：≤6 字 simple；上一題 deep 且 4–15 字、像追問（那/也/可以…/呢/嗎）→ deep；其餘問 luna。
- **問答重用（預設關閉）**：`qa_cache` 表與 `Coach._find_similar/_same_request/_remember_answer` 已做好並有測試（tests/test_reuse.py），用向量初篩 + luna 確認同一請求，再由 luna 更新舊回答。2026-10-20 使用者決定先不用，`REUSE_SIMILARITY=0` 關閉；要開就設 0.5。
- **Responses API**：gpt-6 系列用 function tools + reasoning 必須走 `/v1/responses`，不是 chat completions。assistant 回合要把原始 output items（含 reasoning）原樣塞回（`_raw_items`）才能延續推理；`store=False`。
- **健康狀態要用今天的**：preflight 曾用「訓練當天」的準備度，隔兩天問規劃時就誤判；現在 health_status 永遠是今天，session_day_health 另附。
- **萃取不要重抄數值**：第一次真實對話把 RHR/HRV/功率抄成永久筆記；EXTRACT_PROMPT 已限制。
- **模型亂填欄位**：rpe=0/1、sick=false、priority=「未指定」、expires_at=""。工具層一律驗證：rpe 1–10（0 = 清除）、sick 只記 True、priority 只收 A/B/C、日期格式不對視為沒填。
- **摘要 vs 細節**：資料包的 recent_training_10d.sessions 只是摘要；教練曾在使用者問「週日訓練前 8 分鐘」時說「沒有逐段資料」而不去呼叫工具（2026-10-20）。已在規則 5、工具描述與 get_recent_training 的 note 寫明：分析任一筆訓練先 get_activity_detail（有每公里分段），問到某一段就 get_activity_timeseries。
- **分段表**：規則 14 要求分析單次訓練附分段表（穩定課每公里、間歇課每趟工作段）；Telegram 端 Markdown 表格會轉成 <pre> 等寬對齊，`/splits [activity_id]` 不經模型直接印分段。
- **主題範圍**：SYSTEM_PROMPT 規則 0 限制只回答運動/訓練/健康相關；曾被要求寫一萬字小說就真的寫了（2026-10-20）。萃取也排除無關內容。
- **名詞正規化**：`LT1（第一乳酸閾值）` → key `LT1`，長名當 definition，次數才會累計。
- **Telegram 重啟**：用 `>>` 追加 log；用 `>` 截斷會讓舊程序的尾巴寫出 NUL 空洞，grep 把 log 當二進位。`drop_pending_updates=False`，重啟期間的訊息不丟。
- **pkill -f 自殺**：在同一條 shell 用 `pkill -f "garmin_coach telegram"` 會把自己殺掉（命令列含同字串）；用 pid 檔。
- **sqlite 跨執行緒**：bot 所有 DB 操作走單一 worker thread。
- **測試用複本**：真實 `garmin_coach.db` 的記憶只放真的對話。
- **網路搜尋不要直接掛在主對話上**：OpenAI 內建 web_search 若直接給主模型，沒辦法程式化擋下、也拿不到它讀了什麼。所以包成 function tool，在獨立子請求裡跑，閘門與紀錄都在工具層（§4.5）。web_search 不回傳網頁原文，「完整紀錄」= 整理全文 + 搜尋動作/來源/引用 + 原始 output 項目。
- **模型會把同一賽事拆成多個查詢**（實測一題用了 2 次上網、第二段對話又查了「選手指南」）：所以回傳附 related_cached、每段對話上限 3 次、快取判斷把「之前已寫明找不到」也算命中。費用估算中 `calls` 包含 open_page 項目（保守）。

## 10a. 資安與費用控管（2026-10-20 審計後）

**秘密**：只在 `.env`（600、gitignore）。git 全歷史確認沒有 key/token/密碼（只有 .env.example 的佔位字串）。home 目錄本身是個沒有 remote 的 git repo，已在 `~/.git/info/exclude` 排除 `garmin_project/` 與 `.garminconnect/`（Garmin OAuth token 檔），避免有人在 home 下 `git add -A`。
**權限**：`data/` 700、兩個 .db 600、`~/.garminconnect/` 700、token 檔 600。log 不含秘密（httpx 的 INFO 已關，Telegram token 不會出現在 log）。
**曾經過聊天的秘密**：Garmin 密碼、OpenAI key、Telegram token 都曾貼在 Claude Code 對話裡（逐字稿在 `~/.claude/projects/…jsonl`，600）。如果要徹底，去各平台輪換一次。
**Telegram 攻擊面**：所有 handler 先過 `_guard`（數字 id 白名單），未授權者只會收到自己的 id，且同一人一小時只回一次（限流）；任何模型呼叫都在白名單之後，陌生人無法燒你的 OpenAI 額度。Telegram 訊息上限 4096 字，工具回合上限 8。
**OpenAI 用量控管**：每一次模型呼叫都記在 `llm_calls`（token、費用、用途）。`OPENAI_DAILY_BUDGET_USD`（預設 2）：當天累計達上限就拒絕再呼叫模型，含工具迴圈中途。前幾輪的工具輸出會截到 400 字再送模型（舊的逐秒資料、論文段落不會每輪重送）。`/cost`（Telegram）與 `python -m garmin_coach cost` 看今天/本月費用。embedding 費用不在 llm_calls，看 `data/logs/kb_*.log` 的 tokens。另外建議在 platform.openai.com 設 project 的 monthly budget 當第二道防線。
**input 瘦身（2026-10-20）**：費用 75% 來自未快取的 input，主要是工具回傳。search_knowledge 預設 k=4、每段截到 900 字（留頭尾），要細節用 get_paper(pmid)；get_sport_history 最近 8 筆、效率趨勢 12 點；get_fitness_metrics 取樣 4 點、資料包裡不重複附區間。資料包 27.8k → 24k 字，檢索 13.4k → 4.6k 字，指標 12.7k → 4.6k 字。
**程式面**：SQL 只有內部表名/欄名用 f-string，使用者值全部參數化；工具分派只允許 TOOL_SPECS 內的名稱；所有 HTTP 呼叫有 timeout（Open-Meteo 10s、PubMed 120s）；PubMed 的 email 參數不再送 Garmin 帳號（`PUBMED_EMAIL` 可空）。
**對外連線**：api.openai.com、api.telegram.org、connect.garmin.com、open-meteo.com、eutils.ncbi.nlm.nih.gov、www.ebi.ac.uk。送出的個資：Garmin 資料摘要與對話內容到 OpenAI（`store=False`，不留存）、活動經緯度到 Open-Meteo。

## 11. 變更紀錄（git log，舊→新）

| commit | 內容 |
|---|---|
| 1e74ea3 | 資料層、SQLite、記憶層、訓練後整理包 |
| 5d38850 | 用真實 Garmin 資料修單位；熱適應；reprocess |
| 74c4de2 | AI 教練三層工具、chat 迴圈、profile/history 同步 |
| e5aebc8 / 27192bf | 萃取去重；Responses API；token/費用追蹤；便宜萃取模型 |
| cb9f7b6 | 預設 gpt-6-luna；rpe=0 清除；名詞正規化 |
| 694ad9e / d9baa14 | 論文知識庫 RAG、search_knowledge、--retry-errors |
| b04af9e | preflight 健康狀態永遠是今天 |
| 8540f10 | 萃取筆記只記 Garmin 沒有的 |
| 4050ca2 | chat --continue 接續對話 |
| a002164 / 2fa0dfb / 91f9b7a | Telegram bot；@username 白名單；重啟接續、補結束舊對話、sync 保留舊資料 |
| 23cb966 | update_event 可設 priority/title；priority 限 A/B/C |
| 0449fa0 … 62d2eb8 | kb 主題加分與證據等級加權；記憶合併；sync details 補細節；finish 容錯與 RPE 解析 |
| ec50cd2 | 網路搜尋 search_web：websearch.py、pricing.py、web_searches 表、閘門/快取/紀錄、/web 與 `web` 指令、tests/test_web.py |
| 90812be / 42d080c | 回覆篇幅 300–1500；MANUAL.md 產品說明書 |
| 045c85e | 開新對話自動 sync（移除 /sync）；回答不標論文出處；閒置 30 分鐘；結束訊息只留摘要 |
| （公開版） | 公開到 GitHub：README 改成新使用者導向、MIT 授權、tests/synth.py 合成資料庫（沒有真實 DB 也能跑測試）、WEB_SEARCH_COUNTRY/TIMEZONE 可設定、文件去除個人識別資料、git 歷史從單一 commit 重新開始；知識庫發布：`kb export` / `kb download`（float16 打包 69 MB，GitHub Release）、`kb manifest` / `kb build --manifest`（`kb/papers.tsv` 論文清單）、tests/test_kb.py |

## 11a. 進行中的觀察（2026-10-20 起一週）

設定固定為：簡單題 gpt-6-luna、深入題 gpt-6.1-sol（自動分流）、萃取 luna、推理強度預設、每日上限 US$2、問答重用關閉；2026-10-21 起加網路搜尋（每天最多 5 次、每次約 US$0.04–0.05，`/cost` 另列）。
一週後看 `python -m garmin_coach cost`（或 Telegram `/cost`）的本月分模型費用，再決定要不要：深入模型換 gpt-5.4-mini、分流收緊、每題工具呼叫上限、資料包瘦身。
實測參考：luna 簡單題 US$0.002–0.003；sol 深入題 US$0.04–0.07；sol 推理 low 省 30% 但會少查歷史與文獻，不建議。

## 12. 路線圖（還沒做）

多人版網頁**暫緩**（2026-10-20 決定先維持單人版）；規劃留在 `docs/WEB_PLAN.md`（Garmin 資料來源的三個方案、每人一個 SQLite 的隔離、FastAPI + worker 架構、安全合規、分階段工作量）。


1. 從對話產生課表並上傳 Garmin（`upload_workout`）。
2. Open-Meteo 歷史天氣回填；地形分類。
3. 每週自動摘要（週負荷、恢復趨勢、指標變化）推到 Telegram。
4. Telegram：排程早上自動 sync + 推今天狀態；自動同步天數可設定。
5. 知識庫：引用品質（優先 meta/RCT）、補 taper/peaking 專題、定期增量 build。
6. 筆記過期與整併（避免 athlete_context 越來越長）。
7. 用 Garmin 心率漂移估 LT1 / Zone 2 上限。

## 13. 接手檢查清單

```bash
cd ~/garmin_project && git status && git log --oneline -3
ls -la .env && grep -c . .env                      # 秘密都在
.venv/bin/python -m garmin_coach stats             # DB 正常
.venv/bin/python -m garmin_coach sync daily        # Garmin token 還有效（失效會要 MFA）
tests/run.sh                                       # 三支離線測試
.venv/bin/python scripts/ask.py --copy "今天狀況如何"   # OpenAI 正常（用複本）
scripts/botctl.sh status                           # bot 是否在跑、有無重複程序
```
