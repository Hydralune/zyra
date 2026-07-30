# P2-S04-02 Verifier-gated early exit review

- Slice: `P2-S04-02`
- Verdict: `PASS`
- Base commit: `b575d40f877a5b3511c3fd2ac7cb0a78c7f12d7e`
- Implementation commit: `ec501cbd107724069628a259049d5488879cf8d2`
- Evidence commit: `RECORDED_BY_FOLLOWUP_COMMIT`
- Mechanism version: `maas_verifier_early_exit_v1`
- Gate configuration digest: `fe8cb61afa5f3c0c7c2ebcc31f590ff1df929df532b2e77f09f190aeeef59739`
- Readiness report digest: `90dfe7d6344e46634c75fcffd4befb27cf93fba327df63fa2720cbee83e37b23`

## 结论

本 slice 已实现 versioned `ExitEligibilitySnapshot`、确定性 hard-condition gate、adaptive-depth 层级执行控制、完整 `PolicyOutcome`/cost/causal receipt，以及 checkpoint 绑定与 restore 重判。机制只在所有硬条件同时成立时退出；任意缺失、过期、类型混淆、digest 异常或 owner 不确定均继续执行。

全局默认路径没有在本 slice 切换。`P2-S04-03` 仍负责后续 placement/lease/dispatch 因果链与默认组合接入。

## Canonical owner 边界

快照是现有 owner 的只读投影，不持久化平行状态：

- obligation 与执行记录：`TaskState`
- artifact bytes/digest：`LocalArtifactStore`
- permission pending：`PermissionStateStore` / `PermissionRequestQueue`
- side-effect fence：`RecoveryPlanStore`
- checkpoint：`CheckpointCommitRuntime` / `RecoveryPlanStore`
- continuity：`MemoryContinuityReceipt`
- final verifier：digest-bound `TaskState` decision receipt

`RecoveryPlanStore.side_effect_fences(run_id, task_id)` 是本 slice 对现有 owner 的有界接口增量。它使 gate 查询整个 task scope，而不只信任旧 checkpoint 列出的 fence keys；因此 checkpoint 漏列的新 pending side effect 仍会阻止退出。

Adaptive depth 不取得 placement、lease、tool、provider 或 physical attempt 所有权。它只消费 selector proposal 作为建议上限/下一层候选，并调用外部执行 owner；`ResourceScheduler` 和 `WorkerPoolFoundationRuntime` 的职责不变。

## Exit truth table

Gate 固定检查：

1. early exit enabled；
2. eligibility snapshot fresh；
3. canonical owner refs 完整；
4. final verifier 通过、digest 有效且未超过冻结 TTL；
5. critical obligations 全部在 scope 且 unresolved 为 0；
6. required artifacts 全部存在且 bytes/digest 可验证；
7. permission pending 为 0；
8. side-effect pending/unknown 为 0；
9. minimum operator 与 verification path 已执行；
10. checkpoint requirement revision 为 current；
11. memory continuity 通过；
12. confidence 与 avoided-work 阈值通过。

只有十二项全部为真才返回 `exit`。逐条件参数化测试、missing/unknown field、stale snapshot、stale/超长 verifier TTL、type confusion、obligation removal、verifier mutation 和 hidden pending side effect 均 fail closed。

## True-exit 与 disabled 对照

代表性 proposal 为 4 层、4 个 operator。启用 gate 后：

- 实际执行 2 层、2 个 operator；
- 真实少执行 2 层、2 个 operator；
- actual tokens/cost/latency 为 `80 / 0.008 / 20ms`；
- avoided tokens/cost 为 `200 / 0.02`；
- task、artifact、final verifier、permission、side effect、checkpoint、continuity 全部完成；
- posterior 为 `true_exit`；
- outcome、cost receipt、execution receipts、verifier/artifact refs 与 causal refs 保持完整。

禁用 gate 后，同一语义任务完整执行 4 层、4 个 operator，tokens/cost/latency 为 `160 / 0.016 / 40ms`，任务和 artifact 仍完成。由此形成可解释的 breadth/depth/cost 断开证据。

## Checkpoint / restore

Checkpoint metadata 保存 eligibility/decision binding、完整 decision receipt、配置 digest、policy input/proposal/revision 引用，并固定 restore policy 为 `revalidate_fresh_owner_state_never_reuse_verdict`。

进程重启后从同一 `RecoveryPlanStore` 恢复 checkpoint，`prior_verdict_reused=false`，gate 使用 fresh owner snapshot 重新计算。需求从 `requirement-r1` 变为 `requirement-r2` 后，旧 binding 同时产生 requirement、policy input、proposal 与 eligibility invalidation，final verifier、obligation、checkpoint revision 与 continuity 条件失败，结果确定性转为 `continue`。

## No-training 与来源裁剪

MaAS `MultiLayerController` 的 multilayer/adaptive-depth 概念仅作为 `supplementary_implementation` 来源。Zyra 侧采用固定 hard gate 和可审计 score/receipt，不迁移 MaAS execution runtime、训练器、policy gradient、textual gradient、随机 sampling、dataset、checkpoint、learned parameter、provider framework 或第二套 Agent host。

## 实现分桶

- production：2,885 added lines
- tests：1,154 added lines
- configuration：24 added lines
- runtime-assets、generated、data、adapter-only、mock/fixture：0
- production share：0.71006645

测试、配置和证据不计为 production。

## 验证

- focused slice suites：`27 passed`
- selector/catalog/checkpoint/continuity adjacent regression：合并最终命令 `46 passed`
- Python compileall：passed
- `git diff --check`：passed
- no-policy-training/source scan：passed
- Ruff：repository virtual environment 未安装

## 交接

向 `P2-S04-03` 交接：

- `ExitEligibilitySnapshot`
- `DeterministicEarlyExitGate`
- continue/exit/posterior receipts
- `minimum_operator_path`
- `AdaptiveDepthRuntime`
- checkpoint binding 与 restore invalidation contract

本完成不授权自动执行 `P2-S04-03`。
