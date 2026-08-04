# Zyra CLI

FE-S03 在 FE-S02 的四个交互入口上增加 runtime-owned 控制、权限与恢复闭环：

```text
zyra
zyra "目标"
zyra resume <task-or-session-id>
zyra ls
```

`zyra "目标"` 创建服务端 task 后消费 event-ingress snapshot 与 SSE。`resume` 通过
task-backed session resolver 读取同一 task 的服务端 snapshot/cursor，不重建或重复提交
task。`ls` 展示服务端 task/session projection；`generation:sequence` 是 CLI 显示的真实观察
revision。

交互 transcript 使用普通终端 scrollback。事实事件逐行追加，只有 TTY 的一条底部状态行
会就地更新；没有 alternate screen、Ink、React 或全屏布局。重定向到 pipe/tee 时保留纯行式
内容，不输出状态行控制序列。

本地输入行为：

- 行尾 `\` 继续多行输入；`/edit` 使用 `VISUAL`/`EDITOR`；
- `/cancel-draft` 安全暂存未提交草稿，`/restore` 恢复；
- 长 paste 在 draft 内折叠为 digest ref，只在提交时展开；
- `/help` 与 `/exit` 只控制本地 REPL，不是 runtime command。

task、session、tool、artifact、permission、终态和 cursor 都来自服务端。本地 draft、history、
search、follow、unread 和布局不是 canonical state。SSE 不可用、cursor 无效、schema 不兼容、
事件乱序或断流时，CLI 使用最后确认的服务端 cursor 做有限重连；cursor/generation/gap
失效时重新读取 canonical snapshot。恢复记录会明确标注 `cursor resume` 或
`snapshot replacement`，不会轮询或从屏幕文本猜测任务状态。

运行中的 TTY 会同时开放这些控制输入：

- `/queue` 只读取服务端 command queue；`/now`、`/next`、`/later` 映射真实 priority；
- `/interrupt`、`/redirect`、`/continue`、`/cancel`、`/cancel-command` 和 `/retry` 都调用
  canonical task/command endpoint；
- `/approve <request_id>` 与 `/deny <request_id>` 使用 permission response challenge 的完整
  identity、revision、deadline 与 proof，提交后只认 runtime receipt；
- 普通文本在活动 task 中按 `/change` 投递，不建立本地已发送命令队列；409 revision conflict
  会显示 canonical revision，绝不自动覆盖或自动重试 mutation。

permission custody token 仅存于当前进程内，不写 transcript、日志或 CLI state。进程重启后如
需重新取得已有 permission session 的 custody，必须由调用方显式提供
`ZYRA_PERMISSION_CUSTODY_TOKEN`；缺失或失效时权限动作 fail closed，task/event 观察仍可继续。
sealed autonomous 模式不会等待 CLI 人工输入，高风险或未知动作仍由 runtime 确定性拒绝。

`zyra daemon stop` 在查询到活动任务时默认拒绝，并向 CLI state 目录追加不含秘密的
`daemon-stop-audit.jsonl` 记录；只有显式 `--force=true` 才会强制停止并返回 committed audit。

终端节点和 `zyra ui` 分别属于 FE-S04、FE-S05，FE-S03 不实现 terminal transport 或新的
permission policy。
