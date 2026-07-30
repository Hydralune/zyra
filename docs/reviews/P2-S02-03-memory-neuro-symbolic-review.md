# P2-S02-03 Memory Continuity 与 Neuro-Symbolic Evidence 增量自审

- Slice：`P2-S02-03`
- Slice 基线 commit：`881825a9efa5cdc0f7cb07e7547b6c4bcdf00037`
- 实现 commit：`31fafb83a96da951c5ae5a516aa0777fea56a975`
- 实现 tree：`5fb57a10d4229d36080168c05913790da4380cd2`
- 证据 commit：`SELF`
- 自审日期：`2026-07-30`
- 结论：`PASS`

## 1. 交付结论

本 slice 已在既有 Zyra owner 边界内完成两个基础能力：

1. `MemoryContinuityVerifier` 从 `MemoryFabric` 只读取得 canonical task records，
   对 compact/restore、process restart、role/worker handoff、node replacement、
   checkpoint restart 和 requirement revision 建立 before/after、provenance、
   obligation、状态分类和下游使用 receipt，并在构建 policy input snapshot 前
   fail closed。
2. `NeuroSymbolicEvidenceBuilder` 从真实 `ConstraintAwareProjector` 结果形成
   proposal、可选固定模型信号、符号约束、接受/拒绝理由、operation diff、
   canonical delta、custody commit/no-commit、验证与 outcome 的完整证据链。

实现没有新增 memory store、graph owner、evidence ledger、scheduler owner 或第二套
runtime。没有引入策略训练、强化学习、微调、训练数据、训练 checkpoint 或额外神经
网络。OpenClaw 和工作区外源码均未进入运行依赖。

`phase2_strongest_v1` 没有被请求或激活。当前 LoopX、ARG、CARD、AgentPrune 和
MaAS 的正式 readiness 仍为 `unavailable`，contract verifier 因而继续给出
`strongest_activation_eligible=false`。本 slice 的完成不代表上述后续机制已就绪。

## 2. Canonical owner 与写入边界

| 状态域 | Canonical owner | 本 slice 行为 | 结果 |
|---|---|---|---|
| 长期记忆事实 | `MemoryFabric` | 新增只读 canonical task-record port | 无第二存储、无事实复制 |
| 图状态 | `GraphStateCustody` | 只接受 projector 形成的 delta | owner 未变 |
| topology delta | `GraphDeltaBuilder` | policy adapter 只允许 projector 私有授权调用 | 直接绕过拒绝 |
| 物理 placement | `ResourceScheduler` | 未修改 | owner 未变 |
| evidence | `EventLog` + `ArtifactStore` | 复用 `PolicyEvidencePublisher` | 无第二 ledger/store |

policy input builder 现在要求一张通过的 continuity receipt，且 task、run、
requirement revision、causation 和 accepted memory refs 必须与 receipt 精确一致。
因此不再允许绕过连续性检查后手工构造一份“看似合法”的 policy snapshot。

`PolicyDeltaBuilder` 新增 projector 私有授权；直接调用会抛出
`PermissionError`。这不改变 `GraphDeltaBuilder` 的 canonical contract，只关闭了
policy 路径绕开 symbolic projector 的适配器入口。

## 3. Memory continuity

### 3.1 读取与 receipt

`MemoryContinuityVerifier` 使用 `MemoryFabric.canonical_task_records(task_id)`
读取同一 canonical store 中的 task facts，不复制数据。receipt 保留：

- task/run/requirement revision 与 transition kind；
- before/after snapshot 和 content digest；
- critical fact、obligation、accepted memory ref；
- source event、artifact、causation；
- status、reason code、confidence 与版本信息。

每条内容重新计算 digest。record 声明 digest 与实际内容不一致时标记
`poisoned`；重复 key 但语义冲突时标记 `conflicting`；需求变更后的旧事实分为
`stale` 或 `superseded`。critical fact/obligation 缺失、poison、冲突或过期需求
继续执行均不能通过 gate。

### 3.2 已执行的真实转换

测试覆盖：

- SQLite-backed `MemoryFabric` compact/restore；
- 创建新的 `MemoryFabric` 实例模拟 process restart；
- 两个真实 deployment node process（device、edge）间的 dispatch 和
  `CheckpointHandoffRuntime` handoff；
- handoff ack 对 source/target worker、当前 requirement revision、
  obligation digest 和 critical fact IDs 的确认；
- 真实 `GraphStateCustody` node replacement；
- checkpoint restart；
- requirement revision；
- stale、superseded、conflicting、poisoned 注入；
- first policy decision、first tool use、artifact publish 和 event replay 的因果链。

handoff 后重复已经完成的 artifact 会被拒绝；需求变更后继续执行旧 requirement
也会被拒绝并进入 recover/replan。

### 3.3 指标与硬门

新增 continuity evaluation report：

- critical fact recall；
- critical fact provenance；
- obligation retention；
- stale execution count；
- duplicate work count；
- downstream usage coverage。

critical/obligation 丢失、stale execution、duplicate work 或缺失下游使用证据都会
使 hard gate 失败。

## 4. Neuro-symbolic evidence

### 4.1 信号与裁决权

默认路径是 `deterministic_only`。若以后接入预训练模型，只接受固定的只读
observation contract：

- model ID/version；
- input/output digest；
- output ref；
- confidence。

无法提供上述 receipt 的“neural”标签被拒绝。模型 observation 没有 canonical
mutation 权，最终裁决仍由 symbolic constraints、permission、lease、budget、
scheduler 和 graph custody 保持。

### 4.2 完整证据链

bundle 由真实 projector result 构建，包含：

1. input snapshot 与 proposal；
2. 可选固定模型 observation；
3. 每项 symbolic constraint 的 verdict/reason；
4. proposal operations 与 projected operations 的确定性 diff；
5. accepted canonical delta 或 rejected no-delta；
6. custody commit/no-commit；
7. permission、lease、artifact、verification 和 outcome refs。

接受路径测试真实提交到 `GraphStateCustody`，发布 artifact/event 后再 replay。
拒绝路径验证 canonical graph revision 和 snapshot 均不变化。

### 4.3 对抗语料

固定对抗语料包含 17 类：

`budget`、`capacity`、`churn`、`cycle`、`expired_proposal`、`fanout`、
`idempotency`、`lease`、`minimum_dwell`、`missing_capability`、
`missing_endpoint`、`pending_side_effect`、`permission`、`privacy`、
`readiness`、`stale_environment`、`stale_revision`。

语料 canonical semantic digest：
`a89d13419e1cc22807576ebb69f6b8ee53d2633d3fa27431ac7516a032e30634`。
全部 17 个 case 都动态执行真实 projector/custody 路径，并匹配预期
constraint/reason；结果为 17 次拒绝、0 次 unsafe canonical commit。

该 JSON 明确声明 `training_dataset=false` 和
`policy_training_input=false`，仅作为 evaluation/adversarial corpus。

### 4.4 Disable/mutation 与 bypass

- continuity verifier 和 projector 的 disable switch 只允许 test mode；
  production constructor 传入 disabled 会拒绝。
- 断开 continuity verifier 后，compact/restore 无法形成可用 policy input。
- 直接调用 policy delta adapter 会拒绝。
- test-only、offline 的 raw `GraphDeltaBuilder` counterfactual 展示：省略 projector
  后，privacy proposal 可能形成形式上的 delta；该 counterfactual 从未提交到
  canonical custody。
- production bypass 静态/动态检查为 0。

此外，本 slice 修复了 policy delta 的操作排序：删除 edge/node 优先，随后新增或
替换 node，再执行 node setter，最后新增或替换 edge 和 metadata。对抗 fanout
case 揭示了原先词典序会把 `ADD_EDGE` 放在新 endpoint 的 `ADD_NODE` 之前，导致
合法 joint node+edge proposal 在 custody validation 被错误拒绝。修复后语义顺序
确定且幂等比较保持稳定。

## 5. 测试与验证

### 5.1 Slice 目标测试

提交后命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_memory_continuity_policy_runtime.py `
  tests\integration\test_neuro_symbolic_projector.py `
  tests\scenarios\test_continuity_adversarial_matrix.py `
  --basetemp .tmp\pytest-p2-s02-03-postcommit-2 -q
```

结果：`10 passed in 5.64s`。

目标测试分别证明：

- compact/restore、restart、handoff、replacement、requirement revision；
- unsafe memory fail closed 与下游 usage；
- real projector accept/commit/publish/replay；
- pending side effect、fixed-model receipt、projector bypass；
- no-policy-training release audit；
- 17 类 symbolic hard-constraint adversarial matrix。

### 5.2 相邻回归

- implementation suite：`60 passed in 12.17s`；
- adjacent integration matrix：`57 passed in 132.29s`；
- gate 修改后的 focused regression：`50 passed in 5.93s`；
- `compileall`：PASS；
- `git diff --check`：PASS；
- `verify_phase2_policy_contracts.py`：
  `valid=true`、`target_commit=31fafb8...`、`hard_gate_count=23`、
  `strongest_activation_eligible=false`；
- `verify_internalization_ledger.py`：
  `audit_ok=true`、`errors=0`、`blockers=0`，保留 734 条既有 warning；
- `NoPolicyTrainingValidator`：PASS。

## 6. 增量体量与分类

实现 commit 共 16 个文件，`3166 insertions / 45 deletions`：

| 分类 | Insertions | Deletions | 说明 |
|---|---:|---:|---|
| production | 1372 | 3 | continuity、evaluation、projector/delta/evidence/snapshot |
| test | 1633 | 42 | integration、scenario 与相邻 contract 调整 |
| data | 161 | 0 | evaluation/adversarial corpus，非训练数据 |
| docs | 0 | 0 | review/evidence 在本 evidence commit |
| runtime-assets | 0 | 0 | 无 |
| generated | 0 | 0 | 无 |
| adapter-only | 0 | 0 | 无独立 adapter-only 交付 |
| mock/fixture | 0 | 0 | 无以 mock/fixture 冒充真实路径 |
| training dataset/checkpoint | 0 | 0 | 无 |

## 7. P2-02 父单元收口

结合已完成的 `P2-S02-01`、`P2-S02-02`：

- policy contracts、schema、state owner 与 source-role 冻结保持兼容；
- registry/readiness/rollback 能消费本 slice 的 receipt 与 hard-gate 结果；
- continuity verifier 不取得 memory owner；
- neuro-symbolic evidence 不取得 graph、permission、lease、budget、scheduler 或
  evidence owner；
- proposal -> projector -> delta -> custody -> verification/outcome 链已可达；
- unsafe commit 与 production bypass 均为 0；
- baseline 默认路径与 strongest readiness gate 均未改变。

因此 `P2-02 Policy Foundation` 的三个已授权 slice 已形成可交接基础。后续
`P2-S02A-01` benchmark framework 或 `P2-03` 长程控制仍需用户明确选择，不在本
slice 中提前实现。

## 8. 证据索引

- `docs/reviews/evidence/P2-S02-03/verification-summary.json`
- `docs/reviews/evidence/P2-S02-03/continuity-symbolic-owner-audit.json`

根目录 `G:\agent-zoo\docs\phase2\slice-02-03-memory-neuro-symbolic.md` 仅作为权威
任务输入，本次未修改；它不属于 Zyra Git 仓库，也不会随 Zyra commit 自动提交。
