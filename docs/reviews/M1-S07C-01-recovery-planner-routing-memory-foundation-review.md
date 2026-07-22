# M1-S07C-01 recovery planner、routing、memory foundation 批判式自审

日期：2026-07-22

baseline：`aa494526ed7f32b177f9a10fa3a93d8a684e0264`

实施前冻结：`fd3fb81e0c0d099bfc384e16eeed88e48e7ad9c8`

implementation：`0b331ca6f709a24e663136939257aa061010aacf`

## 结论

本 slice 完成。Zyra 现在由 Python `RecoveryDecisionRuntime` 与 `RecoveryPlanStore` 唯一承担
recovery policy、计划、checkpoint、route receipt、action cursor、routing feedback 和审计 custody；
OMP 只以裁剪后的 TypeScript retry/fallback、append-only session/fork、task terminal generation 与
worktree merge replay-fence receipt 补充候选证据；LangGraph 只提供 checkpoint identity、lineage、
pending/committed write、stable id、atomic commit 与 exact-resume 的窄域 conformance 语义，不进入生产依赖。

保守逐文件审计得到 **8,429 行**有效 production，高于本 slice 最低 **8,000 行**，余量 429。
Python 有效 production 7,568 行，TypeScript 有效 production 861 行；测试、type/interface、DTO/schema、
ledger/data、manifest、普通审计脚本、export 和文档/注释均未计入。机器证据见
`docs/reviews/evidence/M1-S07C-01-recovery-planner-routing-memory-foundation.json`。

## 来源裁决与唯一 owner

| 子域 | 裁决 | 生产 owner / 边界 |
|---|---|---|
| recovery policy、plan、action、outcome | Zyra-owned primary | `RecoveryDecisionRuntime`、`RecoveryActionRuntime`、`RecoveryPlanStore`；LLM/advisor 只能给候选或解释 |
| exact resume checkpoint | LangGraph 窄域 `conformance_only` | `CheckpointCommitRuntime`、`CheckpointResumeBridge` 与 `RecoveryPlanStore` 自主实现；未引入 StateGraph/channel/Pregel/stream/ToolNode/Store |
| retry/fallback/session/task/worktree receipts | OMP `supplementary_implementation` | TypeScript 裁剪模块只拥有当前 process 内 receipt/fence；Python store/policy 是 canonical owner |
| task、graph、worker、backend、provider/model | 复用既有 canonical owner | TaskState/SQLiteStore、GraphStateCustody、07A WorkerPool、05D BackendRegistry/ProviderControlPlane；07C 只持有 before/after ref 与 applied receipt |
| permission、MCP、compact/memory | 复用既有 canonical owner | PermissionControlPlane、MCP event/control、MemoryFabric；07C 只发 action request 并持久化结果 |
| 其它来源 | conformance/reference | Claude Code failure/permission/compact/MCP/subagent/stream 链、Agent Framework、AgentScope、Hermes、opencode 只校验命名和边界，不产生迁移配额 |
| OpenClaw | `excluded_forward_only` | 未读取、未恢复、未引入源码、包、进程、路径或运行依赖 |

实施前 decision 已固定 OMP `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`、LangGraph
`5931a5f0b313feff24e2516a586c55601b868ac1` 和 Claude Code
`c57f5a29e88e9a814bea47abeb9a0a6f725dc102`。生产 ledger 只新增 OMP supplementary 一项；
LangGraph 是 conformance-only，故只记录在 decision/review/evidence，不伪造 production custody 或行数义务。

## 产品化模块与状态 custody

| 模块 | 责任与状态 |
|---|---|
| `contracts.py` / `signal_classifier.py` | typed signal/reason/candidate/plan/checkpoint/route/delta/receipt 契约；permission、tool、MCP、stream、compact、subagent、worker、backend、requirement change 确定性分类，关键 identity 不从自由文本推断 |
| `policy.py` | 基于 signal、owner snapshot、route memory、预算、sealed policy 和 replay risk 排序 action；输出 retry、reroute、degrade model、switch backend/provider、replan、ask permission、authenticate MCP、compact/resume、abort |
| `store.py` / `safe_codec.py` | SQLite FULL transaction/CAS/idempotency；plan/signal/action/outcome/checkpoint/head/write/receipt/fence/route/feedback/delta/journal；只接受有界 versioned JSON，原子导出并拒绝 path escape、corrupt document 与 pickle |
| `checkpoint_runtime.py` / `delta_journal.py` | atomic checkpoint、signature/ancestry/revision、pending/committed separation、processed message/response bypass、side-effect fence、branch-local copy-on-write、read/write-set conflict 与 deterministic commit |
| `route_runtime.py` / `task_owner_runtime.py` | graph、worker、backend、provider/model 分层请求对应 owner；保存每层 before/after ref、policy、escalation 与 owner receipt，不复制 lease/store |
| `action_runtime.py` / `application.py` | durable plan lease、action cursor、idempotency、concrete owner call、outcome 与 feedback；组成 signal→plan→action→memory/event 的默认链 |
| `memory_feedback.py` / `context_runtime.py` / `audit_runtime.py` | 把成功/失败、penalty、route evidence 写入 routing memory并影响下一次排序；构造 owner context；审计 causality、custody、receipt 和 journal integrity |
| TypeScript recovery modules | `OmpRecoveryReceiptRuntime` 实现 bounded retry/backoff/fallback/replay eligibility；continuity runtime 提供 append-only resume/fork、terminal generation、worktree merge receipt；watchdog 输出结构化 `recovery_receipt` |
| API 主路径 | GET task/plan view；POST signal、07B fault handoff、07A worker handoff、checkpoint/resume、delta、resume-waiting；真实调用 TaskState、GraphStateCustody、WorkerPool、BackendRegistry、ProviderControlPlane、Permission、MCP 与 MemoryFabric |

`RecoveryPlanStore` 只拥有 07C 的恢复事实，不复制 task、graph、worker lease、backend lease、provider route、
permission、MCP 或 MemoryFabric 状态。每次跨 owner 动作保存请求、before/after ref、canonical receipt 与错误，
因此 event/trace 能追溯到实际 state mutation，而不是只记录建议。

## 主路径、语义效果与断开即失败

1. `POST /tasks/{task_id}/recovery/signals` 接收非 fixture typed signal，经 classifier、policy、durable plan、
   concrete action、routing feedback 与 canonical event 完成闭环。tool retry 会真实改变 TaskState retry generation；
   requirement change 会提交 GraphStateCustody replan，而不进入 failure retry。
2. permission pending/denied 进入 ask-permission；prompt-too-long 进入 compact/restore；MCP auth-required 进入
   authenticate control；stream stall/retry exhausted 进入受预算和 side-effect fence 约束的 retry/fallback；
   backend、worker、subagent failure 分别请求对应 owner 新 lease 或 replan。
3. checkpoint API 保存 graph/topology signature、ancestry、immutable refs、in-flight messages、pending requests、
   committed/pending writes 和 owner versions；resume 拒绝 signature mismatch/corrupt JSON/path escape，跳过已完成
   step、已处理 response 与已提交 side effect。
4. delta API 的 branch-local nested state 不与 snapshot 共享别名；冲突 write-set 被拒绝，未提交 delta 不进入
   canonical state，稳定排序保证 deterministic commit。
5. 删除/禁用 recovery application 后 signal route 返回 503，TaskState、GraphState 和 routing memory 均不变化，
   没有旧 planner 或事件-only fallback。删除 TypeScript supplementary 后 watchdog 不再产生 recovery receipt；
   删除 checkpoint/delta owner 后 exact-resume、conflict 和 replay-fence 测试失败。

## 批判式缺陷发现与修复

1. 初始 API route 被误插到 GET handler，POST recovery 无法动态到达。已把 route dispatch 移入 `do_POST`，
   HTTP integration 覆盖真实 signal/checkpoint/delta/resume。
2. Windows 下 store 的只读连接在异常路径滞留，临时目录清理失败。`_ClosingConnection` 统一关闭读取 handle，
   直接与相邻测试不再泄漏 07C SQLite 句柄。
3. checkpoint 的 processed response 列表未纳入 replay bypass，且 replay token 不稳定。现按 message/response/
   side-effect 三类 fence 构造确定性 token，重复 resume 不再重发外部效果。
4. 仅有 retry/fallback receipt 不足以覆盖父级指定的 OMP session/task/worktree recovery boundary。补入
   append-only resume/fork CAS、terminal generation/idempotency 与 worktree merge conflict receipt，并保留
   TypeScript 原语言行为测试。
5. generic line-count 会把 tests、DTO、interface 和 manifest 当有效代码。新增非生产审计器逐文件用 Python
   AST/token 与 TypeScript declaration range 扣除，最终有效值 8,429；审计器本身不计行数。
6. LangGraph conformance 项一度被写入 production ledger。收口时移除该伪 production custody，只保留 OMP
   supplementary ledger；LangGraph 裁决继续由 decision、conformance test 和本 evidence 承担。

## 有效行数分桶

统计区间固定为 `aa494526ed7f32b177f9a10fa3a93d8a684e0264..0b331ca6f709a24e663136939257aa061010aacf`。

| 桶 | 行数 | 计入最低线 |
|---|---:|---|
| raw additions（含 predecision 与 tests） | 12,387 | 否 |
| type/interface/declaration/export | 485 | 否 |
| schema/DTO/data/manifest | 1,831 | 否 |
| tests/mock/fixture | 739 | 否 |
| docs/comments/blank | 903 | 否 |
| adapter-only | 0 | 否 |
| generated/vendor/source-pool | 0 | 否 |
| **conservative effective production** | **8,429** | **是** |

按来源角色与语言：Zyra-owned Python recovery primary 6,692，Python API/main path 876，OMP
supplementary TypeScript 861。所有逐文件 raw、排除桶和 effective 值均在机器 evidence 中。raw additions
超过 500 的 11 个文件已逐一人工检查：`main.py` 是真实 owner port/API wiring；`continuity-runtime.ts` 与
`omp-recovery-runtime.ts` 是原语言状态机；`action_runtime.py` 是 action lease/cursor/execution；
`audit_runtime.py` 是运行态 causality/custody 检查；`checkpoint_runtime.py` 和 `delta_journal.py` 是恢复合同；
`policy.py` 是确定性决策；`signal_classifier.py` 是 typed classification；`store.py` 是 transaction/CAS owner；
`contracts.py` 的 1,108 行 DTO/schema 已排除，只计 289 行 validation/digest/refinement 行为。

## 验证

- 直接 Python：`5 passed, 6 subtests passed in 9.12s`。覆盖分类矩阵、policy/预算、safe codec、atomic
  checkpoint/exact resume、delta conflict、routing memory、真实 HTTP TaskState retry、GraphState replan、
  checkpoint/delta API、disable 503。
- 当前与相邻 Python：`56 passed, 6 subtests passed in 58.49s`。覆盖本切片、07B foundation/integration、
  07A worker pool foundation/integration。
- TypeScript 原语言：`9 passed`；覆盖 bounded retry、response/side-effect fence、append-only session/fork、
  task terminal、worktree merge、watchdog receipt/disable/custody。
- 全仓 TypeScript typecheck：通过；Python `compileall`：通过；`git diff --check`：通过。
- source ledger sync `--write` 后 `--check`：aligned，恰好 1 个 `M1-S07C-01` production decision；seed
  targeted audit 为 1 entry、0 validation error、0 current-unit finding。
- strict effective-code gate：8,429/8,000，OMP TypeScript 与 Zyra Python production 都非零。
- dependency/path scan：当前 diff 没有 sibling source-repo、`../` runtime、npm link、pip editable、外部
  Docker context、cache/vendor/source-pool、OpenClaw 或新 lock/dependency。

扩展但非当前 gate 的旧 `test_worker_pool_api_main_path.py` fanout 用例仍稳定出现第二个并发 child failed；
绕过 07C API 初始化后结果不变，detached baseline 也不能通过该用例（更早停在既有 retrieval context）。
因此未越权修改受保护 worker/subagent owner，并把该项保留给 M1-07 聚合回归。ledger 全套 67 个 unit
测试中 64 个通过，3 个失败来自既有 M1-01B missing target 与 MCP route discovery；当前 07C seed finding 为 0。
Claude runtime 的通用 `npm test` 仍受既有 Node strip-types/`.js` import runner 问题影响；本 slice 新增的
`test:recovery` 与全仓 typecheck 均通过。

普通 foundation slice 未执行完整 cleanroom或全仓测试。当前未新增外部依赖、进程、端口、MCP server、
Docker、dynamic import、canonical owner 转移、事务语义迁移或全局默认策略变更；本 unit 首次建立已分配的
07C owner 不属于 owner transfer。完整 cleanroom、全量 ledger/source-to-target audit 与 parent 15,000 行
累计门禁由 `M1-S07C-02` 收口及 M1-07 数字阶段聚合承担。

## 未伪报与下一入口

本 slice 不声称关闭两个跨领域 live task、单 run 2,000 canonical transitions、sealed zero-human、动态图
低熵对照、真实 local/isolated-edge/cloud dispatch、多模型或最终交付冻结门禁；也不提前把父级 M1-07C
标记完成。下一入口严格为 `M1-S07C-02`。

evidence commit 创建后才更新根目录
`G:/agent-zoo/docs/milestones/execution-state.yaml`。该文件不属于 Zyra Git，最终交付必须单独说明。
