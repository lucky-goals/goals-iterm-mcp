# 项目协作要求

- 非必要不要执行全量测试；根据改动选择定向测试及必要的发布检查。
- 启动 Python 进程禁止使用 `--reload` 模式。

## 发布流程

用户要求“发布版本”时，默认必须同时完成 **PyPI 发布和 GitHub Release**，除非用户明确限定发布渠道。只上传 PyPI 不算发布完成。

1. 确认工作区、目标提交及版本号；`pyproject.toml` 和包内 `__version__` 必须一致。同步四种语言 README 的版本与安装命令。
2. 执行与变更相关的定向测试。构建 wheel 和源码包，执行 `twine check --strict`，确认包内版本、模块和入口正确。不得输出或提交发布凭据。
3. 提交并推送发布改动，在对应提交创建 `v<版本号>` tag；不得移动或覆盖已发布的 tag。
4. 创建同名 GitHub Release，填写本版本变更和验证结果，附上 wheel、源码包及 PyPI 版本页面链接。仓库的 `release.published` workflow 会构建并通过 Trusted Publishing 上传 PyPI；正常发布优先使用此流程，并等待 workflow 完成。
5. 若使用本地凭据先上传 PyPI，仍必须补齐 tag 和 GitHub Release。补发前检查已有发布状态，不重复创建版本；核对 PyPI 文件的 SHA256 与本地发布文件一致。workflow 的 `skip-existing` 仅用于兼容这种补发，不代表验证已完成。
6. 完成检查：PyPI 版本页面存在、wheel 和源码包均可下载且校验值正确；GitHub tag 指向预期提交、Release 已公开且附件齐全；安装发布版本后 CLI 能正常启动。检查 workflow 结果，失败时修复或明确报告具体阻碍，不得宣称所有发布步骤成功。
7. 最终回复同时提供 PyPI 和 GitHub Release 链接及版本号。未完成任一渠道时，明确说明剩余工作。
