# Garmin AI Coach

把自己的 Garmin Connect 資料抓到本機 SQLite，讓 OpenAI 模型扮演私人耐力教練：對話時它自己用工具讀你的恢復、訓練與體能資料，查本機的論文知識庫，再把賽事、傷痛、體感這些寫回記憶。入口是 Telegram bot（手機）或終端機。單人、自架、資料不離開你的主機。

> 文件地圖：這份 README 是安裝與使用；`MANUAL.md` 是完整產品說明書（架構、內部原理、運維、費用、坑）；`HANDOFF.md` 是作者那套部署的維護筆記；`CLAUDE.md` 是給 Claude Code 的改碼守則。離線測試 `tests/run.sh`；命令列問一句 `scripts/ask.py`；bot 啟停 `scripts/botctl.sh`。

## 功能

- **Garmin 同步**：每日恢復（HRV、RHR、睡眠、壓力、Body Battery、準備度、負荷 / ACWR）、每筆活動（分段、心率 / 功率區間、天氣、逐秒資料、HR drift、Pa:Hr）、體能快照與一年指標趨勢、Open-Meteo 空氣品質。原始 JSON 全部保留。
- **AI 教練**：對話前打包三層資料包；對話中 18 個工具按需取資料並寫回記憶（賽事、傷痛、體感、常問名詞）；簡單題用便宜模型、深入題用深入模型自動分流；每日費用上限。
- **論文知識庫**：PubMed 14 個運動科學 / 運動醫學主題，本機向量 + BM25 混合檢索，給建議前先查。
- **網路搜尋**：只在本機資料拿不到時放行（快取 → 知識庫 → 每日上限），查到的東西完整記錄、之後重用。
- **Telegram bot**：開新對話自動同步 Garmin，30 分鐘沒講話自動結束並整理記憶；白名單只允許你自己。
- **離線測試**：`tests/run.sh` 不打任何 API；沒有真實資料庫時自動用合成資料。

## 需要準備

| 項目 | 說明 |
|---|---|
| Linux 主機、Python 3.10+ | 作者在 Linux 上跑；macOS 理論上可以但沒測 |
| Garmin Connect 帳號 | 任何會同步到 Garmin Connect 的裝置 |
| OpenAI API key | platform.openai.com；建議單獨開一個 project 並設每月預算。實測簡單問題約 US$0.003、深入問題約 US$0.05，作者一個月約 US$1–4 |
| Telegram bot（選用） | 向 @BotFather 申請；不用 Telegram 也能在終端機 `chat` |

## 快速開始

```bash
git clone https://github.com/shun2249/garmincoach.git && cd garmincoach
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env       # 填 GARMIN_EMAIL / GARMIN_PASSWORD / OPENAI_API_KEY（Telegram 可晚點）
P=".venv/bin/python -m garmin_coach"

$P login                      # 第一次登入 Garmin（可能要輸入 MFA）；token 存 ~/.garminconnect，之後不再需要密碼
$P sync all --days 60         # 每日恢復 + 活動（含細節）+ 體能 + 身體資料，約 10 分鐘（每次呼叫間隔 0.4 秒，避免 429）
$P sync history               # 一年指標趨勢（VO2max / 乳酸閾值 / 比賽預測）
$P stats                      # 看筆數
$P chat                       # 終端機跟教練對話（針對最近一次訓練）；/end 結束並整理記憶
```

選用但建議，論文知識庫（三選一）：

```bash
$P kb download                # 下載作者預建的知識庫：2,285 篇、21,543 段已向量化，約 70 MB，一分鐘、免費
$P kb build --manifest        # 照 kb/papers.tsv 的清單自己向 PubMed / Europe PMC 抓同一批論文再向量化；約 10 分鐘、embedding 約 US$0.1
$P kb build                   # 自己重新搜尋 14 個主題（收錄的論文會和作者的不同）；約 15 分鐘、embedding 約 US$0.1
$P kb stats
```

沒有知識庫教練也能用，但建議會變成「經驗判斷」而沒有文獻依據，而且網路搜尋的閘門算不到相似度會一律放行、比較花錢。

Telegram：

1. 向 @BotFather 送 `/newbot` 拿 token，填進 `.env` 的 `TELEGRAM_BOT_TOKEN`。
2. `scripts/botctl.sh start`，對 bot 講任何話，它會回你的 user id。
3. 把 id 填進 `TELEGRAM_ALLOWED_USERS`，`scripts/botctl.sh restart`。之後直接打字就能聊，開新對話會先自動同步 Garmin。
4. 長期執行改用 systemd：`deploy/garmin-coach-telegram.service` 檔頭有步驟（先把路徑改成你的 clone 位置）。

## 隱私與安全

- 所有資料（Garmin 原始 JSON、SQLite、對話與記憶、知識庫、log）都在本機 `data/`，不進 git。
- 秘密只在 `.env`（已 gitignore，建議權限 600）；Garmin token 在 `~/.garminconnect`。
- 離開主機的只有：Garmin API 的抓取請求、OpenAI API（對話內容、資料包、工具結果、embedding、放行的網路搜尋）、Open-Meteo（經緯度）、PubMed / Europe PMC（建庫時）。
- Telegram 白名單以外的人只會收到自己的 id，拿不到任何資料。

## 語言與模型

提示詞、教練回覆、CLI 與文件都是繁體中文；要換語言改 `garmin_coach/coach.py` 的 `SYSTEM_PROMPT`、`EXTRACT_PROMPT` 與 `telegram_bot.py` 的 `HELP`。模型名稱（`OPENAI_MODEL`、`OPENAI_DEEP_MODEL`、`OPENAI_EXTRACT_MODEL`）在 `.env` 改；費用估算表在 `garmin_coach/pricing.py`。

## 架構

```
garmin_coach/
  config.py     環境變數與路徑
  client.py     Garmin 登入（token 快取 / MFA）＋批次抓取
  normalize.py  原始 JSON → 結構化模型（含 HR drift / Pa:Hr 脫鉤計算）
  models.py     DailyRecovery / TrainingSession / SessionFeedback / PhysioSnapshot
  store.py      SQLite（欄位由模型自動產生；新增欄位會自動 ALTER）
  external.py   Open-Meteo 空氣品質（AQI / PM2.5，免金鑰）
  context.py    個人情境：profile / events / notes / interests
  sync.py       同步流程
  brief.py      訓練後整理包（JSON + Markdown，貼給 ChatGPT 網頁用）
  tools.py      AI 可呼叫的資料工具（三層資料 + 寫回）與 function calling schema
  coach.py      對話迴圈：preflight 資料包 → 工具呼叫 → 結束萃取記憶
  cli.py        命令列
data/
  garmin_coach.db     結構化資料
  raw/daily/          每天的原始 JSON
  raw/activities/     每筆活動的原始 JSON（含逐秒資料、分段、天氣）
  raw/physio/         乳酸閾值 / FTP / 比賽預測
```

### 資料層對應

| 類別 | 欄位 | 來源 |
|---|---|---|
| 生理恢復 | HRV、RHR、睡眠、Stress、Body Battery、訓練準備度、恢復時間、ACWR | `daily_recovery` |
| 運動能力 | 配速、GAP、功率/NP/TSS/IF、HR drift、Pa:Hr、Pw:Hr、VO2max、訓練負荷、TE、跑步動態 | `activities` |
| 外部因素 | 氣溫、體感、濕度、露點、風速、天氣、AQI、PM2.5、爬升/下降 | `activities`（Garmin 天氣 + Open-Meteo） |
| 主觀回饋 | RPE、體感、疼痛、生病 | `session_feedback`（`log` 指令）+ 手錶自評 |
| 個人情境 | 比賽目標、家庭行程、時間限制、受傷、生病、近況 | `events` / `notes` / `profile` |
| 關注詞彙 | 常問的專業名詞（400m 配速、LT1 …）及次數 | `interests` |
| 體能快照 | 乳酸閾值 HR/配速、跑步/自行車 FTP、比賽預測、耐力/爬坡分數 | `physio_snapshots` |

## 使用

```bash
P=".venv/bin/python -m garmin_coach"

$P login                          # 第一次登入（可能要輸入 MFA），token 存 ~/.garminconnect
$P sync --days 30                 # 抓 30 天：每日恢復 + 活動（含逐秒資料）+ 體能快照
$P sync activities --from 2026-01-01 --to 2026-06-30 --no-details   # 回補歷史，較快
$P activities                     # 列出最近活動
$P show day 2026-10-19            # 看某天的結構化資料
$P show activity 123456789

# 訓練後：記錄主觀回饋，再產生整理包
$P log latest --rpe 7 --feeling "腿有點重" --pain "左腳踝輕微痠"
$P brief                          # 印出 Markdown，直接貼給 ChatGPT
$P brief --json --save            # 存到 briefs/<id>.json

# 個人情境（AI 對話後有用的內容也記在這裡）
$P context set goal '"全馬 sub 3:30"'
$P context event add race "台北馬拉松" --date 2026-12-20 --priority A
$P context event add travel "出差" --date 2026-11-02 --end 2026-11-05 --notes "只能用跑步機"
$P context note add injury "左腳踝 10/19 起輕微痠痛"
$P context note add race_progress "馬拉松專項期第 2 週" --expires 2026-11-01
$P context interest add LT1 --definition "第一乳酸閾值"
$P context show
```

## 注意事項

* **登入限制**：Garmin 對帳密登入（SSO）有 Cloudflare 擋與頻率限制。第一次成功後請保留 token 目錄，之後都走 token。
* **抓取速度**：每個 API 呼叫間隔 `GARMIN_API_PAUSE`（預設 0.4 秒）。一筆活動約 6 次呼叫，一天約 8 次。
  回補大量歷史請用 `--no-details`（跳過逐秒資料，但不會有 HR drift）。
* **今天 / 昨天永遠重抓**：這兩天的睡眠、Body Battery、訓練準備度會持續更新。其他天已存在就略過，`--force` 可強制。
* **天氣單位**：Garmin 活動天氣 API 回 °F / mph，預設 `GARMIN_WEATHER_UNITS=imperial` 會轉成 °C / km/h。
  若發現數字不合理，改成 `metric`。
* **原始資料不會遺失**：所有 Garmin 回應都存在 `data/raw/`，欄位名稱改變時只需修 `normalize.py` 再 `sync --force`。

## AI 教練對話（第二階段）

對話開始前，程式會先打包三層資料（`tool preflight` 可以看到 AI 拿到什麼），
對話中 AI 可以再呼叫工具拿更多資料，並把學到的東西寫回資料庫。

```bash
$P sync                            # 每天：恢復 + 活動 + 體能 + 身體資料
$P sync history                    # 每週：一年指標趨勢（VO2max / LT / 預測 / 耐受度）
$P chat                            # 針對最新一筆訓練對話（需 .env 的 OPENAI_API_KEY）
$P chat --model gpt-5.4-mini       # 臨時換模型
$P chat 24762131965                # 指定活動
$P tool list                       # AI 可用的工具
$P sync details --min-km 15 --days 365   # 替只有摘要的舊活動補分段/逐秒細節
$P tool get_health_status          # 直接看某個工具的結果
$P tool get_activity_timeseries '{"step_s": 120}'
```

### AI 取得資料的三層

| 層 | 工具 | 內容 |
|---|---|---|
| 基礎資料 | `get_athlete_profile` | 年齡、身高、體重、VO2max、乳酸閾值、心率區間（跑步/自行車）、功率區間 |
| | `get_health_status` | 當天 RHR / HRV / 睡眠 / 壓力 / Body Battery / 準備度，與 30 天基準比較 → normal / attention / poor + 原因 |
| | `get_daily_recovery` | 任意日期範圍的每日恢復 |
| | `get_weather_outlook` | 近 3 天 + 未來 3 天氣溫、體感、濕度、降雨、日出日落、清晨與傍晚氣溫 |
| | `get_athlete_context` | 目標賽事倒數、行程限制、傷病與近況筆記、常關注主題 |
| 過去訓練 | `get_recent_training` | 最近 10 天所有訓練、各項目總量、急慢性負荷與 ACWR 趨勢 |
| | `get_sport_history` | 同項目一年：每月量、最長/最快/最高負荷、心率效率趨勢（速度/心率） |
| | `get_fitness_metrics` | 乳酸閾值、FTP、比賽預測、VO2max、跑步耐受度、耐力/爬坡分數的最新值與一年趨勢 |
| 這次訓練 | `get_activity_detail` | 摘要、分段、心率/功率區間時間與邊界、HR drift、天氣、AQI、主觀回饋 |
| | `get_activity_timeseries` | 逐秒資料降採樣：心率、配速、GAP、功率、步頻、海拔、溫度、體力、表現狀況 |
| 寫回 | `record_note` | 不適 / 疼痛 / 生病（部位、程度、何時、什麼情況會痛）、近況、比賽進度 |
| | `record_event` / `update_event` | 比賽、行程、時間限制、傷病期間 |
| | `record_interest` | 問到的專業詞彙或常關注主題，累計次數 |
| | `log_feedback` | RPE、體感、疼痛、生病 |

對話結束（`/end`）時會再跑一次萃取：產生摘要，補記對話中漏掉的筆記、詞彙、事件、回饋，存進 `conversations` / `notes` / `interests`。

### 模型與費用

**分流（2026-10-20 起預設開啟）**：每句先用便宜模型判斷是「簡單」（解釋數據、看狀態、單一事實、記錄）還是「深入」（課表、賽事策略、傷痛處置、多因素權衡、修改規劃的追問），簡單的交給 `OPENAI_MODEL`（luna），深入的交給 `OPENAI_DEEP_MODEL`（sol）。分類一次約 US$0.0002。
`OPENAI_ROUTER=auto|simple|deep` 設預設；Telegram 用 `/model auto|simple|deep` 切換（跨對話保留）；終端機 `chat --router simple` 或 `--deep-model`。回覆尾端 ⚡ 表示便宜模型、🧠 表示深入模型。
實測：簡單題 luna 約 US$0.002–0.003，深入題 sol 約 US$0.02–0.07（含工具呼叫）；需要上網的問題另加每次約 US$0.04–0.05（見「網路搜尋」）。中途換模型時，另一個模型的推理項目不會送回（只保留文字與工具呼叫），已驗證可來回切換。

`.env` 的 `OPENAI_MODEL` 是對話模型，`OPENAI_EXTRACT_MODEL` 是結束時做摘要/補記的便宜模型。
程式走 Responses API，gpt-5 / gpt-6 系列都可用；每次對話結束會印出 token 與估算費用，也存在 `conversations.usage`。

實測一問一答（含工具呼叫與結束萃取）約 22K 未快取輸入 + 13K 快取輸入 + 1.3K 輸出：

| 對話模型 | 單次問答約 | 5 輪對話約 |
|---|---|---|
| gpt-6-luna（預設） | US$0.003 | US$0.01 |
| gpt-5.4-mini | US$0.02 | US$0.08 |
| gpt-5.1 | US$0.04 | US$0.16 |
| gpt-6.1-sol | US$0.06 | US$0.22 |
| gpt-5.4 | US$0.08 | US$0.30 |
| gpt-5.5 | US$0.15 | US$0.60 |

### 活動細節層級

一年歷史用 `--list-only` 回補，只有清單欄位（`detail_level: list`）。最近 14 天有完整細節（分段、天氣、逐秒資料）。
要補某段期間的細節：

```bash
$P sync activities --from 2026-09-01 --to 2026-09-30 --force
```

## 背景知識庫（論文 RAG）

近 10 年（2016 起）PubMed 論文，優先系統性回顧、統合分析、RCT，有開放全文的從 Europe PMC 抓全文（2026-10-20：2,285 篇、664 篇全文、21,543 段）。
切段後用 OpenAI `text-embedding-3-small` 建向量，存在 `data/knowledge.db`，檢索用向量 + BM25 混合。
AI 教練給建議時會先呼叫 `search_knowledge`，把研究結論融進建議；回答裡不標作者年份或論文出處（想確認有沒有查文獻看 `/tools`）。

```bash
$P kb topics                       # 14 個主題
$P kb build                        # 全部建庫（每主題 120 高品質 + 60 一般；約 15 分鐘）
$P kb build --topics nutrition --hq 200       # 單一主題加量
$P kb search "polarized training recreational runners" -k 5
$P kb stats
```

主題：Zone 2 / 強度分布、LT1 / LT2 閾值、間歇、耐力、肌力、跑步、自行車、馬拉松、運動飲食、恢復與監控（HRV、睡眠、負荷、熱適應、減量）；
運動醫學四組：跑步傷害（PFP、阿基里斯腱、ITB、足底筋膜、脛骨壓力、骨壓力傷害）、自行車傷害（過度使用、bike fit、膝/腰/頸/會陰）、傷害預防與回場（負荷管理、肌力、復健）、運動醫學（熱傷害、低血鈉、過度訓練/RED-S、心臟、感染）。
教練遇到疼痛、傷害或傷害預防問題會查這四組，紅旗症狀會建議就醫。
建庫可重複執行，已存在的論文會略過，只補新的與缺 embedding 的段落。
原始資料在 `data/knowledge_raw/`，改切段方式可以不用重抓。

### 取得或發布知識庫

`data/` 不進 git，所以知識庫另外走兩條路：

- **預建包**：`kb download` 從 GitHub Release 抓 `knowledge.db.xz`（向量轉 float16、去掉可重建的全文索引後約 70 MB），驗 sha256、解壓、重建索引，放到 `data/knowledge.db`。已有論文的知識庫不會被覆蓋（`--force` 才會）。下載網址在 `KB_PACK_URL`，預設指向作者最近一次發布，404 時改抓 `KB_PACK_FALLBACK_URL`（作者 Dropbox 上的同一個檔）；期望的 sha256 釘在 `KB_PACK_SHA256`，兩個來源都要對得上。
- **論文清單**：`kb/papers.tsv` 是知識庫的書目清單（進 git，約 470 KB），`kb build --manifest` 照清單向 PubMed / Europe PMC 抓同一批論文並自己向量化，不依賴 PubMed 當天的排序。
- 作者更新知識庫後：`kb export`（產生 `data/export/knowledge.db.xz`、`.sha256`，以及 25 MiB 一片的 `.001`、`.002`… 分片）上傳到新的 Release（傳單檔或傳分片都可以，`kb download` 會自動接回來），改 `KB_PACK_URL` 預設值，`kb manifest` 重寫清單後 commit。

預建包裡是 PubMed 摘要與 Europe PMC 開放取用全文，版權屬各論文的出版者與作者（開放全文多為 CC BY / CC BY-NC 授權），這裡只做個人研究用途的檢索；不放心就用 `kb build --manifest` 自己抓。

## 網路搜尋（search_web）

論文知識庫和 Garmin 資料都拿不到的外部事實（賽事日期、路線、爬升、關門時間、規則、裝備規格），教練可以用 `search_web` 上網查，背後是 OpenAI Responses API 的內建 `web_search`。因為費用高（一次約 US$0.04–0.05），工具層有閘門，順序是：

1. **快取**：同樣或相近的問題之前查過，直接回傳舊結果（免費），回答會註明資料日期。「未找到」的結果只保留 14 天。
2. **知識庫檢查**：先算問題和論文知識庫的相似度，知識庫顯然有的（訓練方法、生理、營養、傷害）由便宜模型確認後**拒絕上網**，並把論文段落直接交給教練回答。
3. **上限**：每天 `WEB_SEARCH_DAILY_MAX`（5）次、每段對話 3 次。
4. 放行後由便宜模型（luna）在獨立的子請求裡搜尋並整理成「結論 / 逐條事實 / 資料日期 / 來源 URL」，**完整存進 `web_searches`**（整理全文、實際搜尋字串、來源、引用、API 原始回傳、token、費用），之後同一件事不會再查。

```bash
$P web list                         # 紀錄：✅ 放行 ⛔ 拒絕 ❌ 失敗，含費用與重用次數
$P web show 3                       # 某一筆的完整整理與引用
$P web search "2026 IRONMAN 70.3 Kenting bike course" --reason "本地沒有賽事路線"   # 手動查（走閘門，寫真實資料庫）
```

`WEB_SEARCH_ENABLED=0` 可整個關掉。相關門檻與費用假設都在 `.env.example`，說明見 HANDOFF.md §4.5。

## Telegram 介面

同一個 Coach、同一套記錄邏輯，只是輸入窗口換到手機上。

1. 在 Telegram 找 **@BotFather**，`/newbot`，取名後拿到 token。
2. `.env` 填 `TELEGRAM_BOT_TOKEN=...`，先不用填 `TELEGRAM_ALLOWED_USERS`。
3. 啟動：`.venv/bin/python -m garmin_coach telegram`，在 Telegram 對 bot 說任何話，它會回你的 user id。
4. 把 id 填進 `.env` 的 `TELEGRAM_ALLOWED_USERS=`，重新啟動 bot。沒列在裡面的人只會被告知 id，拿不到任何資料。

| 指令 | 作用 |
|---|---|
| 直接打字 | 問教練；沒有進行中的對話就自動開新對話（先同步 Garmin 最近 3 天，再針對最近一次訓練） |
| `/new [activity_id]` | 開新對話（先同步 Garmin），預設最近一次訓練 |
| `/continue [對話id]` | 接續上一段對話 |
| `/end` | 結束並整理記憶（摘要、筆記、賽事、名詞）；只回摘要，寫入與費用看 `/cost` 與 log |
| `/status` `/activities` `/tools` | 今天身體狀況 / 最近訓練 / 這段對話呼叫過的工具 |
| `/splits [activity_id]` | 某次訓練的每圈/每公里分段表，不經模型、免費 |
| `/cost` `/model` | 今天/本月 AI 費用（含網路搜尋）/ 分流模式 |
| `/web [id]` | 網路搜尋紀錄；給 id 看那一筆的完整內容 |

超過 `TELEGRAM_IDLE_MINUTES`（預設 30）沒講話會自動 `/end`，所以忘了結束也不會漏記；之後再打字就是新對話，會再同步一次 Garmin（5 分鐘內同步過會略過）。同步失敗（token 失效、MFA）不擋對話，開頭會註明改用現有資料。
每則回覆尾端的 🔧 列出教練這一輪查了哪些資料。

長期執行：`deploy/garmin-coach-telegram.service` 是 systemd user unit，檔頭有安裝步驟（開機自動啟動、當掉自動重啟）。
Garmin token 過期需要 MFA 時 bot 會提示回電腦跑 `python -m garmin_coach login`。

## 下一階段（規劃）

1. 從對話結果產生課表並上傳 Garmin（API 支援 `upload_workout`）。
2. Open-Meteo 歷史天氣補 Garmin 沒有天氣的活動；地形分類。
3. 每週自動摘要：週負荷、恢復趨勢、指標變化。
