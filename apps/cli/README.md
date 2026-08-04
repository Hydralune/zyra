# Zyra CLI

FE-S02 提供四个交互入口：

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
事件乱序或断流缺少恢复 cursor 时 CLI 会停止并给出 `zyra resume` 恢复提示，不会轮询或从
屏幕文本猜测任务状态。

完整 permission/command queue、终端节点和 `zyra ui` 分别属于 FE-S03、FE-S04、FE-S05。
