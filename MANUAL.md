# Garmin AI 教練 產品說明書

版本日期：2026-10-28（commit `42d080c` 之後：自動同步、不標論文出處、閒置 30 分鐘、結束訊息只留摘要；公開版加知識庫下載與論文清單）。這份文件是交接用的完整說明：產品是什麼、怎麼裝、怎麼用、內部怎麼運作、怎麼維護。日常使用看 `README.md`，維護細節與踩過的坑看 `HANDOFF.md`，給 Claude Code 的守則在 `CLAUDE.md`；本文件把三者整合成一份可以獨立閱讀的手冊。

---

## 1. 產品概述

Garmin AI 教練是一套單人使用的個人耐力運動教練：把 Garmin Connect 的資料抓到本機 SQLite，讓 OpenAI 模型扮演教練，透過 Telegram 或終端機對話。對話中模型自己呼叫工具取資料、查論文知識庫、必要時上網查賽事資訊，並把學到的事（賽事、傷痛、體感、常問名詞）寫回記憶，下次對話沿用。

**給誰用**：一位耐力運動員（跑步、自行車、游泳、鐵人三項），戴 Garmin 手錶（作者用 fenix 8；任何會同步到 Garmin Connect 的裝置都可以），有 OpenAI API 帳號，用 Telegram。整套系統跑在使用者自己的 Linux 主機上，沒有伺服器、沒有網頁、沒有第二個使用者。公開 repo 裡沒有任何帳號與資料：clone 後填自己的 `.env` 就是自己的教練（第 3 節）。

| 功能 | 內容 |
| --- | --- |
| 資料同步 | 每日恢復（HRV、RHR、睡眠、壓力、Body Battery、訓練準備度、急慢性負荷、熱適應）、每筆活動（配速、功率、心率、分段、區間時間、逐秒資料、天氣、AQI）、體能指標（乳酸閾值、FTP、VO2max、比賽預測）與一年趨勢 |
| 對話教練 | 單次訓練分析（附分段表）、身體狀況判讀、課表與賽事規劃、營養補給、傷痛處置（紅旗症狀會建議就醫） |
| 論文依據 | 2,285 篇 PubMed 論文的本機知識庫，研究結論融進建議（回答不標作者年份） |
| 網路搜尋 | 本機拿不到的外部事實（賽事日期、路線、關門時間、規則、裝備規格），有閘門、上限與快取 |
| 記憶 | 賽事、行程、傷病筆記、關注名詞、對話摘要，跨對話保留 |
| 費用控管 | 每次模型呼叫記帳、每日預算上限、便宜與深入模型自動分流、工具輸出瘦身 |

**不做什麼**：運動與健康以外的任何話題（系統提示規則 0 會拒絕，連部分內容都不給）；多人使用（規劃在 `docs/WEB_PLAN.md`，2026-10-20 決定暫緩）；上傳課表到 Garmin（路線圖項目）；它不是醫療診斷。

**現況快照（2026-10-26）**

| 項目 | 數值 |
| --- | --- |
| 程式碼 | Python 3.10.12；公開版 git 歷史從單一 commit 重新開始（作者本機保留舊歷史） |
| 主要套件 | garminconnect 0.3.2、openai 3.35.1、python-telegram-bot 22.3、pydantic 2.14 |
| Garmin 資料 | 66 天每日資料、344 筆活動（約 65 筆有分段與逐秒細節）、783 筆指標歷史 |
| 記憶 | 12 段對話、43 條筆記、6 個事件、10 個名詞 |
| 知識庫 | 2,285 篇論文、664 篇有全文、21,543 段全部向量化，`knowledge.db` 217 MB；發布包 `knowledge.db.xz` 69 MB（GitHub Release），論文清單 `kb/papers.tsv` |
| 本月 OpenAI 費用 | US$0.955（142 次呼叫；luna 107 次 US$0.068、sol 35 次 US$0.887） |
| Telegram bot | 由 `scripts/botctl.sh` 跑著（已連續執行 5 天）；systemd 服務檔備好但未安裝 |
| 網路搜尋 | 功能已上線，真實資料庫尚無紀錄 |

## 2. 系統架構

整套系統是一個 Python 套件 `garmin_coach`、兩個 SQLite 檔、四個外部服務，全部跑在同一台 Linux 主機上。沒有伺服器、沒有排程器：同步在每次 Telegram 開新對話時自動做（終端機也能手動跑 `sync`），對話靠 Telegram bot 長駐輪詢。

```
 Garmin Connect ──sync──▶ data/raw/*.json ──normalize──▶ data/garmin_coach.db ◀──記憶/對話/記帳──┐
                                                                 ▲                              │
 PubMed / Europe PMC ──kb build──▶ data/knowledge.db ◀──search_knowledge──┐                      │
                                                                          │                      │
 使用者 ──Telegram bot / 終端機 chat──▶ Coach ──Responses API──▶ OpenAI ──tool calls──▶ Tools ───┘
                                                                  │
                                      search_web 子請求（獨立 request，luna + web_search）
 Open-Meteo ──AQI / 天氣展望──▶ Tools
```

| 元件 | 角色 | 位置 |
| --- | --- | --- |
| Garmin Connect | 資料來源。用 python-garminconnect 打 API，OAuth token 快取在 `~/.garminconnect`，密碼只在 token 失效時用 | 外部 |
| OpenAI Responses API | 對話模型 gpt-6-luna（簡單題）與 gpt-6.1-sol（深入題）、分流、結束萃取、web_search 子請求、text-embedding-3-small 向量 | 外部 |
| Telegram Bot API | 手機輸入窗口（自己向 @BotFather 申請的 bot），long polling | 外部 |
| Open-Meteo | 活動當時的 AQI / PM2.5、未來三天天氣展望（免金鑰） | 外部 |
| PubMed E-utilities / Europe PMC | 只在 `kb build` 時抓論文與全文 | 外部 |
| `data/garmin_coach.db` | Garmin 結構化資料、記憶、對話、模型記帳、網路搜尋快取（約 0.9 MB） | 本機 |
| `data/knowledge.db` | 論文、分段、向量、FTS5 全文索引（217 MB，可重建） | 本機 |
| `data/raw/`、`data/knowledge_raw/` | Garmin 原始 JSON（60 MB）與論文原始資料（38 MB），改解析邏輯時不用重抓 | 本機 |

**資料流**

1. 同步：`sync` → `GarminClient` 抓 JSON → 存 `data/raw/` → `normalize.py` 轉成 Pydantic 模型 → `store.py` upsert 進 SQLite。
2. 對話：`Coach` 啟動時 `Tools.preflight()` 打包「訓練前資料包」當 system 訊息 → 模型回答或呼叫工具（最多 8 輪）→ 工具結果回填 → 最終回覆。每則訊息即時寫進 `messages` 表。
3. 寫回：對話中模型呼叫 record_note / record_event / record_interest / log_feedback；結束時 `finish()` 再跑一次萃取補漏。
4. 兩個入口（終端機 `chat`、Telegram bot）共用同一個 `Coach` 類別，工具、提示詞、記憶邏輯完全一樣。

**模組地圖**（行數為 2026-10-26）

| 檔案 | 行數 | 職責 |
| --- | --- | --- |
| `config.py` | 71 | 路徑、`.env` 讀取、所有環境變數與預設值 |
| `client.py` | 173 | `GarminClient`：登入（token → 帳密 → MFA）、`safe()` 包裝、`fetch_*` 各端點 |
| `models.py` | 241 | Pydantic 模型：DailyRecovery、TrainingSession、Split、SessionFeedback、PhysioSnapshot |
| `normalize.py` | 626 | Garmin 原始 JSON → 模型；單位換算、心率漂移與解耦計算、profile / history 整理 |
| `store.py` | 341 | SQLite。資料表欄位由模型自動產生（新欄位自動 ALTER）；記憶、對話、記帳、網路搜尋表在 `CONTEXT_DDL` |
| `sync.py` | 222 | sync_daily / activities / physio / profile / history / all、fill_details、reprocess |
| `external.py` | 92 | Open-Meteo AQI 與天氣展望 |
| `context.py` | 146 | 記憶層 API：profile(kv) / events / notes / interests |
| `tools.py` | 771 | AI 可呼叫的 18 個工具、`TOOL_SPECS` schema、`preflight()`、search_web 的閘門流程 |
| `websearch.py` | 269 | `WebSearcher`（web_search 子請求、yes/no 判斷）、`WebCache`（紀錄、快取、相似查詢）、提示詞 |
| `pricing.py` | 22 | 價目表 `PRICES` 與 `estimate_cost()` |
| `coach.py` | 552 | `SYSTEM_PROMPT`、`EXTRACT_PROMPT`、`ROUTER_PROMPT`、`OpenAILLM`（Responses API、用量）、`Coach`（對話迴圈、分流、記憶寫回、finish、continue_from） |
| `telegram_bot.py` | 626 | `CoachService`（單一 worker thread）、`CoachBot`（指令、白名單、閒置自動結束、重啟接續） |
| `knowledge.py` | 520 | 論文知識庫：PubMed / Europe PMC 抓取、切段、embedding、FTS5 + 向量 RRF 檢索 |
| `brief.py` | 179 | 不用 API 時的「訓練後整理包」Markdown |
| `cli.py` | 389 | argparse 指令入口 `python -m garmin_coach ...` |
| `tests/` | 6 支 | 離線測試，都用資料庫複本 |
| `scripts/` | 3 支 | `ask.py`（命令列問一句）、`botctl.sh`（bot 啟停）、`mfa_login.py`（非互動 MFA） |
| `deploy/` | 1 支 | `garmin-coach-telegram.service`（systemd user unit） |

## 3. 安裝與環境設定

從零裝起來約半小時，最慢的是建知識庫（約 15 分鐘）與回補 Garmin 歷史。交接時如果把 `data/` 與 `~/.garminconnect` 一起搬過去，就只剩裝套件與填 `.env`。

**第一步：程式與套件**（Python 3.10，Linux）

```bash
cd ~/garmin_project
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

**第二步：填 `.env`**。必填四組，其餘用預設值即可（全表在附錄 A）：

| 變數 | 內容 |
| --- | --- |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Garmin Connect 帳密，只在 token 失效時用 |
| `OPENAI_API_KEY` | OpenAI API 金鑰；建議在 platform.openai.com 用單獨的 project 並設每月預算 |
| `TELEGRAM_BOT_TOKEN` | @BotFather `/newbot` 給的 token |
| `TELEGRAM_ALLOWED_USERS` | 允許對話的 Telegram user id（數字，逗號分隔）；第一次先留空 |

秘密只放 `.env`（已在 `.gitignore`，權限 600），不要寫進任何文件、commit 或對話。

**第三步：Garmin 登入**

```bash
.venv/bin/python -m garmin_coach login              # 互動，可能要輸入 MFA 驗證碼
.venv/bin/python scripts/mfa_login.py /tmp/mfa.txt  # 非互動：另一邊 echo 123456 > /tmp/mfa.txt
```

登入成功後 token 存在 `~/.garminconnect`（權限 700），之後所有呼叫都走 token，不再需要密碼。Garmin 對帳密登入有 Cloudflare 阻擋與頻率限制，成功一次就不要刪 token、不要重複登入。Telegram bot 在 token 失效時做不了 MFA，會提示回電腦跑上面的指令。

**第四步：初次資料**

```bash
P=".venv/bin/python -m garmin_coach"
$P sync all --days 60                                     # 每日恢復 + 活動（含細節）+ 體能 + 身體資料
$P sync history                                           # 一年指標趨勢
$P sync activities --from 2025-04-22 --to 2026-08-31 --list-only   # 回補歷史清單（快，沒細節）
$P sync details --min-km 15 --days 365                    # 長距離舊活動補分段與逐秒資料
$P stats                                                  # 確認筆數
```

一筆活動約 6 次 Garmin 呼叫、一天約 8 次，每次間隔 0.4 秒；大量回補分批跑，避免 429。

**第五步：知識庫**（選用但建議；沒有它教練還是能用，只是建議會變成經驗判斷，而且網路搜尋閘門算不到相似度會一律放行）。三選一：

```bash
$P kb download         # 下載作者預建包（GitHub Release 的 knowledge.db.xz，約 70 MB）：驗 sha256、解壓、重建索引，約一分鐘、免費
$P kb build --manifest # 照 kb/papers.tsv 的清單抓同一批論文再向量化：約 10 分鐘，embedding 約 US$0.11
$P kb build            # 自己重搜 14 個主題各 120 高品質 + 60 一般篇：約 15 分鐘，embedding 約 US$0.11；收錄會和作者的不同
$P kb stats
```

**第六步：Telegram**

1. 向 @BotFather 送 `/newbot`，取名後拿到 token，填進 `.env`。
2. `scripts/botctl.sh start` 啟動，對 bot 講任何話，它會回你的 user id。
3. 把 id 填進 `TELEGRAM_ALLOWED_USERS`，`scripts/botctl.sh restart`。沒在白名單的人只會被告知 id，拿不到任何資料。
4. 長期執行改用 systemd（第 10 節）。

**交接時要搬的東西**

| 項目 | 說明 |
| --- | --- |
| git repo | 程式、文件、測試，不含任何秘密與資料 |
| `.env` | 秘密。交接給另一個人時建議重新產生 OpenAI key 與 bot token，不要直接傳送舊的 |
| `data/garmin_coach.db` | 所有記憶與對話都在這，無法重建，一定要備份 |
| `~/.garminconnect/` | Garmin token；不搬就重新登入 |
| `data/knowledge.db`、`data/knowledge_raw/` | 可重建，但搬過去省 15 分鐘與約 US$0.11 |
| `data/raw/` | Garmin 原始 JSON；搬過去才能 `reprocess` 而不用重抓 |

## 4. 日常使用

日常只需要 Telegram：練完直接打字，開新對話時會先自動同步 Garmin 最近 3 天（約 10–30 秒，5 分鐘內同步過會略過；失敗不擋對話，開頭會註明改用現有資料）；終端機留給回補資料、看記憶、建知識庫與除錯。

**Telegram 指令**

| 指令 | 作用 | 花錢？ |
| --- | --- | --- |
| 直接打字 | 問教練。沒有進行中的對話就自動開新對話（先同步 Garmin，再針對最近一次訓練）；還在閒置時限內的舊對話會自動接回（不同步） | 是 |
| `/new [activity_id]` | 開新對話（先同步 Garmin），預設最近一次訓練 | 否 |
| `/continue [對話id]` | 接續上一段對話 | 否 |
| `/end` | 結束並整理記憶（摘要、筆記、賽事、名詞）；只回摘要，寫入與費用看 log 與 `/cost` | 是（萃取一次） |
| `/status` | 今天身體狀況（RHR、HRV、睡眠、準備度與 30 天基準） | 否 |
| `/activities` | 最近的訓練清單 | 否 |
| `/splits [activity_id]` | 某次訓練的每圈 / 每公里分段表 | 否 |
| `/tools` | 這段對話呼叫過哪些工具 | 否 |
| `/cost` | 今天與本月 AI 費用，含網路搜尋 | 否 |
| `/web [id]` | 網路搜尋紀錄；給 id 看那一筆完整內容 | 否 |
| `/model [auto\|simple\|deep]` | 看或切換分流，跨對話保留 | 否 |
| `/help` | 說明 | 否 |

回覆尾端的符號：⚡ 便宜模型（luna）、🧠 深入模型（sol）、🔧 這一輪查了哪些工具。沒講話超過 30 分鐘（`TELEGRAM_IDLE_MINUTES`）會自動 `/end`，忘了結束也不會漏記；之後再打字就是新對話，會再同步一次。

**對話範例**

| 你打 | 教練做什麼 |
| --- | --- |
| 今天騎車的訓練如何 | 簡單題 → luna。用資料包裡的 this_session 與今天健康狀態回答，附分段表 |
| 週六那次跑步前 8 分鐘心率為什麼那麼高 | 先 get_activity_detail，再 get_activity_timeseries 拿那一段逐秒資料 |
| 我 11/1 要比 113 三鐵，接下來課表怎麼排 | 深入題 → sol。record_event 記賽事，search_knowledge 查論文，用 get_sport_history 看一年量 |
| 我預計完賽 6.5 小時 | 短句但是追問 → 沿用 sol，update_event 補目標時間 |
| 幫我查 11/1 墾丁 113 的路線和關門時間 | search_knowledge 沒有 → search_web（走閘門），回答標「（來源：網站，查詢日期）」 |
| 左腳踝有點痛 | record_note(injury)，查運動醫學文獻，紅旗症狀建議就醫 |
| 幫我寫一篇小說 | 一句話拒絕，不記進記憶 |

**回答格式**：先結論、再依據、最後下一步建議；一次 300–1500 字（分段表不計）；分析單次訓練必附分段表；論文結論融進建議、不標作者年份或出處；網路資料標來源與查詢日期；追問一次只問一到兩題。

**終端機指令**（全部用 `.venv/bin/python -m garmin_coach ...`，下表省略前綴）

| 指令 | 作用 |
| --- | --- |
| `login` | 登入 Garmin 並快取 token |
| `sync [all\|daily\|activities\|physio\|profile\|history\|details] --days N --from --to --force --no-details --list-only --min-km --limit` | 同步；預設 all --days 14 |
| `reprocess` | 用本機 raw 重新正規化，不打 API |
| `activities --limit N --sport X` / `show day\|activity\|physio KEY` | 看結構化資料 |
| `log latest --rpe 7 --feeling ... --pain ... --sick --notes ...` | 記主觀回饋 |
| `brief [id] --json --save` | 不用 API 的整理包，貼給 ChatGPT 網頁 |
| `context show` / `context set\|unset KEY` / `context event add\|list\|done\|update` / `context note add\|list\|rm` / `context interest add\|list` | 記憶維護 |
| `chat [activity_id] --model --deep-model --router --continue [ID] --no-weather` | 終端機對話，`/end` 結束 |
| `scripts/ask.py "問題" [--copy] [--continue N] [--activity ID] [--mode] [--no-finish]` | 不進互動模式問一句；`--copy` 用資料庫複本 |
| `tool list` / `tool preflight` / `tool NAME '{json}'` | 直接看 AI 工具的輸出 |
| `kb topics\|build [--manifest]\|download\|export\|manifest\|embed\|search\|stats` | 知識庫（建、下載發布包、打包、寫論文清單） |
| `web list\|show ID\|search "..." --reason ...` | 網路搜尋紀錄；`search` 會真的上網並寫進真實資料庫 |
| `cost` / `stats --weeks N` | 費用、資料庫統計與週負荷 |
| `telegram` | 啟動 bot（平常用 `scripts/botctl.sh`） |

## 5. 資料同步與資料庫

同步是「抓原始 JSON → 存檔 → 正規化 → upsert」四步，每一步都可以單獨重跑；原始 JSON 永遠留著，所以 Garmin 欄位改名或我們算錯時，改 `normalize.py` 再 `reprocess` 就好，不用重抓。

**每日資料（sync daily）**

1. `client.fetch_daily(day)` 打 8 個端點：user_summary、sleep、hrv、rhr、stress、training_readiness、training_status、max_metrics。每個用 `safe()` 包，失敗回 None 並記 warning，不中斷整天。
2. 原始 JSON 存 `data/raw/daily/YYYY-MM-DD.json`。舊檔裡有、這次沒抓到的區塊會合併回來，端點暫時逾時不會把好資料蓋成空值。
3. `normalize_daily()` → `DailyRecovery` → `store.upsert_daily()`。
4. 今天與昨天永遠重抓（睡眠、Body Battery、準備度會持續更新），其他天已存在就略過，`--force` 全抓。

**活動（sync activities）**

1. `get_activities_by_date` 拿清單，每筆再抓 activity、splits、weather、hr_zones、power_zones，加 details（逐秒資料，最多 2000 點），共約 6 次呼叫；`--no-details` 跳過逐秒資料（就沒有心率漂移），`--list-only` 只存清單欄位。
2. 原始 JSON 存 `data/raw/activities/<id>/*.json`。
3. `normalize_activity()` → `TrainingSession`：單位換算、心率漂移（後半 / 前半 − 1）、Pa:Hr 與 Pw:Hr 解耦、區間時間、跑步動態、天氣；`EXTERNAL_AQI=1` 時另打 Open-Meteo 拿活動當時的 AQI / PM2.5。
4. 細節層級記在 `detail_level`：list（只有清單摘要）或 full。現在 344 筆裡約 65 筆是 full（10/7 之後全部、一年內 ≥ 15 km、近 90 天全部），其餘用 `sync details --limit N` 分批補。

**其他**：`sync physio` 拿乳酸閾值 / FTP / 比賽預測快照；`sync profile` 拿身體資料、心率與功率區間、裝置；`sync history` 拿一年指標序列（VO2max、LT、預測、耐力與爬坡分數、熱適應）。`sync all` = daily + activities + physio + profile。

**節流與風險**：每次 Garmin 呼叫間隔 `GARMIN_API_PAUSE`（0.4 秒）。Garmin 對短時間大量請求回 429，不要重複登入、不要一次回補幾百筆細節。

**單位陷阱（已在 normalize 處理，改程式時不要改回去）**

| Garmin 欄位 | 實際單位 | 處理 |
| --- | --- | --- |
| 活動天氣 temp / wind | °F / mph | `GARMIN_WEATHER_UNITS=imperial` 轉 °C / km/h |
| recoveryTime | 分鐘 | 轉小時（曾誤當小時） |
| lactateThresholdSpeed | m/s ÷ 10 | × 10 再轉配速（驗證 4:41/km 正確） |
| 體重 | 公克 | ÷ 1000 |
| 步幅 | cm | ÷ 100 成公尺 |

**data/garmin_coach.db 資料表**

| 表 | 來源 | 重點欄位 |
| --- | --- | --- |
| daily_recovery（60 欄） | DailyRecovery | hrv_*、resting_hr、sleep_*、stress_*、bb_*、readiness_*、recovery_time_hours、vo2max_*、acute / chronic_load、acwr、heat_acclimation_pct、steps |
| activities（60 欄） | TrainingSession | 配速 / 功率 / 心率、hr_zone_s 與 power_zone_s（JSON）、hr_drift_pct、pa / pw_hr_decoupling_pct、跑步動態、TE、training_load、天氣、aqi_us、pm25、海拔、splits（JSON）、detail_level |
| session_feedback | 使用者 | activity_id、rpe、feeling、pain、sick、notes |
| physio_snapshots | PhysioSnapshot | lt_hr、lt_pace_s_per_km、run_ftp_w、cycling_ftp_w、pred_*_s、endurance_score、hill_score |
| metric_history | 一年指標 | date、metric、value、extra |
| profile | kv | garmin_body、sleep_window、hr_zones、power_zones、latest_weight、devices、home_location |
| events | 記憶 | kind(race / travel / family / work / illness / injury / constraint / other)、title、start / end_date、status(active / done / cancelled)、priority(A / B / C)、notes |
| notes | 記憶 | category(status / recovery / injury / illness / race_progress / preference / insight / other)、content、source、active、expires_at |
| interests | 記憶 | term、count、definition、notes、first / last_seen |
| conversations / messages | 對話 | model、summary、usage；messages 含工具呼叫摘要 |
| llm_calls | 記帳 | conversation_id、ts、model、purpose(chat / route / extract / web / web_gate)、input / cached_input / output、cost_usd |
| qa_cache | 問答重用（關閉） | question、answer、depth、embedding |
| web_searches | 網路搜尋 | 見第 8 節 |

前四張表的欄位由 Pydantic 模型自動產生：在 `models.py` 加欄位，`Store` 啟動時會自動 `ALTER TABLE`，巢狀資料存 JSON 字串。其餘表在 `store.py` 的 `CONTEXT_DDL`。

**data/knowledge.db**：`kb_papers`（pmid、title、abstract、authors、journal、year、doi、pmcid、pub_types、mesh、topics、fulltext_status）、`kb_chunks`（pmid、section、ord、text、embedding BLOB）、`kb_fts`（FTS5 外部內容表加觸發器）。

## 6. AI 教練運作原理

教練 = 一段系統提示 + 一包訓練前資料 + 18 個工具 + 兩個模型的分流。模型不會直接碰資料庫，它只能透過 `Tools` 類別讀寫，所以所有驗證、截斷、記帳都在工具層。

**模型呼叫（`OpenAILLM`）**：走 OpenAI Responses API（`/v1/responses`，不是 chat completions；gpt-6 系列的 function tools 加 reasoning 只能走這條），`store=False` 不留存在 OpenAI。內部仍用 chat 風格的 messages，呼叫時 `_to_input()` 轉換。assistant 回合把原始 output items（含 reasoning）原樣塞回（`_raw_items`）才能延續推理；換模型時 `_foreign_items()` 會丟掉另一個模型的 reasoning、function_call 去 id 留 call_id，否則 API 拒收。每次呼叫的 input / cached_input / output token 累計在 `usage`，`OPENAI_REASONING` 可設推理強度（目前空白 = 模型預設）。沒有設 `max_output_tokens`，回答長度靠提示詞規則 13。

**系統提示（`SYSTEM_PROMPT`，16 條規則）**

| 規則 | 內容 |
| --- | --- |
| 0 | 只回答運動訓練、生理、恢復、營養、傷痛、賽事、裝備；其他一律一句話拒絕，不給部分內容、不記進工具 |
| 1–4 | 先看資料再下結論；身體狀況用 RHR / HRV / 睡眠對 30 天基準；強度用使用者自己的區間與閾值；負荷看 10 天量與 ACWR 並對照一年歷史 |
| 5 | 需要資料就呼叫工具，不說「沒有資料」：分析某次訓練先 get_activity_detail，問到某一段用 get_activity_timeseries，趨勢用 get_sport_history / get_daily_recovery |
| 6–10 | 不適、疼痛、生病 → record_note；賽事、行程、限制 → record_event；專業詞彙 → record_interest；感受與 RPE → log_feedback；近況 → record_note |
| 11 | 先結論、再依據、最後建議 |
| 12 | 給建議或解釋原理先 search_knowledge，結論融進建議、不標作者年份或論文出處（2026-10-28 改）；傷痛查運動醫學四主題；紅旗症狀建議就醫；有相關結果就不能說找不到 |
| 13 | 一次 300–1500 字，讀到的資料要用上；追問一次一到兩題 |
| 14 | 分析單次訓練附分段表（穩定課每公里、間歇課每趟） |
| 15 | search_web 是最後手段；標來源與查詢日期；cached 要註明資料日期不重查；denied 就用 kb_results 回答不再試 |

**訓練前資料包（`Tools.preflight()`，約 24k 字）**：對話開始時以 system 訊息塞進去：athlete_profile（身體、區間、閾值）、health_status（永遠是今天）、session_day_health（訓練不是今天才有）、athlete_context（賽事倒數、行程、筆記、名詞）、recent_training_10d（摘要，不含分段）、fitness_metrics（一年趨勢 4 個取樣點）、this_session（完整細節）、weather_outlook。用 `tool preflight` 可以看到完整內容。

**18 個工具（`TOOL_SPECS`）**

| 層 | 工具 | 內容 |
| --- | --- | --- |
| 基礎 | get_athlete_profile | 年齡、身高體重、VO2max、乳酸閾值、心率與功率區間 |
| 基礎 | get_health_status | 當天 RHR / HRV / 睡眠 / 壓力 / Body Battery / 準備度對 30 天基準 → normal / attention / poor |
| 基礎 | get_daily_recovery | 任意日期範圍的每日恢復 |
| 基礎 | get_weather_outlook | 近 3 天與未來 3 天氣溫、體感、濕度、降雨、日出日落 |
| 基礎 | get_athlete_context | 目標賽事倒數、行程、傷病與近況筆記、關注主題 |
| 過去 | get_recent_training | 最近 10 天所有訓練、各項目總量、ACWR 趨勢 |
| 過去 | get_sport_history | 同項目一年：每月量、最長 / 最快 / 最高負荷、心率效率趨勢（最近 8 筆、12 點） |
| 過去 | get_fitness_metrics | LT、FTP、比賽預測、VO2max、耐力 / 爬坡分數的最新值與趨勢 |
| 這次 | get_activity_detail | 摘要、分段、區間時間與邊界、漂移、天氣、AQI、主觀回饋 |
| 這次 | get_activity_timeseries | 逐秒資料降採樣（step_s）：心率、配速、GAP、功率、步頻、海拔、溫度 |
| 知識 | search_knowledge | 論文檢索（query 英文、k 預設 4 最多 8、topic 加分），每段 900 字，附 web_cached |
| 知識 | get_paper | 某篇論文的全文段落（pmid） |
| 知識 | search_web | 網路搜尋（第 8 節） |
| 寫回 | record_note | 傷痛、生病、近況、比賽進度、偏好、教練判斷 |
| 寫回 | record_event / update_event | 賽事、行程、限制、傷病期間；同 kind + 日期會合併不重複 |
| 寫回 | record_interest | 專業詞彙與常關注主題，累計次數（名詞正規化：「LT1（第一乳酸閾值）」→ key LT1） |
| 寫回 | log_feedback | RPE、體感、疼痛、生病 |

工具層驗證（模型常亂填）：rpe 只收 1–10（0 = 清除誤記）；sick 只記 True；priority 只收 A / B / C；日期格式不對視為沒填；「未提供」之類的佔位字串丟掉；工具分派只允許 `TOOL_SPECS` 內的名稱。

**對話迴圈（`Coach.ask()`）**

1. 檢查每日預算（`over_budget`），超過就直接回拒絕訊息不呼叫模型。
2. 分流選模型（下段）。
3. 把上一輪以前的工具輸出截到 400 字（`TOOL_OUTPUT_KEEP`），舊的逐秒資料與論文段落不會每輪重送。
4. 送 messages + `TOOL_SPECS` 給模型；有 tool_calls 就逐一執行 `tools.call()` 回填，最多 8 輪（`max_tool_rounds`）；超過就強制不帶工具作答。
5. 每次模型呼叫寫一筆 `llm_calls`（purpose=chat）；工具迴圈中途超過預算也會停。
6. 每則訊息即時寫 `messages` 表，工具訊息只存「名稱(參數) -> 字數」。

**分流（`classify_depth`）**：≤ 6 字 → simple；上一題是 deep 且這句 4–15 字、像追問（那 / 也 / 可以 / 呢 / 嗎…）→ deep；其餘用 luna 跑 `ROUTER_PROMPT` 判斷（約 US$0.0002，purpose=route）。deep 走 `OPENAI_DEEP_MODEL`（sol），simple 走 `OPENAI_MODEL`（luna）。`OPENAI_ROUTER` 或 Telegram `/model` 可固定。分流失敗用 simple。

**記憶寫回**：對話中模型自己呼叫寫回工具；`finish()`（`/end` 或閒置逾時）用 `EXTRACT_PROMPT` 跑一次萃取（`OPENAI_EXTRACT_MODEL`，luna），輸出 JSON：summary、notes、interests、events、feedback。規則：已用工具記過的不再列；notes 只記 Garmin 沒有的（體感、傷痛、情境、決定），不重抄心率 / 功率 / 睡眠數值；與運動無關的內容不納入。摘要存 `conversations.summary` 並寫一條 `[對話 #N 摘要]` insight 筆記（重跑會先停用舊的那條）。萃取結果裡的壞值（例如 rpe「5–6/10」）會被容錯解析或略過，不會讓 finish 失敗。

**接續與其他**：`continue_from=id` 把該對話的 user / assistant 純文字載回上下文，沿用同一筆 conversation。問答重用（`qa_cache`、`_find_similar` / `_same_request` / `_remember_answer`）已實作並有測試，但 `REUSE_SIMILARITY=0` 關閉；要開設 0.5。

## 7. 論文知識庫

知識庫是 2016 年起的 PubMed 論文，優先系統性回顧、統合分析與 RCT，有開放全文的從 Europe PMC 抓全文；切段後用 OpenAI `text-embedding-3-small` 建向量，檢索用向量加 BM25 混合。教練給建議時必須先查這裡，把結論融進回答；工具回傳的 citation / pmid 只給模型追 `get_paper` 用，回答裡不標出處。

**14 個主題（`knowledge.TOPICS`，每個一組 PubMed 查詢）**

| key | 主題 |
| --- | --- |
| zone2 | Zone 2 / 低強度 / 強度分布 |
| lt1_lt2 | LT1 / LT2 / 閾值訓練 |
| interval | 間歇訓練 |
| endurance | 耐力訓練 |
| strength | 肌力訓練 |
| running | 跑步訓練 |
| cycling | 自行車訓練 |
| marathon | 馬拉松訓練 |
| nutrition | 運動飲食 |
| recovery_monitoring | 恢復與監控（HRV、睡眠、負荷、熱適應、減量） |
| injury_running | 跑步運動傷害（PFP、阿基里斯腱、ITB、足底筋膜、脛骨壓力、骨壓力傷害） |
| injury_cycling | 自行車運動傷害（過度使用、bike fit、膝 / 腰 / 頸 / 會陰） |
| injury_prevention | 傷害預防與回場（負荷管理、肌力、復健、肌腱 / 骨壓力） |
| sports_medicine | 運動醫學（熱傷害、低血鈉、過度訓練 / RED-S、心臟、感染） |

**建庫流程（`kb build`）**

1. 每個主題向 PubMed E-utilities 查兩組：高品質（review / meta-analysis / RCT / systematic，`--hq` 預設 120 篇）與一般（`--general` 60 篇），`--since` 2016。
2. 有 PMCID 的從 Europe PMC 抓 JATS 全文（`--no-fulltext` 跳過）；原始資料存 `data/knowledge_raw/`。
3. 切段 2400 字、重疊 250（`CHUNK_CHARS` / `CHUNK_OVERLAP`），摘要永遠是第一段（section=abstract）。
4. `text-embedding-3-small` 1536 維，正規化後 float32 存 BLOB（下載的發布包是 float16，載入時依 BLOB 長度判斷，兩種可混用）；`kb embed` 可單獨補缺向量的段落。
5. FTS5 外部內容表做 BM25。

建庫可重複執行，已存在的論文略過。注意：`kb build` 不帶 `--topics` 會重搜全部主題，PubMed 相關性排序會變，每次都可能多加幾百篇（2026-10-20 一次 +566 篇，embedding 約 US$0.04）。`--retry-errors` 只重試之前全文抓失敗的（目前 142 篇，Europe PMC 500），不重搜。

**取得知識庫的三種方式**

| 方式 | 指令 | 時間 / 費用 | 結果 |
| --- | --- | --- | --- |
| 下載發布包 | `kb download` | 約一分鐘、免費 | 和作者完全一樣（向量 float16） |
| 照清單重建 | `kb build --manifest` | 約 10 分鐘、embedding 約 US$0.11 | 同一批論文，自己向量化；Europe PMC 全文可能略有增減 |
| 重新搜尋 | `kb build` | 約 15 分鐘、embedding 約 US$0.11 | 依 PubMed 當天排序，收錄不同 |

`kb download` 從 `KB_PACK_URL`（預設作者在 GitHub Release 掛的 `knowledge.db.xz`，旁邊有 `.sha256`；404 時改抓 `KB_PACK_FALLBACK_URL`，目前是作者 Dropbox 的同一個檔）下載到 `data/` 的暫存目錄，sha256 不符就中止（預設來源對 `KB_PACK_SHA256`，其他來源對網址旁的 `.sha256`，`--sha256` 可指定）；解壓後 `kb_fts` rebuild、`os.replace` 換成 `data/knowledge.db`。已有論文的知識庫不覆蓋，`--force` 才會；bot 跑著要重啟才用到新檔。`kb/papers.tsv` 是書目清單（PMID / PMCID / DOI / 年份 / 證據等級 / 是否有全文 / 主題 / 期刊 / 標題，約 470 KB），`kb build --manifest` 用 efetch 每批 100 篇抓，清單標 `fulltext=none` 的不再向 Europe PMC 試。

**發布知識庫（作者）**：`kb export` 把複本的向量轉 float16（餘弦誤差 < 2e-4，實測 20 組查詢 19 組前五名完全相同）、清掉可重建的 FTS 索引、VACUUM、xz 壓縮，約 25 秒得到 69 MB 的 `data/export/knowledge.db.xz`、`.sha256` 與 25 MiB 一片的 `.001`、`.002`… 分片；上傳到 tag `kb-YYYY-MM-DD` 的 GitHub Release（傳單檔或傳分片都可以，`kb download` 先抓單檔、404 就逐片抓再接起來），改 `config.KB_PACK_URL` / `KB_PACK_SHA256` 預設值與 `.env.example`，`kb manifest` 重寫清單後一起 commit。發布包內容是 PubMed 摘要與 Europe PMC 開放取用全文，版權屬出版者與作者，只供個人研究檢索。

**檢索（`KnowledgeBase.search`）**

1. 查詢向量對全部 21,543 段算餘弦，取前幾名；FTS5 BM25 取前幾名（`--bm25-only` 可單用）。
2. 兩邊用 RRF（reciprocal rank fusion）合併。
3. `topic` 是加分不是過濾（× 1.25）；證據等級加權：統合分析 / 系統性回顧 × 1.3、RCT / 共識聲明 × 1.15（`pub_type_weight`）。這是修 Furrer 2023 一篇 171 段的綜述霸占 taper 查詢的結果，現在 taper 查詢回 Wang 2023 統合分析第一。
4. 每篇論文只回一段；回給模型時每段截 900 字（留頭尾，`SNIPPET_CHARS`），要全文用 get_paper(pmid)。
5. `KnowledgeBase.nearest(vec, k)` 另供網路搜尋閘門算「知識庫最高相似度」。

**現況**：2,285 篇（2016–2026，每年 150–265 篇）、664 篇有全文（約七成只有摘要）、21,543 段全部向量化。

```bash
$P kb topics                                  # 14 個主題
$P kb search "tapering endurance meta-analysis" -k 5 --topic recovery_monitoring
$P kb build --topics nutrition --hq 200       # 單一主題加量
$P kb build --retry-errors                    # 只補全文
$P kb stats
```

驗證教練引用是否存在：`kb search "<作者 年份 主題>"` 或直接 `sqlite3 data/knowledge.db "select pmid,title,year from kb_papers where title like '%...%'"`。

## 8. 網路搜尋（search_web）

網路搜尋用 OpenAI Responses API 內建的 `web_search` 工具，但不直接掛在主對話上：主模型只看得到一個普通的 function tool `search_web(query, reason, ignore_cache, cache_id)`，真正上網的是一個獨立的子請求（`WebSearcher.run`，模型 `WEB_SEARCH_MODEL` = luna，不帶對話上下文）。這樣閘門、上限、紀錄都能在程式層控制；若直接給主模型 web_search，既擋不下也拿不到它讀了什麼。

```
 search_web(query) ─▶ cache_id? ──是──▶ 回該筆完整內容（免費）
        │否
        ▼
   WEB_SEARCH_ENABLED? ──否──▶ denied
        │是
        ▼
   查詢向量化 ─▶ 快取：query_norm 相同 / 餘弦 ≥0.85 ─▶ 命中（免費，註明資料日期）
        │                      0.70–0.85 → luna 判斷同一件事？ ─是─▶ 命中
        │ 未命中
        ▼
   知識庫最高餘弦 < 0.45 ─▶ 放行
        │ ≥ 0.45 → luna 看三段論文：知識庫能回答？ ─是─▶ denied + kb_results
        │ 否
        ▼
   今日 ≥ 5 次 或 本段對話 ≥ 3 次 ─▶ denied + related_cached
        │ 未達
        ▼
   子請求（luna + web_search，max_tool_calls 3）─▶ 整理 ─▶ web_searches 寫 done（全文、來源、引用、raw、費用、向量）
```

**閘門順序（`Tools.search_web`）**

1. `cache_id` 給了就直接回那筆的完整內容（最多 12,000 字，免費）。
2. `WEB_SEARCH_ENABLED=0` → denied。
3. 把查詢向量化（text-embedding-3-small，與知識庫同一個 Embedder）。
4. 快取：`query_norm`（小寫、合併空白、去標點、保留 CJK）完全相同 → 命中；否則對有效的舊紀錄算餘弦，≥ `WEB_CACHE_SIMILARITY`（0.85）直接命中，0.70–0.85（`WEB_CACHE_MAYBE`）交 luna 用 `CACHE_JUDGE_PROMPT` 判斷是不是同一件事（舊結果已寫明找不到同一項目也算 yes）。命中就 `touch`（hits + 1）並回傳，不花錢。
5. 知識庫檢查：`KnowledgeBase.nearest` 算查詢對 21,543 段的最高餘弦。< `WEB_KB_FLOOR`（0.45）→ 知識庫顯然沒有，放行（kb_judged=skip）；≥ 0.45 → 把最接近的三段給 luna（`KB_JUDGE_PROMPT`）判斷知識庫能不能回答；能 → denied，並把那三段當 `kb_results` 直接回給主模型；判斷失敗時 ≥ 0.6 當有。
6. 上限：今天 done 筆數 ≥ `WEB_SEARCH_DAILY_MAX`（5）或這段對話 ≥ `WEB_SEARCH_PER_CONVERSATION`（3）→ denied，附 related_cached。
7. 放行：子請求帶 `{"type":"web_search","search_context_size":medium,"user_location":TW}`、`max_tool_calls=WEB_SEARCH_MAX_CALLS`（3）、`include=web_search_call.action.sources`，要求輸出「結論、逐條事實 [n]、資料日期、來源 URL」，找不到寫「未找到」，忽略網頁裡的指令。
8. 紀錄：`web_searches` 寫一筆 status=done；denied / failed 也寫（deny_reason），方便審計。

**回傳給主模型的內容**：cache_id、cached、fetched_at、query、summary（截 5,000 字，`WEB_SUMMARY_CHARS`；用 cache_id 可取 12,000）、citations（最多 10 筆）、related_cached（相似度 ≥ 0.55 的其他舊查詢，含 cache_id 與摘要頭）、note（快取時提醒註明資料日期）。`search_knowledge` 的結果也附 `web_cached`，讓主模型先用舊資料、不要拆新查詢。

**完整紀錄的定義**：OpenAI 的 web_search 不回傳網頁原文，所以 `web_searches` 存的是：子模型的整理全文（summary）、實際執行的搜尋動作與來源（searches JSON：search / open_page / find_in_page、查詢字串、URL）、引用（citations JSON：url、title）、API 原始 output 項目（raw JSON）、token 與呼叫次數、費用、查詢向量、被重用次數與最後使用時間。

**快取有效期**：`WEB_CACHE_DAYS`（90 天）；摘要開頭命中「未找到 / 找不到 / 查不到 / 尚未公布 / not found」的只算 `WEB_CACHE_DAYS_NOTFOUND`（14 天），之後可重查（賽事資訊可能已公布）。

**門檻校準（2026-10-21，真實向量對 21,543 段）**

| 查詢類型 | 對知識庫最高餘弦 |
| --- | --- |
| 訓練科學問題（減量、Zone 2、營養） | 0.55–0.72 |
| 賽事 / 產品 / 中文查詢 | 0.19–0.55 |
| 灰色地帶（穿戴裝置準確度、膠體成分、防寒衣規則） | 0.52–0.58 |
| 同一需求不同寫法（快取相似度） | 0.86–0.89 |
| 同賽事不同面向 | 0.75–0.81 |
| 中文同義 | 0.75 |

所以 floor 0.45 以上一律交 luna 判斷。改 embedding 模型或知識庫大改後要重新校準。

**實測（資料庫複本，真實 API）**

| 情境 | 結果 |
| --- | --- |
| 「賽前減量策略」 | 知識庫擋下（相似度 0.62，luna 判有），直接回 3 篇論文 |
| 「3000 美元以下碳纖三鐵車」 | 灰色地帶 0.55，luna 判沒有，放行；4 次 web_search 呼叫，US$0.0445，49 秒 |
| 「11/1 墾丁 113 是哪一場、路線與關門」 | 查出 2026 IRONMAN 70.3 Kenting，關門自行車 11:50、全賽 14:50，2026 官方路線尚未公布；兩次上網各約 US$0.043，共 87 秒 |
| 第二段對話再問同一題 | 主查詢命中快取，不上網，整題約 US$0.003 |

**已知行為**：模型會把同一賽事拆成不同面向的查詢（路線、關門、選手指南），一題可能用兩次；所以有每段對話上限、related_cached、「找不到」也算快取命中。`max_tool_calls=3` 實測仍回 4 個 web_search_call 項目（open_page 也算），費用估算把全部項目都算進去（保守）。一次上網約 35–50 秒。

```bash
$P web list                   # ✅ 放行 ⛔ 拒絕 ❌ 失敗，含費用與重用次數
$P web show 3                 # 完整整理與引用
$P web search "2026 IRONMAN 70.3 Kenting bike course" --reason "本地沒有賽事路線"   # 走閘門，會真的上網並寫真實資料庫
```

Telegram `/web` 與 `/web ID` 同樣內容；`/cost` 多一行網路搜尋。要整個關掉設 `WEB_SEARCH_ENABLED=0` 並重啟 bot。

## 9. 費用與配額

所有費用都是 OpenAI：Garmin、Telegram、Open-Meteo、PubMed 都免費。本月到 10/26 為止花 US$0.955，其中 93% 是深入模型 sol；每次模型呼叫都記在 `llm_calls`，所以隨時能對帳。

**價目表（`pricing.PRICES`，US$ / 1M tokens，2026-10 OpenAI 標準價）**

| 模型 | input | cached input | output | 用在 |
| --- | --- | --- | --- | --- |
| gpt-6-luna | 0.10 | 0.01 | 0.50 | 簡單題、分流、萃取、網路搜尋子請求與閘門判斷 |
| gpt-6.1-sol | 2.00 | 0.10 | 10.00 | 深入題 |
| text-embedding-3-small | — | — | — | 知識庫與查詢向量；不在 llm_calls，看 `data/logs/kb_*.log` |
| web_search 工具費 | 每次呼叫 `WEB_SEARCH_CALL_USD`（假設 0.01） | | | 要對 OpenAI 帳單校正 |

價目表還有 gpt-5 系列與 gpt-4.1 / 4o，換模型時若不在表內 `estimate_cost` 回 None，費用會記 0，要先補。

**記帳（`llm_calls`）**：每次呼叫一筆，purpose 分 chat（對話）、route（分流）、extract（結束萃取）、web（網路搜尋子請求，token 費 + 呼叫次數 × 工具費）、web_gate（閘門的 yes/no 判斷）。`conversations.usage` 另存每段對話總量。

**每日預算**：`OPENAI_DAILY_BUDGET_USD`（預設 2）。當天 llm_calls 累計達上限就拒絕再呼叫模型，含工具迴圈中途；隔天自動恢復。0 = 不限制。建議在 platform.openai.com 的 project 再設一道每月預算。

**實測單價**

| 項目 | 約 US$ |
| --- | --- |
| 簡單題（luna，含工具呼叫） | 0.002–0.003 |
| 深入題（sol，含工具呼叫） | 0.02–0.07 |
| 分流判斷 | 0.0002 |
| 結束萃取（luna） | 0.001–0.003 |
| 網路搜尋一次放行 | 0.04–0.05 |
| 網路搜尋閘門判斷一次 | 0.0002 |
| 知識庫全部重建 embedding | 0.11 |

一次問答的 input 約 22K 未快取 + 13K 快取、output 約 1.3K；費用約 75% 來自未快取 input（主要是工具回傳），所以 2026-10-20 做了 input 瘦身：論文片段 900 字、歷史與指標取樣減少、資料包不重複附區間、舊工具輸出截 400 字。回覆上限放寬到 1500 字後 output 會增加，sol 的 output 單價是 input 的 5 倍，深入題一題可能多幾分錢。

**網路搜尋配額**：每天 5 次、每段對話 3 次、每次子請求最多 3 個搜尋動作；快取命中不計。全部用滿一天約 US$0.25。

**怎麼看**：Telegram `/cost` 或 `python -m garmin_coach cost`：今天 / 本月呼叫次數與費用、分模型、網路搜尋次數與快取命中。每則 Telegram 回覆尾端也有那一句的費用。

**省錢的旋鈕（由便宜到激進）**

1. `/model simple` 或 `OPENAI_ROUTER=simple`：全部用 luna，深入題品質下降。
2. `OPENAI_DEEP_MODEL=gpt-5.4-mini`：深入題約三分之一價。
3. `OPENAI_REASONING=low`：sol 省約 30%，但實測會少查歷史與文獻，不建議。
4. `REUSE_SIMILARITY=0.5`：開問答重用，相似的深入題改由 luna 更新舊答。
5. 把提示詞規則 13 的字數改回 300–500。

2026-10-20 起的決定：維持 sol + luna 組合一週，再用 `/cost` 的本月分模型費用決定要不要調。

## 10. 運維

運維只有三件事：bot 要活著、Garmin token 要有效、`garmin_coach.db` 要有備份。沒有排程任務，同步在每次開新對話時自動跑（`CoachService.sync_if_due`，3 天、5 分鐘冷卻）。

**Telegram bot 啟停**

| 方式 | 指令 | 說明 |
| --- | --- | --- |
| botctl（目前在用） | `scripts/botctl.sh start\|stop\|restart\|status\|log [行數]` | nohup 背景執行，pid 與 log 在 `data/logs/telegram_bot.pid` / `.log`；start 會檢查有沒有別的 bot 程序 |
| systemd（建議長期用） | 見下方 | 開機自動啟動、當掉自動重啟（RestartSec 10） |

```bash
mkdir -p ~/.config/systemd/user
cp deploy/garmin-coach-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
scripts/botctl.sh stop                                  # 先停 botctl 的那個，否則兩個會搶訊息
systemctl --user enable --now garmin-coach-telegram
loginctl enable-linger $USER                            # 沒登入也要跑
systemctl --user status garmin-coach-telegram
journalctl --user -u garmin-coach-telegram -f           # 看 log
```

裝了 systemd 就不要再用 botctl。同一個 token 只能有一個程序 polling，兩個會互搶（Telegram 回 Conflict）。

**重啟規則**

1. 改了 `garmin_coach/*.py` 或 `.env` 都要重啟 bot 才生效。
2. 重啟是安全的：`drop_pending_updates=False`，重啟期間的訊息不丟；進行中的對話在下一句自動接回（`resume_id`）；只有「正在等模型回覆」那一句會掉，重送即可。啟動時 `finish_stale` 會把之前沒結束、已逾時的對話補跑萃取。
3. 不要用 `pkill -f "garmin_coach telegram"`：在同一條 shell 會把自己殺掉。用 pid 檔或 systemctl。
4. log 用 `>>` 追加；用 `>` 截斷會讓舊程序寫出 NUL 空洞，grep 把 log 當二進位。

**Garmin token**：平常自動續期。失效時開新對話的標題會出現「Garmin 同步失敗，先用現有資料」，跑 `python -m garmin_coach login`（互動 MFA）或 `scripts/mfa_login.py`（非互動），完成後不用重啟 bot。遇到 429 就等幾分鐘，不要重複登入。

**日誌（`data/logs/`）**

| 檔 | 內容 |
| --- | --- |
| `telegram_bot.log` | bot 的所有操作：對話開始 / 結束、分流結果、工具呼叫、網路搜尋閘門、錯誤。不含秘密（httpx INFO 已關） |
| `kb_build_*.log`、`kb_retry_errors.log` | 建庫過程與 embedding token 數 |
| `fill_details.log` | `sync details` 回補紀錄 |

**備份**：`data/garmin_coach.db`（記憶、對話、記帳、網路搜尋快取，無法重建）定期複製到別的地方，檔案不到 1 MB；用 `sqlite3 data/garmin_coach.db ".backup /path/backup.db"` 可在 bot 運行中安全備份。`.env` 與 `~/.garminconnect` 一起存。`knowledge.db`、`raw/` 可重建或重抓，備份是為了省時間。目前沒有自動備份。

**磁碟**：`data/` 共約 320 MB（knowledge.db 217 MB、raw 60 MB、knowledge_raw 38 MB），每天增加幾百 KB。

**健康檢查**

```bash
scripts/botctl.sh status                       # 執行中 (pid N)，有無其他 bot 程序
.venv/bin/python -m garmin_coach stats         # DB 筆數與週負荷
.venv/bin/python -m garmin_coach cost          # 今天費用是否異常
tail -50 data/logs/telegram_bot.log | grep -i "error\|warning"
```

**常見狀況**

| 症狀 | 原因與處理 |
| --- | --- |
| bot 不回話 | `botctl.sh status` 看有沒有在跑；看 log 尾端；兩個程序搶訊息會出現 Conflict |
| 「今天的 AI 費用已達上限」 | 每日預算到了；明天恢復，或調 `OPENAI_DAILY_BUDGET_USD` |
| 新對話標題出現「Garmin 同步失敗」 | token 失效或需要 MFA，回電腦 login；對話照常進行但資料是舊的 |
| 教練說沒有某次訓練的分段 | 那筆是 list 級，`sync details` 或 `sync activities --from --to --force` 補 |
| 教練回答與資料庫數字不符 | `show activity ID` / `show day DATE` 對照；若是單位問題改 normalize 後 `reprocess` |
| Telegram 表格亂掉 | Markdown 表格轉成 `<pre>` 等寬，手機橫向看；或用 `/splits` |

## 11. 測試與驗證

離線測試七支全部用資料庫複本與假模型，不打任何外部 API，約 1–2 分鐘；每個功能做完、commit 前都要跑。真實模型的對話測試一律用 `scripts/ask.py --copy`，不要污染真實記憶。

```bash
tests/run.sh
```

| 測試 | 內容 |
| --- | --- |
| `tests/test_pipeline.py` | 合成 Garmin JSON → normalize → store → context → brief；單位換算、解耦計算 |
| `tests/test_coach.py` | 假 LLM 走 Coach：多工具同時呼叫、工具結果回填、記憶寫回、finish 萃取 |
| `tests/test_telegram.py` | 假 LLM + 假 Telegram 物件走全部指令：白名單、自動開新對話、/status /activities /tools /end /continue /new、閒置結束 |
| `tests/test_router.py` | 分流規則、跨模型 raw items 轉換、模型選擇與 llm_calls 記帳 |
| `tests/test_reuse.py` | 問答重用（假向量）：相似命中、「重新」、不相關 |
| `tests/test_web.py` | 網路搜尋（假子模型 + 假向量）：知識庫擋下、放行並完整紀錄與費用、exact / 相似 / 中間地帶快取、ignore_cache、cache_id、related_cached、未找到 TTL、每日與每段對話上限、關閉、失敗、Coach 整合、真實 API 回傳形狀解析 |
| `tests/test_kb.py` | 知識庫發布（假論文 + 假向量）：float16 載入與混用、export↔download 往返、sha256 驗證、FTS 重建、覆蓋保護、論文清單寫讀與照清單重建 |

`TEST_DB` 環境變數可指定複本來源，預設複製 `data/garmin_coach.db`。

**真實對話測試**

```bash
.venv/bin/python scripts/ask.py --copy "我目前的 zone2 心率應該要抓多少"          # 複本，會印工具呼叫、模型、費用、摘要
.venv/bin/python scripts/ask.py --copy --mode deep "幫我規劃到比賽前的訓練"    # 強制深入模型
.venv/bin/python scripts/ask.py --copy --no-finish "..."                      # 不跑萃取
```

複本在 `/tmp/garmin_coach_copy_*.db`，用完可刪。真實資料庫的記憶只放真的對話。

**驗證教練回答**

1. 數字：`show activity ID`、`show day DATE`、`tool get_activity_detail '{"activity_id":ID}'` 對照配速、心率、分段。
2. 文獻：回答不標出處，所以看 `/tools` 有沒有呼叫 search_knowledge；要核對內容用 `kb search "<主題>"` 找那段原文。
3. 工具：Telegram `/tools` 或 ask.py 的 ⚙ 行，看教練有沒有真的去查資料（分析單次訓練應該有 get_activity_detail）。
4. 記憶：`context show` 看有沒有記錯或重抄數值；錯的用 `context note rm ID` / `context event update`。
5. 費用：`cost` 看那一句的 purpose 分布。

**改了提示詞怎麼驗證**：同一個問題用 `--copy` 跑改前改後，比較工具呼叫、字數、費用；離線測試只能保證程式流程，保證不了回答品質。

## 12. 安全與秘密

攻擊面只有 Telegram bot 一個，所有模型呼叫都在白名單之後，陌生人燒不到 OpenAI 額度；秘密只在 `.env`，git 全歷史確認沒有 key。以下是 2026-10-20 審計的結論與後續維持的規則。

**秘密**

| 項目 | 位置 | 權限 |
| --- | --- | --- |
| Garmin 帳密、OpenAI key、Telegram token | `.env` | 600，`.gitignore` |
| Garmin OAuth token | `~/.garminconnect/` | 目錄 700、檔 600 |
| 資料庫 | `data/*.db` | `data/` 700、db 600 |

home 目錄本身是個沒有 remote 的 git repo，已在 `~/.git/info/exclude` 排除 `garmin_project/` 與 `.garminconnect/`，避免在 home 下 `git add -A` 把秘密加進去。三組秘密都曾貼在 Claude Code 對話裡（逐字稿在 `~/.claude/projects/…jsonl`，600），要徹底就去各平台輪換一次；交接給別人時必輪換。

**Telegram**：所有 handler 先過 `_guard`（數字 id 白名單，也支援 @username 但較不嚴格）；未授權者只會收到自己的 id，同一人一小時只回一次；訊息上限 4096 字，工具回合上限 8。

**模型側**：系統提示規則 0 限制主題，萃取也排除無關內容；網路搜尋子請求的提示詞要求忽略網頁裡的指令；工具分派只允許 `TOOL_SPECS` 內的名稱，參數在工具層驗證。

**費用側**：每次呼叫記帳、每日預算 US$2、網路搜尋每天 5 次、舊工具輸出截 400 字。建議另在 OpenAI project 設每月預算當第二道防線。

**程式面**：SQL 只有內部表名 / 欄名用 f-string，使用者值全部參數化；所有 HTTP 呼叫有 timeout（Open-Meteo 10 s、PubMed 120 s）；PubMed 的 email 參數用 `PUBMED_EMAIL`（可空），不送 Garmin 帳號；log 不含秘密。

**對外連線與送出的資料**

| 目的地 | 送出什麼 |
| --- | --- |
| api.openai.com | Garmin 資料摘要、對話內容、網路搜尋查詢（`store=False`，不留存） |
| api.telegram.org | 對話內容 |
| connect.garmin.com | 登入與讀取，不寫入 |
| open-meteo.com | 活動經緯度與時間 |
| eutils.ncbi.nlm.nih.gov、www.ebi.ac.uk | 只在 `kb build` 時，送查詢字與 `PUBMED_EMAIL` |

**規則**：不在任何輸出、commit、文件印出密碼、key、token；要改就直接寫 `.env`。

## 13. 已知限制與坑

這些都是真的踩過、已經處理或決定先不處理的事；改程式前先看一遍，免得改回去。

**功能限制**

| 限制 | 說明 |
| --- | --- |
| 舊活動只有摘要 | 344 筆裡約 280 筆是 list 級（一年歷史用 `--list-only` 回補）；教練分析舊比賽要先 `sync details` |
| 知識庫七成只有摘要 | 沒開放全文；142 篇全文抓取遇 Europe PMC 500，`kb build --retry-errors` 可補 |
| Garmin 沒有 LT1 | 只有乳酸閾值（LT2）；Zone 2 上限靠 Garmin 心率區間，之後可用心率漂移估 |
| Telegram 自動同步固定 3 天 | 更久要回終端機跑 `sync` |
| 室內與部分舊活動沒天氣 | Garmin 沒給；Open-Meteo 歷史天氣回填還沒做 |
| 筆記不會自動過期 | `expires_at` 有欄位但萃取很少填；久了要手動 `context note rm` |
| 兩個入口 session 分開 | 終端機與 Telegram 同時聊會各開一段對話 |
| 沒有排程 | 沒有每日自動 sync、週報、自動備份 |
| 網路搜尋沒有網頁原文 | OpenAI web_search 不回傳；紀錄 = 整理全文 + 來源 + 引用 + raw |
| 網路搜尋工具費是估算 | `WEB_SEARCH_CALL_USD=0.01` 要對帳單校正 |
| 回答長度沒有硬限制 | 沒設 `max_output_tokens`，靠提示詞 |

**踩過的坑（不要再踩）**

1. **Garmin 單位**：天氣 °F / mph；recoveryTime 是分鐘；lactateThresholdSpeed 是 m/s ÷ 10；體重公克；步幅 cm。
2. **MFA**：非互動環境 `input()` 會 EOF，用 `scripts/mfa_login.py` 等驗證碼檔案。
3. **Responses API**：gpt-6 系列的 function tools + reasoning 必須走 `/v1/responses`；assistant 回合要把原始 output items 原樣塞回；跨模型要轉換（`_foreign_items`）否則 API 拒收。
4. **健康狀態要用今天的**：preflight 曾用訓練當天的準備度，隔兩天問規劃就誤判；現在 health_status 永遠是今天，session_day_health 另附。
5. **萃取不要重抄數值**：第一次真實對話把 RHR / HRV / 功率抄成永久筆記；EXTRACT_PROMPT 已限制。
6. **模型亂填欄位**：rpe=0/1、sick=false、priority=「未指定」、expires_at=""；工具層一律驗證。
7. **摘要 vs 細節**：教練曾在被問「週日訓練前 8 分鐘」時說沒有逐段資料而不去呼叫工具；規則 5 與工具描述已寫明。
8. **Furrer 2023 霸占 taper**：search_knowledge 的 topic 曾是硬過濾，減量論文標在 recovery_monitoring，用 topic=endurance 查就只剩那篇 171 段的綜述；現在 topic 是加分，另依證據等級加權。
9. **主題範圍**：曾被要求寫一萬字小說就真的寫了；規則 0 加上後拒絕。
10. **名詞正規化**：「LT1（第一乳酸閾值）」要切成 key LT1 + definition，次數才會累計。
11. **記憶重複**：同 kind + 日期的事件現在合併不新增；合併的名詞（「A / B」）會拆開；萃取時會告訴模型已有哪些事件。
12. **finish 容錯**：萃取回來 rpe「5–6/10」曾讓 finish 整個失敗；現在解析範圍與字串，壞值略過。
13. **Telegram 重啟**：log 用 `>>`；`drop_pending_updates=False`；不要 `pkill -f`。
14. **sqlite 跨執行緒**：bot 所有 DB 操作走單一 worker thread，不要在 handler 裡直接開連線。
15. **sync 不要蓋空值**：端點暫時逾時曾把好資料蓋成空；現在舊 raw 合併回來。
16. **網路搜尋不要直接掛主對話**：擋不了、記不到；包成 function tool 在子請求跑。
17. **模型會拆查詢**：同一賽事拆成路線 / 關門 / 選手指南幾次上網；所以有每段對話上限與 related_cached。
18. **kb build 不帶 --topics 會膨脹**：每次重搜都多幾百篇。
19. **pkill 自殺**、**`>` 截斷 log**、**重複登入 429**：見第 10 節。

**路線圖（還沒做）**：從對話產生課表並上傳 Garmin（`upload_workout`）；Open-Meteo 歷史天氣回填與地形分類；每週自動摘要推 Telegram；排程早上自動 sync + 推今天狀態；自動同步天數可設定；知識庫定期增量 build；筆記過期與整併；用心率漂移估 LT1；多人版網頁（`docs/WEB_PLAN.md`，暫緩）。

## 14. 維護指南

每種改動都有固定的連動：改模型欄位要回填、改工具要改 schema、改任何 `garmin_coach/*.py` 要重啟 bot、commit 前要跑測試。給 Claude Code 的守則在 `CLAUDE.md`，內容與本節一致。

| 要改什麼 | 怎麼改 | 連動 |
| --- | --- | --- |
| 加一個 Garmin 欄位 | `models.py` 加欄位 → `normalize.py` 從 raw 取值 | `Store` 啟動自動 ALTER；`python -m garmin_coach reprocess` 回填舊資料；要給教練用就在 `tools.py` 對應工具加進回傳 |
| 改解析或單位 | `normalize.py` | `reprocess`（不打 API）；`tests/test_pipeline.py` 有單位測試 |
| 加一個 AI 工具 | `tools.py` 加方法 + `TOOL_SPECS` 加 schema（用 `_p()` 寫參數） | 參數驗證放工具層；回傳要短（費用 75% 是工具回傳）；必要時在 `SYSTEM_PROMPT` 加何時用；`tests/test_coach.py` 加案例 |
| 改教練行為 | `coach.py` 的 `SYSTEM_PROMPT`（規則編號沿用） | 用 `scripts/ask.py --copy` 比較改前後；重啟 bot |
| 改萃取內容 | `EXTRACT_PROMPT`、`Coach.finish()` 的容錯解析 | `tests/test_coach.py` |
| 改分流 | `ROUTER_PROMPT`、`classify_depth`、`_FOLLOWUP` | `tests/test_router.py` |
| 換模型 | `.env` 的 `OPENAI_MODEL` / `OPENAI_DEEP_MODEL` / `OPENAI_EXTRACT_MODEL` / `WEB_SEARCH_MODEL` | 新模型要先加進 `pricing.PRICES`，否則費用記 0；非 Responses API 的模型不支援 |
| 改知識庫主題 | `knowledge.TOPICS` 加 key 與 PubMed 查詢 | `kb build --topics NEW`；`tools.py` 的 `_KB_TOPICS` enum 跟著 TOPICS；必要時在規則 12 提到 |
| 更新發布的知識庫 | `kb export` → GitHub Release（tag `kb-YYYY-MM-DD`）→ 改 `config.KB_PACK_URL` / `KB_PACK_SHA256` 與 `.env.example` → `kb manifest` | 第 7 節；別人 `kb download --force` 後重啟 bot |
| 改切段或 embedding 模型 | `knowledge.py` 的 `CHUNK_CHARS` / `EMBED_MODEL` | 全部重建（`kb build`，原始資料在 knowledge_raw 不用重抓）；**網路搜尋門檻要重新校準**（下段） |
| 改網路搜尋閘門 | `.env` 的 WEB_* 變數；流程在 `tools.py` `search_web`；提示詞在 `websearch.py` | `tests/test_web.py`；用 `web search ... --reason` 實測（會花錢、寫真實 DB） |
| 加 Telegram 指令 | `telegram_bot.py`：`CoachService` 加方法（在 worker thread 跑）、`CoachBot.cmd_*`、handler 表、`BotCommand` 清單、`HELP` | `tests/test_telegram.py` |
| 加終端機指令 | `cli.py` 的 `cmd_*` 與 parser | README 指令表 |
| 加環境變數 | `config.py` 讀、`.env.example` 加範例、附錄 A | |

**重新校準網路搜尋門檻**：用真實 Embedder 把幾組查詢（訓練科學問題、賽事 / 產品查詢、同一需求不同寫法、同賽事不同面向）向量化，對 `KnowledgeBase.nearest` 算最高餘弦、查詢之間算餘弦，把 `WEB_KB_FLOOR` 設在「賽事類最高值」與「科學類最低值」之間偏低的位置，`WEB_CACHE_SIMILARITY` 設在「同需求不同寫法」下緣、`WEB_CACHE_MAYBE` 設在「同賽事不同面向」下緣。目前的數字在第 8 節。

**改程式的規矩**

1. 介面文字（CLI、提示詞、Telegram 回覆）用繁體中文，註解中文即可。
2. 改完跑 `tests/run.sh`，全過再 commit；commit 訊息英文一行為主。
3. 改 `garmin_coach/*.py` 後 `scripts/botctl.sh restart`（或 systemctl）。
4. 真實對話測試用 `scripts/ask.py --copy`。
5. 不要印出秘密；`data/`、`briefs/`、`.env` 不進 git。
6. 改完功能更新 `HANDOFF.md`（現況表、模組地圖、變更紀錄）與本說明書對應段落。

**用 Claude Code 維護**：它會先讀 `CLAUDE.md` 再讀 `HANDOFF.md`；所有上述規矩都寫在那裡。對話裡不要貼秘密，要改 `.env` 請它直接寫檔。

## 15. 交接檢查清單

接手的人把下面八條跑一遍，全部通過就代表環境、秘密、資料、三個外部服務都正常。

```bash
cd ~/garmin_project && git status && git log --oneline -3    # 工作樹乾淨、最新 commit 與本文件一致
ls -la .env && grep -c . .env                               # 秘密檔存在、權限 600
.venv/bin/python -m garmin_coach stats                      # 資料庫正常，筆數合理
.venv/bin/python -m garmin_coach sync daily                 # Garmin token 有效（失效會要 MFA）
tests/run.sh                                                # 七支離線測試全過
.venv/bin/python scripts/ask.py --copy "今天狀況如何"          # OpenAI 正常，教練有回答並印費用
scripts/botctl.sh status                                    # bot 在跑、沒有重複程序；Telegram 按 /status 有回
.venv/bin/python -m garmin_coach cost                       # 費用記帳正常
```

交接給新的人時另外：輪換 OpenAI key 與 Telegram token；把新人的 Telegram id 填進 `TELEGRAM_ALLOWED_USERS`（舊的拿掉）；決定要不要清空記憶（`events` / `notes` / `interests` / `conversations` 是原使用者的個人資料）。

## 附錄 A　環境變數總表（`.env`）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| GARMIN_EMAIL / GARMIN_PASSWORD | （必填） | Garmin 帳密，只在 token 失效時用 |
| GARMIN_IS_CN | 0 | garmin.cn 帳號設 1 |
| GARMINTOKENS | ~/.garminconnect | token 目錄 |
| GARMIN_WEATHER_UNITS | imperial | Garmin 天氣端點回 °F / mph，normalize 會轉；metric 則不轉 |
| GARMIN_API_PAUSE | 0.4 | 每次 Garmin 呼叫間隔秒數 |
| GARMIN_COACH_DATA | 專案內 data/ | 資料目錄 |
| EXTERNAL_AQI | 1 | 活動同步時打 Open-Meteo 抓 AQI |
| OPENAI_API_KEY | （必填） | |
| OPENAI_MODEL | gpt-6-luna | 簡單題模型 |
| OPENAI_DEEP_MODEL | gpt-6.1-sol | 深入題模型；空白 = 不分流 |
| OPENAI_ROUTER | auto | auto / simple / deep |
| OPENAI_EXTRACT_MODEL | （空 = 主模型） | 結束萃取模型；目前設 luna |
| OPENAI_REASONING | （空） | none / low / medium / high |
| OPENAI_DAILY_BUDGET_USD | 2 | 每日費用上限，0 = 不限 |
| REUSE_SIMILARITY / REUSE_DAYS | 0 / 30 | 問答重用，0 = 關閉 |
| WEB_SEARCH_ENABLED | 1 | 網路搜尋總開關 |
| WEB_SEARCH_MODEL | （空 = OPENAI_MODEL） | 子請求模型 |
| WEB_SEARCH_CONTEXT | medium | low / medium / high |
| WEB_SEARCH_MAX_CALLS | 3 | 一次子請求最多幾次 web_search |
| WEB_SEARCH_DAILY_MAX | 5 | 每天放行次數 |
| WEB_SEARCH_PER_CONVERSATION | 3 | 每段對話放行次數 |
| WEB_SEARCH_CALL_USD | 0.01 | 每次 web_search 呼叫的工具費估算 |
| WEB_CACHE_DAYS / WEB_CACHE_DAYS_NOTFOUND | 90 / 14 | 快取有效天數 / 「未找到」結果的有效天數 |
| WEB_CACHE_SIMILARITY / WEB_CACHE_MAYBE | 0.85 / 0.70 | 直接命中 / 交 luna 確認的下限 |
| WEB_KB_FLOOR | 0.45 | 知識庫最高相似度低於此直接放行 |
| WEB_SEARCH_COUNTRY / WEB_SEARCH_TIMEZONE | TW / Asia/Taipei | 搜尋結果偏向的國家與時區 |
| PUBMED_EMAIL | 空 | kb build 時給 PubMed 的聯絡 email（選填） |
| KB_PACK_URL | 作者的 GitHub Release | `kb download` 的來源（網址或本機 .xz） |
| KB_PACK_FALLBACK_URL | 作者的 Dropbox 分享連結 | 預設來源 404 時改抓；空白 = 不備援 |
| KB_PACK_SHA256 | 預設發布包的值 | 預設來源的期望 sha256；`kb download --sha256` 可指定，其他來源看網址旁的 `.sha256` |
| TELEGRAM_BOT_TOKEN | （必填） | |
| TELEGRAM_ALLOWED_USERS | （必填） | 數字 id 或 @username，逗號分隔 |
| TELEGRAM_IDLE_MINUTES | 30 | 閒置自動結束並整理記憶 |

## 附錄 B　文件地圖

| 文件 | 用途 |
| --- | --- |
| `MANUAL.md` | 本說明書（repo 內的正本） |
| `README.md` | 使用說明與指令 |
| `HANDOFF.md` | 維護交接：現況表、資料流、設計決策、踩過的坑、變更紀錄 |
| `CLAUDE.md` | 給 Claude Code 的專案守則 |
| `docs/WEB_PLAN.md` | 多人版網頁規劃（暫緩） |
| `.env.example` | 環境變數範本 |
| `deploy/garmin-coach-telegram.service` | systemd 服務檔，檔頭有安裝步驟 |
