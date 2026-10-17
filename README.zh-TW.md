# goals-iterm-mcp

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

服務入口為 `serve`，透過 stdio 提供 8 個 MCP 工具，涵蓋工作階段列舉、終端機讀取、文字與按鍵傳送，視窗與分頁建立，以及工作階段命名。

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

0.1.1 已發布至 [PyPI](https://pypi.org/project/goals-iterm-mcp/)，無需複製儲存庫即可執行：

```sh
uvx --python 3.12 goals-iterm-mcp@0.1.1 serve
```

Python 匯入路徑為 `goals_iterm_mcp`，啟動命令為 `goals-iterm-mcp`。

## 透過 Agent 對話安裝

將下列提示詞複製給能執行本機命令、設定 MCP 用戶端的 Agent，把 `[MCP 用戶端]` 替換為實際用戶端，例如 Codex 桌面版或 Claude Code。Agent 需要存取本機 macOS；純雲端聊天無法連接本機 iTerm2。

```text
請從 PyPI 安裝 goals-iterm-mcp 0.1.1，並設定至 [MCP 用戶端]。
專案網址：https://github.com/lucky-goals/goals-iterm-mcp

1. 確認本機是 macOS，已安裝並執行 iTerm2。檢查 uv/uvx 是否存在；
   若未安裝，依 uv 官方說明安裝。使用 Python 3.12，不要複製儲存庫，
   不依賴專案目錄中的 .venv。
2. 執行下列命令，驗證套件可以執行：
   uvx --python 3.12 goals-iterm-mcp@0.1.1 --help
3. 檢查用戶端現有的 MCP 設定，保留其他設定。在使用者層級設定中新增或更新
   一個名為 iterm 的 stdio 服務，避免重複安裝：
   command：本機 uvx 的絕對路徑
   args：["--python", "3.12", "goals-iterm-mcp@0.1.1", "serve"]
   使用該用戶端支援的設定格式與位置。修改現有設定前先備份，不變更其他服務。
4. 如有需要，指導我啟用 iTerm2 Python API 並核准授權對話框。
   依用戶端支援的方式重新載入 MCP；若需要我手動重新啟動用戶端，請明確告知。
5. 驗證 MCP 交握、已安裝版本的工具探索，以及唯讀的 list_sessions 呼叫。
   安裝驗證期間不要傳送終端機輸入、讀取工作階段內容，也不要建立或關閉視窗。
6. 回報設定位置、啟動命令與驗證結果，區分「獨立連線成功」與「目標用戶端
   已實際載入工具」。若遇到阻礙，說明具體還需要完成哪一步。
```

安裝此公開套件不需要 PyPI 帳號或 API Token。上述設定會提供已安裝版本的全部工具；安裝檢查僅列舉工作階段。

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

- `--read-only`：僅提供列舉與讀取工具。
- `--session-id <ID>`：限制為指定工作階段，可重複傳入以允許多個工作階段。啟用後禁止建立視窗與分頁。

預設可操作目前使用者 iTerm 中的所有工作階段。每次讀寫都必須傳入 `list_sessions` 回傳的精確 `session_id`；不會依焦點或標題推測目標。

## MCP 工具

| 工具 | 參數 | 功能 |
| --- | --- | --- |
| `list_sessions` | 無 | 列出工作階段、分頁、視窗 ID、可讀的 `name` 與工作階段中繼資料。 |
| `read_screen` | `session_id` | 讀取已呈現的畫面行、尺寸與游標位置。 |
| `read_history` | `session_id`, `lines=100` | 讀取保留的捲動歷史，最多 2,000 行。 |
| `send_text` | `session_id`, `text` | 原樣傳送文字，最多 16 KiB，不自動附加換行。 |
| `send_keys` | `session_id`, `key` | 傳送一個終端機按鍵序列。 |
| `create_window` | `command`（選用） | 使用預設設定檔建立視窗，可指定啟動命令，回傳新目標 ID。 |
| `create_tab` | `window_id`, `command`（選用） | 在指定視窗建立分頁，可指定啟動命令。 |
| `rename_session` | `session_id`, `name`（選用） | 重新命名現有工作階段；省略 `name` 時隨機分配未使用的暱稱。 |

透過 `create_window` 或 `create_tab` 建立的工作階段會自動取得隨機暱稱，內建名稱池包含 200 個互不相同、長度為 4–7 個字元的名稱。既有工作階段可使用 `rename_session` 命名。自訂名稱最多 64 個字元，名稱比對不區分大小寫。其他存活工作階段正在使用的名稱不可重複分配；工作階段關閉後名稱可再次使用。名稱池用盡時，自動命名會在建立工作階段前回報錯誤。

後續對話中以暱稱指定工作階段時，先透過 `list_sessions` 找到對應的正確 `session_id`，再使用該 ID 呼叫操作工具。

`key` 支援：`enter`、`tab`、`escape`、`backspace`、`up`、`down`、`left`、`right`、`ctrl_c`、`ctrl_d`、`ctrl_u`、`ctrl_l`。

文字內的換行、Enter 與控制鍵由前景程式解讀，可能執行命令或結束程式。傳送時明確設定 `suppress_broadcast=True`，僅傳送至指定工作階段。回應中的 `api_acknowledged` 僅表示 iTerm API 已確認；命令是否完成仍為 `unknown`。失敗或逾時不會自動重新傳送。

畫面與歷史讀取回傳終端機呈現結果，並非 CLI 內部編輯器原文或原始 PTY 位元組。服務只在目前行程內依序處理工具操作，不協調其他 MCP 行程或使用者手動輸入。結束服務僅中斷 API 連線，不會關閉使用者工作階段。

## 驗證

執行目前的自動化測試：

```sh
.venv/bin/python -m unittest discover -s tests -v
```

截至 2026 年 10 月 17 日，修復連線初始化資源清理問題後，全部 69 項自動化測試、Ruff 檢查、相依性檢查與 wheel 建置均通過。詳見[測試紀錄](docs/test-results.md)。

實際 stdio MCP → iTerm Python API 驗證涵蓋：建立視窗與分頁、傳送文字與 Enter、中文與 emoji 畫面讀取，以及非空的捲動歷史讀取。驗證後已關閉兩個臨時測試工作階段。

可重現的實際連線測試（僅操作新建立的 `/bin/cat` 工作階段）：

```sh
.venv/bin/python scripts/smoke_iterm.py         # 僅顯示計畫
.venv/bin/python scripts/smoke_iterm.py --run   # 建立、測試並關閉專用工作階段
```

## 專案結構

執行階段模組涵蓋 MCP 工具、iTerm 後端、讀取結果型別、游標座標換算、交易與連線清理。舊有 broker/gateway、管理 UI、CLI 認證與實驗實作已移除。

完整的保留與移除依據見[相依性整理與清理紀錄](docs/cleanup-plan.md)，逐模組清單見[相依性清單](docs/cleanup-dependencies.json)。

## 授權條款

[MIT](LICENSE) © 2026 lucky-goals。
