# M1-S07C-02 recovery planner、routing、memory integration 批判式自审

日期：2026-07-22

slice baseline：`fabe347207bc3d2d144ad68c029cb288f09e73a8`

parent baseline：`aa494526ed7f32b177f9a10fa3a93d8a684e0264`

实施前冻结：`05386ef`

implementation：`774c8f87d9cdf193596d20fedf72fb3034bc5e38`、`8de780f9410308725859a9994bdc2e27e5ba6042`

最终 implementation target：`8de780f9410308725859a9994bdc2e27e5ba6042`

## 结论

本 slice 完成，父级 `M1-07C` 收口完成。恢复主路径现在把 permission、MCP、session、compact、API/provider、
worker/backend、subagent、workspace、tool、control、checkpoint、watchdog 与 OMP receipt 统一收敛到既有 typed
classifier 和唯一的 Python recovery policy owner；只有 applied canonical owner receipt、状态投影和变化后的
downstream continuation 同时成立时，才允许写 routing memory。exact resume、branch-local delta、route isolation、
restart、component disable 与因果审计均进入真实 API/runtime 主路径，不存在 event-only 或旧 planner fallback。

保守审计得到本 slice **7,062 行**有效 production，高于最低 **7,000 行**，余量 62。以父级 baseline 直接
重算 `M1-07C` 为 **15,469 行**，高于父级最低 **15,000 行**，余量 469。冻结的 07C-01 8,429 与本次
7,062 的简单和为 15,491；直接重算少 22 行，来自本 slice 对 07C-01 行的替换/删除，父级结论采用更保守的
直接重算值。机器证据见 `docs/reviews/evidence/M1-S07C-02-recovery-planner-routing-memory-integration.json`。

## 来源裁决与唯一 owner

| 子域 | 裁决 | 生产 owner / 边界 |
|---|---|---|
| recovery classification、policy、plan、action、outcome | Zyra-owned Python primary | 复用并扩展 `RecoveryDecisionRuntime`、`RecoveryActionRuntime` 与 `RecoveryPlanStore`；各 ingress 不得另建分类器或策略 owner |
| state fusion、semantic gates、applied verification、continuation | Zyra-owned Python primary | 新 integration runtimes 校验 canonical refs、freshness、secret rejection、pre/postflight、receipt 投影与 downstream change |
| checkpoint/exact resume、branch commit | Zyra-owned Python primary；LangGraph `conformance_only` | pending/committed writes、stable identity、atomic commit、interrupt correlation 与 exact resume 是窄域语义；无 LangGraph runtime、StateGraph/channel/Pregel/Store/ToolNode |
| route、memory、restart、causal audit | Zyra-owned Python primary | route 分层隔离；memory 仅消费 applied proof；restart 从 RecoveryPlanStore 与 canonical owner refs 恢复；audit 验证完整因果链 |
| provider rotation、partial stream、MCP breaker、worktree WIP、background restart | OMP `supplementary_implementation` | 裁剪 TypeScript 同语言机制只产生 supplementary receipt/evidence，不取得 durable policy、store、route、checkpoint 或 task owner |
| task、graph、worker、backend、provider、permission、memory | 复用既有 canonical owners | 07C 保存并验证 request、before/after ref、receipt 与 continuation，不复制这些域的 store/lease/state |
| Claude Code 等其它来源 | conformance/reference | permission/compact/MCP/subagent/API failure chain 只校验边界与事件语义，不产生第二实现 |
| OpenClaw | `excluded_forward_only` | 未读取、未恢复、未引入源码、包、进程、路径或运行依赖 |

实施前 decision 固定 OMP commit `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`。生产 ledger 仅新增
`ile_oh_my_pi_490e53f66a4d5834` 一项 OMP supplementary 裁决；LangGraph 与 Claude Code 的
conformance/reference 事实只进入 decision、测试、review 与 evidence，不伪造 production ledger 配额。

## 内化模块、状态 custody 与真实主路径

| 模块 | 责任与语义效果 |
|---|---|
| `component_runtime.py` | 每组件 enable/readiness/env 矩阵；禁用、未知或未就绪都 fail closed，且不回落旧 planner |
| `ingress_runtime.py` | 把各真实来源转换成 typed `RecoverySignal`，强制经过既有 classifier；拒绝伪造 owner identity 与 secret payload |
| `state_fusion_runtime.py` | 至少三个 canonical state family 的 immutable snapshot/freshness/owner identity 融合；过期、缺失、冲突或 secret 泄漏拒绝 |
| `semantic_runtime.py` / `verification_runtime.py` | action preflight/postflight；验证 canonical receipt、before/after 投影、side-effect fence、route layer 与真实变化 |
| `exact_recovery_runtime.py` | side effect prepare→execute→crash→resume→complete；恢复 pending writes/requests/in-flight message，拒绝 topology/workflow/graph signature mismatch，跳过 committed effect/processed response |
| `branch_recovery_runtime.py` | immutable snapshot、copy-on-write delta、显式 read/write-set、稳定词法提交；冲突只允许 replan/reject/serialize，不按完成顺序合并 |
| `route_memory_runtime.py` / `feedback_integration_runtime.py` | graph/worker/backend/provider/model/credential 分层 route isolation；只有 applied proof 后写 memory，非 route recovery 同样记录，并证明后续 context/ranking 改变 |
| `restart_runtime.py` / `continuation_runtime.py` | 从 durable plan store、action cursor 与 canonical owner refs 重启；验证 continuation 的实际 route layer 与 changed downstream state |
| `causal_runtime.py` / `audit_runtime.py` | failure/signal→plan→action→outcome→continuation→memory 的因果链；等待/失败 outcome 不得提前写 memory |
| `integration_runtime.py` | 组合 ingress、fusion、plan/action、exact resume、verification、continuation、memory、causal audit，保持一个 policy 与一个 durable store owner |
| API 主路径 | observation、batch integration、restart、component readiness 路由；既有 signal/checkpoint/delta/task view 继续直达 canonical runtime |
| `omp-integration-runtime.ts` | provider credential rotation、partial-stream dedup、MCP breaker、worktree WIP/conflict、background restart 的裁剪状态机；输出 supplementary evidence |

RecoveryPlanStore 仍只拥有 recovery plan/action/checkpoint/receipt/fence/feedback 事实。TaskState、GraphStateCustody、
WorkerPool、BackendRegistry、ProviderControlPlane、PermissionControlPlane 与 MemoryFabric 保持原 owner；integration
只通过 typed request 与 applied receipt 投影这些 owner 的变化。删除或禁用 integration 组件后，batch/observation/
restart 主路径 fail closed；删除 exact/branch/verification/memory 模块后，相应 replay fence、冲突隔离、applied proof
或 later-ranking 测试立即失败，因此不是 manifest、ledger、fixture 或只写 event 的伪内化。

## 关键语义矩阵

1. permission pending/denied 只能进入受控 ask/deny 路径；没有 permission settlement 不执行工具，也不写成功 memory。
2. compact/session 恢复必须匹配 checkpoint、lineage、graph/topology/workflow signature，并绕过 processed response 与
   committed side effect；不重复发出外部效果。
3. API/provider/model failure 先受预算、credential、partial-stream 与 side-effect fence 约束，再选择隔离的
   provider/model route；不能把 provider route 写成 worker/backend route。
4. worker/backend/subagent/workspace failure 请求各自 canonical owner 的新 receipt；route escalation 显式记录，
   不共享 lease 或混用 owner ref。
5. branch delta 的 nested object 不与 canonical snapshot 共享别名；冲突结果显式为 replan/reject/serialize，
   deterministic commit 不依赖并行完成顺序。
6. applied owner receipt、owner-state projection 与 changed continuation 缺一时，routing memory 写入被阻断；成功
   memory 会改变后续 context/ranking，失败/等待 outcome 只保留审计事实。

## 批判式缺陷发现与修复

1. 初版 exact prepare 使用错误的 dataclass replacement，且 processed-response fence 合同不完整。已修正不可变
   替换、response correlation 与 stable branch helper，crash/resume 不再重复 committed effect 或 response。
2. snapshot digest 不能处理 `mappingproxy`，alias 检查还会被短生命周期 object id 复用误报。已改为稳定不可变
   序列化与存活对象级别 alias 验证，并加入 nested COW 对抗测试。
3. serialized conflict checkpoint 可能复用 write sequence。现为每次有序提交生成唯一稳定 sequence，拒绝按并行
   完成顺序覆盖 canonical state。
4. API retry signal 一度未进入既有 classifier 的正确类别。已补 typed normalization，不允许 ingress 直接选择 action。
5. task mutation history 未设上界，长期恢复可能膨胀。已加入有界历史并覆盖 restart/replay 行为。
6. 初版 memory 只覆盖 route recovery，且 route audit 使用候选层而非实际 receipt 层。已加入非 route applied feedback，
   continuation/audit 改用真实 selected route layer，等待/失败 outcome 不再被误判必须存在成功 memory。
7. 首次严格统计只有 6,684 行。没有用 DTO、tests 或注释补量，而是补入 378 行运行态 audit：检查未完成结果
   不提前反馈、applied proof 顺序、route isolation 与 exact-resume fence；对应 task GET/API 断言进入主路径。

## 有效行数分桶与大文件复核

统计区间固定为 `fabe347207bc3d2d144ad68c029cb288f09e73a8..8de780f9410308725859a9994bdc2e27e5ba6042`。

| 桶 | 行数 | 计入最低线 |
|---|---:|---|
| raw additions | 10,466 | 否 |
| type/interface/declaration/export | 316 | 否 |
| schema/DTO/data | 1,376 | 否 |
| tests/mock/fixture | 995 | 否 |
| docs/comments/blank | 717 | 否 |
| adapter-only | 0 | 否 |
| generated/vendor/source-pool | 0 | 否 |
| **conservative effective production** | **7,062** | **是** |

按来源角色与语言：Zyra-owned Python recovery primary 5,953，Python API/main path 359，OMP
supplementary TypeScript 750。没有文件贡献超过本 slice 有效代码的 20%。raw additions 超过 500 的 OMP
integration、causal、exact、feedback integration、ingress、integration、semantic、state fusion、verification
文件均逐一复核：它们分别承担原语言状态机、因果审计、exact resume、memory gate、typed ingress、编排、
pre/postflight、canonical fusion 与 applied proof，并由直接行为测试触发。排除率超过 30% 的 OMP integration、
component、feedback integration、restart、route memory、verification 也逐一核验；排除内容是 type/DTO/validation
data/comments，而保留的 production 行确实改变运行结果。

父级从 `aa494526ed7f32b177f9a10fa3a93d8a684e0264` 直接重算：raw additions 23,788，tests 1,734，
schema/DTO/data 3,863，type declarations 801，docs/comments/blank 1,620，非生产审计工具 301，最终
effective production 15,469。父级门禁据此通过，不使用两个 slice 数值机械相加替代直接审计。

## 验证

- 当前与相邻 Python：foundation、integration、API、07B watchdog、07A worker pool 共 `63 passed, 6 subtests
  passed in 121.03s`。
- 最终直接 Python：integration unit + API `8 passed in 28.42s`；foundation + integration + API 在 clean
  worktree 为 `12 passed, 6 subtests passed in 29.71s`。
- TypeScript 原语言：watchdog、recovery、recovery-integration 共 `15 passed`；相同 15 项在 clean worktree 重放通过。
- TypeScript `tsc -p packages/runtime/claude-runtime/tsconfig.json`：通过；Python `compileall`：通过；
  `git diff --check`：通过。
- strict effective-code gate：slice 7,062/7,000；parent direct recompute 15,469/15,000；Python primary、
  Python API 与 OMP TypeScript production 均非零。
- source ledger：07C-01 与 07C-02 两个 sync `--check` 均 aligned；07C-02 恰好 1 个 production decision，
  targeted audit 为 0 finding。dependency/path scan 为 0 sibling source repo、0 `../` runtime、0 npm link、
  0 pip editable、0 external Docker context、0 cache/vendor/source-pool、0 OpenClaw 与 0 新依赖/lockfile。
- focused clean-state replay 使用 detached `8de780f` worktree，Python import 路径确认位于 cleanroom；Python 12
  项和 TypeScript 15 项通过。临时 worktree registration 与目录已全部删除。

扩展但非当前 gate 的 `tests/integration/test_worker_pool_api_main_path.py` 为 `6 passed, 1 failed`；失败仍是已在
07C-01 记录的 `test_subagent_fanout_maps_each_child_to_one_physical_attempt_and_receipt` 第二个并发 child receipt
为 failed。当前变更未接管 worker/subagent owner，不越权修补。ledger 相关 8 文件选择为 `64 passed, 4 failed`：
既有 planned/unmaterialized target、delegated MCP POST connect、M1-01B completion gate、M0-M3 audit 跨 owner
债务；新增 07C-02 ledger entry 的定向检查为零 finding。这些均保留给 M1-07 数字阶段聚合审查，未隐瞒也未
错误归因成本 slice 回归。

## 未伪报、父级收口与下一入口

本 slice 不声称关闭两个跨领域 live task、单 run 2,000 canonical transitions、sealed zero-human、动态图
低熵对照、真实 local/isolated-edge/cloud dispatch、多模型或最终交付冻结门禁。`M1-07C` 的代码、直接行为、
有效行数、父级累计与 focused clean-state 门禁已收口；整个数字阶段 `M1-07` 尚未聚合审查。

下一强制入口是 `docs/执行单元完成后通用审查任务书.md`，范围为 `M1-07A`、`M1-07B`、`M1-07C`，必须完成
跨 unit 回归、完整 cleanroom、全量 ledger/source-to-target audit 与独立批判式复审后，才能进入 `M1-S08-01`。

evidence commit 创建后才更新根目录 `G:/agent-zoo/docs/milestones/execution-state.yaml`。该文件不属于 Zyra Git，
最终交付单独说明。
