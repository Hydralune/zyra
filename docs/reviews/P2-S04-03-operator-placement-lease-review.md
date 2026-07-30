# P2-S04-03 Operator placement and lease review

- Slice: `P2-S04-03`
- Verdict: `PASS`
- Base commit: `cdb1c4537a22469669a3d412c0990d36eed9832d`
- Implementation commit: `d8f67a1d4854a80a5edee44f449e2fc55506ad9e`
- Evidence commit: `pending_evidence_commit`
- Mechanism version: `maas_operator_placement_lease_v1`
- Configuration digest: `ed216b21558ff32312c7d00faf28c984fa9e19316500acc07801813ebe56322b`
- Readiness report digest: `138cce611d4971ed090f17866a6b4a911dd16312d1eef788df3ac462d0445d2b`

## 结论

本 slice 已把 `OperatorSelectionProposal` 展开为 versioned `OperatorCandidateSet`，交给既有 `ResourceScheduler` 做 operator、物理 placement、执行顺序和并发上限的联合裁决；每个实际 tool/provider/worker/operator 调用之前，都由 `WorkerPoolFoundationRuntime` / `WorkerLeaseManager` 获取 canonical fenced lease，并在调用边界前重新检查 lease、attempt、task、snapshot、permission、catalog、profile、manifest 和 mechanism version。

代表性任务在无 operator 约束时选择 `worker-a`，加入 MaAS candidate set 后确定性选择 `worker-b`，证明候选集合对 placement 产生真实语义影响，而不是只写 metadata。真实主链形成：

`PolicyInputSnapshot -> MaAS proposal/readiness -> ResourceScheduler placement -> lease -> attempt -> operator call -> artifact -> verifier -> PolicyOutcome`

`OperatorOutcomeAssessment` 对该链的十项检查全部通过；两个调用均有 canonical released lease 和 succeeded attempt，`execution_without_lease=0`、`stale_or_revoked_execution=0`、`duplicate_side_effect=0`。

## Canonical owner 边界

- MaAS 只拥有候选 operator、breadth/depth 和排序 proposal。
- `ResourceScheduler` 保持 operator 与物理 placement 的最终裁决权。
- `WorkerPoolFoundationRuntime` / `WorkerLeaseManager` 保持 lease、fence 和 physical attempt 的唯一所有权。
- permission runtime 的 pending/request 状态只读投影到 lease gate。
- `RecoveryPlanner` 保持 retry/replan/reroute 的唯一 owner。
- adaptive-depth gate 只在完成层边界、lease 已释放且无 pending side effect 时允许退出。
- 未新增平行 graph、memory、permission、lease、recovery 或 artifact 状态 owner。

通用 worker lease metadata 只增加调用方传入的不可变 binding，lease owner、store、fence 和状态转换均未移动。

## Placement、lease 与幂等性

Scheduler 对 candidate 的 location、privacy、permission、health、capacity、worker/model/tool/capability、累计 token/cost/time 预算做确定性过滤，并输出 rejected reason、稳定执行顺序、并发上限和 physical worker。

每次外部调用使用稳定的 operator 幂等键，绑定 run、task、proposal digest、operator 和 layer。完整 runtime 重入会复用 canonical completion，不新增 lease，不重复副作用。lease 自身绑定 placement decision、policy input、graph revision/signature、catalog/profile、permission digest 和 mechanism versions。

调用进入外部副作用边界后若结果不确定，runtime 返回 `operator_call_outcome_unknown` 并禁止自动重试；这避免把“可能已产生副作用”误当作安全 worker failure。

## 失败与恢复

- worker 在调用前失败：`RecoveryPlanner` 生成 recovery plan，placement receipt 从 `worker-b` 更新为 `worker-a`，最终只产生一次副作用。
- lease 在调用前被撤销：canonical fence 返回 `lease_fenced`，副作用计数为 0。
- catalog/profile 在 lease 后漂移：fail closed，副作用计数为 0。
- permission 不满足：被拒 candidate 不获得 lease；显式 Phase 1 baseline 取得自己的真实 lease 并完成 artifact/verifier。
- lease execution gate 被关闭：`operator_execution_gate_disabled`，副作用计数为 0。
- 外部调用结果不确定：不自动 retry，避免重复副作用。

## Early exit 与 disable 对照

同一 4 层 task：

- early exit 开启：执行 2 层、2 次 lease/call，tokens/cost 为 `80 / 0.008`；
- early exit 关闭：执行全部 4 层、4 次 lease/call，tokens/cost 为 `160 / 0.016`；
- pending side-effect fence 存在：即使 artifact/verifier 已完成也不退出，执行完整计划；
- selector 关闭：显式 `phase1_resource_scheduler_baseline` 路径以真实 lease/call/artifact/verifier 完成任务。

因此 selector 与 early-exit 两个机制的 disable/mutation 均产生真实、可解释的行为差异。

## No-training 与来源裁剪

本 slice 仅实现确定性组合与只读 proposal 消费，没有 policy training、强化学习、policy/text gradient、随机 sampling、dataset、checkpoint、learned parameter、额外模型训练或 OpenClaw 依赖。MaAS 不取得 canonical mutation、permission、lease、budget、scheduler 或 state custody 权限。

## 实现分桶

- production：3,138 added lines
- tests：1,137 added lines
- configuration：15 added lines
- runtime-assets、generated、data、adapter-only、mock/fixture：0
- production share：0.73146853

测试、配置和证据不计为 production。

## 验证

- focused slice suites：`12 passed`
- frozen implementation combined review：`117 passed, 16 subtests passed`
- Python compileall：passed
- `git diff --check`：passed
- internalization ledger：`audit_ok=True errors=0 blockers=0`
- no-policy-training/source scan：passed
- Ruff：repository virtual environment 未安装

默认用户 TEMP/pytest cache 路径在一次运行中出现访问拒绝；最终证据命令统一使用仓库 `.tmp` 下的独立 `--basetemp` 并通过。

## Readiness 与交接

MaAS readiness 已推进到 `implementation_validated / deterministic_ready`，范围限定为 operator proposal 到 scheduler placement、canonical lease、真实调用、artifact、verifier 和 outcome 的闭环。全局 activation 仍为 `false`，留待 `P2-S06-01`。

本完成不授权自动执行下一 slice。
