# Zyra CLI

Zyra CLI 是长程任务的默认工作入口；Web 是同一后端事实的观察、证据和复杂治理入口。CLI 不拥有 task、session、event、permission、terminal、artifact 或 scenario 的 canonical state，也不包含第二套 Agent runtime。

## 命令面

```text
zyra                              product TUI
zyra "<goal>"                     product TUI with an initial goal
zyra resume <task|session>        resume in the product TUI
zyra dev [<goal>]                 developer event interface
zyra events <task|session>        observe raw canonical events
zyra run <goal | -f file | stdin> non-interactive JSONL execution
zyra ls                           list canonical tasks and sessions
zyra scenario <action> [...]      scenario lifecycle over the daemon API
zyra ui [--task <id>]             ensure daemon, start Web, open product route
zyra daemon <start|stop|status>   local daemon supervision
```

没有隐藏的产品命令。CLI 通过 `@zyra/typed-api-client` 和 `@zyra/commands` 接入现有 Zyra API；不包装 Python 运维脚本，也不依赖 Web、React、DOM、Ink 或 Codex 内部协议。

## 非交互执行

```powershell
zyra run "inspect this repository"
zyra run --file .\goal.md
Get-Content .\goal.md | zyra run
zyra run "inspect this repository" | Tee-Object .\run.jsonl
```

goal 参数、`--file` 和 piped stdin 三者互斥。stdin 只在非 TTY 时读取，最大 256 KiB；空输入、超限、文件读取错误或同时提供多个来源均以 usage error 结束。任务创建成功后，CLI 消费服务端 snapshot/SSE/cursor，final verifier 决定成功或 verifier failure。

non-TTY stdout 的每一行都是独立合法 JSON object；诊断、help 和 warning 只写 stderr。writer 尊重 backpressure，消费者提前关闭时安静处理 EPIPE，不据此取消服务端任务。固定退出码为：

| code | 含义 |
|---:|---|
| 0 | 命令达到定义的成功终态 |
| 1 | task/scenario 执行失败或 runtime contract 不可接受 |
| 2 | 参数、输入或文件 usage 错误 |
| 3 | daemon/API 不可达或未通过 readiness |
| 4 | 显式取消、signal 中断或 non-interactive permission wait |
| 5 | final verifier/completion/evidence gate 未通过 |

## 交互、恢复与控制

默认界面是面向用户的产品 TUI：它把 canonical task 和版本化 `ZyraUiEvent/v1` 投影成用户消息、助手回答、少量活动、工具摘要、权限、文件变更、验证和最终结果，不直接渲染 `runtime.*`。界面使用普通终端 scrollback，不使用 alternate screen；TTY 原位重绘被限制在可见高度，非 TTY 只在结束时输出最终产品视图。

- `Ctrl+J` 插入换行，Enter 提交；运行中 Enter 立即重定向，Tab 排队，Esc 中断当前步骤。
- `Ctrl+R` 恢复草稿/搜索历史，`Ctrl+E` 使用 `VISUAL`/`EDITOR`，PageUp/PageDown 滚动有界 transcript。
- `/queue`、`/cancel`、`/continue`、`/redirect <说明>`、`/interrupt <说明>`、`/cancel-command <id>`、`/retry <id>` 全部调用 canonical command API。
- `/permissions` 打开 canonical pending-request picker；TUI 只展示后端对该请求正式声明的“允许本次 / 本会话允许 / 此工作区始终允许 / 拒绝”。决定绑定 request、revision、deadline、effect 和 scope 的 v2 proof。
- 会话/工作区允许不是宽泛工具白名单：只复用相同 workspace、tool、operation 和完全相同 canonical arguments；会话范围还绑定同一 session。工作区规则由 TypeScript permission owner 持久化并在回执中确认。
- `/ui` 打开当前 canonical task 的 Web 路由；完整 topology、artifact、audit、evidence 和大型 diff 由 Web 展示。
- SSE 断线从最后确认 cursor 有界重连；gap、generation 或 cursor 失效时读取 canonical snapshot，不从 transcript 猜测状态。
- `/exit` 只退出当前 listener。daemon 和 task 继续运行；再次执行 `resume` 恢复同一 task-backed session。

旧的原始事件终端保留在 `zyra dev [<goal>]`。只读附着现有任务使用 `zyra events <task|session>`；这两个入口会显示 sequence、event type、artifact 和 revision，普通用户路径不会显示这些内容。

permission custody token 仅存在于当前进程内。需要在重启后恢复裁决 custody 时，调用方必须显式提供 `ZYRA_PERMISSION_CUSTODY_TOKEN`；缺失或失效时 fail closed。sealed autonomous 模式从不等待人工批准。

## Daemon、terminal node 与 Web

`zyra daemon start|status|stop` 使用 pid、generation、health/readiness 和 revision fence 管理本地 daemon。存在活动任务时，普通 stop 拒绝；只有显式 `--force=true` 才执行强制停止并写无秘密 audit。

`run`、`scenario`、产品 TUI、`resume` 和 `dev` 在需要时注册 loopback terminal node。listener 使用每代高熵 capability path、Host/loopback/root/attestation/digest/sequence 校验，结束时 drain、revision-fenced disable 并关闭；它不是 edge，也不会成为 sealed run 的执行节点。

`zyra ui` 确保 daemon 可用，构建并启动既有 Web 产品入口，然后生成绑定同一 API/task 的 URL。launcher 只拥有本地 Web 进程状态，不复制后端事实。

## 安全输出

所有 JSONL、诊断和公开 projection 都执行防御性脱敏：capability path、URL userinfo、credential/token 字段、内部 Windows/Unix root/cwd 和 ANSI 控制序列不能进入公开输出。真实 endpoint、cwd 和 token 只保留在相应安全 owner 内。

## 构建与发布验证

开发入口由 Bun 直接执行：

```powershell
bun apps/cli/src/index.ts --help
```

Node 发布产物使用固定目标构建：

```powershell
bun run build:cli
node apps/cli/dist/zyra.js --help
bun run product-tui:smoke
python scripts/verify_product_entry_release.py
```

产品验证覆盖 Bun/Node 入口、十项命令面、JSONL、0..5 退出码、projection 重放、80/120 列 golden、输入/resize/恢复/权限/失败和真实 daemon smoke。Windows 是当前主验证环境；Linux/macOS 只有在相应 host 可获得时才记为 passed，否则必须显式记为 unavailable。

本阶段不发布 npm、不制作安装器、不做代码签名。正式 bundle 和 cleanroom 必须离线运行且不依赖 `G:/agent-zoo/claude-code-best` 或其它工作区外源码。
