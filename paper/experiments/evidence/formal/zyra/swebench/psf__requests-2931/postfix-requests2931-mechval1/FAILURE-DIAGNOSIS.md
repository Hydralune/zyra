# requests-2931 机制验证校准运行失败归因

日期：2026-09-10
run label：`postfix-requests2931-mechval1`
task id：`task_a1f45f65503f`

## 结论

本运行暴露了完成门的 **fail-open 缺陷**，是本次机制验证的核心发现：

- `task_status = completed`（顶层收口），但容器 `git diff` 为空，`workspace_deltas` 全空，`strict_success = false`。
- agent 的 `final_answer` 诚实报告「Status: **not completed** — the deliverable is not landed, so I am not claiming the fix. No edit has been applied.」，且根因分析完全正确（`requests/models.py::_encode_params` 对非 ASCII bytes 做 `to_native_string` ASCII decode，抛 `UnicodeDecodeError`）。
- 但最终验证器 35 个 check 全部 `passed`，包括 `delivery_workspace_mutation_observed` 和 `delivery_expected_file_contents_match`，把「零代码改动」放行成了 `completed`。

## 根因

`goal_delivery_contract(user_goal)` 的 `workspace_mutation_required` 由 `_workspace_change_requested(goal)` 用正则匹配 goal 文本里的 mutation 动词（`fix`/`implement`/`修复`/`实现`…）推断。本运行 goal 是纯 issue 文本（`Request with binary payload fails due to calling to_native_string ...`），不含任何 imperative 动词，被误判为 `interaction_kind="answer"`、`workspace_mutation_required=False`，于是空交付 trivially 通过。

sealed benchmark 容器的语义本身就是「必须产生物理代码改动交付」，不该依赖 goal 文本是否恰好含某个动词。

## 修复

已在 `packages/orchestration/zyra_orchestration/goal_contracts.py` 给 `goal_delivery_contract` 和 `validate_goal_delivery` 增加 `require_workspace_mutation` 硬约束参数；`apps/api/zyra_api/main.py` 在 sealed 任务创建、resume 重编译、最终验证三处注入该约束。新增回归测试 `test_sealed_container_forces_mutation_for_prose_issue` 和 `test_sealed_container_rejects_empty_delivery`。

## 运行事实

| 项 | 值 |
|---|---|
| instance | psf__requests-2931 |
| difficulty | 15 min - 1 hour（首次真实烧预算触发机制的中等题）|
| provider calls | 26（全成功，0 失败，无崩溃循环）|
| total tokens | 340,456 / 600,000（56.7%，首次跨过 50% 委派阈值）|
| tool calls | 26 |
| context compactions | 0 |
| task status | completed（但 empty delivery）|
| strict success | false |
| 官方 evaluator | 无（空补丁，无法判分）|

## 机制信号观察

- `total_tokens = 340,456`，占比 56.7%，**首次真实跨越 50% 累计预算线**——`budgetDrivenDelegationSteer` 应在该阈值触发。但本运行没有产生补丁，无法通过本运行确认委派是否被模型执行。
- 无崩溃循环（对比 sphinx 4 attempt），第一步恢复续跑机制持续生效。

## 后续

本运行不重跑（已暴露，且空补丁无判分价值）。修复 fail-open 缺陷后，需用新 seed 选新的未暴露中等题，验证：(1) 空交付不再被放行；(2) 委派 steer 在真实烧预算场景是否落地。
