# astropy-7606 机制验证校准运行诊断

日期：2026-09-10
run label：`postfix-astropy7606-mechval2`
task id：`task_eaa3e77996a4`

## 结论

本运行验证了两个机制修复，并暴露了第三个（也是委派链路最后一环的）机制缺陷：

1. **完成门 fail-open 修复 ✅ 生效**：`task_status = failed`（不再是 completed）。agent 改了代码但未完成验证闭环，被正确判 failed，而非上次 requests-2931 的空交付被误判 completed。

2. **委派 steer 强化 ✅ 生效**：命令性 steer（commit `d3acafe9`）让 deepseek-v4-flash 在 turn 21 **真的调用了 Agent 工具**（对比 requests-2931 的 0 次）。这是从「模型不遵循」到「模型执行委派」的实质行为变化。

3. **委派执行 ❌ 被权限层拒绝（新发现）**：Agent 工具调用两次都被 `permission_denied` 拒绝——`"ASK cannot be satisfied in the active permission mode and was deterministically denied"`。根因是 sealed benchmark 的权限策略只 allow `shell` 工具，`Agent` 在 HIGH_RISK_TOOLS 里无 allow 规则 → 落 ASK → headless 模式 ASK→DENY。

## 运行事实

| 项 | 值 |
|---|---|
| instance | astropy__astropy-7606 |
| difficulty | 15 min - 1 hour |
| provider calls | 33（全成功，无崩溃循环）|
| total tokens | 371,659 / 600,000（61.9%）|
| tool calls | 25（含 2 次 Agent，均被 permission_denied）|
| 压缩 | 6 次 `context_compacted`（turn 5/10/16/18/19/20）+ 恢复会话 2 次 |
| 委派 steer | turn 20 `consumed=311644`（51.9%）触发 + 恢复会话 turn 0 `consumed=381217` |
| task status | failed |
| strict success | false |
| 补丁 | astropy/units/core.py（+5/-1）+ test_units.py（+18）|

## 官方 SWE-bench 判分

- `patch_successfully_applied=true`，`resolved=false`
- **FAIL_TO_PASS `test_unknown_unit3` 通过**（目标 bug 已修对）
- **PASS_TO_PASS `test_compose_roundtrip[]` 失败**（修复引入 1 个回归）
- 结论：补丁部分正确但引入回归，`resolved=false` 判定正确

## 机制信号（runtime 事件为准）

| 机制 | 状态 |
|---|---|
| 累计预算压缩 | ✅ 6 次（turn 5/10/16/18/19/20）+ 恢复 2 次 |
| 委派 steer | ✅ turn 20 触发（51.9%）|
| 模型执行委派 | ✅ 调 Agent 工具 2 次（原始 round27 + 恢复 round1）|
| Agent 工具执行 | ❌ 2 次均 `permission_denied` |

## 修复

`_benchmark_permission_policy` 补一条 agent 委派工具 allow 规则（`namespace=agent`、`tool=*`、`operation=execute`，session+workspace scoped），让 budget-pressure 委派在 sealed benchmark 下可执行。子 agent 走同一 fenced benchmark workspace + 共享 provider control-plane 路由，子工具调用仍各自权限门控。

## 后续

本运行不重跑。下一题验证：委派执行（Agent 工具不再被权限拒绝）是否真正落地。
