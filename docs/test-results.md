# 当前实现验收记录

日期：2026-10-17。范围：iTerm2 Python API 的 7 个 stdio MCP 工具。不包含 CLI 内部状态识别或插件改造。

## 缺陷与修复

连接建立后，如果获取 iTerm app 失败，旧代码会直接丢弃连接引用，导致 dispatcher 和 websocket 未释放。现在所有初始化失败路径（包括 DomainError、取消和空 app 返回）先执行有界清理再返回错误。重复取消不会中断清理。清理失败、超时或 websocket 自行取消后禁止再次连接，需重启服务；正常清理后允许重新建立连接。清理最多等待 2 秒，无法强制保证第三方 SDK 已释放不响应取消的资源。

## 自动化验证

执行 `.venv/bin/python -m unittest discover -s tests -v`：69 项全部通过。

覆盖 MCP 工具注册、真实 stdio 握手、stdout 隔离、只读模式、会话限制、全部 12 种按键映射、文本/历史/命令参数约束、创建参数传递、写入不重试与串行化、屏幕与光标、历史事务、会话生命周期及连接清理。新增初始化异常、取消、重复取消、重连、空 app、清理异常/超时/自行取消回归测试。

`ruff check src tests scripts` 和 `.venv/bin/python -m pip check` 均通过。

最终 wheel 构建通过，SHA-256：`92be388ccf0cea21707319fdd1ad8317f934a99ca19d9a38824bea809d3ceccd`。

## 真实 iTerm 验证

执行 `.venv/bin/python scripts/smoke_iterm.py --run`，退出码 0。通过真实 stdio MCP 子进程调用全部 7 个工具，仅向本次新建的 `/bin/cat` 会话发送测试内容。

| 检查 | 结果 |
| --- | --- |
| 工具枚举 | 7 个 |
| 会话枚举、创建窗口、指定窗口创建标签页 | 通过 |
| 原样文本和 Enter 发送 | API 确认 |
| 中文/emoji 屏幕匹配 | 通过 |
| 非空滚动历史内容匹配 | 通过 |
| 关闭本次创建的测试会话 | 2 个，已确认 |

全部按键的字节映射通过自动化测试；真实终端仅验证 Enter，不把不同前台程序对控制键的处理视为 MCP 的完成保证。API 确认仍不等于前台命令执行完成。
