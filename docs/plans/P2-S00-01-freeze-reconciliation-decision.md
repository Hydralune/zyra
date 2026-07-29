# P2-S00-01 基线冻结与对账实施裁决

日期：2026-07-29  
切片：`P2-S00-01`

## 入口与保护边界

用户明确要求执行
`docs/phase2/slice-00-01-freeze-reconciliation.md`。执行开始时
`docs/milestones/execution-state.yaml` 尚无 Phase 2 节点，但第一阶段状态、
最终追溯审查和需求矩阵一致表明第一阶段已经封闭，Zyra 工作树干净。
因此本轮只建立 `P2-S00-01` 的 `in_progress` 入口，不提前写完成状态，也不
改写第一阶段历史事实。

进入本切片时 Zyra HEAD 为
`57c0def91a69c851c941e7d4df80519cf881a8c7`。该提交仅在第一阶段最终报告
提交 `be8c4733fede34c887b79cb721f8d81e9a2273f2` 之后更新 Phase 2
`AGENTS.md` 指令，且第一阶段 benchmark、report、final-freeze 和 release
目标均为它的祖先。最终 `P2_BASE_COMMIT` 定义为本切片实现提交：它包含
本裁决、当前 benchmark 指针修复、空缓存 clean-install 隔离和基线验证器，
但不包含随后生成的清单与验证回执，从而避免清单自引用。

## 提交身份分层

基线清单必须分开记录并验证以下身份，不能把它们压成一个错误字段：

- 当前正式 benchmark implementation：
  `09e99cdc5ed9cf3a935ccc327f7261110e6c7d1b`；
- 当前正式 benchmark evidence：
  `6b928d96f9181acf94eb9e84cb662df39feb9be3`；
- 当前第一阶段 report implementation/evidence：
  `88b88e05aad14e1091f4536bcead02037622408f` /
  `faf78d1ec6aa1bfe197dbb6120baedabbb723eb7`；
- 当前 final-freeze evidence：
  `725e869c7ee52eb3780851fd2328d09919d812e1`；
- 当前第一阶段最终报告：
  `be8c4733fede34c887b79cb721f8d81e9a2273f2`；
- 本切片实现提交形成唯一 `P2_BASE_COMMIT`，上述提交必须全部是它的祖先。

这一区分保留第一阶段真实执行目标和证据提交，同时用单一 P2 基线封装其
不可变 lineage；不得通过回写旧 evidence 的 target commit 制造表面一致。

## 对账实现

实现采用以下 fail-closed 边界：

1. `BenchmarkEvidenceLinker` 首先读取权威
   `docs/reviews/evidence/M3-S02A-02/formal-current.json`，验证 pointer 与
   report 的 implementation commit 相同；无 pointer 时才保留历史兼容路径。
2. release benchmark link 纳入同目录 current-campaign evidence 的路径、
   SHA-256 和当前 provider IDs，避免以历史 protected provider 列表替代
   DeepSeek、Kimi、GLM 的同 campaign 事实。
3. clean-install 环境强制把 Pip、uv 和 Bun 缓存放入一次性 cleanroom，
   不继承用户 uv cache，不消费用户 site state。
4. `Phase2BaselineVerifier` 重算清单摘要、Git commit/tree/ancestor、
   reference SHA-256/size、JSON pointer commit 绑定、只读证据清单语义和
   禁训练边界。篡改文件、伪造 evidence commit、路径逃逸或脏工作树门禁
   均不得回退为成功。

## 机制与数据口径

`phase2_strongest_v1` 在本切片只冻结为
`baseline_frozen_not_activated`。本切片不声称 LoopX、ARG、CARD、
AgentPrune 或 MaAS 已通过后续 readiness gate，也不把第一阶段 benchmark
轨迹变成训练数据。

第一阶段的 `1,554 raw samples`、`19,191 effective canonical transitions`
和 event/decision/artifact/dispatch/cost/verifier 引用只能标记为
`evidence_volume_only`。`training_sample_count` 固定为 `0`，后续任何训练、
微调、在线学习或策略更新均不获得授权。

## 验证与提交顺序

1. 聚焦与相邻 release 测试通过后提交实现；
2. 以该实现提交作为 `P2_BASE_COMMIT`，从干净提交构建两份字节一致 release；
3. 从发布包和空缓存执行隔离 clean install 与产品生命周期；
4. 生成基线清单、只读证据 inventory、release/clean-install/reconciliation
   回执和增量自审；
5. 执行正式清单验证、篡改与 commit-mismatch mutation、内部化账本验证和
   相关回归；
6. 提交 Zyra 证据后，才把根执行状态和需求矩阵推进到完成。

根目录 `docs/**` 不属于 Zyra Git 仓库。根规划文件摘要和 Zyra
implementation/evidence commit 必须分别记录；Zyra commit 不会自动包含根
执行状态或需求矩阵更新。
