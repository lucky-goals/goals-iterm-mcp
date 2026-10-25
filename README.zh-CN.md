# goals-iterm-mcp

[项目官网](https://lucky-goals.github.io/goals-iterm-mcp/zh/)
[English](README.md) | 简体中文 | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

**让 Coding Agent 操作 Mac 上的各种 CLI，在手机上也能推进 vibe coding 任务。**

goals-iterm-mcp 通过 **iTerm2 Python API**，把支持 MCP 的 Agent 与电脑上的终端连接起来。你只需在对话中描述任务，Agent 就可以新开终端窗口、进入项目、启动另一个 Coding CLI、发送任务，再读取进度和结果。多个项目、多个 CLI 会话，都可以通过对话协调，减少手动切换终端的操作。

## 它能帮你做什么

- **在手机上继续推进任务**：通过 Agent 的手机端或远程入口，指挥 Mac 上的 iTerm2 工作，前提是该入口连接的 Agent 能调用本机 MCP。
- **调度不同 Coding CLI**：让 Agent 启动 Claude Code、Codex CLI 或其他 CLI，进入指定项目并提交开发或诊断任务。
- **在对话中查看进度**：列出窗口和标签页，读取终端输出，汇总各个会话正在做什么。
- **让多个项目独立运行**：为任务创建专用窗口或标签页，通过明确的 ID 操作目标会话。
- **围绕目标持续指导其他 CLI**：给 Codex、Claude 等主 Agent 设置目标（goal）和明确的完成条件。它可以通过本 MCP 反复读取其他 CLI 会话的进度、判断下一步、补充指令并核对结果，持续推进任务。目标管理和持续执行由主 Agent 负责，MCP 提供终端操作能力。

典型使用链路是：**手机或电脑上的对话 → 能访问本机 MCP 的 Agent → iTerm2 → Coding CLI**。Codex、Claude、Cursor、WorkBuddy、Trae、Hermes 等工具，只要所用客户端支持本地 stdio MCP，并具备你所需的手机端或远程连接方式，就可以采用这一模式。目前已通过 Codex 桌面版操作 iTerm2 中的 Claude Code 和 Codex CLI 完成人工验证。

例如，可以给主 Agent 这样的目标：

> 持续指导项目专用 iTerm 会话中的 Coding CLI，直到它完成登录问题的诊断、修复，并通过相关测试。每个阶段检查进度，必要时补充后续指令，最后按这些完成条件核对结果。不要重复提交仍在执行的任务；遇到超出本次范围的决策时询问我，完成后汇总改动和测试结果。

已发布的 0.2.2 包提供 13 个 stdio MCP 工具：原有的会话枚举、终端读写、窗口和标签页创建、会话命名工具，加上只读的 `inspect_sessions` 诊断工具和 4 个关闭工具。4 个关闭工具最初在 0.2.0 版本引入。源码契约见[关闭工具实现规范](docs/closing-tools-spec.md)。

## 前置配置

### 启用 iTerm2 Python API

需要 macOS，并已安装、运行 iTerm2。本项目已使用 **iTerm2 3.7.6** 完成验证。

1. 打开 **iTerm2 → Settings → General → Magic**。
2. 启用 **Python API**。
3. MCP 服务首次连接时，如弹出 iTerm2 API 授权请求，请允许连接。
4. 使用 MCP 工具期间，保持 iTerm2 运行。

设置详情见 [iTerm2 官方说明](https://iterm2.com/documentation-preferences-general.html)。

### 配置要使用的 Coding CLI

通过 iTerm2 操作某个 Coding Agent 的 CLI 前，请先安装该 CLI，并在 iTerm 环境中完成认证配置：登录对应的 OAuth 账号，或按照该 CLI 支持的方式配置 API Gateway 地址与凭据。先在 iTerm2 中手动启动一次，确认它能正常响应简单的提示词。

同时确保新开的 iTerm 窗口和标签页也能读取所需配置。goals-iterm-mcp 负责操作终端，各个 Coding CLI 使用自己的认证和 API 配置。凭据应保存在 CLI 支持的凭据存储或本机配置中，不要写进发给 Agent 的提示词。

## 使用 uvx 运行

0.2.2 已发布到 [PyPI](https://pypi.org/project/goals-iterm-mcp/)，该版本提供全部 13 个工具；无需克隆仓库即可运行：

```sh
uvx --python 3.12 goals-iterm-mcp@0.2.2 serve
```

Python 导入路径为 `goals_iterm_mcp`，启动命令为 `goals-iterm-mcp`。

## 通过 Agent 对话安装

将下面的提示词复制给能够执行本机命令、配置 MCP 客户端的 Agent，把 `[MCP 客户端]` 替换为实际客户端，例如 Codex 桌面版或 Claude Code。Agent 需要访问本机 macOS；纯云端聊天无法连接本地 iTerm2。

```text
请从 PyPI 安装 goals-iterm-mcp 0.2.2，并配置到 [MCP 客户端]。
项目地址：https://github.com/lucky-goals/goals-iterm-mcp

1. 确认本机是 macOS，已安装并运行 iTerm2。检查 uv/uvx 是否存在；
   如果缺失，按 uv 官方说明安装。使用 Python 3.12，不要克隆仓库，
   不依赖项目目录中的 .venv。
2. 执行以下命令验证包可以运行：
   uvx --python 3.12 goals-iterm-mcp@0.2.2 --help
3. 检查客户端现有 MCP 配置，保留其他设置。在用户级配置中添加或更新
   一个名为 iterm 的 stdio 服务，避免重复安装：
   command：本机 uvx 的绝对路径
   args：["--python", "3.12", "goals-iterm-mcp@0.2.2", "serve"]
   使用该客户端支持的配置格式和位置。修改已有配置前先备份，不改动其他服务。
4. 如有需要，指导我开启 iTerm2 Python API 并批准它的授权弹窗。
   按客户端支持的方式重新加载 MCP；若需要我手动重启客户端，请明确告知。
5. 验证 MCP 握手、已安装版本的工具发现，以及只读的 list_sessions 调用。
   安装验证期间不要发送终端输入、读取会话正文，也不要创建或关闭窗口。
6. 报告配置位置、启动命令和验证结果，区分“独立连接成功”和“目标客户端
   已实际加载工具”。如遇阻塞，说明具体还需要完成哪一步。
```

安装这个公开包不需要 PyPI 账号或 API Token。上述配置会提供已安装版本的全部工具；安装检查只枚举会话。

## 更新已安装的旧版本

先检查并备份客户端现有的 `iterm` MCP 配置，在原条目上更新，保留环境变量、`--read-only`、`--session-id` 限制及其他服务，避免重复添加。按原来的安装方式升级。

### 使用 uvx 安装的用户

查看 [PyPI 版本历史](https://pypi.org/project/goals-iterm-mcp/#history)和 [GitHub Releases](https://github.com/lucky-goals/goals-iterm-mcp/releases)，再验证最新已发布包可以启动：

```sh
uvx --python 3.12 goals-iterm-mcp@latest --help
```

这条命令**不会自动修改 MCP 客户端配置**。需要把现有 `args` 中的 `goals-iterm-mcp@0.1.2`（或其他旧版本）改成 `goals-iterm-mcp@<VERSION>`，将 `<VERSION>` 替换为已核实的发布版本号。保留 `uvx` 的绝对路径和其他参数。推荐固定版本，便于主动升级和回退到原版本。

如果希望以后启动时跟随最新发布版，也可以配置为 `goals-iterm-mcp@latest`，但每次启动使用的版本可能变化。仅删除版本号并不等同于使用最新版，因为 uvx 可能复用缓存；详见 [uv 工具版本说明](https://docs.astral.sh/uv/concepts/tools/#tool-versions)。这里的“最新”指已发布包，不包含仓库中尚未发布的改动。

### 从源码或通过 pip 安装的用户

如果使用下文的源码可编辑安装，先保存本地改动并确认目标分支，再在原仓库目录更新，并重新安装到原来的 Python 3.12 环境：

```sh
git pull --ff-only
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m goals_iterm_mcp --help
```

如果 Git 拒绝更新，先处理本地改动或分支分歧，再继续安装。客户端的 Python 绝对路径应继续指向这个环境。如果原来通过 pip 安装的是 PyPI 包，则用该服务实际使用的解释器执行：`/absolute/path/to/.venv/bin/python -m pip install --upgrade goals-iterm-mcp`；升级其他环境中的同名包不会更新当前 MCP。

### 重启连接并验收

按客户端支持的方式重启或重新连接 `iterm` MCP；必要时重启客户端。更新磁盘文件不会替换已经运行的服务进程。Codex 可在 **设置 → MCP servers → Restart** 重启连接，或重启 Codex，参见 [官方 MCP 配置说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

重连后，查询当前客户端实际加载的工具列表，并仅调用 `list_sessions` 验证连接。工具列表应与安装版本一致；0.2.2 版本应有 13 个工具，包含只读的 `inspect_sessions` 诊断工具，以及 `close_session`、`close_tab`、`close_window`、`clean_all_sessions`。独立进程握手成功不代表已有对话已经刷新工具；如果仍只显示旧接口，重连或新建对话后再次检查。升级验收不需要创建、关闭终端或发送输入。启动 Python 时禁止使用 `--reload`。

也可以复制下面的提示词交给本机 Agent：

```text
请把 [MCP 客户端] 中现有的 goals-iterm-mcp 更新到最新已发布版本。
先检查当前启动命令及安装方式，备份并原位更新 iterm 配置，不重复添加；
保留环境变量、只读/session 范围限制及其他 MCP 服务。核对 PyPI 和 GitHub
发布版本，更新固定版本号或该服务实际使用的环境，再重新加载 MCP 连接。
验证当前客户端实际加载的工具列表及只读 list_sessions 调用，报告版本、
配置位置和验证结果；如果仍需我手动重启，请明确说明。
原来是源码安装时，不要未经说明就切换到 PyPI 安装。
```

## 安装与启动

先完成上面的[前置配置](#前置配置)。从源码安装需要 Python 3.12。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m goals_iterm_mcp serve
```

`serve` 等待 MCP 客户端通过 stdin/stdout 发送协议消息，不是交互式命令行。进程诊断写入 stderr。没有 Python `--reload`。

MCP 客户端配置示例（替换为你的绝对路径）：

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

可选参数：

- `--read-only`：已发布的 0.2.2 包暴露 4 个只读工具：`list_sessions`、`read_screen`、`read_history` 和 `inspect_sessions`。
- `--session-id <ID>`：限制为指定会话，可重复传入。启用后禁止创建窗口/标签页，避免扩大访问范围。

默认可以操作当前用户 iTerm 中的全部会话。读取和发送操作必须传入 `list_sessions` 返回的准确 `session_id`；关闭工具还支持下面说明的明确选择器。不通过焦点、通配符、前缀或标题猜测目标。

## MCP 工具

| 工具 | 参数 | 功能 |
| --- | --- | --- |
| `list_sessions` | 无 | 枚举 session、tab、window ID、可读的 `name` 和会话元数据 |
| `read_screen` | `session_id` | 读取屏幕行、尺寸和光标位置 |
| `read_history` | `session_id`, `lines=100` | 读取最近的保留滚动历史，最多 2000 行 |
| `inspect_sessions` | `session_id=None`、`nickname=None` | 只读清理能力审计。可选择一个精确会话 ID 或唯一的忽略大小写昵称，也可以省略二者以检查全部授权可见会话。报告有限的 prompt/process/CLI 证据、活动、身份与持久化观察、阻碍、处置建议及提示性的关闭就绪状态；不提供任务授权或关闭授权，也不会发送输入、退出 CLI、写入回执或关闭会话。 |
| `send_text` | `session_id`, `text` | 原样发送文本，最多 16 KiB，不自动追加换行 |
| `send_keys` | `session_id`, `key` | 发送一个终端按键序列 |
| `create_window` | `command`（可选） | 新建默认 profile 窗口，可指定启动命令，返回新目标 ID |
| `create_tab` | `window_id`, `command`（可选） | 在指定窗口新建标签页，可指定启动命令 |
| `rename_session` | `session_id`, `name`（可选） | 重命名现有会话；省略 `name` 时随机分配未使用的昵称。 |
| `close_session` | `session_id=None`、`nickname=None`、`force=False`、`dry_run=False` | 退出 CLI 并完成原生关闭验证后关闭选定会话。必须且只能提供 `session_id` 或 `nickname` 之一。 |
| `close_tab` | `tab_id=None`、`session_id=None`、`nickname=None`、`force=False`、`dry_run=False` | 关闭选定会话或标签页所在的标签页。必须提供且只能提供一个选择器。 |
| `close_window` | `window_id=None`、`session_id=None`、`nickname=None`、`force=False`、`dry_run=False` | 关闭选定会话或窗口所在的窗口。必须提供且只能提供一个选择器。 |
| `clean_all_sessions` | `force=False`、`dry_run=False` | 根据初始会话快照清理允许范围内的会话。默认跳过忙碌、未知或无法恢复的会话；`force=true` 会尝试该快照中的每个允许会话。 |

### 只读会话能力审计

`inspect_sessions` 是已发布的 0.2.2 包中的只读诊断工具。`session_id` 与 `nickname` 互斥。传入精确 `session_id` 或唯一昵称（去除首尾空白并折叠大小写）时检查一个授权会话；省略二者时检查当前授权可见的全部会话。选择器无效、有歧义、不可用或超出授权范围时，工具在不写入终端的情况下失败。

每项结果包含会话、标签页和窗口身份，以及有限的 `prompt`、`process`、`cli`、`terminal_activity`、`business_acceptance` 和 `close_readiness` 部分。诊断可能报告证据是否可用、观测到的活动、稳定进程/CLI 身份、聊天持久化观察、阻碍代码和处置建议。`business_acceptance` 始终为 `not_observed`；`close_readiness` 仅供参考，其 `status` 为 `eligible_if_unchanged`、`blocked` 或 `unknown`，只有前者对应 `ready=true`。即使结果为 `ready: true`，也不代表获得授权或关闭一定成功；任何生命周期写入前都必须重新探测。该工具只读取证据，不会发送终端输入、退出 CLI、保存恢复回执或调用原生关闭。

### 关闭工具

这 4 个关闭工具最初在 0.2.0 版本引入，并继续包含在 0.2.2 版本中。完整契约见[关闭工具实现规范](docs/closing-tools-spec.md)。

`close_session`、`close_tab` 和 `close_window` 必须且只能使用一个选择器。ID 按明确 ID 匹配，绝不会退化为昵称匹配。昵称是会话范围内的选择器：目录中的名称会先去除首尾空白并折叠大小写后匹配；产生歧义时返回错误。关闭标签页或窗口时，使用会话选择器会选中其所在的标签页或窗口。不支持按焦点、通配符或前缀定位。只要标签页或窗口包含任何超出允许列表的会话，就会拒绝关闭。

普通关闭会在发送任何终端输入前预检所有受影响的会话。只要存在忙碌、等待中或活动状态未知的会话，就拒绝整个目标操作。`clean_all_sessions` 默认跳过忙碌、未知和无法恢复的会话，只关闭已确认空闲的会话，并保留其余会话和窗口。`force=true` 时，清理会尝试初始快照中的每个允许会话，之后新到达的会话不会加入本次请求。`force` 只授权绕过活动状态门槛，不会绕过允许列表、歧义身份、目标变化或恢复记录写入失败，也不等同于隐式 `SIGKILL`。iTerm 原生 force 的跳过界面确认行为与此中断授权是两回事。

在首次中断、退出 CLI 或关闭原生对象之前，服务会先写入私有恢复记录，只保留元数据，不保存终端正文、凭据或聊天内容。识别出的聊天 CLI 必须在中断或退出前提供实际 chat ID；服务不会仅为发现 ID 而向运行中或未知的 CLI 发送聊天文本或斜杠命令。普通 shell 和普通任务的 `cli_sessions` 为空；未知 CLI 身份不会被悄悄降级成 shell 或普通任务。如果当前进程或聊天身份的证据不可用，或无法与目标绑定，操作会安全失败（清理时可以将该会话报告为跳过）。屏幕无输出、运行时间、低 CPU、看起来已完成的回答或会话列表中的 `in_progress` 都不是完成证据。CLI 退出有时间上限并会验证；如果无法验证退出，服务不会为该目标发出原生关闭请求，但 iTerm 配置的“退出时自动关闭”行为仍可能因 CLI 自身退出而移除标签页。服务不会按名称杀掉共享守护进程或其他进程。

当前恢复支持有意保持狭窄。Claude 和 Codex 只能通过绑定 chat ID 的可信进程登记，或通过当前进程确实打开的 transcript/rollout 文件识别聊天；只出现在启动参数中的 UUID 会被报告，但仍会阻止中断。Kiro 的保存会话列表和启动参数都不能证明当前聊天身份，因此必须有与进程绑定的已知 ID；即使 `force=true`，无法证明的身份也会被拒绝。Cursor 的被动路径只接受由提供的 PID 当前打开的受认可日志文件得到的 UUID；仅有启动参数不够。除非有可信的生命周期证据，CLI 任务活动通常仍是 `unknown`；适配器不会自动捕获每个聊天或所有正在运行的 CLI。

结果包含 `operation`、`status`、`force`、`dry_run`、`target`、恢复记录 ID 和路径，以及已关闭、已跳过、失败和未知会话列表。`status` 为 `closed`、`partial`、`refused`、`noop`、`dry_run` 或 `unknown` 之一。每个会话结果包含 iTerm ID、昵称、`cli_sessions`、中断、退出和关闭状态。聊天记录包含 `cli`、`chat_uuid`、`cwd`、身份来源、持久化状态和结构化的恢复 executable/argv。`chat_uuid` 只是聊天标识；它本身不能证明历史已保存，也不能证明恢复命令已经测试成功，这些信息由独立的结果字段表示。关闭后会刷新拓扑，因为 API 确认不等于已经关闭。丢失或超时的响应会变成 `unknown`，不会自动重试。拒绝、失败和未知操作会以带结构化数据的 MCP 工具错误返回；仅有清理跳过时不算错误。部分结果会报告已完成的副作用，不会通过重新打开终端回滚。试运行不会发送输入、写入恢复记录、修改名称或关闭对象。

通过 `create_window` 或 `create_tab` 创建的会话会自动获得随机昵称，内置名称池包含 200 个互不相同、长度为 4–7 个字符的名称。已有会话可使用 `rename_session` 命名。自定义名称最多 64 个字符，名称比较不区分大小写。其他存活会话正在使用的名称不可重复分配；会话关闭后名称可以复用。名称池耗尽时，自动命名会在创建会话前报错。关闭工具的昵称选择器使用同一份存活会话目录，并在匹配前去除首尾空白、折叠大小写。

读取和发送操作需要先通过 `list_sessions` 找到准确的 `session_id`。关闭工具可以按上面规则使用精确昵称或明确目标 ID；不要根据焦点或标题推测目标。

`key` 支持：`enter`、`tab`、`escape`、`backspace`、`up`、`down`、`left`、`right`、`ctrl_c`、`ctrl_d`、`ctrl_u`、`ctrl_l`。

文本内的换行、Enter 和控制键由前台程序解释，可能执行命令或退出程序。发送时明确设置 `suppress_broadcast=True`，只发送到指定会话。响应中的 `api_acknowledged` 仅表示 iTerm API 已确认；命令是否完成仍为 `unknown`。失败或超时不会自动重发。

屏幕和历史是终端渲染结果，不代表 CLI 内部编辑器原文或原始 PTY 字节。服务只在当前进程内串行处理工具操作，不协调其他 MCP 进程或用户手动输入。退出服务只断开 API 连接，不关闭用户会话。

## 验证

运行关闭工具、恢复适配器和 direct backend 的定向测试：

```sh
.venv/bin/python -m unittest tests.test_closing tests.test_closing_backend tests.test_cli_recovery tests.test_direct -q
```

截至 2026-10-22，这 69 项定向测试均通过。改动文件的 Ruff 检查和 `git diff --check` 也通过；本次验证不代表全量测试结果。

关闭工具的实机 smoke 测试是选择性执行的，只创建新的、处于允许列表内的 iTerm 会话。可以先查看计划，或显式执行：

```sh
.venv/bin/python scripts/smoke_closing.py          # 只显示计划
.venv/bin/python scripts/smoke_closing.py --run   # 执行选择性的原生 smoke 测试
```

2026-10-22 的运行通过：3 个定向原生关闭成功；默认清理关闭 1 个空闲会话并跳过 1 个运行中的 `sleep` 会话；force 清理随后关闭 1 个会话；原有 6 个会话全部保留。

此前的真实 stdio MCP → iTerm Python API 验证覆盖：创建窗口/标签页、发送文本与 Enter、中文/emoji 屏幕读取及非空滚动历史读取；本次创建的两个测试会话均已关闭。

如需重现此前的真实联通验证（仅操作新创建的 `/bin/cat` 会话）：

```sh
.venv/bin/python scripts/smoke_iterm.py         # 只显示计划
.venv/bin/python scripts/smoke_iterm.py --run   # 创建、测试并关闭专用会话
```

## 当前结构

运行模块涵盖 MCP 工具、iTerm 后端、读取结果类型、光标坐标换算、事务、连接清理和关闭工具安全契约。旧 broker/gateway、管理 UI 和实验实现已删除；关闭工具的 CLI 身份依赖当前进程与运行时证据。

完整的保留/删除依据见 [依赖梳理和清理记录](docs/cleanup-plan.md)，逐模块清单见 [依赖清单](docs/cleanup-dependencies.json)。

## 许可证

[MIT](LICENSE)，版权归 2026 lucky-goals 所有。
