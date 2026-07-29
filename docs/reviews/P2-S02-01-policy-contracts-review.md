# P2-S02-01 策略输入输出契约与安全执行边界增量自审

- Slice：`P2-S02-01`
- 基线 commit：`1df3c5e19ca00eb9858cbf3cb6b0f411f6424d86`
- 实现 commit：`9eb984657f842144c673a1a8bd1acb8428a102a0`
- post-review fix commit：`8d6151e7bf66b8423e4767ca25098a5f93ac94b1`
- 自审日期：`2026-07-29`
- 结论：`PASS`

## 1. 交付结论

本 slice 已建立第二阶段策略机制共享的九类版本化 contract、不可变 owner snapshot、symbolic constraint projector、唯一 canonical delta adapter、既有 artifact/event 证据桥与 replay 校验。策略提案不能直接修改共享图；唯一生产路径为：

```text
TopologyProposalArtifact
  -> TopologyConstraintProjector
  -> PolicyDeltaBuilder
  -> GraphDeltaBuilder
  -> GraphStateCustody.commit
```

`GraphStateCustody`、`DynamicTopologyRuntime`、`ResourceScheduler`、permission、lease、MemoryFabric、artifact store 和 event spine 的既有 owner 均未被替代。policy 包没有数据库、第二状态仓或共享 graph 原地写入。

本 slice 没有启用 ARG、CARD、AgentPrune 或 MaAS。只有同时达到 `activation_ready + deterministic_ready` 的 readiness ref 才能进入 canonical mutation；现有 Phase 2 frozen gate 仍保持关闭。

## 2. Contract 与兼容策略

已实现并导出：

1. `MechanismEvidenceReadinessReportRef`
2. `PolicyInputSnapshot`
3. `EnvironmentSnapshot`
4. `TopologyProposalArtifact`
5. `PolicyDecisionReceipt`
6. `PolicyOutcome`
7. `MemoryContinuityReceipt`
8. `NeuroSymbolicEvidenceBundle`
9. `PhysicalDispatchReceipt`

共同保证：

- 显式 `schema_version`、`contract_kind`、contract ID、创建时间、source event、correlation、causation、mechanism/input version、configuration digest 和 idempotency key。
- 深层不可变 JSON value、确定性 key/set 排序、UTF-8 compact canonical JSON、禁止 NaN、SHA-256 digest。
- 当前 v1 schema 必须具备完整 header；声明的 v0 可读取并归一为 v1。
- 未知 future schema fail closed；已知 schema 的 additive unknown field 可读取，保证同版本扩展兼容。
- 每类 contract 均生成 Draft 2020-12 JSON Schema；`GET /schema/policy-contracts` 通过真实 API handler 返回九类 catalog 和 schema。
- replay 同时验证 artifact revision、artifact byte digest、contract digest 与 schema；篡改字节确定性失败。

## 3. Snapshot 与 owner 输入

### 3.1 PolicyInputSnapshot

`PolicyInputSnapshotBuilder` 从既有 `TaskState`、`GraphStateSnapshot`、readiness ref、registry version、memory artifact ref 和 policy budget 构造 detached value object。它复制：

- task/run/requirement revision；
- canonical graph revision、signature、commit 和完整 node role/capability/dependency/state/revision；
- task 与未完成 plan node 的 requirement/success/completion obligation；
- registry version、permission、placement、privacy、budget、last topology change；
- environment、memory 和 readiness stable refs。

构造后修改源 task/projection 不会改变 snapshot。projector 会再次逐 node 对照 canonical graph，伪造或遗漏 node snapshot 与 stale signature 一并拒绝。

### 3.2 EnvironmentSnapshot

`EnvironmentSnapshotBuilder` 适配真实 `WorkerPoolFoundationRuntime.api_projection()` 形状，保留：

- worker、backend、location、physical process identity；
- health、telemetry observation time、fresh-until、confidence 和 observation source；
- capacity/allocated 差值、load、active lease、lease availability；
- manifest/telemetry digest、capability、privacy class、allowed placement；
- telemetry、health、physical identity 等缺失字段。

缺失 required category、stale/untrusted observation、无物理身份、unhealthy/unavailable、lease unavailable、capacity 不足、privacy 或 placement 不允许均 fail closed。

## 4. Constraint projector 与 canonical commit

projector 在构造 delta 前检查：

- proposal/input/current graph identity、完整 node snapshot 与 TTL；
- mechanism readiness stage/status；
- role/capability registry；
- 每个 operation 的 permission 声明与 allowlist；
- placement/resource 配对、privacy、fresh telemetry、capacity 和 lease；
- token/cost/time/communication budget；
- topology churn、minimum dwell 和 projected fan-out；
- canonical graph dependency、entity revision、edge endpoint和 DAG/cycle；
- duplicate idempotency key 与不同 content。

拒绝结果包含逐项 `ConstraintResult`、reason code、details、fallback profile 和 fallback reason。通过时，只有 `PolicyDeltaBuilder` 可以导入 `GraphDeltaBuilder`。delta ID、mutation ID、contract timestamps 和 content digest 都由 proposal 确定性派生；不同 retry receipt ID 不改变 canonical delta。

真实 GraphStateCustody 测试覆盖 `accept`、`reject`、`replay`、`rebase` 和 `conflict`。相同 proposal 重放不增加 revision；同 key 不同 content 拒绝；并发 disjoint head advancement 产生 rebase；同 entity race 产生 conflict 与显式 fallback。

## 5. 证据、事件和 replay

`PolicyEvidencePublisher` 通过既有 `LocalArtifactStore` 写入 `STRUCTURED_DATA`，并把稳定 artifact ref、contract digest、schema、source/correlation/causation 和 idempotency 写入既有 `EventRecord`。事件 ID 由 contract 语义确定性派生。它不维护 policy artifact 表、event 表或 replay cursor。

真实 artifact 测试证明：

- contract bytes 写入既有 artifact owner；
- event 进入调用方提供的既有 event admission；
- replay 得到相同 canonical contract/digest；
- 篡改 artifact bytes 后 integrity check 拒绝；
- topology policy package 不导入 `sqlite3` 或创建表。

## 6. post-review 修复

实现 commit 后的独立高风险复审发现并修复：

1. worker-pool 的 `process_identity` 为 scalar、health 为对象、capacity/allocated 为 vector；snapshot builder 已按真实 projection 修正。
2. 随机 mutation ID、默认 node timestamp 和 retry-specific decision ID 会破坏并发幂等；现均由 proposal 确定性派生。
3. 只检查 graph signature 仍可能接受伪造 node snapshot；现逐 node 对照 canonical owner。
4. `deterministic_ready` 必须同时处于 `activation_ready`，不能用 `input_precheck` 越门。
5. topology operation 不能省略 permission，placement 不能省略对应 resource。
6. staged-but-uncommitted idempotent delta 可由 canonical custody 恢复提交；commit/idempotency race 返回 conflict receipt。
7. contract kind/schema mismatch、非十六进制 digest 和 created-after-expiry 现在 fail closed。
8. task-level success criteria 已进入 unresolved obligation snapshot。

## 7. 状态 owner 与直接绕过自审

- `GraphStateCustody` 仍持有 snapshot、delta、commit、conflict、journal 和 idempotency 唯一事实。
- `PolicyDeltaBuilder` 是 policy package 唯一导入 canonical `GraphDeltaBuilder` 的模块；静态测试扫描其余 production module。
- policy 包没有第二 SQLite/file state store。
- environment builder 只读取 scheduler projection；不创建 lease、不执行 placement。
- contract/evidence bridge 只调用既有 artifact/event port。
- MemoryContinuityReceipt 仅携带 verifier 证据，不取得 MemoryFabric owner。
- PhysicalDispatchReceipt 仅表达真实 dispatch 证据，`simulated` 明示，不能替代 scheduler/worker receipt。
- 未引入训练、dataset、checkpoint、OpenClaw 或 `../long-horizon-systems` 运行依赖。

## 8. 增量有效代码审计

从基线到 post-review fix 共新增 `3947` 行：

| 分类 | 新增 | 删除 | 说明 |
|---|---:|---:|---|
| production/config | 2843 | 0 | contract/schema、snapshot、projector、delta adapter、evidence、GraphStateStore lookup、API catalog |
| test | 1104 | 0 | contract、owner snapshot、projector/custody、replay、tamper、API、disable/mutation/race |
| runtime-assets | 0 | 0 | 无 vendor/runtime asset |
| generated | 0 | 0 | 无生成物计入实现 |
| data | 0 | 0 | 无训练数据或 checkpoint |
| docs | 0 | 0 | 本自审与证据在独立 docs commit |
| adapter-only/mock/fixture | 0 | 0 | adapter 计入 production，但没有以 mock/fixture 冒充主路径 |

实现中 `GraphStateStore` 仅增加 existing canonical store 的 idempotency lookup；没有新增 schema/table。测试使用真实 SQLite custody、真实 LocalArtifactStore 和真实 HTTP handler。

## 9. 验证结果

- slice contract/projector/replay + GraphStateCustody regression：`20 passed`。
- 相邻 topology route/placement projection：`1 passed`。
- Python `compileall`：通过。
- `git diff --check`：通过。
- Phase 2 policy：`valid=true`，目标为 post-review fix commit `8d6151e7bf66b8423e4767ca25098a5f93ac94b1`。
- internalization ledger：`audit_ok=true`，`errors=0`，`blockers=0`，`warnings=734`。
- Ruff 未安装于项目 venv，未作为成功检查报告；本 slice 以 compileall、行为测试和 diff check 完成语法/行为/格式验证。

## 10. 最终裁决

九类 contract、完整 JSON Schema、immutable snapshot、owner attribution、constraint projector、canonical commit、idempotency/replay、accept/reject/project/rebase/conflict 表达、fallback、artifact/event/API 兼容和直接绕过防护均有 production code 与行为测试。未发现当前 slice blocker，裁决为 `PASS`。

根目录 `G:\agent-zoo\docs\phase2\slice-02-01-policy-contracts.md` 及其他 Zyra 仓库外文档未修改，不会随 Zyra commit 自动提交。
