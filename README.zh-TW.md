# goals-iterm-mcp

[專案官網](https://lucky-goals.github.io/goals-iterm-mcp/)
[English](README.md) | [简体中文](README.zh-CN.md) | 繁體中文 | [日本語](README.ja.md)

**讓 Coding Agent 操作 Mac 上的各種 CLI，在手機上也能推進 vibe coding 任務。**

goals-iterm-mcp 透過 **iTerm2 Python API**，將支援 MCP 的 Agent 與電腦上的終端機連接起來。只需在對話中描述任務，Agent 就能開啟終端機視窗、進入專案、啟動另一個 Coding CLI、傳送指令，再讀取進度與結果。多個專案與 CLI 工作階段都能透過對話協調，減少手動切換終端機的操作。

## 它能幫你做什麼

- **在手機上繼續推進任務**：透過 Agent 的手機版或遠端介面，指揮 Mac 上的 iTerm2 工作，前提是該介面連接的 Agent 能存取本機 MCP。
- **協調不同 Coding CLI**：讓 Agent 啟動 Claude Code、Codex CLI 或其他 CLI，進入指定專案並提交開發或診斷任務。
- **在對話中查看進度**：列出視窗與分頁，讀取終端機輸出，彙整各工作階段正在執行的工作。
- **讓多個專案獨立執行**：為任務建立專用視窗或分頁，透過明確的 ID 操作目標工作階段。
- **圍繞目標持續指導其他 CLI**：為 Codex、Claude 等主 Agent 設定目標（goal）與明確的完成條件。它能透過本 MCP 反覆讀取其他 CLI 工作階段的進度、判斷下一步、補充指令並核對結果。目標管理與持續執行由主 Agent 負責，MCP 提供終端機操作能力。

典型使用流程是：**手機或電腦上的對話 → 能存取本機 MCP 的 Agent → iTerm2 → Coding CLI**。Codex、Claude、Cursor、WorkBuddy、Trae、Hermes 等工具，只要所用用戶端支援本機 stdio MCP，並具備所需的手機版或遠端連線方式，就能採用此模式。目前已透過 Codex 桌面版操作 iTerm2 中的 Claude Code 與 Codex CLI 完成人工驗證。

例如，可以給主 Agent 這樣的目標：

> 持續指導專案專用 iTerm 工作階段中的 Coding CLI，直到它完成登入問題的診斷、修復，並通過相關測試。每個階段檢查進度，必要時補充後續指令，最後依照完成條件核對結果。不要重複提交仍在執行的任務；遇到超出本次範圍的決策時詢問我，完成後彙整變更與測試結果。

已發布的 0.2.2 套件提供 13 個 stdio MCP 工具：原有的工作階段列舉、終端機讀寫、視窗與分頁建立、工作階段命名工具，加上唯讀的 `inspect_sessions` 診斷工具和 4 個關閉工具。4 個關閉工具最初在 0.2.0 版本引入。原始碼契約見[關閉工具實作規範](docs/closing-tools-spec.md)。

## 前置設定

### 啟用 iTerm2 Python API

需要 macOS，並已安裝、執行 iTerm2。本專案已使用 **iTerm2 3.7.6** 完成驗證。

1. 開啟 **iTerm2 → Settings → General → Magic**。
2. 啟用 **Python API**。
3. MCP 服務首次連線時，若出現 iTerm2 API 授權要求，請允許連線。
4. 使用 MCP 工具期間，保持 iTerm2 執行。

設定詳情見 [iTerm2 官方說明](https://iterm2.com/documentation-preferences-general.html)。

### 設定要使用的 Coding CLI

透過 iTerm2 操作 Coding CLI 前，請先安裝該 CLI，並在 iTerm 環境中完成驗證設定：登入對應的 OAuth 帳號，或依該 CLI 支援的方式設定 API Gateway 位址與憑證。先在 iTerm2 中手動啟動一次，確認它能正常回應簡單的提示詞。

同時確保新開啟的 iTerm 視窗與分頁也能讀取所需設定。goals-iterm-mcp 負責操作終端機，各 Coding CLI 使用自己的驗證與 API 設定。憑證應保存在 CLI 支援的憑證儲存區或本機設定中，不要寫進傳給 Agent 的提示詞。

## 使用 uvx 執行

0.2.2 已發布至 [PyPI](https://pypi.org/project/goals-iterm-mcp/)，該版本提供全部 13 個工具；無需複製儲存庫即可執行：

```sh
uvx --python 3.12 goals-iterm-mcp@0.2.2 serve
```

Python 匯入路徑為 `goals_iterm_mcp`，啟動命令為 `goals-iterm-mcp`。

## 透過 Agent 對話安裝

將下列提示詞複製給能執行本機命令、設定 MCP 用戶端的 Agent，把 `[MCP 用戶端]` 替換為實際用戶端，例如 Codex 桌面版或 Claude Code。Agent 需要存取本機 macOS；純雲端聊天無法連接本機 iTerm2。

```text
請從 PyPI 安裝 goals-iterm-mcp 0.2.2，並設定至 [MCP 用戶端]。
專案網址：https://github.com/lucky-goals/goals-iterm-mcp

1. 確認本機是 macOS，已安裝並執行 iTerm2。檢查 uv/uvx 是否存在；
   若未安裝，依 uv 官方說明安裝。使用 Python 3.12，不要複製儲存庫，
   不依賴專案目錄中的 .venv。
2. 執行下列命令，驗證套件可以執行：
   uvx --python 3.12 goals-iterm-mcp@0.2.2 --help
3. 檢查用戶端現有的 MCP 設定，保留其他設定。在使用者層級設定中新增或更新
   一個名為 iterm 的 stdio 服務，避免重複安裝：
   command：本機 uvx 的絕對路徑
   args：["--python", "3.12", "goals-iterm-mcp@0.2.2", "serve"]
   使用該用戶端支援的設定格式與位置。修改現有設定前先備份，不變更其他服務。
4. 如有需要，指導我啟用 iTerm2 Python API 並核准授權對話框。
   依用戶端支援的方式重新載入 MCP；若需要我手動重新啟動用戶端，請明確告知。
5. 驗證 MCP 交握、已安裝版本的工具探索，以及唯讀的 list_sessions 呼叫。
   安裝驗證期間不要傳送終端機輸入、讀取工作階段內容，也不要建立或關閉視窗。
6. 回報設定位置、啟動命令與驗證結果，區分「獨立連線成功」與「目標用戶端
   已實際載入工具」。若遇到阻礙，說明具體還需要完成哪一步。
```

安裝此公開套件不需要 PyPI 帳號或 API Token。上述設定會提供已安裝版本的全部工具；安裝檢查僅列舉工作階段。

## 更新已安裝的舊版本

先檢查並備份用戶端現有的 `iterm` MCP 設定，在原項目上更新，保留環境變數、`--read-only`、`--session-id` 限制及其他服務，避免重複新增。依照原本的安裝方式升級。

### 使用 uvx 安裝的使用者

查看 [PyPI 版本歷史](https://pypi.org/project/goals-iterm-mcp/#history)及 [GitHub Releases](https://github.com/lucky-goals/goals-iterm-mcp/releases)，再確認最新已發布套件能啟動：

```sh
uvx --python 3.12 goals-iterm-mcp@latest --help
```

這個命令**不會自動修改 MCP 用戶端設定**。請將現有 `args` 中的 `goals-iterm-mcp@0.1.2`（或其他舊版本）改為 `goals-iterm-mcp@<VERSION>`，並把 `<VERSION>` 替換成已確認的發布版本號。保留 `uvx` 的絕對路徑和其他參數。建議固定版本，方便主動升級及退回原版本。

若希望日後啟動時跟隨最新發布版，也可以設定 `goals-iterm-mcp@latest`，但每次啟動的版本可能改變。僅移除版本號不等同於使用最新版，因為 uvx 可能重用快取；詳見 [uv 工具版本說明](https://docs.astral.sh/uv/concepts/tools/#tool-versions)。「最新」指已發布套件，不包含儲存庫中尚未發布的變更。

### 從原始碼或透過 pip 安裝的使用者

如果使用下文的原始碼可編輯安裝，先儲存本機變更並確認目標分支，再於原儲存庫目錄更新，重新安裝到原本的 Python 3.12 環境：

```sh
git pull --ff-only
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m goals_iterm_mcp --help
```

若 Git 拒絕更新，先處理本機變更或分支分歧，再繼續安裝。用戶端的 Python 絕對路徑應繼續指向這個環境。若原本透過 pip 安裝 PyPI 套件，請使用該服務實際使用的直譯器：`/absolute/path/to/.venv/bin/python -m pip install --upgrade goals-iterm-mcp`；更新其他環境中的同名套件不會更新目前的 MCP。

### 重新啟動連線並驗收

依用戶端支援的方式重新啟動或連接 `iterm` MCP，必要時重新啟動用戶端。更新磁碟檔案不會替換已執行的服務程序。Codex 可使用 **設定 → MCP servers → Restart**，或重新啟動 Codex；參見 [官方 MCP 設定說明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

重新連線後，查詢目前用戶端實際載入的工具列表，並僅呼叫 `list_sessions` 驗證連線。工具列表應符合安裝版本；0.2.2 版本應有 13 個工具，包括唯讀的 `inspect_sessions` 診斷工具，以及 `close_session`、`close_tab`、`close_window`、`clean_all_sessions`。獨立程序握手成功不代表既有對話已刷新工具；若仍顯示舊介面，重新連線或開啟新對話後再檢查。升級驗收不需要建立、關閉終端或傳送輸入。啟動 Python 時禁止使用 `--reload`。

也可以將以下提示詞交給本機 Agent：

```text
請將 [MCP 用戶端] 中現有的 goals-iterm-mcp 更新到最新已發布版本。
先檢查目前啟動命令及安裝方式，備份並原位更新 iterm 設定，不重複新增；
保留環境變數、唯讀/session 範圍限制及其他 MCP 服務。核對 PyPI 與 GitHub
發布版本，更新固定版本號或該服務實際使用的環境，再重新載入 MCP 連線。
驗證目前用戶端實際載入的工具列表及唯讀 list_sessions 呼叫，回報版本、
設定位置和驗證結果；如果仍需我手動重新啟動，請明確說明。
原本是原始碼安裝時，不要未經說明就切換到 PyPI 安裝。
```

## 安裝與啟動

先完成上面的[前置設定](#前置設定)。從原始碼安裝需要 Python 3.12。在儲存庫目錄中執行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m goals_iterm_mcp serve
```

`serve` 等待 MCP 用戶端透過 stdin/stdout 傳送協定訊息，並非互動式命令列。診斷資訊寫入 stderr。執行時請勿使用 Python 自動重新載入模式。

MCP 用戶端設定範例（請替換為你的絕對路徑）：

```json
{
  "mcpServers": {
    "iterm": {
      "command": "/absolute/path/to/goals-iterm-mcp/.venv/bin/python",
      "args": ["-m", "goals_iterm_mcp", "serve"]
    }
  }
}
```

選用參數：

- `--read-only`：已發布的 0.2.2 套件提供 4 個唯讀工具：`list_sessions`、`read_screen`、`read_history` 與 `inspect_sessions`。
- `--session-id <ID>`：限制為指定工作階段，可重複傳入以允許多個工作階段。啟用後禁止建立視窗與分頁。

預設可操作目前使用者 iTerm 中的所有工作階段。讀取與傳送操作必須傳入 `list_sessions` 回傳的精確 `session_id`；關閉工具也支援下方說明的明確選擇器。不會依焦點、萬用字元、前綴或標題推測目標。

## MCP 工具

| 工具 | 參數 | 功能 |
| --- | --- | --- |
| `list_sessions` | 無 | 列出工作階段、分頁、視窗 ID、可讀的 `name` 與工作階段中繼資料。 |
| `read_screen` | `session_id` | 讀取已呈現的畫面行、尺寸與游標位置。 |
| `read_history` | `session_id`, `lines=100` | 讀取保留的捲動歷史，最多 2,000 行。 |
| `inspect_sessions` | `session_id=None`、`nickname=None` | 唯讀清理能力稽核。可選擇一個精確工作階段 ID 或唯一的不分大小寫暱稱，也可以省略兩者以檢查全部已授權可見工作階段。回報有限的 prompt/process/CLI 證據、活動、身分與持久化觀察、阻礙、處置建議及提示性的關閉就緒狀態；不提供任務授權或關閉授權，也不會傳送輸入、退出 CLI、寫入回執或關閉工作階段。 |
| `send_text` | `session_id`, `text` | 原樣傳送文字，最多 16 KiB，不自動附加換行。 |
| `send_keys` | `session_id`, `key` | 傳送一個終端機按鍵序列。 |
| `create_window` | `command`（選用） | 使用預設設定檔建立視窗，可指定啟動命令，回傳新目標 ID。 |
| `create_tab` | `window_id`, `command`（選用） | 在指定視窗建立分頁，可指定啟動命令。 |
| `rename_session` | `session_id`, `name`（選用） | 重新命名現有工作階段；省略 `name` 時隨機分配未使用的暱稱。 |
| `close_session` | `session_id=None`、`nickname=None`、`force=False`、`dry_run=False` | 退出 CLI 並完成原生關閉驗證後關閉選定工作階段。必須且只能提供 `session_id` 或 `nickname` 其中之一。 |
| `close_tab` | `tab_id=None`、`session_id=None`、`nickname=None`、`force=False`、`dry_run=False` | 關閉選定工作階段或分頁所在的分頁。必須提供且只能提供一個選擇器。 |
| `close_window` | `window_id=None`、`session_id=None`、`nickname=None`、`force=False`、`dry_run=False` | 關閉選定工作階段或視窗所在的視窗。必須提供且只能提供一個選擇器。 |
| `clean_all_sessions` | `force=False`、`dry_run=False` | 依初始工作階段快照清理允許範圍內的工作階段。預設跳過忙碌、未知或無法復原的工作階段；`force=true` 會嘗試快照中的每個允許工作階段。 |

### 唯讀工作階段能力稽核

`inspect_sessions` 是已發布的 0.2.2 套件中的唯讀診斷工具。`session_id` 與 `nickname` 互斥。傳入精確 `session_id` 或唯一暱稱（去除前後空白並折疊大小寫）時檢查一個已授權工作階段；省略兩者時檢查目前全部已授權可見工作階段。選擇器無效、有歧義、不可用或超出授權範圍時，工具會在不寫入終端的情況下失敗。

每項結果包含工作階段、分頁與視窗身分，以及有限的 `prompt`、`process`、`cli`、`terminal_activity`、`business_acceptance` 和 `close_readiness` 部分。診斷可能回報證據是否可用、觀測到的活動、穩定進程/CLI 身分、聊天持久化觀察、阻礙代碼和處置建議。`business_acceptance` 始終為 `not_observed`；`close_readiness` 僅供參考，其 `status` 為 `eligible_if_unchanged`、`blocked` 或 `unknown`，只有前者對應 `ready=true`。即使結果為 `ready: true`，也不代表取得授權或關閉一定成功；任何生命週期寫入前都必須重新探測。該工具只讀取證據，不會傳送終端輸入、退出 CLI、保存恢復回執或呼叫原生關閉。

### 關閉工具

這 4 個關閉工具最初在 0.2.0 版本引入，並繼續包含在 0.2.2 版本中。完整契約見[關閉工具實作規範](docs/closing-tools-spec.md)。

`close_session`、`close_tab` 與 `close_window` 必須且只能使用一個選擇器。ID 只按明確 ID 比對，絕不退化為暱稱比對。暱稱是工作階段範圍內的選擇器：目錄中的名稱會先去除首尾空白並折疊大小寫後比對；產生歧義時回傳錯誤。關閉分頁或視窗時，使用工作階段選擇器會選取其所在的分頁或視窗。不支援依焦點、萬用字元或前綴定位。只要分頁或視窗包含任何超出允許清單的工作階段，就會拒絕關閉。

一般關閉會在傳送任何終端機輸入前預檢所有受影響的工作階段。只要存在忙碌、等待中或活動狀態未知的工作階段，就拒絕整個目標操作。`clean_all_sessions` 預設跳過忙碌、未知和無法復原的工作階段，只關閉已確認閒置的工作階段，並保留其餘工作階段與視窗。`force=true` 時，清理會嘗試初始快照中的每個允許工作階段，之後新到達的工作階段不會加入本次請求。`force` 只授權繞過活動狀態門檻，不會繞過允許清單、身分歧義、目標變更或復原記錄寫入失敗，也不等同於隱含的 `SIGKILL`。iTerm 原生 force 的跳過介面確認行為與此中斷授權是兩回事。

在首次中斷、退出 CLI 或關閉原生物件之前，服務會先寫入私有復原記錄，只保留中繼資料，不保存終端機正文、憑證或聊天內容。已辨識的聊天 CLI 必須在中斷或退出前提供實際 chat ID；服務不會只為發現 ID 而向執行中或未知的 CLI 傳送聊天文字或斜線命令。一般 shell 和一般工作階段的 `cli_sessions` 為空；未知 CLI 身分不會被悄悄降級成 shell 或一般工作階段。如果目前行程或聊天身分的證據不可用，或無法與目標繫結，操作會安全失敗（清理時可以將該工作階段回報為跳過）。畫面無輸出、執行時間、低 CPU、看起來已完成的回答或工作階段清單中的 `in_progress` 都不是完成證據。CLI 退出有時間上限並會驗證；如果無法驗證退出，服務不會為該目標發出原生關閉要求，但 iTerm 設定的「退出時自動關閉」行為仍可能因 CLI 自身退出而移除分頁。服務不會依名稱殺掉共用守護行程或其他行程。

目前復原支援刻意保持狹窄。Claude 與 Codex 只能透過繫結 chat ID 的可信行程登記，或透過目前行程確實開啟的 transcript/rollout 檔案識別聊天；只出現在啟動參數中的 UUID 會被回報，但仍會阻止中斷。Kiro 的保存工作階段清單和啟動參數都不能證明目前聊天身分，因此必須有與行程繫結的已知 ID；即使 `force=true`，無法證明的身分也會被拒絕。Cursor 的被動路徑只接受由提供的 PID 目前開啟的受辨識記錄檔取得的 UUID；只有啟動參數並不足夠。除非有可信的生命週期證據，CLI 工作活動通常仍為 `unknown`；配接器不會自動擷取每個聊天或所有正在執行的 CLI。

結果包含 `operation`、`status`、`force`、`dry_run`、`target`、復原記錄 ID 與路徑，以及已關閉、已跳過、失敗和未知工作階段清單。`status` 為 `closed`、`partial`、`refused`、`noop`、`dry_run` 或 `unknown` 其中之一。每個工作階段結果包含 iTerm ID、暱稱、`cli_sessions`、中斷、退出和關閉狀態。聊天記錄包含 `cli`、`chat_uuid`、`cwd`、身分來源、持久化狀態和結構化的復原 executable/argv。`chat_uuid` 只是聊天識別；它本身不能證明歷史已保存，也不能證明復原命令已測試成功，這些資訊由獨立結果欄位表示。關閉後會重新整理拓撲，因為 API 確認不等於已關閉。遺失或逾時的回應會變成 `unknown`，不會自動重試。拒絕、失敗和未知操作會以帶結構化資料的 MCP 工具錯誤回傳；只有清理跳過時不算錯誤。部分結果會回報已完成的副作用，不會透過重新開啟終端機回滾。試執行不會傳送輸入、寫入復原記錄、修改名稱或關閉物件。

透過 `create_window` 或 `create_tab` 建立的工作階段會自動取得隨機暱稱，內建名稱池包含 200 個互不相同、長度為 4–7 個字元的名稱。既有工作階段可使用 `rename_session` 命名。自訂名稱最多 64 個字元，名稱比對不區分大小寫。其他存活工作階段正在使用的名稱不可重複分配；工作階段關閉後名稱可再次使用。名稱池用盡時，自動命名會在建立工作階段前回報錯誤。關閉工具的暱稱選擇器使用同一份存活工作階段目錄，並在比對前去除首尾空白、折疊大小寫。

讀取與傳送操作需要先透過 `list_sessions` 找到正確的 `session_id`。關閉工具可依上述規則使用精確暱稱或明確目標 ID；不要依焦點或標題推測目標。

`key` 支援：`enter`、`tab`、`escape`、`backspace`、`up`、`down`、`left`、`right`、`ctrl_c`、`ctrl_d`、`ctrl_u`、`ctrl_l`。

文字內的換行、Enter 與控制鍵由前景程式解讀，可能執行命令或結束程式。傳送時明確設定 `suppress_broadcast=True`，僅傳送至指定工作階段。回應中的 `api_acknowledged` 僅表示 iTerm API 已確認；命令是否完成仍為 `unknown`。失敗或逾時不會自動重新傳送。

畫面與歷史讀取回傳終端機呈現結果，並非 CLI 內部編輯器原文或原始 PTY 位元組。服務只在目前行程內依序處理工具操作，不協調其他 MCP 行程或使用者手動輸入。結束服務僅中斷 API 連線，不會關閉使用者工作階段。

## 驗證

執行關閉工具、復原配接器與 direct backend 的定向測試：

```sh
.venv/bin/python -m unittest tests.test_closing tests.test_closing_backend tests.test_cli_recovery tests.test_direct -q
```

截至 2026 年 10 月 22 日，這 69 項定向測試均通過。變更檔案的 Ruff 檢查和 `git diff --check` 也通過；本次驗證不代表全量測試結果。

關閉工具的實機 smoke 測試採選擇性執行，只建立新的、位於允許清單內的 iTerm 工作階段。可以先顯示計畫，或明確執行：

```sh
.venv/bin/python scripts/smoke_closing.py          # 僅顯示計畫
.venv/bin/python scripts/smoke_closing.py --run   # 執行選擇性的原生 smoke 測試
```

2026-10-22 的執行通過：3 個定向原生關閉成功；預設清理關閉 1 個閒置工作階段並略過 1 個執行中的 `sleep` 工作階段；force 清理隨後關閉 1 個工作階段；原有 6 個工作階段全部保留。

先前的實際 stdio MCP → iTerm Python API 驗證涵蓋：建立視窗與分頁、傳送文字與 Enter、中文與 emoji 畫面讀取，以及非空的捲動歷史讀取。驗證後已關閉兩個臨時測試工作階段。

如需重現先前的實際連線測試（僅操作新建立的 `/bin/cat` 工作階段）：

```sh
.venv/bin/python scripts/smoke_iterm.py         # 僅顯示計畫
.venv/bin/python scripts/smoke_iterm.py --run   # 建立、測試並關閉專用工作階段
```

## 專案結構

執行階段模組涵蓋 MCP 工具、iTerm 後端、讀取結果型別、游標座標換算、交易、連線清理與關閉工具安全契約。舊有 broker/gateway、管理 UI 與實驗實作已移除；關閉工具的 CLI 身分取決於目前行程與執行階段證據。

完整的保留與移除依據見[相依性整理與清理紀錄](docs/cleanup-plan.md)，逐模組清單見[相依性清單](docs/cleanup-dependencies.json)。

## 授權條款

[MIT](LICENSE) © 2026 lucky-goals。
