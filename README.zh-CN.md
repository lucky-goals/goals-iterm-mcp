# goals-iterm-mcp

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

服务入口为 `serve`，通过 stdio 提供 8 个 MCP 工具，覆盖会话枚举、终端读取、文本与按键发送，窗口和标签页创建，以及会话命名。

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

当前源码版本：**0.1.2**（尚未发布到 PyPI）。

0.1.1 已发布到 [PyPI](https://pypi.org/project/goals-iterm-mcp/)，无需克隆仓库即可运行：

```sh
uvx --python 3.12 goals-iterm-mcp@0.1.1 serve
```

Python 导入路径为 `goals_iterm_mcp`，启动命令为 `goals-iterm-mcp`。

## 通过 Agent 对话安装

将下面的提示词复制给能够执行本机命令、配置 MCP 客户端的 Agent，把 `[MCP 客户端]` 替换为实际客户端，例如 Codex 桌面版或 Claude Code。Agent 需要访问本机 macOS；纯云端聊天无法连接本地 iTerm2。

```text
请从 PyPI 安装 goals-iterm-mcp 0.1.1，并配置到 [MCP 客户端]。
项目地址：https://github.com/lucky-goals/goals-iterm-mcp

1. 确认本机是 macOS，已安装并运行 iTerm2。检查 uv/uvx 是否存在；
   如果缺失，按 uv 官方说明安装。使用 Python 3.12，不要克隆仓库，
   不依赖项目目录中的 .venv。
2. 执行以下命令验证包可以运行：
   uvx --python 3.12 goals-iterm-mcp@0.1.1 --help
3. 检查客户端现有 MCP 配置，保留其他设置。在用户级配置中添加或更新
   一个名为 iterm 的 stdio 服务，避免重复安装：
   command：本机 uvx 的绝对路径
   args：["--python", "3.12", "goals-iterm-mcp@0.1.1", "serve"]
   使用该客户端支持的配置格式和位置。修改已有配置前先备份，不改动其他服务。
4. 如有需要，指导我开启 iTerm2 Python API 并批准它的授权弹窗。
   按客户端支持的方式重新加载 MCP；若需要我手动重启客户端，请明确告知。
5. 验证 MCP 握手、已安装版本的工具发现，以及只读的 list_sessions 调用。
   安装验证期间不要发送终端输入、读取会话正文，也不要创建或关闭窗口。
6. 报告配置位置、启动命令和验证结果，区分“独立连接成功”和“目标客户端
   已实际加载工具”。如遇阻塞，说明具体还需要完成哪一步。
```

安装这个公开包不需要 PyPI 账号或 API Token。上述配置会提供已安装版本的全部工具；安装检查只枚举会话。

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

- `--read-only`：只暴露枚举和读取工具。
- `--session-id <ID>`：限制为指定会话，可重复传入。启用后禁止创建窗口/标签页，避免扩大访问范围。

默认可以操作当前用户 iTerm 中的全部会话。每次读写必须传入 `list_sessions` 返回的准确 `session_id`；不通过焦点或标题猜测目标。

## MCP 工具

| 工具 | 参数 | 功能 |
| --- | --- | --- |
| `list_sessions` | 无 | 枚举 session、tab、window ID、可读的 `name` 和会话元数据 |
| `read_screen` | `session_id` | 读取屏幕行、尺寸和光标位置 |
| `read_history` | `session_id`, `lines=100` | 读取最近的保留滚动历史，最多 2000 行 |
| `send_text` | `session_id`, `text` | 原样发送文本，最多 16 KiB，不自动追加换行 |
| `send_keys` | `session_id`, `key` | 发送一个终端按键序列 |
| `create_window` | `command`（可选） | 新建默认 profile 窗口，可指定启动命令，返回新目标 ID |
| `create_tab` | `window_id`, `command`（可选） | 在指定窗口新建标签页，可指定启动命令 |
| `rename_session` | `session_id`, `name`（可选） | 重命名现有会话；省略 `name` 时随机分配未使用的昵称。 |

通过 `create_window` 或 `create_tab` 创建的会话会自动获得随机昵称，内置名称池包含 200 个互不相同、长度为 4–7 个字符的名称。已有会话可使用 `rename_session` 命名。自定义名称最多 64 个字符，名称比较不区分大小写。其他存活会话正在使用的名称不可重复分配；会话关闭后名称可以复用。名称池耗尽时，自动命名会在创建会话前报错。

后续对话中按昵称指定会话时，先通过 `list_sessions` 找到对应的准确 `session_id`，再使用该 ID 调用操作工具。

`key` 支持：`enter`、`tab`、`escape`、`backspace`、`up`、`down`、`left`、`right`、`ctrl_c`、`ctrl_d`、`ctrl_u`、`ctrl_l`。

文本内的换行、Enter 和控制键由前台程序解释，可能执行命令或退出程序。发送时明确设置 `suppress_broadcast=True`，只发送到指定会话。响应中的 `api_acknowledged` 仅表示 iTerm API 已确认；命令是否完成仍为 `unknown`。失败或超时不会自动重发。

屏幕和历史是终端渲染结果，不代表 CLI 内部编辑器原文或原始 PTY 字节。服务只在当前进程内串行处理工具操作，不协调其他 MCP 进程或用户手动输入。退出服务只断开 API 连接，不关闭用户会话。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -v
```

2026-10-17 修复连接初始化资源回收缺陷后，当前全部 69 项自动化测试、Ruff、依赖检查和 wheel 构建均通过，详见 [测试记录](docs/test-results.md)。真实 stdio MCP → iTerm Python API 验证覆盖：创建窗口/标签页、发送文本与 Enter、中文/emoji 屏幕读取及非空滚动历史读取；本次创建的两个测试会话均已关闭。

可复现的真实联通验证（仅操作本次创建的 `/bin/cat` 会话）：

```sh
.venv/bin/python scripts/smoke_iterm.py         # 只显示计划
.venv/bin/python scripts/smoke_iterm.py --run   # 创建、测试并关闭专用会话
```

## 当前结构

运行模块仅保留 MCP 工具、iTerm 后端、读取结果类型、光标坐标换算、事务与连接清理。旧 broker/gateway、管理 UI、CLI 认证和实验实现已删除。

完整的保留/删除依据见 [依赖梳理和清理记录](docs/cleanup-plan.md)，逐模块清单见 [依赖清单](docs/cleanup-dependencies.json)。

## 许可证

[MIT](LICENSE)，版权归 2026 lucky-goals 所有。
