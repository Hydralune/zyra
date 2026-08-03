# LoopX 深入阅读与源码复用分析

> 分析对象：`G:\agent-zoo\long-horizon-systems\loopx`  
> 仓库版本：`0.2.4`  
> 阅读基线：`8e79843704a40d8069a9cab4ede6edc6d29f671b`（`main`）  
> 分析日期：2026-07-18  
> 分析目的：识别 LoopX 的真实运行边界、成熟能力、实验性机制，以及把它作为完整控制面模块接入 Zyra 第二阶段时的状态分工和局部适配范围。本文件不修改 LoopX，也不修改 Zyra 已完成单元。

## 1. 结论先行

### 1.1 项目定位

**[项目事实]** LoopX 将自己定义为面向超长程任务的轻量、本地优先 control plane，而不是模型运行时、通用 Agent 框架或分布式执行平台。它的主循环是：读取活动目标和任务状态，计算当前是否应该运行以及应执行什么，交给外部 host/agent 完成一个有界动作，随后写回证据、状态和历史，并只在验证通过后消费额度。模型调用、工具执行、沙箱、鉴权以及主要工作产物均由 host 持有。

**[分析判断]** LoopX 最成熟、最值得复用的不是“多 Agent 执行器”，而是以下几组控制面机制：

1. 确定性的运行资格、额度、阻塞与下一动作决策；
2. 将同一决策拆为 `user_channel`、`agent_channel`、`cli_channel` 的交互合同；
3. claim、lease、history、quota、validation 和 spend-after-validation 组成的长任务治理规则；
4. 面向公开展示的紧凑、安全投影；
5. Explore 子系统中的结果证据图、实验调度、回放与反事实验证原型；
6. Reward Memory 中“内容可信度”和“权限/作用域权威性”分离的治理规则。

**[分析判断]** LoopX 可以完整承担一个有界的长期任务控制面模块，但不能单独承担整个 Zyra canonical runtime、动态拓扑 scheduler 或统一 event store。它当前同时存在三种成熟度明显不同的代码：

| 层级 | 代表能力 | 判断 |
|---|---|---|
| 主线可用 | status、quota、todo、claim/lease、history、interaction contract、CLI | 已形成真实控制面闭环，但仍以本地文件为中心 |
| 可用但依赖环境或外部 host | tmux/Codex launcher、worker bridge、Explore result layer、dashboard | 合同和胶合较完整，实际工作仍由外部 runtime 承担 |
| 实验或尚未进入产品主路径 | event-store promotion、Replay/Counterfactual、Reward Memory Stage 1/2、Session Runtime writeback、Explore 动态实验 harness | 源码和测试有价值，但不能按“已集成生产能力”计算 |

**[接入建议]** 对 Zyra 第二阶段，默认策略应是把 LoopX `0.2.4` 作为一个完整、版本锁定的长任务控制面模块安装和调用，而不是先拆散其 quota、status、todo、history、Explore 等内部模块。LoopX 可以在“长期目标、todo、claim/lease、quota、validation、history”这个有界状态域内保持完整控制流；Zyra 通过一个窄 adapter 向它提供 run/agent identity、接收 decision/status/outcome，并继续负责模型推理、工具、permission、worker、sandbox、全局 scheduler、artifact 和产品 UI。

整体接入仍需要三条边界：

- 正式运行依赖必须被版本锁定并进入 Zyra 的构建、镜像或依赖清单，不能依赖工作区相对路径 `../long-horizon-systems/loopx`；
- 同一个状态域只能有一个写入 owner。若采用 LoopX 完整模式，建议由 LoopX 负责其长期控制面内部状态，Zyra 只保存带 LoopX identity 的跨域事件和投影，不再并行修改同义 todo/quota/lease；
- Explore harness、Replay/Counterfactual、Reward Memory Stage 1/2、tmux launcher 等成熟度较低或平台相关的能力继续由 feature flag 控制，不因安装整个包而默认全部启用。

只有在实际接入发现 Windows 锁、路径、身份映射、事件导出或故障恢复存在明确缺口时，才维护局部 adapter、patch 或小型 fork；不以“深度内化”为理由预先重写 LoopX 的完整控制流。

## 2. 阅读范围与方法

### 2.1 已阅读的主要材料

本次阅读覆盖了：

- 根目录 `README.md`、`AGENTS.md`、项目配置与架构说明；
- `loopx/` 下状态、额度、任务、历史、锁、事件存储迁移、交互合同、worker bridge、session runtime、multi-agent launcher 等主模块；
- `loopx/capabilities/explore/` 的结果层、调度器、router、harness、checkpoint、trace、replay、counterfactual；
- `loopx/capabilities/auto_research/` 的 kernel、worker runtime 和 worker loop；
- `loopx/capabilities/reward_memory/` 的 candidate review、registry 和 health；
- `docs/` 中 multi-agent、auto-research、worker bridge、session runtime、Explore、Reward Memory 和 dashboard 文档；
- 与上述模块直接对应的 pytest 文件与示例 smoke。

仓库未提供一篇可作为统一权威来源的项目论文或 PDF。代码和文档中对 DSpark、DeepSeek 等机制的提及属于设计借鉴，不等于 LoopX 自身已经通过对应论文实验或 benchmark 复现。

### 2.2 结论标记

- **[项目事实]**：由源码、仓库文档、测试断言或实际命令直接支持；
- **[分析判断]**：基于事实作出的成熟度、风险或架构判断；
- **[接入建议]**：面向 Zyra 第二阶段的整体安装、状态域划分、局部适配和运行门禁建议。

### 2.3 验证边界

本次运行了 11 个选取的零依赖示例 smoke 和若干 CLI 只读/dry-run 命令。它们只是用来核对源码理解，不能替代源码阅读，也不能证明真实多 Agent、真实容器、官方 benchmark、并发持久化或长时间自治已经成立。完整 pytest 未执行，因为当前可用 Python 解释器没有安装 `pytest`，本次没有擅自改变环境安装依赖。

## 3. 仓库规模与结构

### 3.1 规模快照

**[项目事实]** 当前仓库约有 1,621 个文件，其中：

| 区域 | 数量/规模 | 说明 |
|---|---:|---|
| `loopx/` | 253 个 Python 文件，约 221,479 行 | 主控制面和能力模块 |
| `examples/` | 696 个文件 | 大量 smoke、契约示例和演示夹具 |
| `docs/` | 355 个文件 | 设计合同、运行说明和演进记录 |
| `tests/` | 44 个文件，约 16,693 行 | 约 253 个 `test_*` 函数 |
| smoke 文件 | 约 570 个 | 数量很大，但多数是窄合同验证 |
| dashboard | React/Vite 应用 | 主要页面文件体积很大 |

**[分析判断]** 文件数量和 smoke 数量不能直接代表运行时成熟度。LoopX 使用大量示例脚本表达合同，这对回归和文档化有价值，但也使“合同存在”“示例可跑”和“产品主路径真实可达”容易被混淆。分析每项能力时必须继续追踪 CLI、runtime 调用者、持久化 owner 和真实副作用。

### 3.2 主要代码层次

从源码职责看，仓库可分为：

1. **基础状态层**：`active_state.py`、`history.py`、`todos.py`、`task_leases.py`、`locking.py`；
2. **确定性控制层**：`status.py`、`quota.py`、`control_plane/work_items/interaction_contract.py`；
3. **运行胶合层**：`visible_multi_agent_launcher.py`、`worker_bridge.py`、`session_runtime.py`；
4. **可选能力层**：Explore、Auto Research、Reward Memory；
5. **迁移与投影层**：event-sourced state、migration bridge、rollout log、dashboard/API；
6. **适配与呈现层**：CLI、TerminalBench 等 adapter、React dashboard。

若按单文件规模观察，`status.py` 约 8,300 行、`benchmark.py` 约 5,462 行、`cli.py` 约 4,823 行、`quota.py` 约 3,822 行、`active_state.py` 约 3,343 行。dashboard 的 `dashboard-page.tsx` 约 7,486 行，`frontstage-page.tsx` 约 3,951 行。

**[分析判断]** LoopX 的功能广度很大，核心模块也明显趋向单文件巨型 reducer/assembler。正因为这些内部规则耦合紧密，默认把它作为一个版本锁定的完整 package 使用，通常比逐文件移植更能保留原有不变量。代价是局部 fork 的维护成本较高，因此应优先使用公开 Python/CLI 合同和外部 adapter，只对经过验证的缺口打补丁。

## 4. 真实主运行路径

### 4.1 主循环

结合 README、CLI、status、quota、interaction contract 和 history，可以还原出 LoopX 的实际控制路径：

```text
ACTIVE_GOAL_STATE.md / registry / history / optional projections
                           │
                           ▼
                    collect_status(...)
                           │
                           ▼
               build_quota_should_run(...)
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
       user_channel   agent_channel   cli_channel
        提示/审批       必须尝试动作      命令与额度规则
                           │
                           ▼
               外部 host/agent 执行一个动作
                           │
                           ▼
          证据写回 / todo 完成 / refresh / validation
                           │
                           ▼
                 验证成功后才 spend quota
```

**[项目事实]** `loopx/quota.py::build_quota_should_run` 读取目标、任务、历史、额度和可选 agent scope，输出当前运行决定。`loopx/control_plane/work_items/interaction_contract.py::build_interaction_contract` 再把决定转换成三个通道。LoopX 本身不在这里执行模型推理或工具调用。

**[分析判断]** 这是 LoopX 最清晰、最有复用价值的设计：将“该不该做”“谁应当做”“用户需要看到什么”“CLI 应如何继续”“何时允许扣额度”从 LLM 自由判断中抽离，成为可测试的确定性合同。

### 4.2 Interaction Contract

`build_interaction_contract` 根据 scoped user gate、automation prompt upgrade、user action、external evidence、autonomous replan、task orchestration、agent frontier、monitor、recovery、self-repair、delivery、blocked/wait/skip 等模式选择行为，并产生：

- `user_channel`：是否需要用户动作、是否通知、理由和可选动作；
- `agent_channel`：是否必须尝试、是否允许交付、是否为 quiet no-op、主要动作；
- `cli_channel`：下一条 CLI 动作、当前是否扣额度、验证后的扣额度策略。

**[项目事实]** 合同通常保持 `spend_now=false`，把消费放在验证写回之后；部分阻塞模式还显式设置 `do_not_cancel_on_block=true`，避免把一次阻塞误判为整个长期目标取消。

**[分析判断]** 三通道拆分比单一 `next_action` 更适合长任务：同一个状态可以要求 agent 自主恢复，同时只向用户发送低噪声通知，并给 CLI 明确的继续指令。它也为可视化控制台提供了稳定投影面。

**[接入建议]** 整体安装后应直接消费 LoopX 的 interaction contract，而不是在 Zyra 中重写一套同义 reducer。adapter 负责把 LoopX goal/todo/agent identity 关联到 Zyra run/task/permission/event identity，并让真实 worker dispatch、控制命令和 UI 消费三个 channel。若接入结果只显示 JSON、却不改变继续、阻塞、恢复或额度消费行为，仍不算有效集成。

## 5. 状态、历史、claim/lease 与事件存储

### 5.1 当前 canonical state

**[项目事实]** `ACTIVE_GOAL_STATE.md` 仍是活动目标状态的主要真相源。`loopx/active_state.py` 负责解析和写入 Markdown；`write_active_state` 直接调用文件写入，没有临时文件替换、版本比较或事务锁。

`loopx/history.py::append_run_record` 在 registry lock 下更新 registry JSON、历史和状态索引。`loopx/todos.py` 的 claim/complete 会更新 registry、追加事件/历史并更新状态。`loopx/task_leases.py` 提供较硬的 todo lease，区别于 registry 中的软 `claimed_by`。

**[分析判断]** LoopX 已经认识到 claim、lease、历史和投影需要协调，也提供了幂等与迁移保护；但多文件更新仍不是一个原子事务。进程在 registry 已写而 history/active state 未写时崩溃，可能留下需要修复的部分提交状态。

### 5.2 文件锁的 Windows 风险

**[项目事实]** `loopx/file_lock.py::file_lock` 在可用时使用 `fcntl`，否则返回 `contextlib.nullcontext()`。在当前 Windows 环境中没有 `fcntl`，因此该“锁”不会提供跨进程互斥。

`loopx/control_plane/runtime/append_only_state.py::AppendOnlyStateEventStore` 依靠该文件锁维持 JSONL sequence、hash 和 idempotency 的连续性。

**[分析判断]** 这是高优先级工程风险。在 Windows 上，多进程同时 append 可能读取同一尾部 sequence、产生冲突或交错写入。即使单进程 smoke 全部通过，也不能证明并发正确性。

### 5.3 Event store 迁移的真实状态

**[项目事实]** `loopx/control_plane/runtime/event_store_migration_bridge.py` 将 Markdown canonical state 与 event overlay 合并为投影，默认处于 shadow rollout。`build_event_store_migration_status` 明确保留 `promotion_allowed=false`，并要求 dual-read proof；event store 尚未取得 canonical owner。

**[分析判断]** 仓库已经建立从文件状态走向事件状态的迁移脚手架，但没有完成所有权切换。此时把 event-sourced 模块描述成“LoopX 已完成统一事件溯源”是不准确的。

**[接入建议]** 若选择完整 LoopX 模式，应把其 Markdown/registry/event overlay 视为 LoopX 长期控制面内部存储，而不是再复制一份到 Zyra 后双向写入。Zyra 通过稳定 identity、status/decision/outcome 和单向事件导出与其关联。需要改变既有 canonical owner 时，应先在对应执行单元明确状态域转移、恢复和回滚方案；Windows 多进程部署前必须补齐真实文件锁或限制为单写者。

## 6. Multi-Agent 与 Auto Research

### 6.1 Multi-Agent 模型

**[项目事实]** LoopX 的 multi-agent 不是一个中心 manager 永久控制所有 agent。注册 agent 被视为对等参与者；对某个有界工作包临时选择 coordinator，worker/child 回报证据，由 coordinator 验收状态和消费额度。任务协同结合 todo claim、lease、worktree、role profile 和 frontier。

**[分析判断]** 临时 coordinator 和证据验收对长期任务很合适，可以减少固定层级造成的单点瓶颈。其控制语义值得迁移，但当前实现尚不是动态异构资源 scheduler。

### 6.2 可见 Agent Launcher

关键源码位于 `loopx/visible_multi_agent_launcher.py`：

- `build_visible_multi_agent_payload_from_spec` 生成角色、lane prompt、LoopX wrapper、quota/frontier tick 和 worker skill；
- `execute_visible_multi_agent_launcher` 只实现 tmux 执行路径；
- `_start_auto_wake_loop` 启动后台唤醒调度器，读取 readiness，但不替 agent 选择 todo 或直接执行 turn；
- 每个 pane 实际运行交互式 Codex CLI TUI。

**[项目事实]** 执行依赖 `bash`、`tmux`、`chmod` 和 POSIX shell。当前 Windows 主机无法直接运行该执行路径；dry-run 只能构造命令和 artifact。launcher 还可以在显式选项下修改 Codex workspace trust 配置。

**[分析判断]** 这是“外部 Agent 的可视化编排器”，不是 LoopX 自身的 agent runtime。它能证明 LoopX 有真实 host 集成意图，但控制权、推理循环和工具执行都在 Codex pane 中。pane 存活和脚本 marker 也不是任务语义成功。

**[接入建议]** 安装整个 LoopX 不等于必须启用 tmux launcher。可以保留 launcher 源码和 CLI，但默认由 Zyra 自有 worker lifecycle、sandbox、terminal 和 scheduler 执行 LoopX 给出的角色/lane/wakeup 合同；仅在 POSIX/tmux 环境中按 feature flag 使用原 launcher。任何 workspace trust 修改都应走明确权限和审计事件。

### 6.3 Auto Research

**[项目事实]** `loopx/capabilities/auto_research/worker_runtime.py` 读取 status、quota decision 和 live projection。对于 `write_research_contract`、`propose_hypothesis`、`run_dev_eval`、`run_holdout_eval`、`write_evidence` 等动作，它返回 `manual_research_required`；真实研究证据应由可见 Codex role 编写。只有在 rollout evidence 已存在时，summary/review 等动作才能由 headless worker 继续。

`worker_loop.py` 只是重复调用 worker runtime，并在没有动作或没有已执行动作时停止。`kernel.py` 提供较轻的 hypothesis/eval 数据处理，不承担 launcher/frontier。

**[分析判断]** 这种设计主动避免 headless 流程伪造研究指标，是优点；但“Auto Research”当前更准确地说是研究工作合同与证据治理层，研究智能和主要执行仍由外部 agent 承担。

## 7. Explore：证据图、实验调度与回放

Explore 是 LoopX 中算法和实验机制最丰富的子系统，但必须区分稳定结果层与可选实验 harness。

### 7.1 结果证据层

`loopx/capabilities/explore/result_log.py` 定义公开安全的 node、edge、finding 事件，能够构建 topology、tree、Mermaid 和 focused graph。

**[项目事实]** 事件以 append-only JSONL 写入；`append_explore_result_event` 没有文件锁或 `fsync`。加载器遇到 JSON 解码失败或无效记录时会跳过该行。投影按 `result_id` 取最新事件。

**[分析判断]** 数据模型和公开安全校验可复用，但当前持久化不适合多 agent 并发写入。静默跳过损坏记录还可能把证据丢失伪装为“没有结果”，不利于审计。

**[接入建议]** 整包接入时可以直接保留 Explore 的 node/edge/finding、拓扑投影和 public-safe guard。若启用多 agent 并发写结果层，应先为原 JSONL writer 增加可用的跨进程锁和损坏记录告警/quarantine；也可以在不改算法的前提下，通过一个明确的 storage adapter 将 Explore 事件导出到 Zyra。不要同时保留两个可写结果 owner。

### 7.2 Speculative Scheduler

`loopx/capabilities/explore/speculative_scheduler.py` 包含：

- 基于并行负载的吞吐折减 `1 / (1 + load * (k - 1)^1.18)`；
- 带 shrinkage 的负载校准；
- LoopX 自有的 survival-product confidence prefix；
- 在首个低于阈值项处截断的 DSpark-faithful bundle prefix；
- 对独立 lane 使用期望证据价值与吞吐的组合，并记录 opportunistic lane 审计。

**[项目事实]** 模块自身明确区分 LoopX-specific 和 DSpark-faithful 公式，没有把两者混为一谈。

**[分析判断]** 这是一个值得保留的优点：公式来源、变体和适用范围被编码成不同函数，而非在文档中模糊描述。当前问题是调度结果主要用于实验 harness 的排序与选择，还没有取得真实资源分配权。

### 7.3 Router State

`loopx/capabilities/explore/router_state.py` 为不同 family 维护 raw value rate、duration、accept 和 infra failure EMA，并维护全局 novelty ledger。UCB、coverage bonus、coverage/novelty bias 和 infra penalty 只改变选择排序；无偏 value 仍用于 admission 和收益核算。

**[分析判断]** “带偏置探索、无偏核算”的分离很适合赛题中的动态稀疏拓扑与消融设计。迁移后必须让排序实际改变 worker/route，并从真实 canonical outcome 更新 EMA；若只输出建议分数或 UI 字段，不构成 scheduler 内化。

### 7.4 Harness Runtime 与 Checkpoint

`loopx/capabilities/explore/harness_runtime.py` 使用 thread pool 和共享队列，支持 concurrency key、episode grouping、checkpoint/resume、首见 novelty ledger 和 router planning。variant catalog 由外部 agent 提供，harness 只生成执行请求，不自行调用 LLM 或编造变体。

`loopx/capabilities/explore/harness_checkpoint.py` 采用临时文件替换，校验 runtime signature、history、novelty 和 router state。

**[项目事实]** checkpoint 使用 replace，但没有对文件和目录执行 `fsync`。因此它能降低部分写入风险，却不能给出严格的断电持久性保证。retry/backoff/cooldown 多数仍是 runner 指导，而不是 harness 强制执行的状态机。

**[分析判断]** 该 harness 是很好的算法试验台，但不是完整分布式调度 runtime。资源 capacity 来自调用参数，不是持久化权威；失败恢复也没有统一接入 worker lease、permission 和 side-effect fence。

### 7.5 Trace、Replay 与 Counterfactual

`trace_runtime.py` 提供线程安全的内存 append-only `TraceLog`，带连续 parent/sequence 和 public-safety freezing。

`replay_runtime.py` 的主要机制包括：

- exact、semantic、best-effort、non-replayable fidelity；
- adapter protocol 和 opaque state handle；
- trace cursor、agent state、adapter state 的原子绑定；
- capture 失败补偿、锁、release/quarantine；
- restore 后的 equivalence 验证；
- exact 模式下 digest 对齐，semantic 模式下显式等价检查；
- isolated child task 与风险拦截。

`counterfactual_runtime.py` 把 baseline failed case 作为 fix set、baseline passed case 作为 guard set。只有 replay equivalence 可信、所有 fix 通过、guard 不退化时才允许 promotion；best-effort replay 只能进入 observed-only。

**[分析判断]** 这是 LoopX 最有研究价值的机制之一，尤其适合作为 checkpoint/restore、故障恢复和方案晋升的补充验证来源。但 `ReplayPointRegistry`、counterfactual 和 adaptive replay 目前主要由自身模块和 pytest 调用，未发现进入 CLI、主 control plane 或持久化产品路径的调用者。`TraceLog` 和 registry 也是进程内状态。

**[接入建议]** Replay/Counterfactual 源码可随整个 package 保留，但默认关闭。启用前通过 adapter 把 replay point identity、adapter state、trace cursor、side-effect fence 和 exact-resume 关联到真实 Zyra worker/checkpoint，并运行 interruption/resume。只有测试夹具构造 registry 并 replay，不能视为主路径完成。

## 8. Worker Bridge、Session Runtime、Reward Memory 与 Dashboard

### 8.1 Worker Bridge

`loopx/worker_bridge.py` 提供 runner-agnostic 合同：只读 source/runtime mount、Python path/command prefix、worker log artifact 和紧凑 outcome/benchmark record。

**[项目事实]** `build_worker_bridge_benchmark_run` 会明确写入：

- `case_semantics_changed_by_harness=True`；
- `loopx_inside_case=True`；
- `official_score_comparable_to_native_codex=False`；
- `model_plus_harness_pair=True`；
- `control_plane_score_applicable=True`。

**[分析判断]** 这体现了较好的 benchmark 诚实性：bridge connectivity 与 control-plane 指标不能冒充官方 case success 或 leaderboard 分数。现有 smoke 主要验证 builder、CLI 和 TerminalBench 命令组合，没有运行真实容器、模型或官方评测。

**[接入建议]** 直接使用完整 Worker Bridge 合同，并让 adapter 将 benchmark provenance、comparability guard 和 outcome 写入 Zyra artifact/evaluation schema。mount/command 的连通性仍不能代替真实 case success。

### 8.2 Session Runtime

`loopx/session_runtime.py` 是只读投影构建器，输入 compact session、event、outcome、gate、artifact 和 decision result，输出 first screen、attention item、lane 和 source references。它会标记看起来像原始敏感数据的 key 名，并避免复制其值。

**[分析判断]** 这是 presentation read model，不是 live session owner。它没有 host connector、写回、启动、interrupt/resume 或 durable session lifecycle。可复用其紧凑 projection 和 public-safe 策略，不应把它当成完整 session runtime。

### 8.3 Reward Memory

设计文档把记忆分为 run reward、hard policy、soft preference、procedural experience 和 working context，并把 authority 与 confidence/content 分离。

`candidate_review.py` 提供无状态 candidate、guard 和 review：hard policy 必须验证相同 actor、role、project 和 scope；accept 会产生 active record 和下一持久化步骤，但不自行写 provider。`registry.py` 是 provider-owned corpus 的只读 registry；`health.py` 确定性地区分 wrong project/surface、unavailable、empty、stale、index、retrieval、readback 和 applied。

**[项目事实]** 当前处于 Stage 1/2。CLI 即使执行 `candidate-review --decision accept`，也会明确返回 `provider_write_performed=false`、`external_writes=false`，下一步由 caller 持久化。没有 live provider write、跨模块 recall、正式 eval 或 rollout。

**[分析判断]** Reward Memory 的价值主要是记忆晋升权限规则和健康诊断分类，而不是现成 memory fabric。

**[接入建议]** Reward Memory 模块可以随包安装并直接使用其 guard 和 health taxonomy，但 Stage 1/2 仍不执行 provider write。由一个 adapter 把 accepted candidate 提交给既有 memory provider，并回写真实 mutation/readback evidence；不能把 LoopX 返回的 `status=active` 当成已经持久化并影响后续决策。

### 8.4 Dashboard

dashboard 是 React/Vite operator preview。README 明确 CLI、status、history 和 active state 才是真相源。public showcase 使用静态数据，ops mode 需要显式 loopback status URL。默认只读；只有本地 status server 显式启用 `--enable-reward-write-api` 时，才开放窄域 reward dry-run/append API，并受 preview id 限制。

**[分析判断]** 它是本地 operator preview，不是远程、多租户控制台。页面文件体积过大，状态与视图职责集中，不适合整体移植。值得参考的是 public/ops 分离、默认只读、窄写 API 显式启用，而不是组件源码本身。

## 9. 项目强项

### 9.1 确定性控制面优先

**[分析判断]** LoopX 没有让 LLM 决定自己是否有权限、是否扣额度或是否已经完成长期目标。quota、interaction、claim/lease 和 validation 都能被独立测试，这比将控制规则藏在 prompt 中更可靠。

### 9.2 证据和消费绑定

**[项目事实]** spend-after-validation、worker outcome、benchmark comparability、auto-research 禁止伪造指标、counterfactual guard set 都在不同层面坚持“先有可验证证据，再接受状态或消费资源”。

**[分析判断]** 这与长程任务最常见的问题直接对应：重复消耗、虚假完成、失败结果污染记忆，以及实验变体修复一个 case 却破坏既有通过 case。

### 9.3 公开/私有边界意识强

**[项目事实]** session、trace、result log 和 dashboard 多处拒绝原始日志、完整 transcript、凭据和本地路径进入公开投影。

**[分析判断]** 虽然规则仍以字段检查和调用纪律为主，但“内部运行证据”和“公开安全投影”作为不同数据面是正确方向。

### 9.4 对实验机制的适用范围较诚实

**[项目事实]** 仓库会显式区分 shadow 与 canonical、dry-run 与 execute、control-plane score 与 official score、best-effort 与 exact replay、LoopX-specific 与 DSpark-faithful 公式。

**[分析判断]** 这些边界说明比单纯追求“功能已存在”更有工程价值，也便于迁移时设置门禁。

## 10. 局限与风险

### 10.1 高优先级风险

| 风险 | 源码事实 | 后果 |
|---|---|---|
| Windows 文件锁退化为空操作 | `loopx/file_lock.py::file_lock` | 多进程 JSONL/registry 写入缺少互斥 |
| canonical state 分散 | Markdown、registry、history、event overlay 并存 | 崩溃可能留下部分提交和投影分歧 |
| event store 尚未 promotion | migration bridge 默认 shadow，`promotion_allowed=false` | 不能宣称统一事件真相源已经完成 |
| 关键实验机制不可从主路径触发 | Replay/Counterfactual 主要仅被模块测试调用 | 代码存在不等于产品能力 |
| launcher 平台限制 | execute 仅 tmux/POSIX | Windows 和非 tmux host 只能 dry-run |

### 10.2 中优先级风险

1. Explore result JSONL 无锁、无 fsync，读取损坏行时静默跳过；
2. 多个核心文件达到数千行，分支规则集中，变更影响面和审查成本高；
3. Explore scheduler 主要是 advisory/实验排序，并未拥有真实 worker resource capacity；
4. Session Runtime 是投影层，Reward Memory 是无状态合同，却容易被命名理解成完整 runtime；
5. 大量 smoke 主要验证 schema、builder、fixture 和 synthetic adapter，与真实外部依赖之间仍有显著距离；
6. launcher 的 workspace trust 修改属于外部配置副作用，需要更强权限治理；
7. checkpoint replace 没有完整 durable-write 序列，不能仅凭“atomic replace”推导断电安全。

### 10.3 规模与可维护性

**[分析判断]** `status.py`、`quota.py`、`cli.py`、`active_state.py` 和 dashboard 页面已经承担较多装配职责。逐文件复制后再由 Zyra 维护，会同时继承这些内部耦合；采用上游 package 加窄 adapter，反而能把升级和回归边界集中起来。只有当局部缺陷无法通过配置、公开扩展点或 adapter 解决时，才对具体模块维护可追溯 patch，避免形成长期分叉。

## 11. 实际验证结果及其正确解释

### 11.1 已运行命令

以下 11 个零依赖示例脚本在 Python 3.13 下返回成功：

1. `examples/worker-bridge-install-contract-smoke.py`
2. `examples/session_runtime/session-runtime-readonly-projection-smoke.py`
3. `examples/explore-result-layer-smoke.py`
4. `examples/explore-harness-runtime-resume-smoke.py`
5. `examples/auto-research-layered-e2e-acceptance-smoke.py`
6. `examples/long-horizon-self-iteration-rollout-fixture-smoke.py`
7. `examples/reward-memory-architecture-smoke.py`
8. `examples/reward-memory-corpus-registry-smoke.py`
9. `examples/reward-memory-candidate-review-smoke.py`
10. `examples/control_plane/event-sourced-state-contract-smoke.py`
11. `examples/control_plane/event-sourced-status-read-path-smoke.py`

此外，`py -3.13 -m loopx.cli --help`、Reward Memory candidate-review CLI 和 Auto Research 初始合同 CLI 可正常执行。

### 11.2 它们证明了什么

**[项目事实]** 这些脚本证明选取的模块在当前解释器下可以导入和执行对应的窄合同；builder、投影、合成 resume、无状态 review 和部分 event read path 没有立即失败。

### 11.3 它们没有证明什么

这些结果没有证明：

- tmux/Codex visible launcher 在当前 Windows 主机真实运行；
- Auto Research 完成一次真实 dev/holdout 研究；
- worker bridge 运行真实容器、模型或官方 benchmark；
- Explore 在多个真实 worker 间动态调度资源；
- event store 在多进程并发和崩溃中保持原子性；
- Replay/Counterfactual 已进入产品主路径；
- Reward Memory 已持久化并影响后续召回；
- dashboard 已成为远程、多租户运行控制台。

尤其是 Auto Research acceptance smoke 明确断言：没有可见 role 参与时，headless loop 不得生成 dev/holdout 指标。这个测试证明的是“拒绝伪造证据”，不是“自动研究已经执行成功”。

### 11.4 未执行项

尝试执行针对 event-store migration、quota slot accounting、task lease、import boundary、Explore episode/router/replay/counterfactual 的 pytest 子集时，当前 Python 环境报告没有安装 `pytest`。因此不能把 pytest 测试源码的断言与“本机已运行通过”混为一谈。

## 12. 整体安装与最小适配建议

### 12.1 默认采用方式

**[接入建议]** LoopX 的默认采用方式调整为 `whole-package integration`：保持 `loopx/` Python package、CLI、内部 reducer、状态管理和能力模块的整体性，以锁定版本安装到 Zyra 的正式运行环境，通过公开 Python API 或 JSON CLI 使用。除非实际缺陷迫使修改，不预先把 `quota.py`、`status.py`、`todos.py`、Explore、Reward Memory 等拆成 Zyra 自己的一套同义实现。

这与把来源仓库当成“待抽取素材池”不同。LoopX 在这里应被看作一个有独立产品边界的上游控制面模块：

- 上游内部调用图和不变量继续由 LoopX 维护；
- Zyra 只维护 integration adapter、identity correlation、事件/产物映射、配置和必要 patch；
- 上游升级先运行 LoopX 自身测试及 Zyra integration tests，再更新锁定版本；
- 若某项能力尚不成熟，通过 feature flag 关闭该能力，而不是为此拆散整个 package。

### 12.2 “整个安装”包括什么

| 内容 | 默认处理 | 说明 |
|---|---|---|
| `loopx/` 主 Python package | 整体安装并锁定版本 | 保留 status、quota、todo、history、interaction、Explore 等内部结构 |
| LoopX CLI | 保留 | 适合作为运维、诊断和兼容入口；主路径可优先使用 Python API |
| `tests/` | 作为上游回归套件保留在源码/构建阶段 | 不进入产品运行时，但升级前必须可运行 |
| `examples/`、`docs/` | 作为使用和行为证据 | 不需要打入最小生产镜像，也不计运行能力 |
| Dashboard | 可选安装 | 默认不替代 Zyra 产品 UI；可作为本地诊断/开发视图 |
| tmux/Codex launcher | 可选、默认关闭 | 仅在 POSIX/tmux 环境并明确选择时启用 |
| Explore harness、Replay/Counterfactual | 随包保留、默认关闭 | 通过实验开关逐项启用和验证 |
| Reward Memory Stage 1/2 | 随包保留 | 当前只使用 review/health 合同，provider write 需 adapter 完成 |

“整个安装”指完整采用其可发布 package 和内部控制逻辑，不意味着生产环境必须默认启动仓库中的每个演示、dashboard 或实验功能。

### 12.3 推荐的状态域分工

为了让整体接入不变成双重控制，建议一次性明确状态 owner：

| 状态域/责任 | 推荐 primary owner | 集成方式 |
|---|---|---|
| 长期 goal、LoopX todo、claim/lease、quota、validation、history | LoopX | 由 LoopX API/CLI 原生读写；Zyra 不并行修改同义字段 |
| LLM/session/tool loop、permission、sandbox、worker lifecycle | Zyra | LoopX decision 作为约束输入，执行结果作为 outcome 回传 |
| 全局资源 placement 与动态拓扑 | Zyra scheduler | 可消费 LoopX/Explore 建议，但实际 dispatch 和 capacity 由 Zyra 持有 |
| artifact、tool result、公开产品事件 | Zyra | adapter 保存 LoopX identity、decision reason 和 source reference |
| LoopX 内部 Markdown/registry/event overlay | LoopX | 视为该模块内部存储；不得由 Zyra 双向编辑 |
| 跨系统因果追踪 | Zyra integration event + LoopX identity | 投影/关联，不形成第二个 LoopX 状态写 owner |

如果既有 Zyra 状态已经覆盖 LoopX goal/todo/quota 域，启用完整模式前必须做一次明确的 owner 转移决策：确定迁移起点、ID 映射、只写方、回滚方法和历史导入。不能在两个系统之间持续双向同步同义状态。

### 12.4 最小 adapter 的职责

整体安装仍需要一个窄而正式的 Zyra adapter，但它不复制 LoopX 控制流。adapter 只负责：

1. 将 Zyra `run_id`、`task_id`、`agent_id`、actor/role 映射为稳定 LoopX identity；
2. 启动或恢复对应 LoopX workspace/goal；
3. 调用 status、should-run、interaction contract、claim/complete、refresh 等公开入口；
4. 把 `agent_channel` 交给真实 worker，把 `user_channel` 交给 API/UI，把 `cli_channel` 转为控制命令；
5. 将真实 tool、artifact、verification 和 worker outcome 回传给 LoopX；
6. 把 LoopX decision/history 的引用写入 Zyra 因果轨迹，而不是复制并重写其内部状态；
7. 将 LoopX 异常转为明确的 retry、blocked、degraded 或 operator-visible 状态，不能静默 fallback。

### 12.5 局部调整的优先级

只有以下问题在目标部署中真实出现时，才建议维护局部 patch 或小型 fork：

| 优先级 | 局部调整 | 原因 |
|---|---|---|
| P0 | Windows 跨进程文件锁，或强制单写者部署 | 当前 `file_lock` 在 Windows 退化为空操作 |
| P0 | goal/todo/quota 状态 owner 和 crash reconciliation | 防止 Zyra 与 LoopX 双写、部分提交或恢复分歧 |
| P1 | identity、event、artifact 和 outcome adapter | 建立可审计的跨模块因果链 |
| P1 | 配置路径、workspace 隔离和清理策略 | 避免任务间状态串扰和本地绝对路径泄漏 |
| P1 | Explore result 坏行告警、并发 append 或 storage adapter | 原结果层无锁且会静默跳过坏行 |
| P2 | Dashboard/API/launcher 的产品适配 | 只在决定启用对应可选功能后处理 |

不建议为了统一语言、目录风格或有效代码行数而修改 LoopX 内部算法。patch 应记录上游 commit、修改理由、行为差异、回归测试和未来是否可删除。

### 12.6 依赖与交付形式

正式交付可以选择锁定 wheel/sdist、源码构建包或 Zyra 维护的轻量 fork，但必须满足：

- 版本或 commit 固定，可在 clean environment 重建；
- 保留 MIT license、来源和本地 patch 清单；
- 不依赖 `G:\agent-zoo\long-horizon-systems\loopx` 或 `../loopx` 运行；
- 不依赖开发机缓存、未声明环境变量或残留 workspace；
- 上游 package、Zyra adapter 和本地 patch 分层清楚。

**[分析判断]** 这种方式属于“完整上游模块集成”，不是“把上游源码深度内化为 Zyra-owned 实现”。它可以是合理的产品架构选择，但上游原始行数不能当作 Zyra 深度内化新增代码；应以真实功能、集成代码、状态接管、故障处理和行为测试评价。

## 13. 面向 Zyra 第二阶段的整体接入方案

### 13.1 阶段 A：可重复安装与上游基线

在干净环境中从锁定版本安装完整 LoopX package，先建立未经本地修改的基线：

- Python 版本、package metadata、CLI 和导入路径固定；
- 安装 test extras 并运行与接入路径相关的完整 pytest，而不只运行 smoke；
- 保存上游 commit、配置默认值和功能开关清单；
- 验证单写者状态读写、init/status/should-run/refresh/history 基本链路；
- 明确 Linux/POSIX 与 Windows 的支持矩阵。

这个阶段不抽取源码，也不启用全部实验能力。目标是证明“完整包可以被稳定安装和升级”。

### 13.2 阶段 B：接入一个真实 Agent 主路径

建立 `LoopXControlPlaneAdapter` 一类的正式边界，把一个真实 Zyra task/run 接到 LoopX：

1. 创建或恢复 LoopX goal；
2. LoopX 计算 should-run 和 interaction contract；
3. Zyra worker 执行一个真实 tool/reasoning action；
4. adapter 回传 artifact、verification 和 outcome；
5. LoopX refresh/complete，并仅在验证后 spend；
6. Zyra UI/API 展示 LoopX reason、channel 和 history reference。

这一阶段的通过条件不是“CLI 能打印 JSON”，而是关闭 LoopX adapter 后，长期任务的继续、阻塞、恢复、claim/lease 或额度消费行为会实际改变。

### 13.3 阶段 C：状态 owner 切换与恢复验证

若决定让 LoopX 正式拥有长期控制面状态，应在一个受控切换点完成：

- 冻结 Zyra 对同义 goal/todo/quota/lease 字段的写入；
- 导入或关联未完成任务和历史 identity；
- 设定 LoopX workspace 的生命周期、备份和清理规则；
- 验证进程中断、重复 outcome、过期 lease、部分写入和 restart；
- Zyra event log 记录 owner 切换与跨域引用，但不反向改写 LoopX 内部文件；
- 准备可执行回滚方案，而不是长期双写。

该步骤改变 canonical owner 或事务/lease/restore 语义时，应按 Zyra 对应执行单元的高风险门禁处理。

### 13.4 阶段 D：按需启用可选能力

完整 package 已经包含可选能力，因此这里是“启用和集成”，不是“拆出并重写”：

- **Explore result layer**：先启用公开安全结果和拓扑投影；并发前处理 JSONL 锁和坏行告警；
- **Speculative scheduler/router**：保持 experimental，只有实际改变 Zyra worker/route 并完成基线消融后才提升；
- **Replay/Counterfactual**：接入真实 checkpoint、adapter state 和 side-effect fence 后启用；
- **Reward Memory**：先启用 review/health，provider write/readback 仍由 adapter 完成；
- **Worker Bridge**：保留 comparability guard，运行真实容器/模型后才报告 case success；
- **Dashboard/tmux launcher**：按部署环境选择，不作为完整安装成功的必要条件。

### 13.5 阶段 E：升级、patch 与故障隔离

建立长期维护流程：

- 上游版本更新先在隔离环境运行 upstream tests、Zyra integration tests 和 owner/recovery tests；
- 所有本地 patch 保持小而独立，可重放、可删除、有行为测试；
- LoopX 不可用时进入明确 degraded/blocked/recovery，不允许静默切换到一套语义不同的 Zyra 旁路 reducer；
- 对 package import、CLI subprocess、workspace I/O 和可选 dashboard/launcher 分别设置超时与故障边界；
- 监控 version、workspace、goal、todo、decision 和 outcome correlation，支持问题回溯。

## 14. 推荐的整体集成门禁

### 14.1 安装与供应链

1. 能否从锁定版本在 clean environment 完整安装，不读取工作区外的 LoopX 源码？
2. package、license、来源 commit、本地 patch 和构建产物是否可追溯？
3. 生产镜像是否只包含需要的运行内容，而不是依赖 examples、测试缓存或开发 workspace？

### 14.2 状态所有权

1. LoopX 与 Zyra 分别写哪些状态域，是否存在同义字段双写？
2. goal/todo/quota/claim/lease 的 primary owner 是否唯一？
3. owner 切换、备份、restart、部分提交和回滚是否经过真实验证？
4. LoopX 内部文件与 Zyra 事件之间是引用/投影关系，还是危险的双向同步？

### 14.3 真实行为接入

1. LoopX decision 是否从真实 Agent run 动态可达？
2. `agent_channel` 是否真实影响 worker 行为，`user_channel` 是否进入 API/UI，`cli_channel` 是否影响控制命令？
3. validation 失败是否真实阻止 spend，claim/lease 是否真实阻止重复 worker？
4. 禁用 adapter 后，长期任务控制行为是否明显变化？

### 14.4 并发、持久化与恢复

1. 目标平台上的文件锁是否真实有效，或部署是否保证单写者？
2. 多进程 claim、JSONL append、history/registry 更新是否经过竞争测试？
3. 崩溃发生在多文件更新中间时，系统能否检测并 reconcile？
4. retry、重复 outcome 和 restart 是否具有幂等 fence？

### 14.5 可选能力诚实性

1. dry-run、smoke、synthetic adapter 和真实执行是否在证据中明确区分？
2. Explore、Replay、Counterfactual 和 Reward Memory 未完成主路径接入前是否默认关闭或标记 experimental？
3. Worker Bridge 是否继续区分 control-plane score、harness score 和 official comparable score？
4. tmux launcher、dashboard 和 provider write 是否只在环境和权限满足时启用？

### 14.6 维护与竞赛口径

1. 上游升级是否可以在不修改 Zyra 主运行时的情况下回归和回滚？
2. 本地 patch 是否保持最小化，并有上游版本对应关系？
3. 完整安装是否被如实记录为 dependency/module integration，而不是宣称成 Zyra 自研或深度内化行数？
4. 是否仍能提供真实 live task、canonical transition、动态 route、恢复和可视化因果证据，而不是用 LoopX 文件数量或 smoke 数量替代？

## 15. 最终评价

**[分析判断]** LoopX 是一个适合完整安装的独立长任务控制面产品，而不仅是一个等待抽取算法的源码仓库。它已经把 status、quota、todo、claim/lease、history、interaction contract、CLI 和多种可选能力组织为一个相互依赖的 package；逐模块重写不仅成本高，也容易破坏验证后消费、阻塞恢复和状态投影之间的原有不变量。

因此，对 Zyra 第二阶段的首选裁决调整为：

- **采用方式：完整、版本锁定地安装 LoopX package；**
- **产品角色：Zyra Agent Runtime 外围的长期任务 control-plane module；**
- **状态角色：在明确划定的 goal/todo/quota/claim/lease/history 域内，可以由 LoopX 担任 primary owner；**
- **集成方式：使用窄 adapter 连接 Zyra identity、worker、permission、artifact、event 和 UI，不复制 LoopX reducer；**
- **修改策略：优先配置和 adapter，其次是有测试、可追溯的局部 patch，最后才考虑长期 fork；**
- **实验能力：随包保留，但 Explore scheduler、Replay/Counterfactual、Reward Memory provider write 和 tmux launcher 按验证结果逐项启用；**
- **非目标：LoopX 不替代 Zyra 的模型推理、tool loop、sandbox、permission runtime、worker runtime 或全局异构资源 scheduler。**

需要同时诚实记录：整体安装是一种完整模块集成，不会自动变成 Zyra-owned 深度内化，也不能用上游源码行数替代赛题行为证据。但如果目标是尽快获得一套连贯、可升级的长任务控制面，保持 LoopX 整体并做少量本地适配，比预先把它拆成多个 Zyra 重写模块更符合该项目 README 的使用方式，也更符合本次采用偏好。
