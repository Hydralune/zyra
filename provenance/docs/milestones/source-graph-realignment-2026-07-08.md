# Source Graph Realignment After Detailed Repository Reading

日期：2026-07-08

## 范围与保护边界

本文件记录在逐一详细阅读 `claude-code-best`、`agent-framework`、`agentscope`、`browser-use`、`hermes-agent`、`langgraph`、`openclaw`、`OpenHands`、`opencode` 后，对当前 milestone 文档的重排和补强裁决。

## 2026-07-20 OpenClaw 前向排除（最高优先级补充）

- `M1-S05D-02` 及此前的 OpenClaw 实现、执行文档、账本、验证和历史来源事实保持不变。
- 从 `M1-S06A-01` 起，OpenClaw 为 `excluded_forward_only`；后续 M1、全部 M2 和全部 M3 不再把它列为 primary、supplementary、conformance、reference、experimental 或 deferred 来源，也不再要求源码阅读、迁移、适配或对照测试。
- 本文 2026-07-08、07-10、07-13 各节中与未来 06A 及之后单元、M2/M3 有关的 OpenClaw 候选角色均被本节覆盖；其它来源角色与 Zyra-owned 能力责任保持不变。
- M3 只保留既有 OpenClaw-derived 代码的历史 provenance、许可证和无根目录运行依赖审计。该审计不构成继续参考，且不得恢复已删除的根目录仓库或 source graph。
- 取消该来源不降低任何父级/切片预算、主路径、状态 owner、行为测试、失败恢复或赛题证据门禁。

## 2026-07-13 LangGraph 前向纠偏

LangGraph 的当前角色以本节和 `source-graphs/langgraph/source-graph.md` 第 12 节为准。本文保留 2026-07-08 的源码阅读和来源重排事实，但凡把 `StateGraph -> channel/reducer -> Pregel -> stream/ToolNode/SDK` 整条链设为 Zyra production primary 的旧结论均已失效。前向执行必须拆成两个状态域：

- **动态 topology / graph custody**：primary 是 Zyra-owned `DynamicTopologyRuntime` + immutable `GraphStateCustody`；LangGraph StateGraph/channel/Pregel 只做 reference/conformance。
- **checkpoint commit / exact resume**：LangGraph 只在 identity/namespace/lineage、pending/committed writes、stable ids、atomic commit、interrupt/resume correlation 和 exact-resume 测试语义中作为窄域 primary semantic source；production owner 是 Zyra `GraphCommitRuntime` / `CheckpointRecoveryRuntime`。

同时强制 branch-local delta、显式 write-set、冲突检测、确定性 commit 和 side-effect fence；禁止节点原地修改共享 state。Claude-derived CodeWorker query/tool/observe/revise 循环保持内聚，不能被拆成 LangGraph 微节点。该纠偏不降低任何能力、父级有效代码或赛题证据门禁。

对本轮三项批评的正式判断也收敛在这里：所谓“closed loop”不是准确术语，因为 LangGraph 能调用工具、读取环境并条件路由；真正成立的是 compile 前预声明 node/edge/state/target 容易形成封闭世界，不能冒充运行时可增删改 node/edge/role/capability 的开放拓扑。并行共享 state/channel/reducer 的可变别名、非确定合并和完成顺序污染风险属实，因此不得迁入 canonical state。逻辑抽离本身不是反模式，但把连续的 `reason -> tool -> observe -> revise` 高内聚循环拆成共享状态微节点属于明确禁止项；图层只接粗粒度 durable boundary。

保护范围和下一执行入口不再写死在本文，以 `docs/milestones/execution-state.yaml` 为准。2026-07-10 校准时已完成并复审到 `slice-02d-02-context-compact-codeworker-api-integration.md`，后续从 `slice-03a-01-permission-runtime-foundation.md` 继续；已完成文档不得为倒填新规划而回写。

本轮重排采用语言中立的严格内化定义：来源仓库可作为原语言源码迁移、成熟机制、contract 或 reference 来源；最终源码、构建、状态、事件、权限、错误处理、测试与维护责任必须落到 `zyra/apps/**`、`zyra/packages/**`、`zyra/skills/**`、`zyra/scripts/**` 等正式边界，不能依赖根目录来源仓库。正式纳入 Zyra 的 TypeScript/Bun/Node、Python、Rust/native package 均可成为 production owner；禁止的是未迁入源码的上游黑箱，不是多语言或跨进程本身。

## 总体裁决

`claude-code-best` 仍是 CodeWorker runtime、Query/session/tool loop、permission、MCP、SkillTool、AgentTool、compact/control command 的主源码迁移来源；这些复杂高内聚主链默认保留 TypeScript 原语言，裁剪进入 Zyra 正式 TypeScript package，并通过 typed state/event/permission/artifact/checkpoint contract 接入 Python 控制平面。该既定边界直接约束后续执行；局部边界适配由执行 agent 自主裁决并记录。若执行中发现单项或累计改动将整体改写 primary 主链、转移 canonical owner 或改变 runtime custody/restore 语义，实施该边界前应合并为一次 unit 级决策记录并按现有权威规则处理，不受影响的工作继续推进；只有不存在合规路径且必须推翻用户明确指定的 primary 边界时才在 unit 检查点集中升级一次，slice 内不得逐模块反复请示。它不应被 `opencode` 替代，但需要用 `opencode` 的 durable session/event、typed protocol、provider/catalog、Web/TUI 控制台和 productized session UI 经验补强后续 M1/M2/M3 文档。

`opencode` 是本轮新增的高价值 TypeScript 来源；其 durable session/event、provider/catalog、protocol 和 Web/TUI 状态流在被裁决为 primary/supplementary 时默认保留 TypeScript，不应整体 Python 化。当前旧 milestone 文档没有覆盖它，因此从 M1-02C 以后、M2 全部父级单元、M3 全部父级单元都必须显式裁决 `opencode` 的来源状态。重点不是整仓迁移，而是吸收以下链路：`packages/opencode/src/session/**`、`packages/opencode/src/tool/**`、`packages/opencode/src/permission/**`、`packages/opencode/src/mcp/**`、`packages/opencode/src/skill/**`、`packages/opencode/src/command/**`、`packages/opencode/src/plugin/**`、`packages/protocol/**`、`packages/app/**`、`packages/tui/**`、`packages/web/**` 中可拆解的 session/event/protocol/UI/permission/terminal/timeline 模式。

`browser-use` 继续作为 BrowserWorker 的主来源，重点落在 BrowserSession、CDP lifecycle、DOM serializer、action registry、sensitive data/file rules、message/history compression、watchdogs、trace artifacts。

`OpenHands` 不适合作为 QueryEngine 主体来源，但适合作为外部 agent server、sandbox/conversation/event persistence、browser/terminal/files/diff UI、演示控制台和运行状态聚合来源。

`agent-framework` 的价值主要在 typed Agent/Client/Tool/Skill/MCP contract、middleware/session/compaction/evaluation、workflow/checkpoint/HITL、AG-UI event contract 和 durable hosting。受保护早期选择保留；自 04B-02 向后它只作为 workflow/checkpoint/AG-UI 等 conformance/reference 来源，不新增 production adapter/runtime 配额。

`agentscope` 的候选价值覆盖 Python runtime、FastAPI app/ChatService、ReAct loop、Toolkit/PermissionEngine、MCP/skill/workspace/RAG/session/message bus；当前 active 责任只限 05A workspace supplement、06A RAG/KB primary、07A worker lifecycle primary，其余模式做 conformance/reference。

`hermes-agent` 的价值主要在长会话 runtime、SessionDB、compaction/memory、approval/gateway、slash control、skills lifecycle、delegation/cron/kanban、JSON-RPC control plane。应裁剪吸收机制，避免迁移大文件黑箱。

`langgraph` 只在 07C checkpoint commit/exact-resume 恢复合同中作为窄域 primary semantic source；07A 动态 topology/graph custody 的 production primary 改为 Zyra-owned runtime。StateGraph/channel/Pregel/streaming/prebuilt/Store/SDK 不取得 store/controller/scheduler/UI owner。

`openclaw` 只做轻量来源：gateway/control plane、plugin registry、tool policy、subagent/task runtime、memory plugin、streaming/channel delivery。它不应承担重型 runtime 主体。

## M1 后续单元调整

下表保留 2026-07-08 当时的候选来源重排历史，不是当前 active backlog。当前完成状态以 `execution-state.yaml` 为准；2026-07-10 校准用于 owner/maturity，04B-02 以后的生产实现角色最终以本文末尾 2026-07-13 表为准。

| 单元范围 | 原主线 | 详细阅读后的补强裁决 |
| --- | --- | --- |
| M1-02C tool loop/result budget | `claude-code-best` tool registry/result budget | 增加 `opencode` tool registry、tool result shaping、permission question、session event append；用 `hermes-agent` tool approval/search 作为辅助来源。 |
| M1-02D context/compact/API | Claude compact/session/API | 增加 `opencode` system context/context epoch/session event model、V2 Catalog/Integration/Credential/AISDK hook、V1 provider compatibility quirks、`hermes-agent` SessionDB/compaction/provider resolver、`agent-framework` session/compaction/provider contract。 |
| M1-03A permission | Claude permission runtime | 增加 `opencode` permission/question/approval path、`agentscope` PermissionEngine、`openclaw` tool policy；必须证明 approval 真实影响 tool/browser action。 |
| M1-03B MCP | Claude MCP client | 增加 `opencode` MCP service/tool/resource/prompt handling、`agent-framework` MCP tool contract、`agentscope` MCP server/tool use；保留 Claude 为主。 |
| M1-03C skill runtime | Claude SkillTool/Markdown skills | 增加 `opencode` skill loader/command/plugin relation、`hermes-agent` skills lifecycle、`agent-framework` skills contract。 |
| M1-03D subagent/commands | Claude AgentTool/commands | 增加 `opencode` Task tool/background job/command/plugin host、`hermes-agent` delegation/kanban/cron、`agentscope` subagent/HITL。 |
| M1-04A-D browser worker | browser-use | 增加 OpenHands browser/terminal artifact UI contract、`opencode` terminal/session event projection；browser-use 仍为主来源。 |
| M1-05A-D workspace/sandbox/event/failover | OpenHands/AgentScope/OpenClaw | 增加 `opencode` worktree/location/control-plane/session event/provider catalog、`hermes-agent` gateway/JSON-RPC/provider runtime、`agentscope` provider/formatter/credential factory、`langgraph` stream/checkpoint coupling。 |
| M1-06A-C memory/retrieval/compact restore | LangGraph/Claude/Hermes/AgentScope | 增加 `opencode` system context/session memory hooks、`agent-framework` memory/checkpoint/eval；OpenClaw 自 06A 起不再参与。 |
| M1-07A-C scheduler/fault/recovery | LangGraph/AgentScope/OpenHands/browser-use | 增加 `opencode` durable event/session status/control-plane signals、`hermes-agent` delegation/cron/kanban failure handling。 |
| M1-08 main path hardening | role-aware 来源角色/落位状态收束 | 必须生成包含 `opencode` 的来源角色/落位状态差异表；纳入表中不等于 active，并按当前父级预算校准 M1 总行数。 |

M1 行数表曾存在旧计划残差。2026-07-12 按 slice 实际任务重量重新校准后，不再把残差集中堆到硬化阶段：M1-05C 从 14,000 调整为 16,000，M1-05D 从 13,000 调整为 18,000，M1-08 从 23,000 调整为 16,000。2026-07-13 又根据已完成 04A-01 的实际边界，将未执行的 04A-02 从 7,500 调整为 6,000，M1-04A 父级从 15,000 调整为 13,500；已执行的 04A-01 文档保持不动。因此当前 M1 合计为 378,500。同期 M2 按 event store、PTY/trace、permission control 和 session/memory/provider panels 的实际重量调整为 135,000，M3 按 benchmark/evaluation 与 packaging/semantic health 的实际重量调整为 51,000，第一阶段合计为 564,500。

## M2 调整

M2 之前未参与上一轮切片重排。本节表格保留候选来源全集；正式实现的 primary/supplementary/conformance/reference 分工以 2026-07-13 M2 角色表为准，并应按 `docs/milestones/README.md` 的规则拆成更小 slice。

| M2 单元 | 候选来源/交互（必须裁决角色，不等于全部迁移） |
| --- | --- |
| M2-01A Web app shell/API client | `opencode/packages/protocol/**`、`packages/app/server-sdk`、`server-sync`、`server-session`；OpenHands API service；Agent Framework AG-UI；新增裁决 `claude-code-best/src/screens/REPL.tsx`、`src/components/PromptInput/**`、`src/hooks/useCommandQueue.ts`、`src/utils/handlePromptSubmit.ts`、`src/utils/messageQueueManager.ts` 的 shell/input/control 交互模型；明确 `opencode/packages/web` 不是主 workbench，`packages/app` 和 `packages/tui` 才是高价值来源。 |
| M2-01B event stream/state store | `opencode` ServerSession/EventList/Sync projector；OpenHands event-service；LangGraph 只做 checkpoint/reconnect/replay conformance；AG-UI event contract；新增裁决 Claude Code command queue/local-jsx/control event 如何投影为 Zyra UI state。 |
| M2-02A topology graph | Zyra canonical topology-mutation/graph-revision/checkpoint 事件为状态来源；LangGraph checkpoint identity/lineage 只提供恢复输入 contract，StateGraph/subgraph/Pregel 仅作封闭图风险与 conformance 对照。 |
| M2-02B timeline/worker state | `opencode` session timeline/status projection；OpenHands status/conversation；browser-use history/action artifacts；Hermes gateway/session events。 |
| M2-03A artifact/diff | OpenHands file/diff viewer；`opencode` session-ui review/file/diff/message parts；AG-UI artifact contract。 |
| M2-03B terminal/browser/trace | OpenHands terminal/browser panels；browser-use screenshot/DOM/action history；`opencode` terminal panel/session event model；Hermes PTY/gateway。 |
| M2-04A command/permission | Claude commands/permission as backend truth；新增裁决 Claude Code `PromptInput`、slash suggestion/submit、local-jsx command panel、permission dialogs、keyboard/queue interaction；`opencode` command palette/permission question; Hermes slash/approval; AgentScope PermissionEngine/HITL. |
| M2-04B session/memory/MCP/skill | Claude context/compact/MCP/skills；新增裁决 Claude Code context/compact/memory/MCP/skills 命令 UI 与 REPL state handoff；`opencode` session/memory/MCP/skill/agent stores; Hermes SessionDB/skills; AgentScope KB/RAG/MCP. |
| M2-05 scenario runner | 演示包必须输出包含 `opencode` 的 UI role-aware 来源角色/落位状态差异表，并消费 M1-08 source-to-target ledger。 |

## M3 调整

M3 不是第一次做重型迁移的阶段，只能收束 M1/M2 已完成能力。下表是必须审计的来源范围，不产生新增迁移配额：

| M3 单元 | 必须审计的来源/产品化范围 |
| --- | --- |
| M3-01A source custody | source map 必须覆盖 `opencode`，并区分 V2 durable session/event、V1 provider/product loop、protocol/app/tui/web/desktop 的不同状态；动态 plugin install、未接入 Desktop/WSL、process-local background job 不得混同为主路径内化。 |
| M3-01B cleanup | 清理后 `zyra` 不能呈现为 Claude/OpenHands/opencode/browser-use 多仓堆叠；若保留任何 opencode-derived UI/runtime 代码，必须落入 Zyra module 边界并有默认入口。 |
| M3-02A test/eval | 默认主路径 trace、启用/禁用对照、event causality、clean scenario 必须覆盖 opencode-derived session/event/UI projector 和 M1/M2 内化模块。 |
| M3-02B packaging | 健康检查必须验证无 `../opencode`、`../OpenHands`、`../browser-use` 等根目录依赖；若使用 desktop/WSL/terminal 参考，只能作为 Zyra-owned 包装或明确外部依赖。 |
| M3-03 freeze report | 冻结报告必须列出 `opencode` 与其它所有来源的 role-aware 来源角色/落位状态；不得把“列入审计”写成默认 active，也不得把本轮 source graph 阅读成果笼统写成“参考过”。 |

## 承接关系

M1 后续单元负责把后端 runtime、permission、MCP、skills/subagent、memory、scheduler、fault recovery 真正内化。M2 只能消费这些真实 API/event/control command/UI state，不能用静态样例替代。M3 只做 source custody、清理、验证、打包和冻结；如果 M3 发现 M1/M2 的核心能力仍是 mock、source pool 或黑箱 runtime，应阻断冻结并回补 M1/M2，而不是登记为第二阶段优化。

后续任何执行文档若与本文件冲突，以更能防止伪内化、更能保持 source-to-target 链路完整的要求为准。

## 2026-07-10 二次校准：唯一 Owner 与成熟度边界

本轮在逐文件全文复核 96 份 source graph 后，新增以下强制裁决。总原则是“一个 Zyra state machine + 多来源机制拼装”，禁止多套 runtime/store 并存。

| 能力链 | 唯一 owner 与单元 | 主要成熟来源 | 必须排除或降级 |
| --- | --- | --- | --- |
| Query/session/tool/context | 02B-D 已有状态；03A-D 只能向同一状态机插入能力 | completed primary：Claude QueryEngine/query/tool；AF FunctionInvocation/per-call history 仅保留受保护历史裁决 | 另建 session/tool store；Claude reactive compact/context collapse/snip 等 stub |
| Permission | 03A permission runtime/store；M2 只调用和投影 | completed primary：Claude deny/ask/hook/user；supplement：Hermes staged approval；AF/AG-UI pending registry 向后只作 conformance | hook/LLM/UI 直接提交决定；无人值守等待人工 |
| MCP | 03B config/connection/projection/auth owner | completed primary：Claude MCP；protected supplements：Hermes OAuth/stdio/elicitation、AF polling/cancel/default-deny | Browser MCP 变第二套 generic runtime；Claude `mcpSkills` stub |
| Skill/Extension | 03C discovery/invocation/plugin；06C 只 memory/restore | primary：Claude skills/plugins；supplement：Hermes staged skill supply chain；AF remote skill 仅 conformance | Claude remote search/DiscoverSkills/MCP skills stub；03C/06C 双 store |
| Subagent/worker | 03D child session/task/control；07A worker instance/lease/inbox/capacity | logical task primary：Claude，supplements：Hermes、OMP；physical worker primary：AgentScope，supplement：OMP；AF durable task 仅 conformance | opencode/AgentScope process-local background job 充当 durable owner |
| Browser | 04A session/SessionManager/CDP；04B DOM/AX/selector + MessageManager；04C action/security；04D active watchdog/history/artifact | primary：browser-use；各子域 supplements 仅限 Claude/OMP/OpenHands 的独立缺口 | 未 attach 的 `CrashWatchdog`、cloud captcha、cloud sandbox/skills 作为 active |
| Event/message | 05C durable EventStore/projector + transient MessageBus + LowEntropy envelope | primary：opencode projector；supplements：OpenHands event fold、OMP frames；AgentScope/LangGraph/AG-UI 仅 conformance | AG-UI facade 或 frontend transcript 成为 canonical truth |
| Provider | 05D `ProviderControlPlane` 唯一写 catalog/integration/credential/route；02D descriptor 只作兼容投影 | primary：opencode Catalog/Integration/Credential/AISDK；supplements：Hermes resolver、OMP wire/retry；Claude stream/retry 仅 conformance | 02D 与 05D 双写；secret 进入 event/catalog；模拟 frame 冒充 provider 兼容 |
| Retrieval/index | 06A retrieval/index job state machine；checkpoint 不归本单元 | primary：AgentScope lifecycle/RAG/KB；supplement：OMP Mnemopi retrieval/job；FTS/vector 为 Zyra-owned | 只做 adapter/schema；把 LangGraph checkpoint 混入 memory index |
| Dynamic graph custody | 07A `DynamicTopologyRuntime` + immutable `GraphStateCustody` | primary：Zyra-owned runtime mutation、branch delta、conflict detector、deterministic commit；LangGraph StateGraph/channel/Pregel 仅 reference/conformance | 把条件选边冒充动态拓扑；共享对象原地 mutation；迁移第二 graph runner |
| Checkpoint/recovery | 07C `GraphCommitRuntime` + `CheckpointRecoveryRuntime` + `RecoveryPlanner` | 窄域 semantic primary：LangGraph checkpoint identity/lineage、pending/committed writes、stable ids、atomic commit、interrupt/exact-resume；recovery policy supplement：OMP；AF durable workflow 仅 conformance | Azure host/外部 checkpointer/Pregel 当 durable core；只保留 latest state；恢复重复副作用 |
| Workspace/sandbox | 05A/B Zyra-owned lifecycle/policy，外部 backend 只作 provider | primary：OpenHands start-task/sandbox；workspace supplements：AgentScope、OMP；sandbox supplements：OpenClaw、OMP | OpenHands agent-server/SDK/tools 黑箱；host path check 直接套进 sandbox |
| Frontend state | M2-01B canonical snapshot+delta client -> single reducer/store -> selectors | primary：opencode server-session；supplement：OpenHands history/WS fold；LangGraph StreamController 仅 conformance | 每个 panel 自建 store/reducer；来源 UI state 反向成为后端 truth |

### 精确来源与验收补充

- 03B 中 Claude 路径应使用 `src/utils/mcpOutputStorage.ts`、`src/utils/mcpValidation.ts`，不是 `src/services/mcp/*` 下同名文件。MCP 必须覆盖 config/provenance/connection、tool-resource-prompt projection、auth/needs-auth/elicitation/instructions-delta/compact restore 三链。
- 03C 中 attachment 路径是 `src/utils/attachments.ts`，plugin 路径是 `src/utils/plugins/*`。Plugin/Extension lifecycle 由 03C/03D 正式承接，动态 marketplace/npm install 只可 deferred/dev-only。
- Browser DOM owner 使用 `browser_use/dom/service.py`、`enhanced_snapshot.py`、`views.py` 和 `dom/serializer/*`；selector generation 必须进入 04C 真实 click/input/extract，禁用后行为测试失败。
- 04D 只引用 active watchdog attach 清单。若需要 crash 行为，Zyra 应根据 CDP disconnect、browser process exit、heartbeat/timeout 实现 detector 并用真实 kill/disconnect 验证。
- OpenClaw 当前 source graph 只是轻量边界。已验证可定点使用 gateway protocol/server methods、plugin registry、tool policy、subagent control/task registry、embedded subscription；未定点全文阅读的 `src/acp/control-plane/**` 等路径不得标 active migrated。
- 对受保护的早期 Query/tool/compact 决策，Agent Framework 的 FunctionInvocationLayer、per-call history、atomic compaction 曾作为选择性实现来源；该历史事实不回写。自 04B-02 向后，core workflow/checkpoint durable 与 AG-UI 均只作 conformance，不再与 LangGraph 或 Zyra owner 并行迁移；Azure Functions hosting、in-memory snapshot 和 pickle fallback 从来不是生产 owner。
- OpenHands 只提供 app-server/start-task/sandbox/event/settings/secrets/frontend console 机制；`openhands-sdk`、`openhands-agent-server`、`openhands-tools` 是外部黑箱，不计 CodeWorker 深度内化。

### 赛题证据前移

后续 M1/M2/M3 必须同时读取 `docs/比赛要求追踪矩阵.md`。M1-05C 要量化 route density、broadcast ratio、message/token、duplicate fact 和 artifact offload；M1-07/08 要形成真实 local/edge/cloud、多 provider、有效千步、零人工和 exact recovery 后端证据；M2-05 要完成两个跨领域 clean-state live run；M3-02A 要做 static/full-connect、no-memory、fixed-placement、no-recovery 等 baseline/ablation。任何代码行数或 role-aware 来源角色/落位状态表都不能替代这些动态证据。

## 2026-07-12 增量重排：Oh My Pi

新增来源图：`source-graphs/oh-my-pi/`，固定 commit `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`。

### 2026-07-12 保护边界

- 本增量不反向修改 `slice-03d-01-subagent-commands-foundation.md` 或更早 slice。
- 最早执行入口为 `slice-03d-02-subagent-commands-integration.md`；其它来源项只进入其后的 M1、M2、M3 文档。
- 本次只更新 source graph 与未来计划，不将任何 Oh My Pi 项标为 active/migrated，也不更新 `execution-state.yaml`。

### 来源职责重排

| Oh My Pi 链 | 优先承接单元 | 目标 owner | 限制 |
| --- | --- | --- | --- |
| `packages/coding-agent/src/task/{index,executor}.ts`、AsyncJob、AgentRegistry、typed yield | 03D-02、07A | 03D logical task；07A physical lease | process-local registry不能恢复canonical task |
| AgentLoop message/tool-pair/result budget、tool hooks/approval、OTel/Hashline/Snapcompact | 04B-D | Browser context/action/history owners | OMP session不是owner；yolo禁用；Snapcompact仅experimental |
| `task/{worktree,isolation-runner}.ts`、PAL/`pi-iso` | 05A、07A | WorkspaceManager、WorkerPool | 不得暴露host path或依赖`../oh-my-pi` |
| isolation/host/native/RPC tool、Hashline、credential relay | 05B | SandboxGateway/PatchEngine | 所有入口必须消费不可变permission/workspace/route receipt |
| AgentLoop/RPC/subagent/collab event | 05C、M2-01B/02B | EventStore/MessageBus/Projector、single frontend store | delta/UI/replica不是事实源 |
| `packages/ai`、catalog、model registry、retry/fallback | 05D、07C | ProviderControlPlane | 只迁移wire/quirk/分类，不能建第二catalog/credential owner |
| coding memory jobs、`packages/mnemopi` | 06A、06B | MemoryFabric canonical + derived index/curator | LLM extraction只能产candidate；OMP DB不是SSOT |
| compact/snapcompact | 06C、M3-02A | compact/restore projection | snapcompact先标experimental并做跨provider fidelity消融 |
| advisor/watchdog、MCP reconnect、roboomp restart | 07B、07C | active observer、RecoveryPlanner | advisor不能直接改状态，注入不能冒充真实观察 |
| RPC/ACP/collab-web/TUI/tool cards/Hashline | M2-01A至04B | typed client、timeline/artifact/control panels | 迁移状态流，不迁品牌壳或OMP subprocess |
| Bun/Rust/native/roboomp/OMP stores | M3-01/02B/03 | source custody、clean-room、packaging | whole monorepo、generated proto、binary、sidecar不得计深度内化 |

### 与既有来源的主次关系

- `claude-code-best` 仍是 QueryEngine、permission、MCP、SkillTool、AgentTool、compact主来源。
- `opencode` 仍是 durable session/event、canonical ProviderControlPlane结构和Web/TUI control-plane补强来源。
- Oh My Pi新增的是AgentLoop执行细节、TaskTool/PAL、Mnemopi、provider wire/RPC和长期运营模式；不得借新增来源重开已完成02A-03C或03D-01。
- Oh My Pi parent-child tree/IRC只作为Zyra动态拓扑已选节点的执行substrate，不能关闭动态稀疏拓扑或低熵对照门禁。

### 新增红线

- OMP默认`yolo`、subagent yolo、advisor mutating-tool绕wrapper均不得进入sealed policy；child scope必须由03A单调收窄。
- `AgentSession`、JSONL、`AgentRegistry.global()`、AsyncJobManager、Mnemopi DB、roboomp SQLite和collab replica不得形成平行state owner。
- 正式`zyra`不能spawn `omp --mode rpc`承担CodeWorker核心决策；RPC只作协议/行为来源或显式conformance工具。
- M1-08/M3 必须把 Oh My Pi 列入 role-aware 来源角色/落位状态表、disable matrix、dependency/process、source similarity 和 clean-copy 审计；只有表中已裁决为 primary/supplementary 且确实进入主路径的机制才可标 active，其余项不得因“列入审计”产生迁移配额。

## 2026-07-13 来源职责去重与未来执行重排

### 2026-07-13 保护边界

- 2026-07-13 本节规则制定时采用严格前向边界，第一份应用该规则的详细执行文档是 `slice-04b-02-message-manager-state-compression-integration.md`；04B-02 对来源角色的收窄不追溯改变此前已承接的事实。
- 该日期和 slice 只标识规则的历史适用边界，不代表当前编辑授权。任何已完成 unit/slice 均受 `execution-state.yaml` 保护，当前保护范围和下一入口只以该文件为准。
- 上层分析、总计划和里程碑 README 可以更新未来口径，但不得追溯改写受保护执行事实。

### 角色规则

source graph 是候选机制全集，不是逐仓实现 backlog。每个具体状态域设一个 `primary_implementation`，最多两个只补独立缺口的 `supplementary_implementation`；其余使用 `conformance_only`、`reference_only`、`experimental` 或 `deferred/rejected`。只有 primary/supplementary 产生生产迁移、主路径和断开即失败义务；其它角色只按其用途取证，不要求等价实现。`experimental` 默认关闭，不能成为默认主路径或替代正式能力；unit 明确要求时只提供有界消融证据。所有 active 机制必须进入同一个 Zyra canonical owner，不得创建平行 runtime/store/reducer。

### M1-04B-02 以后来源职责

| 状态域/单元 | primary implementation | supplementary implementation | conformance/reference/experimental |
| --- | --- | --- | --- |
| 04B-02 message/state compression | browser-use MessageManager/context | Claude context/tool-result contract；OMP tool-pair/result-budget 语义 | Agent Framework/opencode/Hermes 仅 contract 对照；Snapcompact experimental |
| 04C action/permission | browser-use action registry/security | Claude permission/tool contract；OMP hook/Hashline preflight | OpenClaw、AgentScope、opencode 仅 policy/conformance/reference |
| 04D watchdog/history/artifact | browser-use active watchdog/history | OpenHands artifact/event projection；OMP trace/tool-pair/Hashline receipt | Claude/opencode/Hermes 仅 event/trace conformance；未 attach CrashWatchdog rejected |
| 05A workspace lifecycle | OpenHands start-task/workspace lifecycle | AgentScope workspace backend；OMP PAL/worktree/isolation | Claude/opencode/OpenClaw/Hermes 只作边界核对 |
| 05B sandbox/gateway | OpenHands sandbox lifecycle | OpenClaw gateway/tool-policy；OMP isolation/credential/Hashline | AgentScope/Hermes/opencode/Claude 只作 conformance/reference |
| 05C event/message | opencode durable session/event projector | OpenHands event fold；OMP AgentLoop/RPC/subagent frame | Agent Framework AG-UI、AgentScope bus、LangGraph stream、OpenClaw/Claude 仅协议/恢复对照；owner 始终是 Zyra EventStore/Projector |
| 05D provider/backend | opencode ProviderControlPlane | Hermes resolver；OMP provider wire/retry | OpenClaw/AgentScope/LangGraph/Claude/OpenHands 仅 capability/stream/failover conformance |
| 06A retrieval/index | AgentScope RAG/KB 与 job lifecycle | OMP Mnemopi retrieval/job lease | LangGraph checkpoint、Hermes memory search、opencode/Claude 仅边界对照；代码索引为 Zyra-owned 实现 |
| 06B curator | Hermes memory/curation | OMP Mnemopi candidate/consolidation | AgentScope/LangGraph/opencode/Claude 仅 conformance/reference；validator/committer 为 Zyra-owned |
| 06C skill memory/compact | Claude SkillTool/compact contract | Hermes procedure memory；OMP compact/restore 语义 | Agent Framework/opencode 仅 contract/reference；Snapcompact experimental |
| 07A worker lifecycle 子域 | AgentScope lifecycle/inbox/wakeup | OMP TaskTool/PAL/AsyncJob lifecycle | OpenHands/opencode/Hermes/Claude 只作 conformance/reference |
| 07A dynamic graph custody 子域 | Zyra-owned runtime-evolvable topology + immutable snapshot/branch delta/commit | 无平行来源实现 | LangGraph StateGraph/channel/Pregel/stream 与 Agent Framework workflow 仅 reference/conformance；不得建立第二 graph runner/checkpoint owner |
| 07B browser observer 子域 | browser-use active watchdog | OMP process/MCP/provider observer | 未 attach watchdog、advisor mutation rejected |
| 07B cross-runtime fault 子域 | Zyra-owned classifier/state machine | OMP restart/provider signals | Agent Framework/LangGraph/Hermes/opencode/Claude 仅 fault taxonomy/event conformance |
| 07C checkpoint commit / exact-resume 子域 | LangGraph checkpoint identity/lineage、pending/committed writes、stable ids、atomic commit、interrupt/resume 的窄域语义 | 无 | LangGraph StateGraph/channel/Pregel/stream/prebuilt/Store/SDK、Agent Framework durable workflow、AgentScope scoped-handle 仅 conformance/reference |
| 07C recovery policy 子域 | Zyra-owned RecoveryPlanner | OMP retry/reroute receipts | Hermes/opencode/Claude 仅 provider/control conformance |
| 08 hardening | 不新增来源实现；审计前述 active owner | 只修复 disable/clean-room/主路径缺口 | 所有 inactive 来源只校验裁决，不得转化为迁移配额 |

### M2 来源职责

| 单元 | primary implementation | supplementary implementation | conformance/reference |
| --- | --- | --- | --- |
| 01A shell/API client | opencode typed protocol/server session | Claude PromptInput/command queue；OpenHands API lifecycle | Agent Framework、AgentScope、OMP 只做 envelope/RPC conformance |
| 01B event/store | opencode server-session/reducer | OpenHands history/WS fold | LangGraph StreamController、Agent Framework AG-UI、OMP 只做 event/reconnect/replay conformance |
| 02A topology | Zyra-owned dynamic sparse topology projection | 无平行来源实现 | LangGraph graph state、opencode status、OpenHands/OMP 只提供输入 contract 或交互参考 |
| 02B timeline/worker | opencode session/tool timeline | OpenHands worker/status；browser-use action history | Agent Framework/OMP 只做 lifecycle/event conformance；LangGraph/Hermes 只提供 M1 input contract |
| 03A artifact/diff | opencode session-ui/review/diff | OpenHands file/diff；OMP Hashline receipt | Agent Framework AG-UI artifact 仅 contract |
| 03B terminal/browser/trace | terminal：opencode；browser：browser-use | OpenHands terminal/browser；OMP trace/PTY 语义 | Hermes 仅 auth/replay conformance；trace owner 为 Zyra event spans |
| 04A command/permission | Claude command/permission UI state | opencode permission question/palette | Hermes、AgentScope、OMP 仅 policy conformance/reference |
| 04B session/memory/MCP/skill | Claude session/context/MCP/SkillTool/AgentTool | opencode session panels；Hermes memory/MCP/skill | Agent Framework、AgentScope、browser-use、OMP 仅 contract/drill-down reference |
| 05 scenario runner | Zyra-owned runner/evidence pipeline | 不新增来源实现 | 全来源仅审计 role-aware 来源角色/落位状态与能力覆盖；reference-only 不因未迁移自动阻断 |

### M3 收束规则

M3 不为任何仓库新设生产迁移配额。M3-01A 必须列全来源角色并审计每个状态域 `1 + 2` 上限、唯一 owner 和 inactive 理由；M3-01B 只能修复已选 active owner 的真实产品化缺口，不能为了“来源覆盖”再吸收平行实现；M3-02A/B 按 active 模块做 disable、clean-room、benchmark、打包和 semantic health；M3-03 报告全部来源，但 reference/conformance/experimental/deferred 项只需有正确裁决，不能因没有迁移而自动判失败。

### 行数预算裁决

来源去重只删除同义来源实现义务，不删除 Zyra-owned 能力或工程责任，因此 M1/M2/M3 父级预算保持不变。只有实际任务责任减少并完成上下级文档同步时，才允许调整行数。

本轮删除的是重复来源义务，不是父级功能、状态 owner、真实主路径、安全/恢复语义、赛题证据或产品化工具责任。逐单元复核后没有出现“任务责任显著减少而预算仍按多仓重复实现计算”的大幅错配，因此 M1/M2/M3 父级与切片最低有效新增代码暂保持不变，总额仍为 `564,500`。执行中若进一步拆解发现某单元的真实 Zyra-owned production 责任明显变化，必须同时修改父级、全部未执行子 slice、里程碑 README、总工程计划和总额，不能只在单片口头豁免。

### M2/M3 低停顿切片路由

- M2 已固定为 20 个未来 slice：03B 和 05 各三片，其余七个父级各两片；M3 固定为 10 个 slice，每个父级各两片。
- 切片只沿本文件已裁决的状态 owner、primary/supplementary source chain 和真实主路径拆分，不按仓库切片，也不为 conformance/reference/inactive 来源创建独立实现片。
- 父级最后一片直接累计核对父级预算、source-to-target、owner 和行为；不另建只做审查的 slice。精确文件名、预算和语义边界由 M2/M3 README 与对应 `slice-*.md` 共同约束。
- 固定片数用于减少阶段内停顿。若执行中发现既定两片无法隔离 canonical owner 或真实验证链，必须先说明冲突并取得用户认可，不能自行增片或缩减能力。
