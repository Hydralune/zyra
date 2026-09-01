# ADR-015：计划是独立、版本化的产品状态

状态：已接受

日期：2026-09-01

## 背景

旧 TUI 将 `plan_nodes` 降格成通用 activity，只能显示完成/进行中/待执行数量。用户无法知道当前 graph revision、真实步骤顺序、依赖/执行者、需求变化和恢复节点，也无法区分“步骤完成”与“步骤被替代”。

## 决策

- 从 canonical task 的 `dynamic_graph_ref.revision`、`stage_order`、`plan_nodes`、`requirement_changes` 和 `failure_injections` 构造 `zyra.ui-plan/v1`。
- `plan.updated` 独立于通用 activity，包含 revision、revision source、graph identity、最多 2,000 个步骤和最多 512 条有界变更历史。
- 步骤状态保留 pending/running/completed/failed/cancelled/superseded，不把失败或替代伪装成完成；同时保留依赖与 assigned agent identity。
- canonical graph revision 缺失的旧快照明确标为 `compatibility`，使用 v1 只为兼容显示，不能伪称 canonical revision。
- 主视图显示版本、进度、异常步骤和最近变更；`/plan` pager 显示完整的 retained change/step 事实。

## 结果

在线刷新、离线 replay、resume 和 Web task snapshot 仍共享 task owner。TUI 不创建本地计划，也不从 activity 文本猜测重规划。
