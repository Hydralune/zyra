# Oh My Pi Source-to-Target Notes

日期：2026-07-12

## 1. 总体定位

Oh My Pi应成为`claude-code-best`与`opencode`之外的第三条高价值coding-agent来源链：

- `claude-code-best`继续负责QueryEngine/permission/MCP/SkillTool/AgentTool/compact主来源；
- `opencode`继续补强durable session/event/provider control/UI；
- Oh My Pi重点补强AgentLoop执行细节、TaskTool/PAL isolation、Mnemopi、provider wire/RPC和长期运营模式。

三者不能形成三套session/tool/permission/provider事实源。Zyra owner唯一，来源机制按链拆解。

## 2. Future-only source-to-target

用户保护边界：不修改`slice-03d-01-subagent-commands-foundation.md`及其之前文档。最早入口为03D-02。

| 最早owner unit | OMP来源 | Zyra目标 | 建议状态 | 主要行为证据 |
| --- | --- | --- | --- | --- |
| M1-S03D-02 | `task/index.ts`、`executor.ts`、AgentRegistry、AsyncJobManager | `packages/runtime`、`packages/orchestration`、`packages/commands` | active migration | fan-out/yield/background/resume/cancel进入logical task/event |
| M1-04B | AgentLoop message/tool-pair/result budget、Session compact、Snapcompact | BrowserMessageManager/context disclosure | selective + experimental | malformed/oversize/partial result归一化；视觉压缩有fidelity消融 |
| M1-04C | tool registry/context、approval/rulebook、hooks | BrowserActionRegistry + 03A permission | selective migration | extension/advisor/subagent action不能绕permission/selector fence |
| M1-04D | AgentLoop/OTel/RPC/TaskTool trace、Hashline/worktree receipts | browser history/artifact/failure evidence | contract + selective migration | partial/final/tool/subagent因果与artifact lineage，advisor不产fault truth |
| M1-05A | `task/worktree.ts`、`isolation-runner.ts`、`pi-iso` | `packages/workspace` | active migration | dirty baseline/nested repo/merge conflict/snapshot cleanup |
| M1-05B | isolation runner、pi-iso、Hashline、host/RPC/native tool、RoboOmp credential relay | SandboxGateway/PatchEngine | selective migration | structured argv、fencing、deadline、redaction、side-effect receipt，禁止yolo旁路 |
| M1-05C | AgentLoop events、RPC subagent frames、collab bus | `packages/runtime` EventStore/MessageBus/Projector | selective migration | tool/subagent progress有causation，payload spill/ref |
| M1-05D | `packages/ai/providers`、catalog、model registry、retry | ProviderControlPlane | selective migration | 双wire真实request、credential rotation、新route lease |
| M1-06A | Mnemopi store/recall/vector/MMR/polyphonic/temporal | `packages/memory`、`packages/code_index` | active algorithm migration | recall影响context/recovery，index可删重建 |
| M1-06B | coding memories job queue、Mnemopi consolidation | MemoryCurator/worker | active state-machine migration | lease/heartbeat/requeue，candidate deterministic commit |
| M1-06C | compact、memory backend、snapcompact | compact/restore projection | mixed active + experimental | restore改变下一turn，视觉压缩有消融 |
| M1-07A | AsyncJob/AgentRegistry/PAL lifecycle、roboomp WorkerPool | physical WorkerPool/Lease | selective migration | real worker heartbeat/lease fencing/restart |
| M1-07B | advisor/watchdog、MCP crash breaker、roboomp restart | Watchdog/Fault signals | selective migration | active observer与injection分离 |
| M1-07C | retry/fallback、session resume、worktree recovery | RecoveryPlanner | selective migration | provider/worker/workspace route分层改变 |
| M1-08 | 全链 | main-path hardening | audit | active/deferred、disable matrix、clean-room |
| M2-01A/01B | RPC/ACP、Python binding、session/collab events | typed API client + single reducer | contract + selective migration | correlation/snapshot+cursor/reconnect无第二事实源 |
| M2-02B | task progress、AgentRegistry snapshot、telemetry | timeline/worker/subagent projection | selective UI migration | 因果链与数千step虚拟化 |
| M2-03A/03B | Hashline/tool cards/TUI/native terminal | artifact/diff/terminal/trace | selective migration | patch transaction、PTY/control受permission |
| M2-04A/04B | slash commands、approval、session/MCP/memory/subagent state | command/permission/session panels | selective migration | control真实改变backend，sealed只读/deny |
| M2-05 | roboomp durable scenario、RPC/headless | scenario runner evidence | reference + port | clean new input、restart resume、final artifact |
| M3-01A/01B | monorepo/source/runtime dependencies | source custody/cleanup | audit | 无`../oh-my-pi`、无OMP DB/CLI owner |
| M3-02A/02B | tests/provider/native/compact | eval/packaging | selective | ablation、双provider、Bun/native optionality |
| M3-03 | 全链planned-to-active | freeze report | audit | 每项最终状态/入口/test/limitation |

## 3. 稳定 source chain 建议

- `SRC-SUBAGENT-OMP`：TaskTool -> executor -> AgentRegistry/AsyncJob -> typed yield -> worktree outcome。
- `SRC-ISOLATION-OMP`：baseline -> PAL/worktree -> child execution -> patch/commit -> merge/cleanup receipt。
- `SRC-MEMORY-OMP`：event/session delta -> durable job -> candidate/consolidation -> recall/index -> context。
- `SRC-PROVIDER-OMP`：catalog/model -> credential -> wire -> retry/fallback -> route event。
- `SRC-CONTROL-OMP`：RPC/ACP command -> AgentSession transition -> event/state -> client projection。
- `SRC-EDIT-OMP`：snapshot/hashline -> preflight -> patch/recovery -> diff/artifact。

这些chain只能作为来源ID；最终owner仍由现有`SRC-*`稳定链和Zyra模块决定，不能新建重复owner。

## 4. 必须裁剪/改造

### Permission

- 把default yolo改为Zyra policy；
- child permission按parent decision + task scope单调收窄；
- advisor/memory/MCP/host tools都重新走统一gate；
- sealed ask -> deterministic deny/replan，零人工。

### State custody

- OMP JSONL、Mnemopi DB、roboomp SQLite、collab replica不写canonical state；
- AgentRegistry/AsyncJob只作live coordination；
- restart从Zyra EventStore/task/lease/workspace/memory/route refs恢复。

### Packaging

- 不整仓引入Bun/Rust/native；
- provider wire、Hashline、Mnemopi可按模块迁移或重写成Zyra接口；
- native性能backend必须可选且有pure fallback；
- generated proto/research/benchmark/assets不计production LOC。

### Topology/communication

- OMP task tree只执行router已选择的节点；
- IRC/chat消息转换成05C `AgentMessageEnvelope`，正文有预算，大内容spill artifact；
-不允许full broadcast作为默认。

## 5. 比赛要求映射

| Requirement/score | Oh My Pi作用 | 必须由Zyra追加的动态证据 |
| --- | --- | --- |
| REQ-LOOP / SCORE-COMPLETE | loop/session/subagent/restart | sealed run、有效transition、最终artifact verifier |
| REQ-MEMORY | Mnemopi/compact/jobs | 跨worker canonical memory、restore效果/消融 |
| REQ-TOPOLOGY | child/IRC执行substrate | dynamic sparse router、低熵baseline |
| REQ-EDGE-CLOUD | PAL/provider roles | real device/edge/cloud dispatch与privacy/latency/cost |
| REQ-FAULT | retry/reconnect/worktree/roboomp | observed faults、layered route/lease/checkpoint recovery |
| SCORE-UX | RPC/TUI/collab/tool cards | Zyra single-store workbench、causal navigation |
| SCORE-COMPAT | provider wires/MCP/skills | live multi-provider/model run、role add/remove |

## 6. Source-to-target 失败判定

- 直接spawn OMP RPC作为默认CodeWorker：失败；
- OMP yolo/subagent yolo进入sealed policy：失败；
- OMP AgentSession/JSONL/Mnemopi DB/roboomp SQLite成为Zyra state owner：失败；
- 把OMP task tree宣传成动态稀疏拓扑：失败；
- 仅复制tool cards/TUI样式而不连canonical events：失败；
- snapcompact无fidelity/compatibility消融即标active_real：失败；
- clean `zyra`仍需`../oh-my-pi`、npm link、editable path或Docker context：失败；
- 本source graph或ledger体量被计入550,000 production LOC：失败。
