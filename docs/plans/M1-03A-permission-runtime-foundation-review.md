# M1-S03A-01 Permission Runtime Foundation Adversarial Review

## Review Status

结论：`verified_pass`，仅关闭 `slice-03a-01-permission-runtime-foundation.md`。

本 review 不关闭父级 `M1-03A`。下一入口必须是
`slice-03a-02-permission-runtime-integration.md`；父级 16,000 行累计门槛、结构化审批 API、
bridge/interactive resolve-retry 和完整交互式恢复仍由下一切片承担。

证据基线为 `e7f679f04f6a043bf99618eddb4198a2e1c4e721`。运行时实现提交为
`46790d8`，中央 source-to-target ledger 校准提交为 `c17c528`，本文件所在提交是最终
review evidence commit。根目录 `docs/milestones/execution-state.yaml` 不属于 Zyra Git 仓库，
需在 review evidence commit 产生后单独更新。

## Outcome

本切片把 permission 从旧静态规则/审批投影提升为默认 CodeWorker 工具副作用前的确定性
runtime guard。连续主路径为：

`CodeWorkerRuntime -> ZyraClaudeQueryEngine -> ToolExecutionRuntime -> ToolPermissionRuntime -> ToolExecutor`

Zyra 现在拥有 rule/mode/risk、hook/classifier advisory、pending queue、decision log、exact
approval identity、durable approval claim、one-use execution grant、session custody、sealed deny、
recovery input 和 permission event projection。模型输出中的 `approved=true`、调用方自带 validator、
worker constraints 中的 bypass/auto 请求、复用 request/grant、跨 session/workspace/server 的响应均不能
直接授权副作用。

## Target Coverage Matrix

| Slice 目标/详细任务 | 状态 | 证据 | 阻断 |
| --- | --- | --- | --- |
| 完整来源裁决与边界拆解 | 完成 | `permission/source_audit.py` 的 35 条显式 decision；24 条 Claude 主来源，11 条补强来源 | 否 |
| allow/deny/ask、scope、expiry、reason、rule source schema | 完成 | `models.py`、`rules.py`、`canonical.py`、`store.py` | 否 |
| deny -> ask -> tool/risk -> hook/classifier -> mode 的确定性顺序 | 完成 | `evaluator.py`、`risk.py`、`modes.py`；deny/bypass/hook/classifier tests | 否 |
| `ToolPermissionRuntime`、RuleStore、Mode、Queue、DecisionLog、Hook/Classifier owner | 完成 | `permission/**` 正式 package；无上游 runtime/sidecar 参与核心裁决 | 否 |
| pending created/delivered/resolved/expired/cancelled/aborted foundation | 完成 | `request_queue.py`、`store.py`、immutable request/response state | 否；API delivery/resume 属 03A-02 |
| exact request identity 与 resolve-once | 完成 | request/session/tool/server/canonical digest/scope/expiry/revision；跨实例原子 claim | 否 |
| tool call 前 guard 与最后副作用边界 | 完成 | `tool_runtime_foundation.py` guard；`executor.py` authority-owned grant consume | 否 |
| session-backed standing rule/pending/decision snapshot | 完成 | `.permission/state.json`、permission snapshot/restore monotonic merge、custody binding | 否 |
| sealed 零人工、危险动作零副作用、recovery input | 完成 | sealed shell behavior、human metric、recovery causality tests | 否 |
| hook/classifier/user transport 边界 | foundation 完成 | adapter timeout/redaction/deny-precedence；transport output 不拥有最终 decision | 否；生产 registry wiring 属 03A-02 |
| 动态可达、断开即失败、失败/并发/重放测试 | 完成 | CodeWorker/API integration、disable runtime/rule/queue/decision/budget tests | 否 |
| source-to-target 中央 ledger | 完成 | `c17c528`：35 条、34 productized、1 deferred、0 vendored、0 旧 target | 否 |
| 8,000 行 production 门槛 | 完成 | permission package 排除 `source_audit.py` 后 `+10,425`；更保守核心 `+8,012` | 否 |
| 干净目录与提交边界 | 完成 | `git archive c17c528` cleanroom；90 tests、ledger audit/accounting、boundary | 否 |
| 批判式自审与 evidence commit | 完成 | 本文件及其 containing commit | 否 |

## Main Path Evidence

| 能力 | Zyra owner/目标路径 | Runtime/API 入口 | Event/state 接入 | 行为测试 |
| --- | --- | --- | --- | --- |
| policy composition | `permission/evaluator.py`、`risk.py`、`rules.py`、`modes.py` | `ToolPermissionRuntime.guard()` | decision source/reason/risk/mode | precedence、sealed、bypass、hook/classifier tests |
| durable rules/requests/decisions | `permission/store.py`、`request_queue.py`、`decision_log.py` | session runtime construction/restore | one JSON authority + revision/CAS | cross-instance, replay, expiry, concurrent resolver tests |
| pre-call tool guard | `tool_runtime_foundation.py` | QueryEngine tool turn | permission events before tool start | CodeWorker read/ask/deny/disable tests |
| final side-effect authorization | `executor.py`、`grants.py` | file/shell/network executor | grant issued/consumed/rejected | exact binding, one-use, tamper, workspace mismatch tests |
| CodeWorker session custody | `custody.py`、`code_worker_runtime.py` | new/resumed worker session | token hash and run/task/workspace binding | concurrent first claim, hijack, secret-redaction tests |
| query/session recovery signal | `events.py`、`claude_query_engine_runtime.py` | sealed/denied tool turn | `permission_decision -> recovery_input` | zero-human sealed and denial-limit abort tests |
| direct task tool API | `apps/api/zyra_api/main.py` | `POST /tasks/{task_id}/tools` | permission events plus canonical tool result | API control/tool regression tests |
| default CodeWorker API | `apps/api/zyra_api/main.py` | `POST /tasks/{task_id}/workers/code` | session/tool/permission event stream | CodeWorker permission integration tests |

Raw model `approved` metadata is ignored. The legacy `/permissions` store remains a compatibility projection and
cannot mint a new execution grant or authorize shell. Direct API low-risk actions use the same exact one-use policy
capability path; shell/network/unknown actions remain ASK or sealed DENY.

## State Custody And Restore

| State | Unique owner | Persistence/restore rule | Security property |
| --- | --- | --- | --- |
| global/session rules | `PermissionStateStore` / `PermissionRuleStore` | `artifact_root/.permission/state.json`; global base plus frozen session base and monotonic overlay | finite rules are atomically shared/consumed; stale snapshots cannot roll back newer use counts |
| pending approvals | `PermissionStateStore` / `PermissionRequestQueue` | durable request revision and terminal phase | resolve/claim once across fresh runtimes; expiry rechecked at resolution and claim |
| decisions/metrics | `PermissionDecisionLog` backed by state store | append/merge by durable identity | interactive approval increments human metric; sealed path remains zero |
| session custody | `PermissionSessionCustodyStore` | PBKDF2 hash only; run/task/workspace binding; atomic first claim | plaintext token is nonserialized and removed from report/event/model projections |
| execution grants | `ExecutionGrantStore` owned by `ToolPermissionRuntime` | HMAC/nonce, short expiry, exact binding; signing key is not checkpointed | unconsumed grants are invalid after restore; a durable approved request may issue one fresh grant |
| permission checkpoint | QueryEngine/CodeWorker session snapshot | safe rule/request/decision/mode projection; deployment policy clamps restored modes | old snapshots cannot enable `auto_in_plan`, revive claims, or widen current deployment authority |

There is no second permission truth in an upstream process, MCP server, plugin, Docker image, dynamic import or
editable parent repository. LLM/classifier output is advisory only.

## Approval Identity And Causal Ordering

Pending approval identity binds all of:

- `request_id`, `session_id`, `run_id`, `task_id`, `tool_use_id`;
- tool namespace/name plus MCP/server identity;
- canonical argument digest;
- scope/workspace identity and expiry;
- durable request revision and terminal/claim state.

The side-effect ordering is:

`evaluation started -> hook/classifier advisory -> durable decision -> request or grant -> tool start -> atomic grant consume -> side effect/result`.

A blocked/ASK path has no tool start or side effect. Grant material is prepared before committing a durable allow,
but is not exposed until the durable rule/decision transaction succeeds; failure invalidates the prepared grant.
Permission events and recovery events carry the same tool/session identity and precede the associated execution.

## Source-To-Target Internalization

`PERMISSION_SOURCE_DECISIONS` contains 35 complete decisions:

| Source group | Count/disposition | Zyra target | Decision |
| --- | --- | --- | --- |
| Claude permissions/rule/mode/denial/risk/tool execution | 16 active plus contract/reference items | evaluator, rules, modes, risk, store, executor handoff | rewritten/productized into one Zyra state machine |
| Claude hook/classifier/bridge/interactive handlers | adapter decisions | hooks, classifier, queue, resolution models | transport/advisory semantics retained; authority removed |
| Agent Framework approval + AG-UI pending/message | 3 | queue, rules, exact resolution models | supporting adapter patterns |
| opencode permission/question/ACP | 3 | store, queue, evaluator | supporting adapter/reference; last-match override is not retained |
| AgentScope PermissionEngine | 1 | risk/evaluator | tool precheck pattern adapted |
| OpenClaw policy pipeline/before-call | 2 | rules/hooks/tool guard | layered diagnostics and before-call semantics adapted |
| Hermes staged/write approval | 2 | queue/risk | staged notification adapted; yolo/smart approval not authoritative |

Disposition totals are `17 active`, `12 adapter`, `3 reference-only`, `2 contract-only`, `1 deferred`.
The only deferred item is Claude `bashClassifier.ts`: upstream is a placeholder, current deterministic
`ToolRiskPolicy` is the active replacement, and structured Bash AST enrichment is assigned to M1-S03A-02.

The central bundled ledger was regenerated from this source map by
`scripts/sync_permission_source_ledger.py`. Its M1-03A projection contains six repositories, 35 identities,
34 productized/tested-main-path rows and one reasoned deferred row. It contains no `vendored_runtime` strategy,
no old `packages/runtime/zyra_runtime/permissions.py` target and no runtime parent-path reference. Supporting
multi-source bindings do not create false ownership conflicts; only competing primary owners are conflicts.

The seed, synchronizer and `source_audit.py` are evidence/audit machinery. They do not establish the line minimum
and are explicitly excluded below.

## Adversarial Findings And Remediation

| ID | Finding | Remediation/evidence | Status |
| --- | --- | --- | --- |
| 03A-R1 | normal ALLOW grant used a different identity at executor boundary | full request identity is carried into executor; exact real shell consume test | fixed |
| 03A-R2 | hook argument rewrite could retain pre-rewrite workspace safety | arguments are re-canonicalized and workspace preconditions recomputed | fixed |
| 03A-R3 | caller-chosen session id could inherit another session rules | server-random custody token, hash-only store, run/task/workspace binding | fixed |
| 03A-R4 | browser/web search could be mistaken for local read | network/egress capability is classified as ASK/sealed DENY | fixed |
| 03A-R5 | sealed deny plus `continue_on_error` could return success | denial circuit breaker/recovery preserves failure and abort semantics | fixed |
| 03A-R6 | worker constraints could enable bypass/auto | deployment-owned mode capabilities; constraints cannot widen authority | fixed |
| 03A-R7 | permission event could appear after tool-start | guard and permission events moved before executor/tool-start | fixed |
| 03A-R8 | public execution runtime could run without permission authority or validate budget after side effect | fail-closed authority and budget dependency checks occur before permission/execution | fixed |
| 03A-R9 | finite global rules were copied per session | atomic global use count shared across frozen sessions | fixed |
| 03A-R10 | approved request claim was process-local | durable state-store claim; two fresh runtimes have one winner | fixed |
| 03A-R11 | caller-supplied `grant_validator` could bypass authority | executor owns a fixed `ToolPermissionRuntime`; caller validator rejected | fixed |
| 03A-R12 | mutable ToolCall/workspace context allowed TOCTOU | deep-copied call plus frozen workspace/registry scope | fixed |
| 03A-R13 | old snapshot could expand mode or roll back newer overlay/request/decision | deployment clamp plus monotonic restore merge | fixed |
| 03A-R14 | relative path scope used process cwd | path/scope canonicalization is bound to request workspace | fixed |
| 03A-R15 | approved request expiry and grant commit order were unsafe | expiry checked at claim; grant invalidated if durable commit fails | fixed |
| 03A-R16 | human intervention metric was always zero | durable decision events count real user approval; sealed remains zero | fixed |
| 03A-R17 | central ledger still claimed planned vendored permission runtime | `c17c528` replaced stale rows and added source-map/ownership tests | fixed |
| 03A-R18 | adding opencode to every historical seed requirement broke M1-01B fixtures | split allowed source repos from baseline-required repos; authoritative seed still requires/tests opencode | fixed |

## Disable And Semantic Effect Evidence

- Disabling `ToolPermissionRuntime`, rule store, request queue or decision log fails closed before the executor.
- A disabled tool-result budget dependency fails before permission evaluation or workspace mutation.
- Raw model approval cannot create a shell side effect.
- Direct file mutation without an execution grant has zero side effect.
- A valid real-shell grant executes exactly once; replay/tamper/workspace/server/argument mismatch cannot consume it.
- Sealed shell creates a deny and `recovery_input`, leaves the filesystem unchanged and reports zero human intervention.
- A default shell creates an exact pending ASK and no side effect.
- Concurrent resolvers/claimers and finite global/session rules have exactly one winner.
- Removing source ledger alignment reintroduces vendored/old-target rows and fails the new source coverage regression.

## Effective Line Count Review

Base commit: `e7f679f04f6a043bf99618eddb4198a2e1c4e721`.
Code/evidence head: `c17c528`.

| Bucket | Numstat / calculation | Counted toward 8,000 | Conclusion |
| --- | ---: | --- | --- |
| raw apps/packages/skills/scripts | `+15,632 / -2,071` | not directly | includes data and audit tooling; raw total is not used |
| bundled ledger seed data | `+2,706 / -1,896` | no | data/source-to-target evidence only |
| `source_audit.py` + ledger sync script | `+919` (`601 + 318`) | no | source map/audit tooling only |
| production after named data/audit exclusions | `+12,007` | yes, subject to behavioral review | default-path code plus ledger policy integration |
| permission package excluding `source_audit.py` | `+10,425` | yes | standalone permission production package exceeds 8,000 |
| conservative state-machine core | `+8,012` | yes | additionally excludes `models.py`, `__init__.py`, classifier, hooks and canonical modules |
| tests | `+3,448 / -58` | no | behavior evidence only |
| docs/review | excluded | no | evidence narrative only |
| vendor/vendor-runtimes | `0` | no | no new vendor/source-pool implementation |

The conservative `8,012` consists only of risk/store/runtime/grants/evaluator/request queue/rules/modes/decision
log/events/custody. This deliberately excludes real but schema- or adapter-adjacent modules, so the threshold does
not depend on DTO volume, hooks/classifier adapters, API/scenario compatibility glue, audit scripts, seed data or
tests. No generated, mock-only, fixture-only, data-as-code or source-pool lines are counted.

The repository linecount CLI reported a broader effective number because its default surface includes tests; that
number is not used as the production gate.

## Tests And Verification

| Command / verification | Result |
| --- | --- |
| `python -m unittest tests.unit.test_permission_runtime_foundation tests.integration.test_code_worker_permission_runtime_foundation` | `OK, 72 tests` after ledger regression was added |
| ledger unit/accounting regression set | `OK, 18 tests` |
| historical M1-01B compatibility set after allowed/required split | `OK, 17 tests` |
| combined permission + ledger set | `OK, 90 tests` |
| implementation-snapshot full discovery before ledger-only calibration | `OK, 357 tests, 344.730s` |
| first final-tree full discovery attempt | `358/359 passed`; one transient live browser readiness failure outside this slice |
| isolated rerun of failed live browser test | `OK, 1 test, 8.820s`; Windows Proactor cleanup warning only |
| final `python -m unittest discover -s tests` rerun | `OK, 359 tests, 341.398s` |
| `scripts/sync_permission_source_ledger.py` | aligned, 35 decisions |
| ledger audit with bundled seed and `--owner-unit M1-03A --fail-on-error` | exit 0; 0 errors, 0 blockers; global report retains pre-existing warnings from other units |
| ledger accounting with bundled seed and `--owner-unit M1-03A --fail-on-error` | exit 0; 0 findings |
| `scripts/verify_submission_boundary.py` | passed |
| `compileall` and `git diff --check` | passed |
| `git archive c17c528` final cleanroom | imports resolved inside archive; 90 tests passed in 14.096s; ledger audit/accounting/boundary passed |

The first final-tree full-discovery attempt hit a transient browser live-page readiness timeout. The exact test passed
immediately in isolation and the subsequent complete 359-test discovery passed. It is retained as process evidence
rather than hidden. Windows asyncio Proactor cleanup/readiness warnings did not alter the final exit code.

## Requirement Alignment

| Requirement / score | This slice's real contribution | Matrix state after slice | Remaining closure owner/evidence |
| --- | --- | --- | --- |
| `REQ-CLOSE-01` | sealed low-risk allowlist, high-risk deterministic deny, `human_intervention_count=0`, recovery input | remains `planned` | M1-08/M2-05/M3-02A live multi-agent closed loop |
| `REQ-TRACE-01` | causal permission decision/grant/recovery events bound to tool/session identity | remains `planned` | M1-08 and M2 timeline/UI live trace |
| `SCORE-LOOP` | permission decision and recovery become valid canonical state transitions; blocked side effect cannot be counted as success | not closed | one run with at least 2,000 valid transitions |
| `SCORE-ROBUST` | forged/replayed/expired/concurrent/cross-scope failures are deterministic and recoverable | not closed | fault matrix, repeat live runs, MTTR/recovery rate |
| `SCORE-ALGO` | Zyra-owned deterministic ordering, exact identity and state custody replace prompt approval | not closed | system-level pseudocode, complexity and ablation evidence |

This foundation does not claim a live multi-agent benchmark, 2,000-transition run, dynamic topology, real edge/cloud
dispatch or final competition gate closure. It supplies the permission state transition and sealed-policy substrate
those downstream owners require.

## Explicit M1-S03A-02 Handoff

The following are intentionally not claimed by this foundation and must be implemented in
`slice-03a-02-permission-runtime-integration.md`:

- structured permission API for query/create/deliver/resolve/retry/resume;
- a secure API envelope for returning/rotating session custody material;
- real coordinator/swarm/interactive/bridge transport registration;
- production hook/classifier registry and configuration wiring;
- projection of resolved/denied/expired/cancelled/aborted events into the active session and recovery loop;
- user DENY consumption on the next exact guard and exact-call retry/resume semantics;
- migration/removal of the legacy permission projection without allowing legacy approval to mint grants;
- Bash AST/parser enrichment beyond the current deterministic `ToolRiskPolicy` replacement;
- parent M1-03A 16,000-line cumulative gate and full API/interactive acceptance.

These items do not block the foundation because the durable state machine, ports, exact identity, deterministic
guard, side-effect boundary and handoff contracts are present and behavior-tested. They do block any claim that the
parent unit or full interactive approval product is complete.

## Completion Gate

The slice passes source-to-target, production line, default main-path, state custody, exact identity, semantic effect,
disable/mutation, concurrency/replay, sealed recovery, cleanroom, submission-boundary and evidence-ledger gates.
There are no unresolved blockers for M1-S03A-01.

The parent `M1-03A` remains incomplete, and the only valid next execution entry is
`slice-03a-02-permission-runtime-integration.md`.
