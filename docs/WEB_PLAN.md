# 多人版網頁規劃：讓別人連自己的 Garmin、跟教練對話、留下訓練紀錄

狀態：**暫緩**（2026-10-20 使用者決定先維持單人版，這份只是日後要做時的規劃）。目前程式是單人版（一個 `.env`、一個 `data/garmin_coach.db`、一個 Telegram 白名單）。這份文件定義多人版的流程、架構、資料隔離、安全與分階段做法。

---

## 0. 先回答三個會決定整個方案的問題

### Q1. 別人的 Garmin 資料怎麼拿？
| 方案 | 做法 | 優點 | 代價 |
|---|---|---|---|
| **A. 沿用 python-garminconnect（非官方）** | 使用者在網頁輸入 Garmin 帳密（＋MFA 驗證碼）一次，伺服器登入後**只保存 OAuth token**（加密），密碼不落地 | 現有程式全部可用，資料最完整（HRV、準備度、逐秒、LT、FTP 全有） | 違反 Garmin 服務條款的風險；使用者要把帳密交給你的網站（信任門檻高）；Garmin 可能對同一 IP 大量請求限流或封鎖；token 約一年要重連 |
| **B. Garmin 官方 Health / Activity API** | 向 Garmin Developer Program 申請合作，使用者用 Garmin 官方 OAuth 授權（像「用 Google 登入」） | 合法、使用者不用交帳密、Garmin 主動推資料（webhook） | 要申請（通常要公司／產品說明，審核數週）；資料欄位比非官方少（沒有訓練準備度、逐秒功率要另外要） |
| **C. 使用者自己上傳** | 上傳 Garmin 匯出的 FIT / 每日資料 CSV | 零法律風險 | 體驗差，每日恢復資料拿不到，多數人不會持續做 |

**建議**：先用 **A 做封閉測試**（朋友、俱樂部，十人內，明確告知風險與資料用途），同時**申請 B**。程式架構設計成「Garmin 來源可抽換」（同步層只認一個 `GarminSource` 介面），之後換成官方 API 不動上層。

### Q2. OpenAI 的錢誰出？
- **你出、設配額**：每人每日 US$0.2、每月 US$3（luna 一天 3 段對話約 US$0.02，很夠）。現有 `llm_calls` 已經是每個資料庫一份，改成每人一份資料庫就自然變成每人配額。
- **使用者自帶 key（BYOK）**：進階選項，key 加密存放。
- 建議：預設你出＋配額，設定頁可填自己的 key 覆蓋。

### Q3. 資料隔離怎麼做？
**每個使用者一個 SQLite 檔**（`data/users/<uid>/garmin_coach.db`）＋ 一個 Garmin token 目錄（`data/users/<uid>/garmin_tokens/`）。
- 現有 `Store / Tools / Coach / sync` 全部是「給它一個 store 就能跑」，幾乎不用改。
- 隔離是檔案層級的，不會因為一個 SQL 少寫 `WHERE user_id=` 就漏資料。
- 備份、刪除帳號 = 刪一個資料夾。
- 到幾百人以上再考慮 Postgres。知識庫 `knowledge.db` 所有人共用、唯讀。

---

## 1. 使用者流程

```
註冊/登入（email + 密碼，或 Google 登入）
   │
   ▼
連接 Garmin（輸入 Garmin 帳密 → 若需 MFA，網頁顯示「請輸入 Garmin 寄給你的驗證碼」）
   │   伺服器：登入成功 → 存加密 token → 丟掉密碼 → 排入「初始同步」工作
   ▼
初始同步（背景，1–3 分鐘）：profile、60 天每日恢復、90 天活動（含逐秒）、一年指標歷史
   │   網頁顯示進度條（每日 xx/60、活動 xx/90）
   ▼
儀表板：今天身體狀況（normal/attention/poor + 原因）、最近訓練、目標賽事倒數、週負荷
   │
   ├─ 訓練紀錄：活動列表 → 單筆詳情（分段、心率/功率區間、漂移、天氣、AQI）→ 填 RPE/體感/疼痛
   ├─ 對話：選一次訓練（預設最近）或一般規劃 → 對話（教練會自己查資料與論文）→ 結束整理記憶
   ├─ 記憶：賽事 / 筆記 / 常問名詞，可編輯、可刪
   ├─ 知識庫：直接搜論文（唯讀）
   └─ 設定：時區、住家位置（天氣用）、模型、每日預算、自己的 OpenAI key、重新連接 Garmin、匯出/刪除所有資料
   
每日自動（排程）：06:00 當地時間同步昨天＋今天 → 若有新活動，推播「要不要聊聊今天的 XX？」（網頁通知或 Telegram）
```

## 2. 系統架構

```
瀏覽器 ──HTTPS──▶ Caddy/nginx ──▶ FastAPI（web/）
                                   ├─ auth（session cookie）
                                   ├─ pages：dashboard / activities / chat / memory / settings
                                   ├─ api：/api/chat（送一句、回結果或 SSE 串流）、/api/sync、/api/feedback …
                                   └─ jobs：工作佇列（同步、對話、結束萃取）
                                           │
                                           ▼
                              Worker（同一台機器的背景程序，或 RQ/Celery）
                              ├─ 每個使用者一把鎖：同一人同時只跑一件事（Garmin 怕併發、SQLite 單寫）
                              ├─ 全域 Garmin 節流：所有使用者排隊，請求間隔 ≥ 0.4s，429 就退避
                              └─ 呼叫現有 garmin_coach：sync.* / Coach.ask / Coach.finish
                                           │
            ┌──────────────────────────────┼──────────────────────────┐
            ▼                              ▼                          ▼
   data/users/<uid>/garmin_coach.db   data/users/<uid>/garmin_tokens/   data/knowledge.db（共用唯讀）
   data/app.db（帳號、session、使用者設定、工作狀態）
```

- **為什麼要 worker**：`Coach.ask` 要 10–30 秒、同步要 1–3 分鐘，不能在 HTTP request 裡做。網頁送出後拿到 job id，前端輪詢或用 SSE 等結果。
- **Telegram 多人版**：bot 用 Telegram user id 對應到 app 使用者（在設定頁「綁定 Telegram」產生一次性代碼），之後 `CoachService` 改成依 chat_id 取該使用者的 store。現在的 bot 架構已經是「每個 chat 一個 session」，只差 store 來源。

## 3. 現有程式要改什麼（多租戶化）

| 現況 | 改法 |
|---|---|
| `config.DB_PATH`、`Store()` 預設全域路徑 | `Store(path)` 已可指定；新增 `UserContext(uid)` 提供 `db_path / token_dir / tz / settings`，所有入口改吃它 |
| `GarminClient()` 從 `.env` 讀帳密與 token 目錄 | 改成 `GarminClient(token_dir=user.token_dir, prompt_mfa=web_mfa_callback)`；帳密只在「連接」那一次用，不存 |
| `date.today()` 散在 tools/sync | 改成 `ctx.today()` 依使用者時區 |
| `Tools(store)`、`Coach(store, llm)` | 不用改（已經是依 store） |
| `OpenAILLM()` 讀全域 key/模型 | 建構時傳入該使用者的 key（或預設 key）與模型 |
| `llm_calls` 每日預算 | 已經在每個 store 裡 → 自動變成每人預算；再加一個全域總預算（app.db） |
| `telegram_bot.CoachService` 單一 store | `sessions[chat_id]` 改存 `(uid, coach)`；用綁定表找 uid |
| `.env` 單一組秘密 | 伺服器只留：app secret、資料加密 key、預設 OpenAI key、Telegram token。使用者的 Garmin token / 自帶 key 用 Fernet 加密存檔 |

## 4. 資料模型（新增的 app.db）

```
users(id, email, password_hash, created_at, tz, display_name, status)
sessions(id, user_id, expires_at)
user_settings(user_id, model, daily_budget_usd, openai_key_enc, home_lat, home_lon, home_name, telegram_chat_id)
garmin_links(user_id, status: none/pending_mfa/linked/expired, linked_at, last_sync_at, last_error)
jobs(id, user_id, kind: initial_sync/daily_sync/chat/finish, status, created_at, started_at, finished_at, payload, result, error)
audit_log(ts, user_id, action, detail)   -- 登入、連接 Garmin、刪除資料、預算拒絕
```
使用者的訓練資料、記憶、對話、llm_calls 都留在各自的 `garmin_coach.db`（schema 不動）。

## 5. 安全與合規（多人版必做）

1. **HTTPS**、session cookie `HttpOnly; Secure; SameSite=Lax`、CSRF token、登入失敗限流、密碼用 argon2。
2. **Garmin 密碼不落地**：只在記憶體用一次；MFA 流程中 pending 狀態也只存 token 半成品，不存密碼。token 檔用 Fernet 加密，key 放環境變數不放 repo。
3. **每人、全域雙層預算**：每人每日上限（現有機制）＋ 全站每日上限；超過就拒答並記 audit_log。
4. **主題限制已在教練提示**（只回答運動與健康）；另外在 API 層加：每人每分鐘 N 句、單句 ≤ 2000 字、工具回合 ≤ 8。
5. **資料權利**：設定頁提供「匯出我的所有資料（zip）」與「刪除帳號與所有資料」；刪除 = 刪整個資料夾 + app.db 列，立即生效。
6. **隱私聲明**要寫清楚：資料存哪、送去 OpenAI（`store=False`）、送經緯度到 Open-Meteo、用非官方 Garmin API 的風險、不做醫療診斷。
7. **log 不含個資與秘密**；錯誤回傳給使用者的訊息不帶內部路徑。
8. **備份**：每日 rsync `data/users/` 到另一台（加密）；還原演練一次。

## 6. 技術選型（建議）

- 後端：**FastAPI**（同一個 Python 專案，直接 import `garmin_coach`）＋ `uvicorn`。
- 前端：**HTMX + Jinja2 模板**（最省，頁面少、互動簡單）；對話頁用 SSE 顯示「教練查了 X」與逐段回覆。之後要 app 化再換 React。
- 工作佇列：第一版用 **內建 worker thread pool + per-user lock**（和現在 Telegram bot 一樣的做法），存進 `jobs` 表；人數多再換 RQ（Redis）。
- 排程：APScheduler（每日同步）。
- 部署：一台 VPS（2 vCPU / 4 GB 夠跑 50 人），Docker Compose（app、worker、caddy），systemd 守護。
- 監控：健康檢查端點、每日費用與失敗工作的 Telegram 通知給你。

## 7. 分階段與工作量（一人全職估）

| 階段 | 內容 | 估計 |
|---|---|---|
| P0 多租戶化 | UserContext、Garmin 來源抽象、時區、worker + per-user lock、加密 token、tests 跑在多個 store | 3–4 天 |
| P1 帳號與連接 | FastAPI 骨架、註冊/登入、連接 Garmin（含 MFA 頁）、初始同步工作與進度頁、儀表板 | 4–5 天 |
| P2 訓練紀錄與對話 | 活動列表/詳情/回饋、對話頁（SSE）、結束整理、記憶頁 CRUD | 4–5 天 |
| P3 自動化與通知 | 每日排程同步、新活動提醒、Telegram 多人綁定、`/cost` 等 | 2–3 天 |
| P4 上線前 | 預算雙層、匯出/刪除、隱私聲明、備份、HTTPS 部署、封測 5–10 人 | 3–4 天 |
| 合計 | | 約 3–4 週 |

## 8. 風險與對策

| 風險 | 對策 |
|---|---|
| Garmin 對伺服器 IP 限流／封鎖（多人共用一個出口 IP） | 全域排隊、間隔 ≥ 0.4s、每人每天最多 2 次同步、429 退避 10 分鐘；長期走官方 API |
| 使用者不願交 Garmin 帳密 | 清楚說明只存 token、提供「上傳 FIT」替代；官方 API 申請到就換 |
| MFA 流程卡住（驗證碼 10 分鐘內要填） | 網頁 pending 狀態＋倒數＋重送；失敗回到重新連接 |
| OpenAI 費用失控 | 每人＋全站預算、模型固定 luna、工具輸出截斷（已做） |
| 健康資料外洩 | 檔案層隔離、加密 token、HTTPS、最小 log、備份加密、刪除權 |
| 教練給出醫療建議被當診斷 | 提示已有紅旗就醫規則；頁面加免責聲明；傷痛類回答固定附「非醫療診斷」 |

## 9. 第一步（下次開工就做）

1. 在 `garmin_coach/` 加 `user_context.py`（uid → 路徑、時區、設定），把 `cli`、`telegram_bot` 改成透過它拿 store（單人版就是 uid=`default`，行為不變）。
2. `client.py` 拆出 `GarminSource` 介面，現有實作改名 `GarminConnectSource`；token 目錄從參數來。
3. `tests/` 加一個「兩個使用者各自同步與對話，互不可見」的測試。
4. 建 `web/` FastAPI 骨架：登入、連接 Garmin（含 MFA）、同步進度、儀表板四個頁面。
