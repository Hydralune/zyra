# Zyra CLI + Web 一页演示流程

本文用于课堂或交接时快速启动。完整的首次安装、provider、T1 和故障排查见
[`QUICKSTART.zh-CN.md`](QUICKSTART.zh-CN.md)。

## 演示前一次性检查

```powershell
Set-Location G:\agent-zoo\zyra
Test-Path .\.venv\Scripts\python.exe
Test-Path .\apps\cli\dist\zyra.js
Test-Path .\apps\web\dist\index.html
node .\apps\cli\dist\zyra.js --help
```

三个 `Test-Path` 应返回 `True`，help 应列出 `zyra`、`resume`、`dev`、`events`、`run`、
`ls`、`scenario`、`ui` 和 `daemon`。真实 Agent 任务还要求本机至少配置一个有效 provider。

## 窗口 A：启动 daemon 和 Web

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js ui
```

该命令会确保 API daemon 位于 `http://127.0.0.1:8000`，Web 位于
`http://127.0.0.1:5173`，并打开浏览器。它不是第二套 Agent runtime。

## 窗口 B：启动产品 CLI

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js
```

输入：

```text
测试，收到请回复
```

产品 TUI 应直接显示用户消息、少量可理解的活动、助手最终回答和验证状态，不应出现百余条
`runtime.node.updated` / `runtime.agent.message`。也可用一条命令直接演示：

```powershell
node .\apps\cli\dist\zyra.js "测试，收到请回复"
```

## 在 Web 打开同一个 task

记下 CLI 底部的 `task_...`，执行：

```powershell
node .\apps\cli\dist\zyra.js ui --task task_替换为实际ID
```

CLI 与 Web 应显示相同终态和最终回答。CLI 适合对话、权限、控制、文件/验证摘要；Web 适合
查看完整拓扑、原始事件、artifact、audit、evidence 和大型 diff。

## 演示运行中控制

- Enter：立即重定向当前任务；
- Tab：把当前输入排队；
- Esc：中断当前步骤；
- `/queue`：查看 canonical 队列；
- `/cancel` / `/continue`：取消或继续任务；
- `/approve <ID>` / `/deny <ID>`：处理权限；
- `/ui`：打开当前 task 的 Web；
- `/exit`：仅退出 listener，不隐式取消远端任务。

恢复同一任务：

```powershell
node .\apps\cli\dist\zyra.js resume task_替换为实际ID
```

## 显式开发者事件界面

需要证明底层事件仍可观察时：

```powershell
node .\apps\cli\dist\zyra.js events task_替换为实际ID
```

它会显示 sequence、`runtime.*` event type、artifact 和 revision。创建一个带原始事件输出的
新任务则使用：

```powershell
node .\apps\cli\dist\zyra.js dev "测试，收到请回复"
```

## 演示结束

Web 页面可以直接关闭。确认没有仍需运行的任务后停止 daemon：

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js daemon stop
```

普通 stop 会在存在活动任务时拒绝，不要为了方便默认使用 `--force=true`。
