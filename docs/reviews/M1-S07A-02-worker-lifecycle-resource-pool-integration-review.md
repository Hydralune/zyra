# M1-S07A-02 Worker Lifecycle / Resource Pool Integration Review

Date: 2026-07-22

Review level: slice closeout, parent `M1-07A` closeout, exact-implementation-commit cleanroom.

Baseline: `8981599da7312f2bfba7b3303498efebb8339e39`

Implementation / final cleanroom target: `16f24689c1817a1f74fec9afb451d24b97983670`

## 结论

**通过。** `M1-S07A-02` 已把 07A-01 的 worker-pool foundation 接到真实 task/subagent、动态拓扑、
05D backend health、独立 edge gateway、M2 投影和 07C recovery handoff 主路径。capacity admission、
attempt、lease 和 binding 在同一个 canonical SQLite transaction 内提交；长时续租只接受新的进度；
cancel/drain/wake/stop/park/revive 先持久化并 fence canonical state，再尝试进程或 edge 副作用。

AgentScope primary 继续以 Python 同语言裁剪，OMP semaphore/session/park/revive/single-yield 继续由
TypeScript 原语言 supplementary runtime 承担；TypeScript 只消费验签、校验和保护的 Python lease
projection，不取得 durable state owner。Zyra `GraphStateCustody` 仍是 topology owner，LangGraph 仅用于
checkpoint/exact-resume conformance，OpenClaw 按 forward-only 边界未读取、未迁移、未形成运行依赖。

精确提交 cleanroom 通过 Python 31 项相关行为测试、OMP TypeScript 11 项测试、`tsc --noEmit`、
`compileall`、账本同步、历史 API/SQLite 句柄回归和生产依赖扫描。来源语言 verifier 记录 AgentScope
role 新增 7,470 行 Python、OMP role 新增 768 行 TypeScript，violations 为 0。

本 slice 保守有效 production 为 6,700 行，高于 6,000 最低线；与 07A-01 已审计的 9,970 行累计为
16,670 行，高于父级 15,000 行，因此 `M1-07A` 随本 slice 收口，下一入口为 `M1-S07B-01`。

## 批判式审查中发现并已修复的问题

1. **admission 的 lease 与 binding 最初跨两个 transaction。** 这会在第二次写入失败时遗留无 binding
   的 live lease，也无法原子执行配额检查。最终实现把 quota guard 和 `LeaseCommitHook` 放入
   attempt/lease transaction，binding、journal 与 canonical lease 一起 commit；故障注入测试证明任一写入
   失败时四者全部回滚。

2. **并发 admission 可能越过容量。** 单纯先查再写不能约束两个并发 caller。最终实现让 capacity、
   session/run/worker quota 在持有 canonical store transaction 时复核，并用并发测试证明只产生允许数量的
   live lease。未知 graph node 也在分配 lease 前验证，避免 topology 失败后的资源泄漏。

3. **graph mutation 重放与物理 binding 可能分叉。** 如果 graph commit 已成功但 metadata 写入中断，
   重试可能重复变更 topology。最终 replay 识别既有 graph reference 并修复 binding；相同 admission/failover
   replay 不推进 graph revision，冲突则执行可审计 compensation。

4. **OMP 并发 enter/revive 存在重复 permit 和陈旧 release。** 同一 physical attempt 的并发调用原本可能
   重复占用 capacity，旧 generation 的 release 还可能释放 revive 后的新 permit。最终加入 per-attempt
   single-flight `pendingPermit` 和 generation-bound release，duplicate enter/revive/execute 测试均通过；
   queued abort 会清理 waiter 与 parent listener，不产生 typed yield。

5. **parked job 可绕过 revive 重新 dispatch。** 最终 dispatch gate 检查 canonical/session-local 状态，
   parked attempt 必须显式 revive 并重新取得 permit；同一 attempt 的真实 operation 由 `activeTasks` 去重。

6. **控制副作用失败可能阻止 fence。** 最终 durable control 先写 intent、terminal/cancel fence 与 journal，
   再调用 TypeScript process projection 或 edge cancellation。projection 故障只写入 effect failure，不能撤销
   canonical cancel；测试故意令投影抛错并验证 lease 已不可继续使用。

7. **heartbeat health 的 route 语义和恢复顺序不正确。** 最终 bridge 区分 physical worker 与 scheduler
   route，把 LOST 写成 terminal binding、expired lease、真实 05D health 和持久化 07C evidence；恢复只能由
   显式的新 generation lifecycle start 加新 heartbeat 触发，不允许陈旧 heartbeat 反向复活。

8. **05D adapter 持有 SQLite connection 导致 Windows 文件句柄回归。** 全量 fail-fast 首次暴露旧浏览器
   API 测试的临时目录无法清理。最终 adapter 改为每次调用短连接，修复后该历史回归在 worktree 与精确
   cleanroom 均通过。

9. **M2 cursor 只按内存切片会跨 run 丢事件。** 最终由 SQL 按 run 过滤 canonical journal，再计算并校验
   checksummed cursor；交错 run 测试证明 cursor 不跳过目标 run 的事件。

10. **checkpoint 最初缺少跨 owner 的精确关联。** 最终 checkpoint 固化 run-scoped control/drain、binding
    digest、lease fingerprint、graph revision 和 journal head；篡改 digest 或 restore correlation 均 fail closed。

## 仍未解决的本 slice 阻断问题

无。

## 状态归属与集成边界

| 状态域 | Canonical owner | 本 slice 的写入/恢复边界 | 证据 |
|---|---|---|---|
| logical task/subagent | M1-S03D TypeScript runtime | 07A 只保存 typed foreign reference | alias/replay 与 API 测试 |
| worker/attempt/lease/binding/control/yield/checkpoint | Python worker-pool store/repository | SQLite CAS、fence token/epoch、同 transaction admission、reopen restore | integration runtime tests |
| process-local permit/job projection | OMP TypeScript runtime | 验签 Python projection 后 rehydrate；不写 durable owner | 11 Bun tests |
| dynamic topology | Zyra GraphStateCustody | immutable revision、conflict receipt、replay repair | graph/replay/failover tests |
| workspace/gateway/event/backend | 05A/05B/05C/05D owners | typed refs 和 owner API；07A 不建第二 store | API、edge、health bridge tests |
| recovery evidence | 07A canonical journal/binding；07C consumer | LOST/fence/control/checkpoint 事实只读 handoff | health→05D→07C test |

LLM 不参与 capacity、lease renewal、fencing、control ordering、checkpoint verification、health transition、
route failover 或 topology commit。edge-only route 不存在 local fallback。

## 来源到目标裁决

| 来源 | 角色 | 原语言 | Zyra 目标 | 不取得的 owner |
|---|---|---|---|---|
| AgentScope | primary implementation | Python | `worker_pool/admission.py`、`integration*.py`、`control.py`、`checkpoint.py`、health/projection/handoff 与 API 主路径 | logical 03D、graph、05A-D |
| oh-my-pi | supplementary implementation | TypeScript | `omp-worker-control/session-runtime.ts`、`dispatch-runtime.ts`、contracts/index | durable lease/store/checkpoint |
| Zyra | topology owner | Python | `GraphStateCustody` 与 integration binding | 无外部 generic graph owner |
| LangGraph | conformance only | 不迁移 | exact-resume/checkpoint identity 对照 | StateGraph/Pregel/store/runtime |
| claude-code-best | reference only | 不迁移 | task/background mapping 对照 | worker-pool production owner |
| OpenClaw | excluded forward only | 不适用 | 无 | 无源码、包、进程或路径依赖 |

删除或禁用本 slice 模块会改变真实行为：`ZYRA_WORKER_LEASE_STORE_DISABLED=1` 阻断真实 dispatch；取消
integration transaction 会使原子回滚测试失败；断开 TypeScript session runtime 会使并发/park/revive/单次
yield 测试失败；断开 edge adapter 会使 edge-only failover 与 cancellation 失败；断开 health bridge 会使
05D route health 和 07C recovery evidence 不再变化。

## 有效行数审查

范围 `8981599d..16f24689`：

| 桶 | Added | 计入最低线 |
|---|---:|---|
| Whole implementation commit | 10,352 | 否，原始总量 |
| Raw production candidate | 8,366 | 候选 |
| Python production candidate | 7,598 | 候选 |
| TypeScript production candidate | 768 | 候选 |
| Nonblank/non-comment production | 7,855 | 候选 |
| Tests | 1,235 | 否 |
| Ledger sync script | 293 | 否 |
| Ledger seed/data | 458 | 否 |
| Conservative production adjustments | 1,155 | 否 |
| **Conservative effective production** | **6,700** | **是** |

保守调整排除：300 行重复 DTO/serialization、200 行 edge adapter、285 行 API composition、129 行
export-only surface、91 行 compatibility plumbing，并再预留 150 行 declarative/docstring。测试、脚本、ledger、
generated、vendor/source-pool、mock/fixture-only、data-as-code 和 adapter-only 均不计入最低线。通用工具报告
`effective_added=9,894`，但它包含测试与脚本，只作辅助。

父级累计采用 07A-01 已审计 9,970 加本 slice 保守 6,700，得到 16,670；不依赖 ledger、测试或未接入
样板，高于父级 15,000 最低线。

## 测试与 cleanroom

最终 exact detached cleanroom target：`16f24689c1817a1f74fec9afb451d24b97983670`。

- 独立 `git archive` + fresh cache + `bun install --frozen-lockfile`：通过，Bun 1.2.15，17 packages；
- worker pool foundation/integration、graph custody、source language、real edge、API main path：31 passed in 171.79s；
- `bun test packages/runtime/claude-runtime/test/omp-worker-control.test.ts`：11 passed；
- `tsc -p packages/runtime/claude-runtime/tsconfig.json --noEmit`：通过；
- `python -m compileall`：通过；
- 旧 browser API SQLite handle 回归：1 passed；
- source ledger sync：aligned，恰好 2 个 `M1-S07A-02` decisions；
- source-language custody verifier：通过，Python 7,470 / TypeScript 768，violations=0；
- production root-source relative path：0；parent-path package link：0；
- implementation range `git diff --check`：通过。

全量 `unittest discover` 曾运行约 28 分钟。首次 fail-fast 找到的当前 05D handle 回归已修复；下一处失败
`test_browser_worker_permission_api_resumes_exact_network_action_once` 在 detached baseline `8981599` 也失败，
故不判为本 slice 回归，也不越权修改受保护 owner。`verify_m5.py` 的旧 browser scenario 同样受 live browser
reset 与旧 route assertion 影响。全局 ledger 工具仍报告 11 个受保护 M1-01B target-path blocker，当前
`M1-S07A-02` 新增 blocker 为 0。这些结果均不伪报为通过，交给 M1-07 数字阶段聚合复核。

## 赛题门禁边界

本 slice 真实增强异构 worker、动态 topology、节点 LOST/fence/failover、可追溯 checkpoint/control 与
edge-only dispatch；但不宣称关闭两个跨领域 live tasks、单 run 2,000 canonical transitions、sealed
zero-human、多 provider/model、真实 local/isolated-edge/cloud 全矩阵、低熵对照或最终 100 分证据门禁。

## 提交与状态边界

- Implementation / final cleanroom target: `16f24689c1817a1f74fec9afb451d24b97983670`。
- Review/evidence commit: 本报告和机器证据提交后生成。
- 根目录 `G:/agent-zoo/docs/milestones/execution-state.yaml` 不在 Zyra Git 仓库；只有 evidence commit
  存在后才更新，并在最终交付中单独说明。

## 下一步

review/evidence commit 和根目录 execution state 更新后进入 `M1-S07B-01`。`M1-07A` 完成；数字阶段
`M1-07` 仍保持 open，聚合层负责完整 cleanroom、适用全仓回归和全量 ledger/source-to-target audit。
