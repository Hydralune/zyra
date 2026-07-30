# P2-S05-01 增量评审：低熵、连续性、符号与 Dispatch 指标升级

## 结论

P2-S05-01 已按 slice 边界完成。实现将 P2-03/P2-04 的 canonical proposal、decision、outcome、continuity、symbolic、operator 和 physical dispatch receipt，以及 AgentPrune communication observation，转换成 57 个固定口径指标，并输出 run、scenario、mechanism、aggregate 四层结构化报告。

本 slice 没有修改 graph、memory、permission、lease、scheduler 或 physical dispatch 的 canonical owner。指标引擎是只读消费者；API 也是 GET-only artifact projection。

## 提交身份

- Slice base commit：`6b81f660822b1f8d5c30206d38f7d4d9d998f0fc`
- Implementation commit：`9eb2e750b5ceaa2df9e5ca68fadad4f6ab18ff15`
- Evidence commit：在证据提交后由 `execution-state.yaml` 记录
- 固定组合：`phase2_strongest_v1`

## 实现落位

- `packages/evaluation/zyra_evaluation/policy_benchmark/metric_specs.py`
  - 57 个 `zyra.phase2-metric-spec/v1` 指标；
  - numerator、denominator、unit、direction、aggregation unit、minimum sample、requirement IDs、evidence contracts、empty/missing/failed semantics；
  - 第一阶段指标兼容映射只读保留，`history_rewrite=false`。
- `packages/evaluation/zyra_evaluation/policy_benchmark/metrics.py`
  - canonical receipt resolver；
  - exact duplicate 去重与冲突幂等键 fail closed；
  - supplied digest 校验；
  - proposal→decision→outcome 引用、过期时间和 digest 绑定；
  - 通信、拓扑、readiness、连续性、符号安全、operator、dispatch 指标计算。
- `packages/evaluation/zyra_evaluation/policy_benchmark/report.py`
  - run、scenario、mechanism、aggregate 四层报告；
  - 失败 run 单列；
  - aggregate 只聚合 `observed` 且允许优化的样本，不用失败任务的低成本冲高结果。
- `apps/api/zyra_api/policy_api.py`
  - GET-only spec 与 report projection；
  - report 由 evaluation owner 写入后只读加载；
  - 加载时重新校验 report digest；
  - 不写 task、graph、lease、permission 或 runtime state。

## 指标与 requirement 映射

注册表覆盖以下类别：

1. 通信与拓扑：normalized entropy、sender-conditioned entropy、spatial/temporal delivery、bytes/tokens/cost、duplicate ratio、evidence utilization、useful ratio、cost/effective transition、adaptation latency、normalized churn、oscillation、proposal disposition、decision overhead。
2. 机制 readiness：stage/status、required/optional coverage、freshness、confidence、missingness、scenario/failure coverage、causal completeness、deterministic replay、default/diagnostic/baseline mode、no-policy-update audit。
3. 连续性与符号安全：critical-fact recall、obligation retention、provenance、stale requirement、duplicate work、first decision correctness、adversarial reject/project/accept、unsafe commit、projector bypass。
4. Operator 与 physical dispatch：early-exit true/false positive、breadth/depth、local/edge/cloud real completeness、reroute、privacy violation、provider receipt coverage、六段 causal chain completeness。

`metric-spec-registry.json` 内含完整 requirement→metric 反向映射，覆盖 `REQ-COMM-01`、`REQ-TOPO-01`、`REQ-MEM-01`、`REQ-EDGE-01`、`REQ-FAULT-01`、`REQ-TRACE-01` 及对应评分项。

## 防指标游戏裁决

- Graph-size normalization：communication entropy 使用参与节点的最大有向 pair 空间归一化；小图不会因为 edge 少而自动被判定为低熵。
- Failed task：失败 run 的 token/cost 会保留原始值和 `failed` 状态，但 `eligible_for_optimization=false`，aggregate 不使用该低成本。
- Evidence volume：`evidence.canonical_transition_count` 明确标为 `evidence_volume_only`，不会改变 readiness stage/status，也不生成独立样本量或学习曲线。
- Simulated dispatch：`simulated=true` 或 `semantic_only=true` 从 local/edge/cloud real numerator 和 denominator 中排除，并在原因中显式记录。
- Memory continuity：critical fact 同时要求 `present_after=true`、`consumed=true` 和非空 usage event；“存过但未使用”得分为 0。
- Symbolic reject：projector 拒绝且无 commit 时 reject ratio 记录为 1，unsafe commit 保持 0。
- Empty/missing：zero denominator、empty cell、partial failure 与 resolver disconnect 均为 `degraded`、`failed` 或 `not_applicable`，不会显示 `observed` 成功。
- Idempotency：相同 event set 重复导入 digest 不变；相同幂等键对应不同 digest 直接失败。
- Freshness/reference：过期 proposal、缺 outcome、proposal digest 不一致或 supplied digest 不一致直接 fail closed。

## 真实 receipt→metric lineage

证据生成器使用 immutable canonical contract 类构造：

- `CommunicationOutcomeObservation`
- `TopologyProposalArtifact`
- `PolicyDecisionReceipt`
- `PolicyOutcome`
- `MemoryContinuityReceipt`
- `NeuroSymbolicEvidenceBundle`
- `ExitDecisionReceipt`
- `AdaptiveDepthCostReceipt`
- `PhysicalDispatchReceipt`

正式 metric report 的每个指标都携带 source receipt digest；`receipt-metric-lineage.json` 同时给出 kind→receipt digest 与 metric→source digest 映射。UI projection 和自报 completion 均未进入输入。

## 验证结果

Slice 建议的三个命令分别通过：

- unit metrics：6 passed；
- real receipt integration：1 passed；
- anti-gaming integration：7 passed。

相邻 contract、AgentPrune spatial/temporal 和 physical dispatch 回归与 slice 测试合并运行：30 passed。

`git diff --check` 与相关目录 `compileall` 通过。项目虚拟环境没有安装 ruff、black 或 isort，因此未伪造格式器通过结果。

一次更宽的运行额外包含 `tests/integration/test_api_control_commands.py`，结果是 41 passed、15 failed。失败集中在既有 POST permission/tool/skill/command/browser-worker 路径；本 slice 只新增 GET metric projection，未修改这些 POST handler。该结果不计为 slice 通过，也没有被隐藏；完整摘要保存在 `verification-summary.json`。

## 证据索引

- `docs/reviews/evidence/P2-S05-01/metric-spec-registry.json`
- `docs/reviews/evidence/P2-S05-01/metric-report.json`
- `docs/reviews/evidence/P2-S05-01/receipt-metric-lineage.json`
- `docs/reviews/evidence/P2-S05-01/evidence-manifest.json`
- `docs/reviews/evidence/P2-S05-01/verification-summary.json`
- `docs/reviews/evidence/P2-S05-01/effective-lines.json`

固定 digest：

- Registry：`64bab25967951abc5f43ce4c7df834475f700d147d2d54a3d0685dcaf2a176a6`
- Report：`88f14995bf57f26eff174728a253aacc640da73df52b6b024b50c062840cd133`
- Lineage：`3f7748b5c6eb3899b327bbd20f94269adf84eefd6e534e84ba37b1267d6ce03b`
- Manifest：`1ab0b9ef4775cc364f92d72d74edafe81eeb6b019ddcad1bc456a6ecbf5d68a3`

## 有效行分桶

- Production：1796 added lines；1651 nonblank/non-comment effective lines。
- Test：931 added lines；871 nonblank/non-comment effective lines。
- Validation：250 added lines；234 nonblank/non-comment effective lines。
- Runtime-assets/vendor-like：0。
- External data/models：0。
- Adapter-only：0。
- Generated source：0。
- JSON report/spec/lineage 与本评审均归为 data/docs，不计入 Zyra implementation。

## 交接

P2-S05-02 可以只读消费：

- `phase2_strongest_v1.metrics` registry；
- `zyra.phase2-metric-report/v1`；
- canonical receipt resolver 的 missing/degraded/fail-closed 语义；
- report API 的 GET-only projection；
- anti-gaming 状态与 source lineage。

记录下一候选不构成 P2-S05-02 授权。
