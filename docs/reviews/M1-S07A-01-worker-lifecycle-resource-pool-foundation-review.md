# M1-S07A-01 Worker Lifecycle / Resource Pool Foundation Review

Date: 2026-07-21

Review level: slice closeout with language-custody remediation and exact-final-commit cleanroom.
Parent `M1-07A` remains incomplete; `M1-S07A-02` is the next authority entry.

Baseline: `f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b`

Implementation commit: `150389567046f0cf43205b2e649e74f8543421ae`

Review-fix commits: `31d4dac8748de88526e4936664905f41252b7b40`,
`0e2826bedb420db4ef550a40532d758c92a1258c`

Final cleanroom target: `0e2826bedb420db4ef550a40532d758c92a1258c`

## 结论

**修复后通过。** 原提交 `83251b1` 和原 evidence `569ce4b` 的通过结论已作废：它们把规划为
OMP TypeScript/native supplementary implementation 的能力全部写成 Python，而且物理 lease 在子任务执行后
才分配，不能约束真实 dispatch。修复后，AgentScope lifecycle 继续由 Python `WorkerPoolStore` 持久化，
OMP 的 semaphore、bounded fanout、AsyncJob park/revive/cancel 和 dispatch gate 以原 TypeScript 落入
`@zyra/claude-runtime/omp-worker-control`；Python 只向其提供签名、只读的 canonical lease projection。

最终 cleanroom 在包含所有 review-fix 的 `0e2826b` 上通过 25 个 Python 行为/HTTP/edge/ledger 测试、
6 个 OMP TypeScript 测试、7 个 Agent TypeScript 测试和 `tsc --noEmit`。语言 custody 自动检查确认
AgentScope role 有 4,304 行 Python production additions，OMP role 有 892 行 TypeScript production
additions，违规数为 0。保守有效 production 为 9,970 行，高于本 slice 的 9,000 行最低线。

## 审查中发现并已修复的问题

1. **来源语言责任坍缩。** 原实现把 OMP `TaskTool` / concurrency / `AsyncJobManager` 语义跨语言重写为
   Python，没有任何对应的 TypeScript production implementation，违反执行文档的原语言迁移合同。
   `1503895` 新增 `packages/runtime/claude-runtime/src/omp-worker-control/**`，保留 OMP 的
   `Semaphore`、`mapWithConcurrencyLimit`、AsyncJob 状态和 abort-safe admission 控制流。

2. **物理 lease 没有约束真实子任务。** 原 API 在 TypeScript 子任务返回后才建立 attempt/lease，
   scheduler 事实只是事后记录。`1503895` 改为在调用 E03 runtime 前创建并启动 physical attempt，生成
   带 SHA-256 digest 的只读 projection；`TaskExecutor` 必须先经过 `OmpWorkerDispatchRuntime` 才执行
   `runAgent`，终态再由 Python 提交 execution receipt 并释放 lease。

3. **缺少真实 approval/retry、mutation 和 fanout 证据。** 新增 HTTP 集成测试覆盖：首次 ask 挂起、
   approve 后重试并复用 lease、禁用 OMP gate 后 fail-closed 且写 failed receipt，以及两个 fanout child
   分别映射到唯一 attempt/lease/receipt。TypeScript 测试还覆盖 abort waiter 不泄漏 permit、并发上限、
   park/revive/cancel、task mismatch 和 projection digest 篡改。

4. **Windows clean-state 隐患。** 真实 HTTP 测试暴露 checkpoint 临时文件替换冲突、Git 向父仓库
   递归导致 workspace 假脏、workspace root 使用 Bun cwd，以及 mutation 环境变量被子进程清洗等问题。
   `1503895` 分别用唯一临时文件与 retrying replace、只检查 task root 自身 `.git`、manager-owned root、
   以及最小非秘密环境变量 allowlist 修复。

5. **ledger 自洽但语义倒置。** review-fix 前，同步脚本和 seed 同时写成 AgentScope
   `Python -> TypeScript`、OMP `TypeScript -> Python`，所以 `--check` 错误地通过。`31d4dac` 更正为
   Python -> Python、TypeScript -> TypeScript，补入 `parallel.ts`、具体 source symbols/tests，固定 ledger
   ID 并清理 owner-unit 陈旧重复条目；新增 mutation test，任何 `same_language` 决策语言不相等立即失败。
   `0e2826b` 进一步让 checker 从目标提交读取 ledger，用独立 evidence policy 三方核对 owner entry 数量、
   重复项、migration mode 和 source/target language；“生成器与 seed 一起写反”也会失败。

6. **通用审查流程不能防复发。** 根目录审查任务书已新增 fail-closed role-language 矩阵、独立 policy
   对 executable diff 的自动检查，以及“sync 自洽不能证明语义正确”的三方核对和倒置字段 mutation
   要求。对应自动化入口为 `scripts/verify_source_language_custody.py`。

## 仍未解决的阻断问题

未发现仍未解决的本 slice 阻断问题。

## 非阻断风险

- `test_code_worker_query_session_lifecycle.py` 与 `test_code_worker_query_session_integration.py` 的 11 个旧
  失败在修复前提交 `569ce4b` 可完全复现；它们不是本轮回归。本轮不改受保护的既有 owner，交由原 owner
  或 M1-07 聚合审查处理。
- 全局 internalization-ledger 工具仍报告 11 个受保护的 M1-01B 历史 target-path blocker；当前
  `M1-S07A-01` 新增 blocker 为 0。该债务不能用本 slice 改写历史事实消除。
- 本 slice 完成 worker-pool foundation；长时 lease renewal、完整 admission/capacity 和父级累计 15,000
  行收口仍属于 `M1-S07A-02`。M1-07 数字阶段聚合仍需执行适用的全仓回归与全量 ledger audit。

## 目标覆盖矩阵

| 目标 | 状态 | 证据 | 阻断 |
|---|---|---|---|
| 单一 physical lifecycle owner | 完成 | `WorkerPoolStore`、lifecycle/lease/heartbeat/inbox/cancellation/recovery runtimes | 否 |
| logical task 与 physical attempt/lease 分离 | 完成 | 03D task ID 仅作 foreign reference；attempt number、fence epoch/token 独立持久化 | 否 |
| heartbeat、telemetry、lost recovery | 完成 | health sweep 令 worker lost、lease expired、生成 recovery request | 否 |
| cancel/wakeup/drain 有语义效果 | 完成 | cancel fences lease；inbox requeue 写 wakeup；drain 拒绝新 lease | 否 |
| 动态 topology 与 immutable commit | 完成 | `GraphStateCustody` 支持 run 内 node/edge/role/capability add/replace/delete 和 CAS conflict receipt | 否 |
| 真实 edge process 且无 local fallback | 完成 | authenticated localhost child、artifact、real cancel、disabled connector mutation | 否 |
| subagent dispatch 受 physical lease 约束 | 完成 | HTTP acquire -> signed projection -> TypeScript gate -> child -> Python receipt/release | 否 |
| 来源语言 custody | 完成 | AgentScope Python 4,304；OMP TypeScript 892；自动检查 violations=0 | 否 |
| 断开即失败 | 完成 | worker acquisition disable、OMP gate disable、edge connector disable 均改变真实主路径 | 否 |
| production 最低 9,000 行 | 完成 | conservative effective production 9,970 | 否 |

## 内化审查

### Source-to-target

| 来源与角色 | 语言 / migration | 抽样 source symbol、callsite、test | Zyra 目标与 owner | 主路径语义 | 计入 |
|---|---|---|---|---|---|
| AgentScope `b6698c5` primary | Python -> Python；cropped same-language | `ChatRunRegistry`、`WakeupDispatcher`、`CancelDispatcher`、`InboxMiddleware`；inbox-before-wakeup、single-flight、cancel-before-cleanup；dispatcher/middleware tests | `packages/scheduler/zyra_scheduler/worker_pool/**`；`WorkerPoolStore` canonical | durable worker/attempt/lease/heartbeat/inbox/receipt/recovery | 是 |
| Oh My Pi `c6b83c1` supplementary | TypeScript -> TypeScript；cropped same-language worker-control | `task/parallel.ts` 的 `Semaphore`/`mapWithConcurrencyLimit`，`async/job-manager.ts` 的 `AsyncJobManager`，`AgentRegistry`；task-batch/task-spawn/async-job-manager tests | `packages/runtime/claude-runtime/src/omp-worker-control/**`；process-local TS owner，Python store 仍是 durable owner | 在真实 E03 child 前执行 admission、并发、abort、park/revive/cancel；fanout 使用同一 bounded map | 是 |
| Zyra-owned dynamic topology | design -> Python | immutable snapshot/delta/read-write-set/CAS requirements | `packages/orchestration/zyra_orchestration/graph_custody/**` | run 内真实拓扑变化和 conflict receipt | 是 |
| LangGraph conformance | 不迁移 production | checkpoint identity/exact-resume 对照 | 无 production owner | 不取得 StateGraph/Pregel/store owner | 否 |
| Claude reference | 不迁移 production | task/background mapping | existing 03D owner | 仅对照 | 否 |
| OpenClaw forward excluded | 不适用 | 未读取、未迁移 | 无 | 无依赖 | 否 |

OMP 选中的 worker-control 机制位于 TypeScript 源码。此 slice 不另造 native owner；PAL/native isolation
继续由此前 workspace/sandbox 单元负责，否则会形成第二个 workspace/sandbox canonical owner。

### 状态归属

| 状态 | System of record / writer | 提交与恢复 | 测试 |
|---|---|---|---|
| logical task/subagent | M1-S03D TypeScript task runtime | task checkpoint/revision；07A 只引用 ID | Agent durable/replay/resume tests |
| worker、attempt、lease、receipt | Python `WorkerPoolStore` | SQLite transaction、CAS version、fence epoch/token、reopen | worker-pool unit + HTTP tests |
| TS dispatch admission/job projection | `OmpWorkerDispatchRuntime` / `OmpAsyncJobProjectionManager` | process-local；每次从已验签 Python projection 建立，不冒充 durable restore | OMP 6 tests + Agent 7 tests |
| heartbeat/telemetry | `WorkerHeartbeatRuntime` | monotonic sequence、generation/manifest match | heartbeat lost/recovery tests |
| inbox/wakeup | `WorkerInboxRuntime` | durable claim deadline、ack/requeue/recover | inbox restart tests |
| topology | `GraphStateStore` / `GraphStateCustody` | immutable revision + atomic head CAS | dynamic graph tests |
| workspace | M1-S05A | manager-owned root；不得从 caller/Bun cwd 推断 | HTTP clean workspace path |
| gateway/backend/artifact | M1-S05B/C/D owners | foreign refs and canonical receipts | edge/API integration |

LLM 不决定 lifecycle transition、lease fencing、permission suspension、dispatch admission、health loss、
cancel、receipt commit 或 graph conflict。

## 有效行数审查

范围 `f2db58c4..0e2826be`：

| 桶 | Added | 计入最低线 |
|---|---:|---|
| Whole commit | 14,598 | 否，原始总量 |
| Raw production candidate | 11,601 | 候选 |
| Python production candidate | 10,617 | 候选 |
| TypeScript production candidate | 984 | 候选 |
| Nonblank/non-comment production | 10,771 | 候选 |
| Tests | 1,330 | 否 |
| Scripts | 718 | 否 |
| Docs/review | 443 | 否 |
| Ledger seed/data | 488 | 否 |
| Notice/package metadata | 18 | 否 |
| Conservative production adjustments | 1,631 | 否 |
| **Conservative effective production** | **9,970** | **是** |

保守值从原审计的 9,240 开始，先扣除 remediation commit 的全部 162 个删除行，再只加回独立语言
checker 证明的 892 个 OMP TypeScript role 行；其它新 HTTP/worker/Agent integration code 全部不用于最低线。
因此 `9,970 >= 9,000` 不依赖测试、脚本、ledger、文档或 adapter-only 行数。

仓库通用工具报告 `effective_added=13,649`，但包含测试和脚本，故只作辅助；最低线采用上述 production-only
保守值。generated、vendor/source-pool、mock/fixture-only、opaque bundle 和 data-as-code 计入 production
均为 0。

## 测试与验证

最终 exact detached cleanroom：`0e2826bedb420db4ef550a40532d758c92a1258c`。

- fresh Bun cache + `bun install --frozen-lockfile`: 通过，Bun 1.2.15，16 locked packages；
- `bun test .../omp-worker-control.test.ts`: 6 passed；
- `bun test .../agents.test.ts`: 7 passed；
- `tsc -p packages/runtime/claude-runtime/tsconfig.json --noEmit`: 通过；
- worker pool、dynamic graph、TypeScript port、language/ledger mutation、edge、HTTP main path：25 passed；
- `sync_worker_pool_foundation_source_ledger.py --check`: aligned，恰好 2 个 owner-unit decisions；
- `verify_source_language_custody.py --target 0e2826b`: 通过，violations=0；
- production forbidden root-source dependency scan：0 matches；
- cleanroom `git status --short`：空；`git diff --check`：通过。

默认主路径验证已执行：单 subagent 和 fanout 都从真实 HTTP route 进入 Python lease，再进入 TypeScript gate
和真实 child runtime。干净目录/缓存验证已执行。断开即失败已执行：`ZYRA_OMP_WORKER_CONTROL_DISABLED=1`
会阻断 child 并产生 failed receipt；edge connector 和 worker acquisition disable 同样 fail-closed。失败路径还
覆盖 digest tamper、task mismatch、queued abort、permission suspension/retry 和 fanout terminal settlement。

两组旧 QuerySession 文件在当前相邻运行中 11 failed，同时有 23 surrounding tests passed；相同 11 个失败
在修复前 `569ce4b` 全部复现，因此不把它们伪报为通过，也不判为当前回归。全仓 suite 按计划留给 M1-07
数字阶段聚合。

## 赛题需求与评分回归矩阵

| 触达能力 | 本 slice 动态证据 | 状态变化 | 后续 owner |
|---|---|---|---|
| 异构 worker / 资源调度 | real local TypeScript child、independent edge child、physical lease/receipt | foundation strengthened | S07A-02 / M1-07C |
| 动态稀疏拓扑 | run 内 add/replace/delete + immutable conflict receipt | foundation delivered | later benchmark |
| 节点失效与恢复 | heartbeat lost、lease fence、takeover/replan、cancel | foundation delivered | M1-07C fault matrix |
| 可追溯执行 | task/attempt/lease/receipt/event/artifact causal references | foundation delivered | M2 trace UI / M3 freeze |

本基础设施 slice 不宣称关闭两个跨领域 live run、2,000 canonical transitions、sealed zero-human、真实
local/isolated-edge/cloud 三类 dispatch、多 provider/model、完整 fault/change/node-loss、低熵对照或最终
比赛分值门禁。

## 提交与状态边界

- Baseline: `f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b`。
- Superseded implementation: `83251b198abe88e6b848f1095b6faed1005cd39d`。
- Superseded evidence: `569ce4bcc98b975a01145449ff57bc7d01c4070c`。
- Remediated implementation: `150389567046f0cf43205b2e649e74f8543421ae`。
- Review-fix 1: `31d4dac8748de88526e4936664905f41252b7b40`。
- Review-fix 2 / final cleanroom target: `0e2826bedb420db4ef550a40532d758c92a1258c`。
- Review/evidence commit: 本报告与 evidence 完成后生成。
- 根目录 `docs/milestones/execution-state.yaml` 和 `docs/执行单元完成后通用审查任务书.md` 不在 Zyra Git
  仓库内；前者只能在 review/evidence commit 存在后更新，后者的流程修订需单独说明提交边界。

## 下一步

Review/evidence commit 与根目录 execution state 更新完成后，进入 `M1-S07A-02`。父级 `M1-07A` 和
数字阶段 `M1-07` 仍保持 open；本 slice 不再保留可安全修复的阻断项。
