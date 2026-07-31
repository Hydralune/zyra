# AGENTS.md

`zyra` 是比赛项目本体：面向超长程复杂任务的动态异构群体智能与深度协同推理 Agent 系统。当前开发阶段是第二阶段，也是参赛前最后一个代码建设阶段。

## 第二阶段目标

- 在已经冻结的完整系统上进行赛题定向优化，不重新建设基础 runtime。
- 强化长程目标保持、记忆连续性、动态图拓扑、低熵协作、神经符号推理、异构 operator 选择、端边云调度和可视化证据。
- 第二阶段完成后，代码进入真实场景测试、材料整理、演示和参赛流程。
- 默认只构建并冻结 `phase2_strongest_v1`，不维护多套开发期候选策略。

## 必读文档与执行优先级

- 工作区总约定：`../AGENTS.md`
- 第二阶段总计划：`../docs/第二阶段总工程计划.md`
- 第二阶段执行索引：`../docs/phase2/README.md`
- 当前父级单元：`../docs/phase2/unit-*.md`
- 当前独立切片：`../docs/phase2/slice-*.md`
- 赛题要求与证据定义：`../docs/比赛要求追踪矩阵.md`

优先级为：用户当前明确指令 > 当前 slice > 当前 unit > Phase 2 README > 第二阶段总计划 > 赛题要求矩阵。

开始实现前必须完整阅读 `../AGENTS.md`、当前 unit 和当前 slice。首次进入第二阶段或涉及跨单元架构裁决时，还必须完整阅读第二阶段总计划和 Phase 2 README。涉及评分、benchmark 或证据关闭时必须完整阅读赛题要求矩阵。

凡是被要求读取的项目文档，都必须从开头到结尾完整阅读；不得只读取摘要、目录、搜索命中或局部片段。

不再把第一阶段计划、旧 milestone、旧 source graph 或第一阶段状态文件作为第二阶段主动入口。只有用户明确要求历史复审，或当前 P2 slice 明确要求核对冻结证据时，才读取对应历史材料。

## 冻结基线

- 第一阶段代码、公共 contract、canonical owner、持久化语义、权限边界、测试和证据是第二阶段只读基线。
- 不得追溯改写第一阶段完成事实，也不得为了论文机制接入而更换现有 canonical owner。
- 第二阶段允许在保持 public contract 和状态 owner 的前提下新增实现、扩展字段、proposal、projector、verifier、receipt、API 和 UI。
- 如果当前改动必须进行不兼容 schema 迁移、转移 canonical owner、改变 transaction/lease/idempotency/restore 语义或全局安全策略，必须先形成当前 unit 的集中决策记录。
- OpenClaw 在第二阶段不作为实现、设计、测试或依赖来源，不得恢复或重新引入。

## 固定机制链

第二阶段默认主路径为：

```text
LoopX
  -> MemoryContinuityVerifier
  -> ARG joint role-node-edge base topology
  -> CARD condition residual correction
  -> AgentPrune spatial/temporal pruning
  -> symbolic projector + GraphStateCustody commit
  -> MaAS deterministic operator selection
  -> ResourceScheduler + permission + lease + physical dispatch
  -> verifier + receipts
```

所有机制输出都必须是 typed proposal 或只读信号。只有 Zyra canonical owner 可以提交状态、分配 lease、批准动作、消费预算或触发物理 dispatch。

## 禁止策略训练

- 第二阶段采用 `no_policy_training`。
- 禁止新增强化学习、策略梯度、文本梯度、在线学习、离线微调、额外神经网络训练、训练 dataset 和训练 checkpoint。
- 不迁移 ARG、CARD、AgentPrune 或 MaAS 的训练器与训练流水线。
- 不把规则、排序、阈值、prompt 或确定性适配表述为训练结果。
- 不进行开发期全组合消融。封闭测试不理想时，只根据分层 receipt 对单个机制层做定点诊断。

## 模型 API 默认顺序

- 已配置模型的默认顺序为 `zhipu/glm-5.2`、`deepseek/deepseek-v4-flash`、`kimi-platform/kimi-k2.7-code`。
- 未显式指定 provider/model 的真实模型调用必须选择 `zhipu/glm-5.2`。
- DeepSeek 是第二候选，Kimi 是最后候选；不得在 runtime、测试入口或 release 配置中把 DeepSeek 固定成默认值。
- 显式的当前任务选择可以覆盖默认顺序，但必须保留实际 provider/model 调用结果。

## LoopX 产品形态

- P2-01 已完成并冻结；最终证据为
  `docs/reviews/P2-S01-04-loopx-v0213-embedded-source-cutover-review.md`。
- LoopX 固定为完整的长程目标控制插件，版本 `0.2.13`，annotated tag
  object `a2c072d412d90839132e1cf39c23dd431c394175`，peeled source commit
  `7232dca45ec2ca996edc43b2d3558edc802c844e`，tree digest
  `66af2de0082dbadf7c7cb3ffc85b9b91b889c0433103ea3bc05abb313d831961`。
- source role：`supplementary_implementation`。
- migration mode：`pinned_embedded_source_integration`。
- LoopX 完整可运行源码进入 Zyra release，但按
  `runtime-assets/vendor-like` 单列，不计作 Zyra 深度内化代码。
- Windows 与 Linux/WSL 使用同一 canonical source manifest、package lock
  和 tree digest；运行时不执行 LoopX 专用安装或 archive extraction。
- 运行时不得依赖 `../../long-horizon-systems/loopx`、在线 Git 仓库、用户级
  LoopX、`.zyra/loopx/install` 或任何工作区外路径。

正式目标路径：

```text
packages/integrations/loopx_runtime/
  SOURCE-MANIFEST.json
  loopx/
  packages/
  skills/
  templates and package resources

packages/integrations/zyra_integrations/loopx/
  runtime/
  install/
  bridge/
    contracts.py
    outbox.py
    dispatcher.py
    single_writer.py
    state_mapping.py

apps/api/zyra_api/loopx_api.py
apps/web/src/features/long-horizon/loopx/
```

`LoopXRuntimeResolver` 是唯一 runtime locator；`install/**` 只保留最薄兼容
facade 与 retired-path 诊断。`LoopXDoctor --deep` 必须验证 import、CLI、
extension、skill/template、package resource、manifest、source digest 和
workspace private state provenance，并在任何损坏时 fail closed。

状态边界：

- LoopX 拥有私有 goal、todo、claim、history 和插件状态。
- Zyra 拥有 run、task、node、attempt、graph、memory、worker lease、permission、provider/tool/resource budget、event、artifact 和 physical dispatch。
- LoopX claim 不能映射成无条件 worker lease。
- LoopX quota 不能覆盖 Zyra execution budget。
- bridge 必须使用 workspace-local state、durable outbox、Windows single writer、幂等 dispatch、ack 和 restart replay。
- LoopX 失败、缺失或状态损坏时，Zyra 必须 fail closed 或进入显式降级，不能静默伪装为长程控制已启用。

## Topology policy 落位

正式目标路径：

```text
packages/orchestration/zyra_orchestration/topology_policy/
  contracts.py
  registry.py
  continuity.py
  arg/
  condition/
  pruning/

packages/evaluation/zyra_evaluation/policy_benchmark/
  mechanism_readiness.py
  continuity.py
  neuro_symbolic.py
```

职责：

- ARG：基于任务、阶段、能力和资源条件，联合生成 role-node-edge 基础拓扑 proposal。
- CARD：对 ARG 基础拓扑做条件残差修正和有向边重评分；不得生成第二份 canonical graph。
- AgentPrune：执行确定性的空间边和时间边剪枝，记录保留/删除理由与通信预算影响。
- `MemoryContinuityVerifier`：在 topology proposal 前检查 memory lineage、checkpoint、compact/restore、restart 和 requirement change 连续性。
- symbolic projector：把 proposal 投影为满足 schema、权限、预算、容量、安全和依赖约束的 `GraphDelta`。
- `GraphStateCustody`：执行冲突检测、rebase/replan、幂等提交和恢复，是唯一 graph commit owner。

任何 topology policy 都不得：

- 原地修改共享 snapshot、dict、list 或 graph object。
- 绕过 `GraphDeltaBuilder` 或 `GraphStateCustody` 写 canonical state。
- 根据并行完成顺序产生不确定 commit。
- 直接执行工具、副作用或物理资源分配。
- 用 LLM 文本替代可审计的 typed proposal 和 symbolically checked delta。

## Operator 与物理调度

正式目标路径：

```text
packages/scheduler/zyra_scheduler/operator_policy/
```

- MaAS 机制只负责确定性选择 operator、skill、worker、tool、model、breadth 和 depth。
- 选择必须受 capability、permission、privacy、cost、latency、availability、budget 和 readiness 约束。
- `ResourceScheduler` 保留 physical placement、lease、route、dispatch、retry 和 degradation 的唯一 owner 身份。
- operator selection 与 physical placement 必须是两个独立 receipt；不得把“选择了 cloud model”当作已经发生 cloud dispatch。
- local/terminal、隔离 edge runtime、cloud provider/model 都必须通过真实 transport、process、provider request 或 worker receipt 证明。
- 模拟 backend 只用于开发验证，不能关闭真实端边云证据门。

## 算法数据可行性门

每个机制必须生成 versioned `MechanismEvidenceReadinessReport`：

```text
input_precheck
  -> implementation_validated
  -> activation_ready
```

允许状态：

- `deterministic_ready`
- `evidence_only`
- `unavailable`

要求：

- 每项输入必须有来源、schema、coverage、freshness、缺失处理和 receipt。
- 每项机制必须证明代码动态可达、proposal 非空、约束确实生效、失败路径可解释。
- `phase2_strongest_v1` 的必需机制必须是 `deterministic_ready`。
- `evidence_only` 只能进入 diagnostic；`unavailable` 必须 fail closed 或使用明确 baseline。
- 不得通过造数据、重复调用模型、隐藏缺失值或硬编码预期输出绕过 readiness。

## 记忆连续性与神经符号证据

- memory 不能只证明“可以检索”。必须证明目标、约束、证据、决策和未完成工作跨 checkpoint、compact、restart、需求变更和恢复保持 lineage。
- 每次 memory 使用都应关联 run、task、checkpoint、source event、retrieval reason 和 downstream decision。
- graph delta、operator choice、permission result、lease、dispatch、verification 和最终 commit 必须能沿同一 causal chain 回放。
- UI 展示必须来自真实 event/artifact/readiness receipt，不得由静态演示数据拼接。

## 实现约定

- 先复用和扩展 Zyra 已有 schema、event、artifact、permission、memory、scheduler、recovery、API 和 UI contract。
- 新模块必须进入真实 CLI、API、runtime、worker、event 或 UI 主路径；只被 import smoke、health check、示例脚本或 fixture 调用不算完成。
- 来源项目必须裁剪为上述 Zyra 模块，或者作为 LoopX 固定完整包进入 release；不得新增指向工作区来源目录的 editable install、npm link、Docker context 或相对路径。
- 不能用薄 adapter 掩盖未接管的状态、权限、错误、预算和恢复语义。
- 不得用大型配置、论文数据、manifest、source map、生成文件、runtime-assets、mock 或测试体量冒充 production 实现。
- 当前 slice 之外的重构仅在修复直接阻塞问题时进行，并记录原因和影响。

## 验证与自审

每个 P2 slice 至少验证：

- 当前目标的真实主路径和失败路径。
- proposal 到 canonical commit 的动态可达性。
- permission、budget、lease、idempotency 和 recovery 边界。
- disable/mutation 后行为失败、降级或出现可解释差异。
- 非 fixture 输入和干净状态运行。
- event、artifact、receipt 与实际副作用的因果一致性。
- 当前 diff 未引入工作区外运行依赖。

普通 slice 运行当前改动和相邻路径测试；父级最后一个 slice 累计核对 unit 目标与跨 slice 主路径。最终冻结负责 cleanroom、长程封闭场景、真实端边云、多模型、故障恢复、性能回归和提交边界。

自审必须区分：

- `production`
- `test`
- `runtime-assets`
- `adapter-only`
- `generated`
- `data`
- `docs`
- `mock/fixture`

只有真实进入主路径并承担 Zyra 状态、事件、权限、错误、预算、恢复或产品交互责任的实现可以计入 production。

## Git 工作流

- 开始修改前运行 `git status --short`，识别并保护已有改动。
- `G:\agent-zoo\zyra` 是 Git 边界；根目录文档不会随 Zyra commit 自动提交。
- 完成当前 slice 的代码、相关测试、自审和证据后，主动创建范围清晰的 commit。
- 提交前再次检查状态，避免混入无关改动。
- 最终答复报告 commit hash、提交主题、验证命令、未运行项及工作区状态。
- 除非用户明确要求，不自动 push、不改写历史、不 reset、不清理未跟踪文件、不切换分支。
