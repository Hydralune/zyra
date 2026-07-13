# M1-S04B-02 message manager 与状态压缩 integration 增量批判式自审

## 1. 审查身份与结论

- Slice：`M1-S04B-02`
- 父级：`M1-04B message manager 与状态压缩`
- 基线 commit：`6897ac7beef1e9fc006ec5e02e04c108180b8b6f`
- 实现 commit：`d1ce095f191b6c5db38db988418c132dd24da947`
- 审查范围：本 slice diff、04B-01 相邻主路径、02D context owner、04A browser session/target owner、source ledger 增量。
- 结论：**通过本 slice 增量验收，并关闭父级 M1-04B；无阻断项。**

本结论不以账本、代码行数或静态 contract 代替行为。默认 BrowserWorker 路径已经把 04B-01 的 DOM/state、selector、message、artifact、memory signal 模块接入 TaskState checkpoint、02D provider messages、event/artifact store 和 API projection。若禁用 task integration、API projection 或 memory candidate consumer，行为测试会 fail closed；不存在回落到旧上下文路径的静默 fallback。

## 2. 当前 slice 目标覆盖矩阵

| Slice 目标 | 状态 | 生产证据 | 行为/失败证据 | 阻断 |
|---|---|---|---|---|
| 复用 04B-01 七个核心模块并进入 BrowserWorker 默认路径 | 已完成 | `browser_worker.py`、`browser_context/message_manager.py`、`browser_context/application.py`、`browser_state/runtime.py` | foundation 6 tests/10 subtests；integration 5 tests | 否 |
| 压缩结果真实改变 02D 后续 provider context | 已完成 | `task_integration.py`、`code_worker_runtime.py`、API code-worker route | API Browser→CodeWorker test 校验 provider envelope 精确选择一次；第二次 delivery 为空 | 否 |
| TaskState/重启/read-once 状态闭环 | 已完成 | `BrowserContextTaskCheckpoint`、claim/commit/release/reconcile、TaskState metadata persistence | checkpoint JSON round-trip、scope mismatch、restart 后不 replay | 否 |
| OMP tool-pair/result normalization | 已完成 | `action_envelope.py`、`message_manager.py` | typed receipt、partial、malformed、conflict、oversize externalization | 否 |
| shadow DOM、same-origin iframe、OOPIF、selector generation/stale、live CDP resolve | 已完成 | `frame_capture.py`、`selector_probe.py`、`browser_state/runtime.py` | 集成测试验证 OOPIF target/session routing 和 stale-before-CDP；真实 Chrome smoke 捕获 3 frames/1 OOPIF 并完成 16/16 live probes | 否 |
| action→capture→artifact→disclosure 因果链 | 已完成 | `causal_runtime.py` | event id、artifact digest/bytes、capture/revision/action receipt 关联审计 | 否 |
| memory/recovery 可消费信号，不抢 06B/06C canonical owner | 已完成 | `BrowserMemoryCandidateConsumerPort` | schema/scope/artifact/capture/action 验证；只标记 accepted-for-review，不提交 MemoryFabric | 否 |
| M2 timeline/trace 可读 projection | 已完成 | `api_projection.py`、`GET /tasks/{task_id}/browser-context` | queue/tool-pair/memory/timeline projection 行为测试 | 否 |
| full/structured/bitmap 三 lane 消融且不掩盖质量下降 | 已完成 | `ablation.py`、live smoke | 三 lane 指标；structured 为默认；bitmap default-off/non-authoritative；OCR 不可用时明确 unavailable | 否 |
| 预算超限确定性 externalize/truncate | 已完成 | action/state artifact externalizers、02D budget bridge | 16 KiB 预算 live 预跑触发 `BrowserStateBudgetExceeded`；65 KiB 成功跑明确记录 offload/ratio | 否 |
| source role、账本、路径边界 | 已完成 | sync script、ledger seed、integration audit、ledger policy | 25 决策二次 `--check`；隔离 strict audit 0 blocker/0 error | 否 |
| 本 slice 生产最低 5,000 行，父级累计 15,000 行 | 已完成 | numstat 严格分桶 | 当前 active production 5,017；父级累计 15,921 | 否 |

## 3. 主路径与动态可达性证据

| 主要模块 | Runtime/API 入口 | 状态/event/artifact 接入 | 语义效果 | 断开后结果 |
|---|---|---|---|---|
| `BrowserContextTaskIntegrationRuntime` | BrowserWorker 结束写 checkpoint；API Browser route enqueue；API CodeWorker route prepare/verify/commit/release | `TaskState.metadata.browser_context_runtime_state`；`AGENT_MESSAGE.browser_external_context` | disclosure 以 read-once typed message 进入真实 02D provider envelope | `disabled=True` 的 `begin_browser_turn` 行为测试抛错；API 不存在旧路径 fallback |
| `BrowserActionEnvelopeNormalizer` | `BrowserMessageManagerRuntime.after_action` | raw/oversize result 写 artifact；normalized receipt 进入 history/event | partial/malformed/conflict 仍一输入一 receipt，tool pair 不丢失 | 移除后 integration action-envelope test 无法得到 5 个确定性 receipt 和 raw artifacts |
| `BrowserFrameCaptureRuntime` | `BrowserDomStateRuntime.capture` | 复用 04A TargetRegistry/CDP session；合并 DOM/AX/Snapshot roots | same-origin frame 与独立 OOPIF 状态进入同一 capture/revision | 移除后 OOPIF target/session fixture 和真实 Chrome frame_count 证据失败 |
| `BrowserLiveSelectorProbeRuntime` | browser state application live-audit/preflight | `DOM.pushNodesByBackendIdsToFrontend`、`DOM.resolveNode`、`Runtime.callFunctionOn` | backend node ref 在当前 target/frame/session 中被真实解析 | stale identity 在任何 CDP call 前拒绝；移除后 selector resolution test 失败 |
| `BrowserTurnCausalRuntime` | message manager capture 后 | `BROWSER_SESSION_LIFECYCLE`、`ARTIFACT_WRITTEN`、`AGENT_MESSAGE` | action/capture/artifact/disclosure 可沿 event ids 和 digests 追溯 | digest/bytes/id 缺失时 audit 不通过，worker 不得宣称完整集成 |
| `BrowserContextApiProjectionRuntime` | `GET /tasks/{task_id}/browser-context` | TaskState checkpoint + persisted events/artifact ids | M2 可读取 queue、atomic tool pairs、memory signals、causal timeline | `disabled=True` 行为测试抛错，不返回静态空面板 |
| `BrowserMemoryCandidateConsumerPort` | BrowserWorker/API projection 下游 port | `AGENT_MESSAGE.browser_memory_candidate` | 验证 signal 可被 06B/06C schema/port 消费但不提前提交 canonical memory | `disabled=True` 行为测试抛错；不存在伪 accepted ACK |
| `BrowserCompressionAblationRuntime` | 明确 constraints 开启；live evaluator | fidelity report/bitmap artifact | 输出 full/structured/bitmap 的 bytes/tokens/fidelity/task_success | 默认关闭 bitmap；不会取代 DOM/selector/context owner |
| `BrowserMessageIntegrationAuditRuntime` | BrowserWorker result 返回前 | source roles、state custody、event/artifact linkage | 拒绝外部 parent repo 路径、缺失默认模块和因果断链 | audit invalid 时 `require_valid` 抛错 |

生产调用链为：

`POST browser worker -> 04A session/target/focus -> DOM/AX/Snapshot + frame composition -> selector revision -> action envelope -> low-entropy disclosure/artifacts/memory candidate -> causal audit -> TaskState checkpoint enqueue -> POST code worker -> 02D ClaudeContextWindowManager -> QueryEngine provider envelope -> exact-once commit -> GET browser-context projection`。

这条链由 API 和 worker 真实调用，不依赖 import smoke、health 固定返回、fixture replay 或 parent source repository。

## 4. Source-to-target 裁决

| 角色 | 来源路径/机制 | Zyra target | 裁决与唯一 owner |
|---|---|---|---|
| primary | `browser-use/browser_use/agent/message_manager/service.py`、`utils.py`、`views.py` | `browser_context/message_manager.py`、`action_envelope.py`、`task_integration.py`、`api_projection.py` | 拆解为 Zyra action/history/context/checkpoint/event 模块；TaskState 和 02D context owner 不变 |
| primary | `browser-use/browser_use/dom/service.py`、`enhanced_snapshot.py`、serializer/interactivity mechanisms | `browser_state/runtime.py`、`frame_capture.py`、`selector_probe.py` | 复用机制但改用 Zyra 04A target/CDP identity、event/artifact/schema；不保留 browser-use runtime |
| supplementary | `claude-code-best/src/query.ts` 及 batch-02/04/06/08 context/tool-result contract | `task_integration.py`、`code_worker_runtime.py`、`apps/api/zyra_api/main.py` | 接入既有 M1-02D `ClaudeContextWindowManager` 和 provider envelope；未复制第二套 compact owner |
| supplementary | `oh-my-pi/packages/agent/src/agent-loop.ts` | `action_envelope.py`、`causal_runtime.py` | 迁移 tool pair/result normalization 与因果边界 |
| supplementary | `oh-my-pi/packages/agent/src/append-only-context.ts` | `task_integration.py` | 迁移 append-only/read-once delivery 语义；TaskState 是持久 owner |
| experimental | `oh-my-pi/packages/snapcompact/src/index.ts` | `ablation.py` | 只承担 default-off bitmap 对照；不拥有 DOM、selector、context 或 restore，660 行不计 active production 最低线 |
| conformance | `agent-framework/python/packages/core/agent_framework/_harness/_loop.py` | integration tests/ledger | 只对照 fresh context/history；不生产平行 runtime |
| conformance | `opencode/packages/core/src/session/compaction.ts` | integration tests/ledger | 只对照 session/context epoch；TaskState/02D owner 不变 |
| reference | `hermes-agent/agent/context_compressor.py` | integration audit/ledger | 只用于长会话 failure/anti-thrash 反例；不迁移 global compact owner |

同步器记录 25 条 M1-04B 决策：16 migrated、4 reference、1 experimental、3 conformance、1 deferred。同步器新增字段保持旧 positional contract，避免把 migration strategy 错写为 source repository；`oh-my-pi` 作为 source graph 已采用来源加入 ledger policy 的 allowed（非 required）集合。

## 5. 状态责任与恢复边界

| 状态域 | Canonical owner | 持久/恢复位置 | 本 slice 是否夺权 |
|---|---|---|---|
| browser session/target/focus/CDP session | M1-04A BrowserSession/TargetRegistry | 04A runtime/session state | 否；04B 只读取并做 generation/focus guard |
| DOM capture/selector revision | `BrowserDomStateRuntime` / `BrowserSelectorMapStore` | capture/event/artifact projection；revision identity | 04B 父级 owner，未引入第二套 store |
| context budget/compact blocks/provider messages | M1-02D `ClaudeContextWindowManager` | `context_window_state` checkpoint | 否；04B 提供 external block，最终选择由 02D 验证 |
| disclosure delivery lifecycle | `BrowserContextTaskIntegrationRuntime` | `TaskState.metadata`，由 SQLiteStore 随 task 持久化 | 是，本 slice 首次分配的 owner；pending/claimed/consumed/released/indeterminate 可恢复 |
| task/event/artifact | 既有 `SQLiteStore`、event log、`LocalArtifactStore` | task/event/artifact configured roots | 否；使用现有 port，无新数据库/sidecar |
| memory canonical state | 后置 06B/06C MemoryFabric | 后置 memory store | 否；当前只生成和验证 candidate signal |
| permission/session custody | 既有 permission/02D session owner | 既有 permission store/TaskState | 否；第二次 API 调用的 409 来自既有 session custody，且 browser disclosure 已不重放 |

checkpoint 包含 scope digest、revision、queue/history/candidates/context state 和整体 digest。scope mismatch、digest corrupt、foreign session、重复 claim、provider 未精确选择、worker failure 都有确定性拒绝或 release/reconcile，不使用 LLM 作为 owner 或裁决器。

## 6. 量化行为与质量披露

真实本地 Chrome smoke 使用 1,800 个 section、open shadow DOM、same-origin iframe 和跨 host site-isolated iframe：

- 37 个真实 events、6 个 artifacts；清理成功。
- 捕获 3 个 frame，包含 1 个 cross-origin、1 个 OOPIF；OOPIF 使用独立 target/session identity。
- full DOM/state：20,003,787 bytes，约 5,556,608 tokens。
- structured disclosure：65,320 bytes，约 18,145 tokens；压缩比 0.003265。
- selector 总数 1,804，inline 96；selector fidelity 1.0。
- live CDP probe：16 attempted / 16 resolved / 0 failed；shadow DOM 计数 1。
- 三个 ablation lane 均输出 bytes/tokens、selector/fact/OCR/coordinate fidelity 和 task_success。
- structured lane 是默认，但其严格 coordinate fidelity 为 0.875，报告 `degraded/task_success=false`；没有用压缩率掩盖该下降。selector/fact fidelity 和 live resolution 仍为 1.0。
- bitmap lane 是 experimental/non-authoritative；没有 OCR provider 时明确 `unavailable`，不伪造 OCR 成功率。
- 低预算预跑在 16 KiB 预算下因约 42 KiB disclosure 触发 `BrowserStateBudgetExceeded`；成功场景把预算调到 65 KiB/20k tokens，不存在隐式越界。

smoke 的测试装配通过 CDP 创建页面和附着已由 04A 发现的 OOPIF；实际验收动作仍经 04A target runtime 与 BrowserWorker sealed read-only capture 主路径。测试用本地 HTTP server/Chrome 子进程不进入默认产品运行依赖或核心决策。

## 7. 有效行数分桶

基于 `git diff --numstat 6897ac7... d1ce095...`：

| 桶 | 新增行 | 是否计入 slice 5,000 行 | 说明 |
|---|---:|---|---|
| active production (`apps/**`、`packages/**`) | **5,017** | 是 | 已接入 API/worker/event/artifact/context 主路径；已主动扣除 experimental ablation |
| experimental production | 660 | 否 | `ablation.py`，default-off/non-authoritative |
| productized smoke/evaluator + ledger sync scripts | 574 | 否（保守） | 372 live evaluator + 202 source-ledger sync；即使工具可产品化计数，本审查也不用于跨线 |
| tests | 674 | 否 | foundation 增量 13 + integration 661；只作验证 |
| data/ledger seed | 1,452 | 否 | source-to-target ledger 数据，明确排除 |
| docs | 0（实现 commit） | 否 | 本自审在 evidence commit 单列 |
| generated | 0 | 否 | 无生成源码 |
| vendor/vendor-runtimes/source-pool | 0 | 否 | 无新增 |
| adapter-only | 0 | 否 | API glue 与转换代码均接入状态/错误/event，未单列计数充数 |
| mock/fixture | 0 production | 否 | fixture 仅在 tests；不计 production |
| 合计 raw added | 8,377 | - | 与 implementation commit 一致 |

父级累计：04B-01 已审 production 10,904 + 当前严格 active production 5,017 = **15,921**，达到父级 15,000 最低线。自动 line-count audit 报告 6,925 effective additions，但它包含 tests/scripts/experimental；本审查采用更严格的 5,017，不使用宽松数字完成门禁。

## 8. 验证记录

| 命令/范围 | 结果 |
|---|---|
| `pytest tests/integration/test_browser_message_state_compression_integration.py` | 5 passed（最终重跑见 evidence commit） |
| `pytest tests/integration/test_browser_message_state_compression_foundation.py` | 6 passed, 10 subtests / 95.30s |
| `pytest tests/integration/test_browser_session_productization_integration.py` | 9 passed / 60.91s |
| 02D context foundation + integration tests | 10 passed, 3 subtests / 39.34s |
| `pytest tests/unit/test_internalization_ledger.py tests/unit/test_internalization_ledger_accounting.py` | 18 passed / 1.75s；1 个既有 collection warning |
| `python scripts/smoke_browser_message_state_compression_live.py` | passed；真实 Chrome 指标见第 6 节 |
| `python scripts/sync_browser_message_state_source_ledger.py --write` 后二次 `--check` | passed；25 decisions，0 drift |
| 隔离 `ZYRA_INTEGRATION_LEDGER` 严格 audit | `ok=true`，0 blocker / 0 error / 420 warning |
| staged line audit，minimum 5,000 | tool `effective_added=6,925`、seed excluded 1,452、shortfall 0；人工严格 active=5,017 |
| `python -m compileall` affected API/workers/scripts | passed |
| `git diff --cached --check` | passed |
| dependency/vendor/path audit | pyproject/lock 无改动；vendor/vendor-runtimes 新增 0；strict ledger parent-path blocker 0 |

420 warnings 是全 seed 中许可证、后续 planned targets、跨 owner/shared entry 等未冻结事项；本 slice 先修复了自身引入的 parent-path blocker 和 14 个错误 source_repo records，最终没有把新 blocker/error 留给聚合审查。

## 9. 批判式缺口、残留债务与风险升级判定

### 非阻断残留

1. structured lane 的 coordinate fidelity 为 0.875，已显式 degraded；04C 的 click/input 消费和 M1-08 的真实交互验收必须继续使用 live selector probe 与 target identity，不能只看 selector fidelity。
2. 真实 smoke 已捕获 OOPIF 独立状态；对 OOPIF selector 的精确 target/session 路由由 integration fake-CDP 行为测试覆盖，live smoke 的 16 个抽样 probe 未宣称覆盖每个 target。数字阶段聚合可扩大 real-CDP 跨 target sample。
3. 06B/06C 尚未把 candidate 提交 MemoryFabric，这是执行边界而非 04B 缺失；当前 port 已证明 schema/causation 可消费。
4. bitmap lane 没有 OCR provider，因此只报告 unavailable；它按计划 experimental，不能成为默认能力或生产行数依据。
5. 全 seed 尚有 420 warnings，必须由对应后续 owner/聚合审查处理；当前 slice 没有倒填未来 unit。

### 高风险触发审计

- 未做不兼容公共 schema/event/persistence migration；新增内容位于 TaskState metadata 的 versioned additive key。
- 未转移既有 canonical state owner；02D context、04A target/session、artifact/event/memory owners 保持不变。
- 未改变全局 permission/scheduler/recovery/compact 默认策略。
- 未向默认主路径新增 pip/npm dependency、MCP server、plugin、Docker、local port service、dynamic import 或外部 source repo dependency。
- live evaluator 自行启动的 Chrome/HTTP server 只属于显式测试装配，不承担产品核心决策。
- ledger policy 只把 source graph 已采用的 `oh-my-pi` 加入 allowed evidence repo，不改变 required repo、runtime state 或完成状态。

因此本 slice **未命中 AGENTS.md 高风险升级条件**。未运行全仓测试和完整 cleanroom：它们按分层规则移至 M1-04A～04D 数字阶段聚合审查；当前已运行受影响 04A、04B、02D、ledger 路径和真实 Chrome smoke。若后续代码改变上述 state owner、默认策略或依赖边界，必须在聚合审查前按影响重跑。

## 10. 父级收口判定

- 04B-01 foundation：DOM/AX/Snapshot、selector map、low-entropy disclosure、artifact externalization、action result、memory signal 基础模块已保护完成。
- 04B-02 integration：上述模块已进入 BrowserWorker→TaskState→02D provider→event/artifact/API 主路径，并具备 malformed/oversize/stale/restart/read-once/disabled/budget failure 证据。
- 父级有效生产累计 15,921 行，超过 15,000。
- source roles、state custody、主路径、失败路径、真实 CDP smoke、量化 fidelity 和 ledger 均有证据。

据此 `M1-04B` 可标记完成，下一入口为 `M1-S04C-01`。数字阶段 M1-04A～04D 聚合审查仍是后续强制层级，不被本自审替代。
