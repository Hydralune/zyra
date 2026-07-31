# P2-04 audit_and_fix 聚合审查

## 裁决

`PASS_AFTER_FIX`

- 数字阶段原始 base：`ab02cb0fded584a799cfbecce996b837f99ebf8c`。
- 数字阶段原始 evidence head：`6b81f660822b1f8d5c30206d38f7d4d9d998f0fc`。
- 聚合审查开始时仓库 head：`8c1a49464870a399b3cf0e3828ce43e5517bd231`。
- 修复后 target：`afab2781fdf25787ff89814504c862501856d63d`。
- P0/P1 open：0；两名独立复审员最终均给出 PASS。

本轮只执行 P2-04 审计、修复和定向复验。全量回归、cleanroom、sealed 与
preflight 按任务书保留到 P2-04/P2-05/P2-06 全部修复后的唯一最终运行，避免
重复消耗重型流程。

## Slice 聚合结论

| Slice | 结果 | 聚合判断 |
| --- | --- | --- |
| P2-S04-01 | `PASS_AFTER_FIX` | MaAS candidate set 已进入 production composition root，operator 由实际物理 adapter 执行；不同 operator 产生不同领域结果。 |
| P2-S04-02 | `PASS_AFTER_FIX` | early-exit 在 canonical artifact、permission、pending side effect 和独立 final verifier 之后裁决；disable 时会继续执行未完成层。 |
| P2-S04-03 | `PASS_AFTER_FIX` | ResourceScheduler decision、WorkerPool lease/attempt 与实际 process、endpoint、node/generation 精确绑定；所有失败边界均终态收口。 |
| P2-S04-04 | `PASS_AFTER_FIX` | `phase2-operator-execution` 在 local/edge/cloud deployment node 上执行，不再以 metadata marker 冒充 operator 结果；receipt 会重算 payload、execution、contract 和 artifact digest。 |

## 审计发现与修复

### 1. 最强组合未形成真实 production execution chain

原实现的 P2-04 单模块 contract 成立，但正常 task graph 仍可能停留在逻辑
worker 路径，物理 dispatch 主要证明 process/marker，而不是执行被选中的
operator。修复后 `Phase2StrongestProductionBridge` 成为 composition root 的
生产入口，执行顺序固定为 proposal → symbolic/custody → MaAS candidate set →
ResourceScheduler → lease/attempt → physical operator adapter → artifact/owner commit
→ independent verifier → completion gate。

### 2. AgentPrune 与 reroute 使用当前合成窗口

生产桥现在只消费早于当前 policy decision 的已完成 communication window；
二次 route 使用新的 causal event，不能把同一 route 的候选边伪装成实际历史。

### 3. adaptive depth 与 final verifier 不足

修复后 early-exit disable 会逐层重建 candidate set、resource decision、lease、
attempt、physical call 和 receipt。最终完成由独立 verifier 重验 artifact owner、
execution/contract digest、placement binding、event causality 和 memory owner commit，
不再接受 operator 自报成功。

### 4. 物理执行身份和失败终态不精确

部署进程 v6 的 process identity、endpoint、node id 和 generation id 被注册为
WorkerPool worker 的真实 backend 身份，并在调度前、节点内和 receipt validator
三次交叉校验。prepare、execute、missing receipt、gate、output binding、artifact
commit 和 MemoryFabric commit 失败均生成 canonical failed/rejected receipt，关闭
lease/attempt，并明确 retry/reconcile 边界。

### 5. digest 曾只检查“存在”

生产桥和 physical receipt validator 现在独立重算 task payload、operator execution
body、contract outputs 与 domain artifact content digest。四类 mutation 均会 fail
closed，且不会写入成功 artifact/layer receipt。

### 6. MemoryFabric success receipt 顺序错误

审查发现 memory layer 曾先 `finalize_task(success=True)`，再调用 MemoryFabric。
现在 MemoryFabric canonical owner 先提交并逐 record read-back，形成可重算的
`zyra.memory-owner-mutation-receipt/v1`；该 receipt 同时绑定 physical dispatch、
operator execution、worker completion 和 layer digest。owner commit 失败时不会产生
success worker receipt 或 layer record。

## 定向复验

- P2-04 四个 slice 的合并测试：79 passed。
- production bridge + physical receipt 最终相邻组：23 passed。
- payload/execution/contract/artifact mutation、adapter disable、preflight failure、
  process binding、multi-layer 与 MemoryFabric failure 均通过。
- TypeScript permission/API port：15 passed；runtime TypeScript typecheck 通过。
- 相关 Python compile、`git diff --check` 通过。
- Phase 2 policy contracts：`valid=true`，23 个 hard gate，五机制均为
  `deterministic_ready`，`strongest_activation_eligible=true`。
- ledger audit：`ok=true`、`errors=0`、`blockers=0`；既有 982 条 warning 不作为
  本轮实现量或通过证据。

完整命令见
`docs/reviews/evidence/phase2/P2-04/afab278/commands.json`。

## 独立复审

第一名复审员在修复后核验 7 个关键场景，结论 PASS（P0/P1/P2 均为 0）。第二名
复审员发现 MemoryFabric 提交晚于 success finalize 的 P1；修复后第三轮只读定点
复审 2 passed，并确认该 P1 已关闭、无剩余 blocker。

## 代码量边界

为避免把后续 P2-05/P2-06 已有提交计入 P2-04，本报告使用两个互不重叠范围：

1. `ab02cb0..6b81f660`：原 P2-04 数字阶段；
2. `8c1a494..afab278`：本次 P2-04 audit fix。

两段合计 97 个 changed-file observations、24,238 additions、97 deletions。其中
production 39 files / 16,228 additions，tests 21 / 5,647，config 6 / 92，
docs/evidence 31 / 2,271。测试、配置、文档和 evidence 均不冒充 Zyra 深度实现；
OpenClaw 变更为 0。

## 证据

- `docs/reviews/evidence/phase2/P2-04/afab278/aggregate-review.json`
- `docs/reviews/evidence/phase2/P2-04/afab278/gate-matrix.json`
- `docs/reviews/evidence/phase2/P2-04/afab278/bucket-summary.json`
- `docs/reviews/evidence/phase2/P2-04/afab278/commands.json`
