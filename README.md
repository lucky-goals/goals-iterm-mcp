# cc-iterm-mcp

通过 **iTerm2 Python API** 向 MCP 客户端提供终端工具。直接入口为 `serve`，使用 stdio 通信；不需要 Claude/Codex 插件、源码修改、CLI 适配认证或管理 UI。

## 安装与启动

需要 macOS、运行中的 iTerm2、Python 3.12。在 iTerm2 的 Settings → General → Magic 中启用 Python API；首次连接时允许 iTerm2 的 API 授权请求（[官方设置说明](https://iterm2.com/documentation-preferences-general.html)）。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m cc_iterm_mcp serve
```

`serve` 等待 MCP 客户端通过 stdin/stdout 发送协议消息，不是交互式命令行。进程诊断写入 stderr。没有 Python `--reload`。

MCP 客户端配置示例（替换为你的绝对路径）：

```json
{
  "mcpServers": {
    "iterm": {
      "command": "/Users/jwyuan/source_code/cc-iterm-mcp/.venv/bin/python",
      "args": ["-m", "cc_iterm_mcp", "serve"]
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
| `list_sessions` | 无 | 枚举 session、tab、window ID 和会话元数据 |
| `read_screen` | `session_id` | 读取屏幕行、尺寸和光标位置 |
| `read_history` | `session_id`, `lines=100` | 读取最近的保留滚动历史，最多 2000 行 |
| `send_text` | `session_id`, `text` | 原样发送文本，最多 16 KiB，不自动追加换行 |
| `send_keys` | `session_id`, `key` | 发送一个终端按键序列 |
| `create_window` | `command`（可选） | 新建默认 profile 窗口，可指定启动命令，返回新目标 ID |
| `create_tab` | `window_id`, `command`（可选） | 在指定窗口新建标签页，可指定启动命令 |

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
