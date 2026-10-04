# Mac 本機使用 ChatGPT 訂閱

這是可選的 `LLM_PROVIDER=chatgpt` 模式，使用 OpenAI 官方 **Sign in with ChatGPT** 授權及公開 Responses API。原有 Gemini／GLM 模式保留。此方案旨在提供 Gemini 額度以外的模型來源；仍須通過 Epic 登入、可能的驗證及新增訂單確認，不能保證領取成功。

本機程式直接管理自己註冊的 OAuth 連線，不讀取 Codex 的 `auth.json`、ChatGPT 瀏覽器 cookies 或既有登入憑證。它不使用 OpenAI API key，也不會在授權／額度失敗時切換到其他計費方式。

## 先完成你自己的官方授權

在儲存庫根目錄，使用專案 Python 環境執行：

```sh
uv run python scripts/chatgpt_login.py login --profile personal
```

指令顯示 **Continue with ChatGPT**，開啟系統瀏覽器的官方頁面。請親自選擇帳號及 workspace、確認 app 名稱 **Epic Freebies Helper** 和訂閱用量權限。指令不執行模型推論、Epic 登入或領取。新 profile 使用動態 client 註冊；重新登入同一 profile 時沿用 issued client ID 及本機 host ID。

檢查這個 profile 的狀態及可選模型：

```sh
uv run python scripts/chatgpt_login.py status --profile personal
uv run python scripts/chatgpt_login.py models --profile personal
```

`plan_usage_enabled` 必須為 true。只有登入成功或列出模型，仍不足以證明訂閱推論可用。本機訂閱模式目前預設為已在 `personal` 合成圖測試通過的 `gpt-6.1-sol`；可用 `CHATGPT_MODEL` 指定其他模型。模型目錄可能不完整，實際資格與圖片能力仍以服務回覆為準。不同帳號或 workspace 使用不同 profile 標籤，例如 `personal`、`work`；標籤不區分大小寫，`host` 保留給安裝識別。

若第一次授權沒有訂閱用量權限，程式會保留登入但停止推論。請在 [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage) 檢查 app 權限；若你要重新同意，可明確執行 `uv run python scripts/chatgpt_login.py login --profile personal --enable-plan-usage`。這才會使用官方支援的 OAuth `prompt=consent`，普通登入不強制重新同意。不能靠反覆登入提高用量。

## 選擇 provider

完成上述人工授權後，可自行在本機設定：

```dotenv
LLM_PROVIDER=chatgpt
CHATGPT_PROFILE=personal
CHATGPT_MODEL=gpt-6.1-sol
CHATGPT_REQUEST_TIMEOUT_SECONDS=90
BROWSER_BACKEND=playwright
ENABLE_APSCHEDULER=false
ALLOW_CAPTCHA_SOLVING=false
```

`CHATGPT_MODEL` 可省略，省略時使用 `gpt-6.1-sol`；明確設定其他 slug 會保留，不會被預設值改寫。四個既有 `*_MODEL` 路由在未單獨指定時使用 `CHATGPT_MODEL`。若舊設定仍指定 Gemini／GLM 的模型名，請先核對路由；選擇 ChatGPT 模式後不會自動切回那些 provider。上游套件要求非空 Gemini 欄位，因此此模式內部使用公開的非憑證 sentinel，所有請求仍由 OAuth adapter 處理，無需填寫新的 API key。

本次實作沒有修改你的 `.env` 或正式排程，也沒有啟用真實執行。請先確認本機授權與模型推論，再另行決定 Epic／CAPTCHA／領取的執行範圍。既有 CAPTCHA 預設關閉、共用預算、零元／條款停止條件及訂單確認仍適用。

## 憑證與登出

本機狀態位於 `~/.config/epic-freebies-helper/chatgpt/`：目錄權限 `0700`，JSON／lock 檔案 `0600`，憑證以暫存檔加原子替換寫入。每個 profile 分開保存 issued client ID、已驗證的身份、tokens、scope 及到期時間。刷新使用跨程序鎖，避免 rotating refresh token 被同時使用；終止性刷新拒絕會清除 tokens 並要求重新登入，暫時連線錯誤會停止該請求並保留憑證。

```sh
uv run python scripts/chatgpt_login.py logout --profile personal
```

登出先依官方 discovery 的 revocation endpoint 撤銷 refresh session，再清除本機 tokens，保留 client／身份對應與 host ID。網路失敗時會回報遠端撤銷未確認；請親自在 ChatGPT Settings 中 disconnect app。不要分享這個目錄、token 或含 `id_token_hint` 的授權網址，也不要把憑證放到 GitHub Secrets、Actions artifacts 或提交中。

本版本僅供 Mac／Unix 本機或自行管理的常駐主機程序使用，遇到 `GITHUB_ACTIONS=true` 會直接拒絕。GitHub hosted Actions 的 OAuth 生命周期與資格尚未驗證，不能把這份本機授權當成雲端排程方案。

## 推論與用量限制

- 使用 `POST https://api.openai.com/v1/responses`，固定 `store=false`、`stream=true`；每次傳入完整所需 input，使用 instructions。
- 本地圖片轉成 `input_image` data URL，未使用不受支援的 Files upload API。圖片能否使用仍取決於所選模型與帳號。
- Gemini 的 temperature、thinking 等設定不會轉成此 route 不支援的參數。JSON schema 透過 instructions 提供，結果必須通過本機 Pydantic 驗證；這不保證辨識或座標正確。
- 只在 `response.completed` 且輸出完整、schema 有效時回傳結果。失敗、不完整、拒絕或中斷的串流不會採用部分答案。
- 訂閱用量上限錯誤立即停止，連結 [Manage usage](https://chatgpt.com/settings/usage)，不猜測重置時間。資格、region、scope 或模型限制停止請求；暫時錯誤沿用既有最多兩次 provider 嘗試與全程期限。

初版開發階段只有靜態及使用假資料的離線針對性驗證；沒有進行真實 OAuth、模型推論、Epic 登入、CAPTCHA 或領取，也沒有執行倉庫禁止的測試套件。後續單次真實驗證記錄見下方；目前仍未證明完整程式的 JSON 轉換、圖片／座標準確率或實際訂單結果。

官方協定與限制來源：[Overview](https://developers.openai.com/siwc/token-sharing-open-source)、[Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、[Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)、[Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)、[Errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)、[Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)、[官方 cookbook](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt)。

## 2026-10-04：單次合成圖推論結果

使用者自行授權後，`personal` 的安全 status 顯示 connected／plan_usage_enabled 為 true、access token 未過期，既有會話也可取得模型目錄。官方文件與帳號 metadata 均確認 `gpt-5.6-luna` 支援 text／image；此後已批准一次不含使用者資料的 256×192 紅方形／藍圓形推論。

這次直接透過實作的 `ChatGPTModels.generate_content`，使用臨時 low reasoning／low image detail、60 秒上限、零重試及單次 marker。只有一次 Responses POST 嘗試；HTTP 200，但原 adapter 沒有辨識為 SSE，因此沒有取得 completed、schema 結果或 usage，不能當作推論成功。原回覆的 Content-Type／body 未保存，尚無法判定實際格式或是否已扣用量。

後續僅修正 MIME 大小寫／參數解析，並讓非 SSE JSON 回覆以安全 HTTP／error code／body shape／request ID 摘要停止；不輸出 body 或 token。新增兩項離線檢查，合計 17 項通過。沒有追加真實推論、切換模型或修改正式 provider 設定；驗證修正是否解決實際回覆格式仍需另一次明確批准的單次請求。Epic、CAPTCHA 及訂單結果保持未驗證。

## 2026-10-04：第二次單次驗證確認內容為完整 SSE

取得新的單次批准後，先確認 Mac 可連線、上次 marker／結果完整且第二次尚未執行。`personal` 仍有 app／plan 權限，但 access token 已到期；請求使用既有 OAuth session，沒有另開登入或擴權。使用完全相同 SHA256 的合成圖、同模型與 low 設定，只發出本次一個 POST、零重試；第二次 marker／結果獨立保存，逐階段寫入安全摘要以便斷線後核對。

確切回覆為 HTTP 200，Content-Type 安全摘要無法辨識，但 body 是完整 SSE frames，含 `response.created`、文字增量與 `response.completed`。completed response 的用量為 input 377、output 212（含 reasoning 164）、total 589 tokens。這證明該訂閱請求有完成模型回覆；當時 adapter 仍因 media type 判斷拒絕，沒有成功的 app schema 結果。診斷用的獨立 schema 擷取也未通過，未保存原始文字，尚不能判定圖片內容／座標是否正確。

依本次 framing 證據修正 adapter：media type 無法辨識時，可解析實際 SSE framing，但仍要求完整 `response.completed`、非空輸出及有效 Pydantic schema；普通 JSON、部分／失敗回覆不因此視為成功。新增兩項離線檢查，合計 19 項通過；修正後沒有再發推論。程式完整整合及 Epic／CAPTCHA／訂單結果仍未驗證。


## 2026-10-04：合成圖專用安全紀錄與離線重播

新增 `scripts/probe_chatgpt_vision.py`，只允許上述固定 256×192 紅方形／藍圓形 PNG、原提示詞與 `gpt-5.6-luna`。程式重新生成圖片並核對 SHA256 `4fbb440df62760c75ee1272a30f9c8fa0fddce17f2a395ff8b17e99b0cb32636`；不接受其他檔案、私人圖片、CAPTCHA 或自訂提示詞。正式 provider 未引入這個 module，也不預設記錄使用者內容。

每次 probe 必須使用新的 attempt 目錄。既有目錄立即停止，同一 attempt 物件也不能再執行；目錄 `0700`、marker／原子 checkpoint `0600`。在 POST 前保存階段和計數，各 SSE frame 到達後逐筆保存脫敏 payload。保留 output_text 的 delta／done／completed 原文與 JSON、structured JSON 欄位、refusal、failed／incomplete 原因、完成 marker、HTTP status、Content-Type 是否存在及安全值、解碼方式與數字 usage。僅保留允許的回覆欄位；不保存 request headers、Authorization、cookies、身份、account／client／host metadata 或原始 response body。識別出的 token／API key／email／身份識別碼等會遮蔽；分段 delta 合併後也再檢查。未知非 JSON 的 HTML／純文字 body 直接省略，其重播範圍只限格式錯誤。若模型內容經脫敏或紀錄超過 256 KiB，不能聲稱原始行為已完整重播。

專用 schema 使用嚴格整數和欄位檢查：x 在 `[0,256)`、y 在 `[0,192)`，拒絕數字字串、浮點數、布林值、額外欄位與非 JSON 包裝。schema 失敗仍保留模型文字、可解析 JSON、Pydantic 錯誤 type／loc／message，以及該位置的 expected／actual。診斷輸出擷取直接调用實際 `completed_response`，不建立另一套答案擷取規則；replay 再由 mock HTTP transport 呼叫實際 `ChatGPTModels.generate_content`，走相同 completion 和 schema 路徑。答案僅來自 completed 的 output，delta 另留證據，不重複拼入。schema 合格與圖片辨識正確分開記錄：按繪圖 groundtruth 比對紅方形 (64,64)、藍圓形 (184,128)，容許 8 pixels 誤差，始終保留模型實際值。

離線命令須使用已安裝依賴的 Python，避免啟動器自動同步或下載。在這台 Mac 工作目錄實際使用的命令如下；輸出檔／目錄必須選未使用的新名字：

```sh
../.verification/venv/bin/python scripts/check_vision_probe_recording.py --output-directory ../.verification/vision-probe-offline-new
../.verification/venv/bin/python scripts/probe_chatgpt_vision.py replay ../.verification/vision-probe-offline-4/invalid_integer_string/record.json --output ../.verification/vision-probe-invalid-replay-new.json
```

replay 與檢查程序封鎖真實 socket／DNS，使用 mock session，不建立 OAuthSession、不讀憑證、不載入 settings／.env。32 項針對性離線案例全部通過：有效／無效 schema（型別、座標邊界、JSON fence、缺欄位、額外欄位）、schema 有效但座標錯誤、structured JSON 保存、缺 completed／缺 frame 結尾／incomplete／refusal／failed、delta／done／completed 重疊、缺 Content-Type、非 SSE JSON 與私密 HTML 省略、429、連線／串流逾時及零重試、UTF-8 分段／其他 charset 重播一致、metadata／headers 排除、脫敏與篡改資料拒絕重播、尺寸上限、marker／權限與重送阻擋。這些是明確標記的 fake fixtures，並非新的模型答案或圖片準確率證據；未執行測試套件。

本輪沒有任何真實請求，也不能補回第二次遺失的模型原文。589 tokens 的既有 completed 仍不代表 JSON／座標正確或 Epic 領取成功。下一次真實驗證尚未批准；只有新的明確單次批准後，才可使用 `live-once --approve-single-synthetic-request --attempt-directory <新的目錄>`：固定同圖、同模型、personal 既有 app OAuth session（如需刷新須包含在新批准範圍）、low reasoning／image detail、store=false／stream=true、60 秒模型期限、一個 Responses POST、零重試。不可另開登入、擴權、讀出 token、切換計費來源或進入 Epic；斷線後先讀取 marker／record，不能重送。正式排程、推送／合併以及 Gemini 額度問題保持原狀。


補充最終驗證：`../.verification/vision-probe-offline-5/offline-result.json` 的 32 案例全部通過；connection timeout 保存 `ConnectTimeout`／before_response_headers，stream timeout 保存 `CancelledError`／response_stream，均只有一個 mock POST、零重試。CLI 從落盤的 invalid_integer_string record 重播，正確再現 schema failure，loc 為 shapes[0].center_x、expected integer／0..255、actual 字串 "64"，沒有模型／OAuth／真實網路或憑證讀取。Python AST、Ruff、Black、git diff --check 均通過。


## 2026-10-04：第三次單次合成圖測試與 completed 空 aggregate 的離線修復

使用者重新批准同圖、gpt-5.6-luna、low reasoning／image detail、一個 inference POST、60 秒總期限與零重試。先確認 third-live-vision-probe 目錄和 marker 均不存在，並把專用 probe 的外層期限涵蓋既有 personal session 操作；32 項離線案例重新通過後才執行。沒有另開 OAuth grant、讀出／輸出憑證、Epic／CAPTCHA／領取、付費 API fallback 或 commit／push。

本次確定只有一個 inference POST，約 7.0664 秒，HTTP 200、Content-Type 確認缺失（null／present=false），SSE exhausted 且 response.completed status=completed。用量 input 389、output 28、total 417、cached 0、reasoning 0。原 adapter 報 Completed ChatGPT response contained no output text：最終 completed 的 output=[]，但先前 output_text.done、content_part.done、output_item.done 的相同 JSON 均已保存，item status=completed。原 record 與 replay-before 保留失敗事實，沒有重送。

離線修改 app/extensions/chatgpt_provider.py：僅在有效 response.completed 的 aggregate 明確為空時，使用之前已閉合的 assistant item／part／text DONE markers 還原；要求索引、completed status、marker 集合和文字一致，不使用 delta 作答案。缺 marker、item 未完成、衝突或未完成 terminal 均停止，refusal 繼續拒絕。scripts/probe_chatgpt_vision.py 診斷標記實際答案來源，scripts/check_vision_probe_recording.py 增加 11 項針對性案例，合計 43 項離線案例通過，AST／Ruff／Black／git diff --check 亦通過，未執行測試套件。

從未改寫的 record.json 再跑實際 adapter 離線 replay-after：嚴格 JSON／schema 通過。實際答案只有 blue circle，中心 (184,128) 正確，漏掉 red square，完整 groundtruth 比較為 false；不插入缺失形狀或改写答案，因此本次不能算完整圖片辨識通過。修正後沒有追加 live 推論。結果位於 ../.verification/third-live-vision-probe/final-result.json，原 record、replay-before／after、marker 和模型目錄核對分開保存。

使用者另問 gpt-6.1-sol：Luna 單次請求已完成，故只用既有 personal OAuth 唯讀 GET 官方 /v1/models 一次，沒有第二次 inference。HTTP 200，所有回傳模型中均未找到精確 slug gpt-6.1-sol；可列出 gpt-6-astra、gpt-5.6-sol、gpt-5.6-terra、gpt-5.6-luna、gpt-5.5。這個 profile 目前無法確認 gpt-6.1-sol 的資格或圖片能力，不猜別名、不更改訂閱 provider／正式排程或程式任務的 agent 模型。任何下一次推論仍需新的單次批准。


## 2026-10-04：精確 gpt-6.1-sol 的新單次推論獲官方 API 接受

- 授權與範圍：使用者明確要求，即使模型列表未列出也直接用精確 gpt-6.1-sol 測同一非私人 256×192 合成圖。這是新的一次授權；先確認 gpt-6-1-sol-live-probe 目錄／marker 尚不存在，不重跑 Luna attempt。只用既有 personal 訂閱 OAuth、low reasoning／image detail、一個 inference POST、60 秒總期限、零重試；不新建 grant、不讀出／輸出憑證、不 fallback 模型或付費 API、不執行 Epic／CAPTCHA／領取或 commit／push。
- 修改：scripts/probe_chatgpt_vision.py 的專用 probe 支援明確 --model gpt-6.1-sol；contract／payload／replay 保留精確 slug，預設 Luna 及舊紀錄重播保持可用，未更改正式 provider／排程。scripts/check_vision_probe_recording.py 新增精確 slug 不回退案例，合計 44 項離線案例通過；AST／Ruff／Black／git diff --check 通過，未執行測試套件。本轮未再修改 parser。
- 真實服務證據：只發出一個 inference POST，約 5.4996 秒；HTTP 200、Content-Type 缺失、response.completed status=completed，回傳 model 精確為 gpt-6.1-sol。用量 input 389、output 86、total 475、cached 0、reasoning 38（已含於 output 86）。因此先前目錄未列出不能作為該模型不可推論的結論；本次 API 確實接受且完成。
- 答案与驗證：原模型答案完整列出 red square (64,64)、blue circle (184,128)；嚴格 JSON／schema、完整形狀集合及座標 groundtruth 比對全部通過。仍使用先前已離線修好的 matching item／part／text DONE markers 加 response.completed 擷取，不採用 delta 或插入 groundtruth 答案。去敏事件、模型文字／JSON、HTTP／usage、schema／coords 均已保存；磁碟離線 replay 再走實際 adapter，結果與原 live record 完全一致，沒有額外推論。
- 保存與限制：獨立目錄 ../.verification/gpt-6-1-sol-live-probe/ 內保留 attempted.json、record.json、replay.json、final-result.json；record 不改寫。這證明本次該 profile／模型的合成圖路徑可用，不代表 Epic／CAPTCHA／完整領取已驗證，也未把正式 CHATGPT_MODEL 配置改成此值。本次單次授權已使用完，不重送。


## 2026-10-04：本機訂閱模式預設模型改為 gpt-6.1-sol

- 授權與症狀：使用者在精確 gpt-6.1-sol 合成圖／schema／座標／adapter replay 成功後，同意將本機訂閱模式的預設模型設成該 slug。原 CHATGPT_MODEL 預設為空，未設定時會配置錯誤。
- 修改檔案：app/settings.py 將 CHATGPT_MODEL 的 Field default 由空字串改成 gpt-6.1-sol；空模型提示改為要求 nonempty slug，沒有把官方模型目錄作 allowlist。scripts/check_chatgpt_oauth.py 同步原本「省略模型應失敗」的離線 assertion；docs/chatgpt-local-oauth.md 更新預設、範例與 override 說明，並追加本維護紀錄。沒有更改 LLM_PROVIDER、CHATGPT_PROFILE、provider 啟用方式、正式 workflows 或 probe 的固定測試預設。
- Override 與有效值：開始時本機 .env 不存在，影響選模的環境變數均未設定。LLM_PROVIDER=chatgpt 且未提供模型時，解析後 CHATGPT_MODEL=gpt-6.1-sol，CHALLENGE_CLASSIFIER_MODEL／IMAGE_CLASSIFIER_MODEL／SPATIAL_POINT_REASONER_MODEL／SPATIAL_PATH_REASONER_MODEL 均為同值。明確的 CHATGPT_MODEL 或任務欄位 override 保留，未硬編碼改寫使用者指定；優先順序為 constructor > environment > dotenv > Field default，空環境值沿用既有 env_ignore_empty 規則。
- 離線驗證：14 項針對性設定解析檢查通過，涵蓋預設及四路繼承、其他模型、每個任務 override、環境／純模型 fake dotenv／constructor 優先順序、空／空白值和 Gemini／GLM 原有路由。由實際 EpicSettings class／validators 與 Pydantic sources 解析；從已有 hcaptcha-challenger 0.19.0 wheel 核對／擷取相關設定與 validator，不下載或啟動 app。socket／DNS 封鎖，使用清空環境的隔離 Python，不讀真實 .env、API secrets 或 OAuth 憑證。結果 ../.verification/chatgpt-model-default-result.json；Python AST、Ruff、Black、git diff --check 通過，未執行全套測試或既有 OAuth mock suite。
- 範圍與結果：僅修改本機 prototype 的模型預設設定及相關說明／assertion；零推論、OAuth、Epic／CAPTCHA／領取請求，無新增 grant、commit／push、正式 provider 切換或部署。先前真實模型 probe 結果保持不變。


## 2026-10-04：審查並準備發佈本機訂閱 OAuth prototype 功能分支

- 授權與範圍：使用者要求 commit 後推上去並測試。核對目前為 codex/chatgpt-oauth-local，HEAD／遠端 master 均為 5575e2e，遠端尚無同名功能分支。保留這批已批准的 OAuth／parser／診斷／gpt-6.1-sol 預設改動，不 merge master、不執行正式 Epic／LLM／OAuth grant 或付費 fallback。
- 審查與必要修正：README 更新為已有一次真實合成圖／JSON／座標／adapter 成功證據、Epic 領取未驗證；check_chatgpt_oauth.py 補入四個任務路由的預設與明確 override 檢查。核對已緩存 hcaptcha-challenger 0.19.0 的 upload／Content／generate_content 接口與 adapter 相符。此次不改正式 workflows、Epic 流程或 active provider。
- 隔離 CI：新增 .github/workflows/chatgpt-local-check.yml，只在此功能分支 push 且 repo/ref/event 精確相符時執行，contents read、checkout 不保留憑證、Python 3.12、僅安裝 uv.lock 指定版本及 wheel hashes 的必要工具。只跑既有 fake OAuth／model routing、synthetic recorder／actual adapter replay 和 runtime stopping checks；各程式封鎖真實 socket，無 Epic／模型／真實 OAuth 憑證、login 或 live-once。現有 browser startup 只綁其他分支；正式 Epic 只接受 schedule／manual，Docker 只接受 release，不因本次 push 啟動。
- 本機驗證：20 項 OAuth／設定、44 項 recorder／replay、15 項既有 runtime control 通過；Ruff、Black（新檔與 settings）、AST、TOML lock／CI requirements、workflow trigger／permissions 靜態檢查及 git diff --check 通過。沒有執行全套測試，沒有新增 live 模型／OAuth／Epic 請求。
- 發佈資料：明確限制 staging 為 14 個本功能程式／說明／workflow 檔案，檢查新增內容無實際 token／JWT／private key／email 或 Mac 使用者路徑；源碼中的協定欄位、正則與 fake fixture 值保留。所有本機 .verification／OAuth credentials／cookies／runtime files 均不進 Git。commit／push 與 exact-SHA CI 結果在此紀錄之後核對，未先宣稱 CI 成功。
