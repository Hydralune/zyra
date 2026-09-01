# Zyra 启动与使用指南

本文面向第一次接手 Zyra 的开发者和运行人员，说明如何在 Windows
PowerShell 中完成环境准备、模型配置、CLI/Web 启动、任务提交、恢复与停止，
以及如何通过正式宿主入口运行比赛 T1。

> 当前主要验证环境是 Windows。以下命令默认仓库位于
> `G:\agent-zoo\zyra`，比赛任务包位于
> `G:\agent-zoo\competition-tasks`。如果实际路径不同，请替换为对应绝对路径。

## 1. 先理解三个入口

Zyra 的运行关系是：

```text
CLI / Web Workbench
        │
        ▼
Zyra API daemon（canonical state）
        │
        ▼
Agent runtime / workers / providers / tools
```

- CLI 是默认任务入口，适合交互、自动化、恢复和 scenario 执行。
- Web Workbench 连接同一个 daemon，用于观察、证据检查和复杂治理；它不是第二套 runtime。
- daemon 保存 task、session、event、permission、terminal 和 artifact 等权威状态。
- `zyra ui`、`zyra run`、交互模式和 `resume` 默认都会在 daemon 不可用时自动启动本地 daemon。

## 2. 当前工作区的最快启动方式

如果 `.venv`、`node_modules`、`apps\cli\dist\zyra.js` 和
`apps\web\dist\index.html` 已存在，直接执行：

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js ui
```

启动 Web 不要求模型请求立即成功，但要执行真实 Agent 任务，仍需按第 4 节配置至少一个
有效 provider。

该命令会：

1. 确保 API daemon 可用，默认地址为 `http://127.0.0.1:8000`；
2. 确保 Web Workbench 可用，默认地址为 `http://127.0.0.1:5173`；
3. 打开系统浏览器。

不希望自动打开浏览器时：

```powershell
node .\apps\cli\dist\zyra.js ui --open=false
```

Web 端口冲突时，可让系统选择临时端口：

```powershell
node .\apps\cli\dist\zyra.js ui --web-port 0
```

### 2.1 给老师演示 CLI + Web

先在第一个 PowerShell 窗口启动 daemon 和 Web：

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js ui
```

再在第二个 PowerShell 窗口启动产品 CLI。要让任务操作 Zyra 仓库本身：

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js
```

第一次无参数启动会在同一个 TUI 中显示当前 workspace、daemon/runtime readiness、
可用 provider/model 数量和权限边界。选择 canonical 自动路由即可进入 composer；也可为
本次会话选择模型与推理强度。Zyra 不会在这个界面中收集或保存 provider 密钥。

如果引导显示没有可用模型，先进入 composer 后执行：

```text
/doctor
```

根据恢复动作检查第 4 节的 provider 配置。损坏或未知版本的首次使用状态不会被静默覆盖。

在 TUI 中输入任务；也可以直接提交：

```powershell
node .\apps\cli\dist\zyra.js "测试，收到请回复"
```

CLI 底部会显示 `task_...`。要让 Web 直接打开同一个任务：

```powershell
node .\apps\cli\dist\zyra.js ui --task task_替换为实际ID
```

这两个窗口不是两套 Agent：CLI 负责对话、控制和简洁结果，Web 负责完整拓扑、事件、
artifact、audit、evidence 和大型 diff；关键终态与最终回答来自同一个 daemon。

## 3. 新机器首次准备

### 3.1 前置软件

需要安装：

- Git；
- Python 3.12 或更高版本；
- Node.js 22；
- Bun 1.2.15；
- Docker Desktop（仅正式比赛任务、容器场景或相关测试需要）。

检查版本：

```powershell
git --version
python --version
node --version
bun --version
docker version
```

仓库的 `package.json` 固定使用 Bun 1.2.15。如果 Bun 没有加入全局 PATH，可通过
`npx` 完成首次安装，然后使用仓库本地 Bun。

### 3.2 创建 Python 虚拟环境

```powershell
Set-Location G:\agent-zoo\zyra
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

根项目是 Python 包的权威安装边界，不需要再分别安装 `packages/**` 或
`vendor/browser-use`。

### 3.3 安装 JavaScript 依赖

全局 Bun 可用时：

```powershell
bun install --frozen-lockfile
```

全局 Bun 不可用时：

```powershell
npx --yes bun@1.2.15 install --frozen-lockfile
```

安装完成后，本地 Bun 位于：

```text
G:\agent-zoo\zyra\node_modules\.bin\bun.exe
```

### 3.4 构建 CLI 和 Web

```powershell
.\node_modules\.bin\bun.exe run build:cli
.\node_modules\.bin\bun.exe run build:web
```

确认产物存在：

```powershell
Test-Path .\apps\cli\dist\zyra.js
Test-Path .\apps\web\dist\index.html
```

两行都应返回 `True`。

### 3.5 验证 CLI

```powershell
node .\apps\cli\dist\zyra.js --help
```

开发过程中也可以直接运行 TypeScript 源码：

```powershell
.\node_modules\.bin\bun.exe .\apps\cli\src\index.ts --help
```

交接和正式运行优先使用构建后的 Node 入口，以减少环境差异。

## 4. 配置模型 Provider

真实任务至少需要一个有效的模型 provider。使用以下任一文件保存本机密钥；这些
`*.local` 文件已被 Git 忽略，不得提交，也不要把密钥复制到日志或任务提示中。

### DeepSeek

文件 `G:\agent-zoo\zyra\.env.deepseek.local`：

```dotenv
DEEPSEEK_API_KEY=替换为真实密钥
```

### 智谱 GLM

文件 `G:\agent-zoo\zyra\.env.glm.local`：

```dotenv
ZAI_API_KEY=替换为真实密钥
```

### Kimi

文件 `G:\agent-zoo\zyra\.env.kimi.local`：

```dotenv
KIMI_API_KEY=替换为真实密钥
```

daemon 会从这些文件中仅读取对应的 allowlisted key。配置多个 provider 时，实际选择
和故障切换由运行时 provider policy 决定。

可选的真实 provider smoke test 会产生一次真实网络请求，可能产生费用，只在确认可以
调用相应账号时执行：

```powershell
.\node_modules\.bin\bun.exe run provider:deepseek:smoke
.\node_modules\.bin\bun.exe run provider:glm:smoke
.\node_modules\.bin\bun.exe run provider:kimi:smoke
```

只需测试已经配置的 provider，不要无条件执行全部三项。

## 5. 启动与检查 daemon

通常不需要手工启动 daemon，`ui` 和任务命令会自动启动。需要单独管理时使用：

```powershell
Set-Location G:\agent-zoo\zyra

node .\apps\cli\dist\zyra.js daemon start
node .\apps\cli\dist\zyra.js daemon status
```

也可以直接检查 HTTP health：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

使用其他 API 端口：

```powershell
node .\apps\cli\dist\zyra.js daemon start `
  --base-url http://127.0.0.1:8010

node .\apps\cli\dist\zyra.js ui `
  --base-url http://127.0.0.1:8010 `
  --web-port 5174
```

自动启动只允许 loopback 地址，这是预期的安全限制。

## 6. 提交普通任务

### 6.1 工作目录很重要

Zyra 将启动 CLI 时的当前目录作为本地 terminal/workspace 上下文。要操作另一个项目，
应先进入目标项目，再通过绝对路径调用 Zyra CLI：

```powershell
Set-Location G:\path\to\target-project
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js "检查当前项目，修复测试失败并完成验证"
```

不要为了操作目标项目而先进入 `zyra` 目录，否则任务的初始 workspace 会指向 Zyra 自身。

### 6.2 交互模式

不带目标启动一个交互 session：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js
```

直接带目标启动交互任务：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js `
  "理解这个仓库，修复当前缺陷，运行必要测试并报告剩余风险"
```

常用交互命令：

```text
Enter             运行中立即重定向
Tab               将当前输入排队
Esc               中断当前步骤
/queue            查看 canonical 队列
/interrupt <说明> 中断并给出新说明
/redirect <说明>  立即改变任务方向
/continue         继续任务
/approve <ID>     批准指定权限请求
/deny <ID>        拒绝指定权限请求
/cancel           取消任务
/retry <ID>       重试可恢复队列命令
/ui               打开当前 task 的 Web 看板
/exit             仅退出当前 listener
```

`/exit` 不会停止 daemon，也不会自动取消后台任务。之后可以用 `resume` 重新连接。

### 6.3 开发者事件模式

默认产品 TUI 不显示原始 `runtime.*` 事件。排障时显式使用：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js dev "测试，收到请回复"
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js events task_xxxxxxxxxxxx
```

`dev` 创建/执行任务并显示完整事件；`events` 只附着观察现有任务，不会主动重新执行它。

### 6.4 非交互模式

直接传入目标：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js run `
  "检查当前仓库，修复失败并完成验证"
```

从 UTF-8 文件读取目标：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js run --file .\TASK.md
```

从管道读取：

```powershell
Get-Content .\TASK.md |
  node G:\agent-zoo\zyra\apps\cli\dist\zyra.js run
```

保存 JSONL 输出：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js run --file .\TASK.md |
  Tee-Object .\zyra-run.jsonl
```

goal 参数、`--file` 和管道输入三者互斥。非 TTY stdout 的每一行都是独立 JSON object；
诊断和警告写入 stderr。

### 6.5 sealed 自主模式

需要无人值守的比赛策略时：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js run `
  --file .\TASK.md `
  --sealed `
  --timeout 0
```

- `--sealed` 使用自主比赛策略，不等待人工 permission 决策；
- `--timeout 0` 表示 CLI 不设置任务总时限，各 HTTP/tool 操作仍有自己的边界；
- 普通开发任务优先使用交互模式，不要默认启用 sealed。

## 7. 查看、恢复和用 Web 观察任务

列出 canonical task 和 session：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js ls
```

按 task 或 task-backed session ID 恢复：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js resume task_xxxxxxxxxxxx
```

直接在 Web 中打开指定任务：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js ui `
  --task task_xxxxxxxxxxxx
```

CLI 和 Web 连接同一 daemon，因此可以一边在 CLI 中运行任务，一边在 Web 中观察事件、
拓扑、证据、permission、terminal 和 artifact。

## 8. 退出码

自动化脚本应检查 CLI 进程退出码：

| 退出码 | 含义 |
|---:|---|
| 0 | 命令达到该命令定义的成功终态 |
| 1 | task/scenario 失败或 runtime contract 不可接受 |
| 2 | 参数、输入或文件错误 |
| 3 | daemon/API 不可达或 readiness 失败 |
| 4 | 显式取消、signal 中断或非交互 permission wait |
| 5 | final verifier、completion 或 evidence gate 未通过 |

注意：比赛任务中 CLI 返回 `0` 只表示 Zyra CLI 认为任务达到自身终态，不能替代外部
Evaluator 的领域验收结果。

## 9. 停止服务

确认没有需要继续运行的任务后：

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js daemon stop
```

存在活动任务时，普通 stop 会拒绝执行，这是保护机制。只有明确决定取消活动任务时才可
强制停止：

```powershell
node .\apps\cli\dist\zyra.js daemon stop --force=true
```

不要把 `--force=true` 作为日常停止方式。关闭浏览器页面本身不会停止 daemon 或任务。

当前 CLI 没有 `ui stop` 命令。`zyra ui` 启动的本地 Web 进程信息保存在
`$env:LOCALAPPDATA\Zyra\cli\ui.json`。确需停止 Web server 时，先核对记录的进程确实是
`scripts\dev_web.py`，再停止它：

```powershell
$ZyraUiState = Get-Content -Raw `
  -LiteralPath "$env:LOCALAPPDATA\Zyra\cli\ui.json" | ConvertFrom-Json
$ZyraWebPid = [int]$ZyraUiState.pid

Get-CimInstance Win32_Process -Filter "ProcessId = $ZyraWebPid" |
  Select-Object ProcessId, ExecutablePath, CommandLine

# 仅在上一步确认 CommandLine 指向 Zyra scripts\dev_web.py 后执行：
Stop-Process -Id $ZyraWebPid
```

不要在未核对 CommandLine 时直接停止状态文件中的 PID，因为过期 PID 可能已被其他进程
复用。

## 10. T1 正式证明运行

T1 的正式入口不是手工执行一条裸 `zyra run`。必须通过 T1 formal runner，让宿主完成
干净环境准备、动态事件、测试农场、证据固化和独立验收。

### 10.1 前置检查

1. Docker Desktop 已启动且当前用户可以访问 daemon；
2. 至少一个模型 provider 密钥有效；
3. API 端口 `8000` 和任务端口 `48650`～`48654` 未被无关进程占用；
4. C/G 盘有足够空间；
5. 没有另一轮 T1 或需要保留的 Zyra 任务正在运行；
6. `zyra` 工作区位于需要证明的准确 commit，且明确记录工作区是否干净。

先检查已有 daemon：

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js daemon status
```

如它是当前 CLI 管理且没有活动任务，可以正常停止：

```powershell
node .\apps\cli\dist\zyra.js daemon stop
```

### 10.2 基础设施预检

使用唯一 run ID：

```powershell
Set-Location G:\agent-zoo\competition-tasks\task-1-software-engineering

G:\agent-zoo\zyra\.venv\Scripts\python.exe `
  -m implementation.runner.formal_run `
  --seed showcase `
  --run-id t1infra-team-01 `
  --infrastructure-check
```

只有 `formal-host-outcome.json` 的状态为 `infrastructure_check_passed` 才进入完整运行。

### 10.3 完整展示运行

```powershell
Set-Location G:\agent-zoo\competition-tasks\task-1-software-engineering

G:\agent-zoo\zyra\.venv\Scripts\python.exe `
  -m implementation.runner.formal_run `
  --seed showcase `
  --run-id t1showcase-team-01
```

必须为每次尝试使用新的 run ID，不要覆盖或复用历史运行目录。完整运行可能持续很久；不要
仅因为终端暂时没有输出就终止进程。

运行目录为：

```text
G:\agent-zoo\competition-tasks\runs\t1\<run-id>
```

重点证据：

```text
evidence\formal-runner.jsonl
evidence\zyra-cli.stdout.jsonl
evidence\zyra-cli.stderr.log
evaluator\evaluation.json
evaluator\formal-host-outcome.json
```

完整通过必须同时满足：

```text
formal-host-outcome.status == "completed"
formal-host-outcome.domain_result == "passed"
formal-host-outcome.evidence_qualification == "qualified"
```

不要仅凭以下现象宣称 T1 成功：

- Zyra CLI 成功启动；
- CLI 返回码为 0；
- 生成了 `submission/`；
- 跑过仿真或公开测试；
- 某些测试分片通过。

最终结论只以 formal runner 固化的独立 Evaluator 结果为准。

## 11. 常见问题

### `bun` 不是可识别的命令

使用本地 Bun：

```powershell
G:\agent-zoo\zyra\node_modules\.bin\bun.exe --version
```

如果本地 Bun 也不存在，先执行：

```powershell
Set-Location G:\agent-zoo\zyra
npx --yes bun@1.2.15 install --frozen-lockfile
```

### CLI 构建产物不存在

```powershell
Set-Location G:\agent-zoo\zyra
.\node_modules\.bin\bun.exe run build:cli
```

### Web 提示 production bundle missing

```powershell
Set-Location G:\agent-zoo\zyra
.\node_modules\.bin\bun.exe run build:web
```

### daemon/API 不可用

依次检查：

```powershell
Test-Path G:\agent-zoo\zyra\.venv\Scripts\python.exe
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js daemon status
Invoke-RestMethod http://127.0.0.1:8000/health
```

再确认 provider 文件中的 key 名正确，并检查 `8000` 端口是否被其他服务占用。

### Web 端口被占用

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js ui --web-port 0
```

### daemon 拒绝停止

先查看任务：

```powershell
node G:\agent-zoo\zyra\apps\cli\dist\zyra.js ls
```

恢复任务后使用 `/cancel`，或等待任务结束。只有确认可以丢弃活动任务时才使用
`daemon stop --force=true`。

### T1 中 Docker `Access is denied`

确认 Docker Desktop 正常运行，当前 PowerShell 用户能够执行：

```powershell
docker version
docker ps
```

不要通过操作其他 compose project、历史运行或 Evaluator 私有容器来规避权限问题。

## 12. 正式发布安装、迁移和卸载

日常开发和教师演示使用前文的源码工作区入口。需要验证“交给另一台机器后能否独立安装”时，使用正式 release 工具，不要复制当前 `.venv` 或 `node_modules`。

先在干净提交上构建两份可重复归档：

```powershell
.\.venv\Scripts\python.exe .\scripts\run_release_pipeline.py `
  --release-id zyra-替换为提交号 `
  --expected-commit 替换为完整提交号 `
  --benchmark-commit 替换为已验证benchmark提交号 `
  --output-root .tmp\release-candidate `
  --format zip
```

发布前必须对上一步生成的精确 archive 做 clean-install。它会在系统临时目录中新建 venv，安装带 hash 的 Python 依赖和 frozen Bun 依赖，构建并探测 Node/Bun CLI 与 Web，执行 transaction install、schema migration、daemon/Web start、semantic health、stop、uninstall 和端口释放：

```powershell
.\.venv\Scripts\python.exe -m zyra_productization.release.cli `
  clean-install .tmp\release-candidate\zyra-替换为提交号.zip `
  --expected-commit 替换为完整提交号 `
  --output .tmp\clean-install-receipt.json
```

只有 receipt 顶层 `ready` 为 `true`，且 `source_commit`、`archive_sha256` 与候选一致时才能交付。真实依赖首次下载可能需要二十分钟以上，当前门禁单命令上限为 30 分钟、CI clean-install gate 上限为 60 分钟。

正式安装是事务化的：receipt 中记录 transaction id。升级 state 使用 `migrate <transaction-id> --target-version <n>`，回退使用 `rollback <transaction-id> --target-version <n>`，卸载使用 `uninstall <transaction-id>`；除非明确要删除用户状态，不加 `--purge-state`。详见 `docs/product-tui/release-evidence-20260901.md`。

## 13. 交接检查清单

交给下一位维护者前，至少确认：

- [ ] 对方知道 Zyra 仓库和目标项目的绝对路径；
- [ ] Python、Node、Bun 版本满足要求；
- [ ] `.venv` 与 `node_modules` 可以重新构建，而非只依赖本机缓存；
- [ ] 至少一个 provider 已在本机安全配置并完成授权；
- [ ] `node apps\cli\dist\zyra.js --help` 正常；
- [ ] `zyra ui` 可以启动 daemon 和 Web；
- [ ] 对方知道从目标项目目录启动 CLI；
- [ ] 对方知道 `/exit` 不会取消后台任务；
- [ ] 对方知道普通任务与 T1 formal runner 的区别；
- [ ] 对方知道比赛成功必须以独立 Evaluator 为准；
- [ ] 密钥、运行私有数据和 Evaluator 私有材料没有进入 Git。

## 14. 相关文档

- 项目概览：[`README.md`](README.md)
- CLI 设计与完整命令面：[`apps/cli/README.md`](apps/cli/README.md)
- 环境变量模板：[`.env.example`](.env.example)
- T1 任务包说明：
  [`../competition-tasks/task-1-software-engineering/implementation/README.md`](../competition-tasks/task-1-software-engineering/implementation/README.md)
