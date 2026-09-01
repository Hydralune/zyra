# ADR-016：选择 agent 只切换检查上下文，不创建本地会话真相

状态：已接受

日期：2026-09-01

## 背景

Codex 的 subagent 是可切换 thread，因此 `/subagents` picker 能重建另一个 thread transcript。Zyra 的协作单元由 topology/runtime owner 管理，是 task 内 worker/attempt，不是独立用户会话。旧 `/agents` 只有静态文本页，在代理较多时无法定位具体 worker，也看不到其计划职责和恢复信息。

## 决策

- `/agents` 与 `/subagents` 打开可搜索 picker；`/agents <agent-id>` 支持自动化与直接定位。
- 选中后显示稳定 agent identity、状态、摘要、failure impact/code/retryability/recovery，以及 `zyra.ui-plan/v1` 中分配给它的步骤。
- 选择只改变本地检查 overlay，不改变 canonical task、路由或控制目标；Zyra 没有 user-addressable worker thread API 时不得伪造“已切换会话”。
- 主 transcript 只聚合 active/failed 和总数，最多展开 5 个 agent；完整集合进入 picker，当前状态上限 512。
- 100-agent 产品门覆盖 reducer、主视图聚合、失败恢复信息和按 identity 选择的上下文。

## 结果

这一设计对齐 Codex 的“可发现、可定位、可检查”产品目标，同时保留 Zyra 的 canonical topology owner。未来若后端提供版本化 worker-thread transcript，可在同一 picker 后增加只读 replay，而不改变当前 identity 绑定。
