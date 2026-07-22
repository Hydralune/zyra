# M1-S07A-02 新增有效代码/语言迁移门禁 remediation

Date: 2026-07-22

Original implementation: `16f24689c1817a1f74fec9afb451d24b97983670`

Original evidence: `66b3c42da106a799b65eea3e17a7efcdff0b5f2b`

Remediation audit/test: `72732e2167772b306dfd1921912995b48389ed99`

## 结论

原完成流程**没有完整读取和执行**两份新增门禁，不能维持原来的完成判定。两份根目录门禁文件时间为
2026-07-22 11:27，早于 implementation commit（11:54）和 evidence commit（12:09）；原 evidence 虽有
三 commit 边界、粗粒度语言统计和行为测试，但缺少 29 文件逐文件桶、触发文件专项审查、严格
type/interface/DTO/generated/adapter 排除，以及从 07A-01 真实 baseline 到 07A-02 implementation 的父级重算。

本 remediation 从头到尾完整读取两份门禁，并保持原 review/evidence 不变。严格重算结果：

- slice `8981599..16f24689`：有效 production `6,078 >= 6,000`，通过；
- AgentScope primary Python：`5,167 > 0`；
- OMP supplementary TypeScript：`622 > 0`；
- OMP Python protocol adapter：174 行单列 adapter，计入 production 为 0；
- Type/interface 269、schema/DTO/data 1,743、generated 0、tests 1,235、tooling 293、comments/blank 560，全部排除；
- 父级 `f2db58c4..16f24689`：有效 production `14,646 < 15,000`，短缺 354，失败。

因此总体 verdict 是 **BLOCKER**。原 `66b3c42d` 的完成结论在新增门禁下失效；`M1-S07A-02` 和父级
`M1-07A` 必须重新打开，不能进入 07B。未添加任何为凑数的 production；审计脚本和新增测试均不计入
冻结 implementation 或父级有效行数。

## Commit 边界重建

| 字段 | Commit | 证据 |
|---|---|---|
| 07A-02 baseline | `8981599da7312f2bfba7b3303498efebb8339e39` | `16f24689^` 精确等于该 commit |
| 07A-02 implementation | `16f24689c1817a1f74fec9afb451d24b97983670` | 最后一项 production/直接测试提交 |
| 原 evidence | `66b3c42da106a799b65eea3e17a7efcdff0b5f2b` | `66b3c42^` 精确等于 implementation |
| 07A 父级 baseline | `f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b` | 第一实现 `83251b1^` 精确等于该 commit |
| remediation audit/test | `72732e2167772b306dfd1921912995b48389ed99` | 只含审计工具和新增 disable 测试 |

原统计区间只允许 `8981599..16f24689`。`72732e2` 不用于替原实现增加行数或辩护。

## 实施前来源/语言事实

| 角色 | 来源与路径 | Source | Target | Migration | Canonical owner | 既存引用 |
|---|---|---|---|---|---|---|
| primary | AgentScope manager/inbox/session lifecycle | Python | worker_pool Python + API | cropped/same-language | Python WorkerPoolStore/IntegrationRepository | 原 slice 24-28、71-79 节及父级裁决 |
| supplementary | OMP task/executor/parallel/semaphore/AsyncJob | TypeScript | omp-worker-control TypeScript | cropped/same-language | TS process-local；Python durable owner | 原 slice 24-28、71-79 节及父级裁决 |
| protocol adapter | OMP/edge contract boundary | TypeScript | edge integration Python | protocol_adapter | 不取得 owner | 原 slice 第 75 节只授权模式；精确路径是事后事实，不伪造事前记录 |
| topology primary | Zyra graph custody | Python | graph_custody Python | Zyra-owned | GraphStateCustody | 父级/原 slice 26-28 节 |

Python edge adapter 不替换 OMP TypeScript control flow，也不取得 lease/transaction/restore owner；全部 174 个
adapter 行排除，不能抵扣 OMP 原语言或父级成熟实现义务。

## 逐文件有效代码分桶

`UI-B/UI-P` 均为 0；本 slice 无 UI。`Docs` 列含纯注释和空行。

| 文件 | Lang | 角色 | Raw | Runtime | UI-B | UI-P | Type | DTO/data | Adapter | Gen | Test | Tool | Docs | Effective |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| apps/api/zyra_api/main.py | py | integration | 156 | 156 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 156 |
| apps/api/zyra_api/worker_pool_api.py | py | integration | 134 | 133 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 133 |
| internalization_ledger_seed.json | json | data | 458 | 0 | 0 | 0 | 0 | 458 | 0 | 0 | 0 | 0 | 0 | 0 |
| omp-worker-control/contracts.ts | ts | OMP supplement | 23 | 17 | 0 | 0 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 17 |
| omp-worker-control/dispatch-runtime.ts | ts | OMP supplement | 72 | 70 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 70 |
| omp-worker-control/index.ts | ts | OMP export | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| omp-worker-control/session-runtime.ts | ts | OMP supplement | 672 | 535 | 0 | 0 | 89 | 0 | 0 | 0 | 0 | 0 | 48 | 535 |
| omp-worker-control.test.ts | ts | test | 232 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 232 | 0 | 0 | 0 |
| zyra_scheduler/__init__.py | py | export | 2 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| worker_pool/__init__.py | py | export | 122 | 0 | 0 | 0 | 122 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| worker_pool/admission.py | py | AgentScope primary | 481 | 431 | 0 | 0 | 7 | 5 | 0 | 0 | 0 | 0 | 38 | 431 |
| worker_pool/application.py | py | AgentScope primary | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| worker_pool/checkpoint.py | py | AgentScope primary | 399 | 360 | 0 | 0 | 1 | 15 | 0 | 0 | 0 | 0 | 23 | 360 |
| worker_pool/control.py | py | AgentScope primary | 601 | 546 | 0 | 0 | 7 | 11 | 0 | 0 | 0 | 0 | 37 | 546 |
| worker_pool/health_bridge.py | py | AgentScope primary | 616 | 461 | 0 | 0 | 7 | 84 | 0 | 0 | 0 | 0 | 64 | 461 |
| worker_pool/integration.py | py | AgentScope primary | 1,097 | 1,000 | 0 | 0 | 12 | 45 | 0 | 0 | 0 | 0 | 40 | 1,000 |
| worker_pool/integration_models.py | py | AgentScope primary | 1,077 | 290 | 0 | 0 | 1 | 682 | 0 | 0 | 0 | 0 | 104 | 290 |
| worker_pool/integration_store.py | py | AgentScope primary | 943 | 783 | 0 | 0 | 1 | 107 | 0 | 0 | 0 | 0 | 52 | 783 |
| worker_pool/invariants.py | py | AgentScope primary | 505 | 427 | 0 | 0 | 1 | 51 | 0 | 0 | 0 | 0 | 26 | 427 |
| worker_pool/leases.py | py | AgentScope primary | 20 | 17 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 2 | 17 |
| worker_pool/projection.py | py | AgentScope primary | 247 | 141 | 0 | 0 | 1 | 81 | 0 | 0 | 0 | 0 | 24 | 141 |
| worker_pool/recovery_handoff.py | py | AgentScope primary | 332 | 230 | 0 | 0 | 1 | 79 | 0 | 0 | 0 | 0 | 22 | 230 |
| worker_pool/renewal.py | py | AgentScope primary | 249 | 206 | 0 | 0 | 3 | 23 | 0 | 0 | 0 | 0 | 17 | 206 |
| worker_pool/scheduler_bridge.py | py | AgentScope primary | 335 | 230 | 0 | 0 | 1 | 76 | 0 | 0 | 0 | 0 | 28 | 230 |
| worker_pool/store.py | py | AgentScope primary | 51 | 40 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 11 | 40 |
| edge_pool/__init__.py | py | export | 4 | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| edge_pool/integration.py | py | OMP protocol adapter | 222 | 0 | 0 | 0 | 1 | 26 | 174 | 0 | 0 | 0 | 21 | 0 |
| sync_worker_pool_integration_source_ledger.py | py | tooling | 293 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 293 | 0 | 0 |
| test_worker_pool_integration_runtime.py | py | test | 1,003 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1,003 | 0 | 0 | 0 |
| **总计** | | | **10,352** | **6,078** | **0** | **0** | **269** | **1,743** | **174** | **0** | **1,235** | **293** | **560** | **6,078** |

审计器从冻结 Git object 获取 target-side additions；Python 通过 AST/token 区分 Protocol、dataclass/enum
字段、DTO mapping、SQLite DDL、validator 和执行语句，TypeScript 扣除 interface/type/import-type/declaration。
generated 为 0，不用“未检测到 generated”代替其它排除桶。

## 按来源角色与语言

| 来源角色 | Source language | Migration | Production | Test | Adapter | Type/data/docs/tool | 默认入口 | Disable/mutation |
|---|---|---|---:|---:|---:|---:|---|---|
| AgentScope primary | Python | cropped/same-language | 5,167 | 0 | 0 | 1,915 | API → scheduler.admit → integration | lease-store disabled；binding rollback |
| OMP supplementary | TypeScript | cropped/same-language | 622 | 0 | 0 | 146 | OMP execute → session.enter → semaphore → operation | OMP disabled；counter unchanged |
| OMP protocol adapter | Python | protocol_adapter | 0 | 0 | 174 | 48 | edge-only execute | connector disabled/no local fallback |
| Zyra API integration | Python | integration | 289 | 0 | 0 | 1 | task/subagent/control routes | lease disabled before executor |
| Mixed verification | Python/TS | test | 0 | 1,235 | 0 | 0 | pytest/Bun | behavior only，不计 production |
| Ledger/tooling | JSON/Python | evidence/tool | 0 | 0 | 0 | 751 | 无 runtime entry | 不计 production |

同语言义务通过：AgentScope Python 与 OMP TypeScript 均大于 0。Python edge adapter 不得替 OMP
TypeScript 抵扣；本审计确实按 0 production 处理。

## 大文件/高占比触发审查

| 文件 | 触发 | 可执行 symbol / 默认入口 | 状态/错误责任 | 测试与差额 |
|---|---|---|---|---|
| session-runtime.ts | raw 672 | enter/park/revive/yield/rehydrate；OMP dispatch | process-local permit/abort；Python durable owner 不变 | 89 type + 48 comments 排除；OMP 11 tests |
| control.py | raw 601 | submit/claim/apply/recover；control API | intent/fence 先于 process/edge effect | 18 type/DTO + 37 docs 排除；projection-failure cancel |
| health_bridge.py | raw 616 | sweep/mark lost/persist recovery；health API | LOST、lease expiry、05D/07C | 91 type/DTO + 64 docs 排除；heartbeat→05D→07C |
| integration.py | raw 1,097 | admit/start/progress/yield/complete/edge/failover | physical binding orchestration；不写 logical/graph owner | 57 type/DTO + 40 docs 排除；atomic/replay/fence |
| integration_models.py | raw 1,077；DTO 63% | 仅 post-init validator、advance/digest 计 runtime | validation，不是 store owner | 682 DTO + 104 docs 排除；digest/mutation tests |
| integration_store.py | raw 943 | binding/control/yield/recovery/checkpoint CAS | canonical SQLite transaction；DDL 不计 | 107 schema + 52 docs 排除；rollback/restart |
| invariants.py | raw 505 | audit/assert_safe/assert_completion | fail-closed consistency | 52 type/DTO + 26 docs 排除；random replay |
| projection.py | DTO 33% | run-filter/cursor validation | read-only M2 projection | 81 mapping + 24 docs 排除；interleaved cursor |
| edge integration.py | adapter 78% | execute/cancel edge adapter | 不取得 owner；无 local fallback | 174 adapter 全排除；real edge/fence |
| integration runtime test | raw 1,003 | test-only | 无 production owner | 全部 test 排除 |
| ledger/index/init 文件 | type/data >30% | 无独立 runtime 行为 | 无 | 全部相应桶排除 |

没有单文件贡献超过 slice effective production 的 20%；最高为 `integration.py` 的 16.45%。

## Lease 在任务执行前生效

真实顺序为：

`POST subagent → _acquire_subagent_physical_dispatch → scheduler.admit → integration.start → signed physical projection → _run_typescript_agent_request → OmpWorkerDispatchRuntime.execute → sessions.enter → semaphore.acquire → operation`。

因此 Python canonical attempt/lease/binding 在 TypeScript child 启动前已提交，TypeScript session permit 和
semaphore 在 operation callback 前获取；不是执行后补 telemetry。

原冻结 TypeScript mutation 用 operation counter 证明 `ZYRA_OMP_WORKER_CONTROL_DISABLED=1` 时 callback
没有执行。本 remediation 新增 API 级测试，在创建 parent 后设置 `ZYRA_WORKER_LEASE_STORE_DISABLED=1`，
并把 TypeScript executor 替换为一旦调用即失败的 marker；结果为 HTTP 409、marker 0 次、logical child 0、
child lease 0。相关批次：Python 32 passed，OMP 11 passed，`tsc --noEmit` 通过。

## 父级去重重算

父级区间固定为 `f2db58c4..16f24689`。只允许三个 07A production commit：`83251b1`、`1503895`、
`16f2468`。最终 target-side additions 只计一次，并以 blame 排除两个 evidence 流、31d/0e 审计修复和
60af/eb16/4c83 等非 07A remediation；被排除的 unrelated/evidence 行为 8,128 行。

| 桶 | 行数 |
|---|---:|
| Raw final-range additions | 32,306 |
| Unrelated/evidence scope | 8,128 |
| Type/declaration | 692 |
| Schema/DTO/data | 3,801 |
| Adapter-only | 174 |
| Generated | 0 |
| Test/mock/fixture | 2,495 |
| Tooling | 877 |
| Docs/comments/blank | 1,493 |
| **Effective production** | **14,646** |
| Parent minimum | 15,000 |
| **Shortfall** | **354** |

父级失败不能用 slice 间自报值相加、后续 audit/test commit、adapter、DTO 或 evidence 修补。门禁命令按预期
exit 1，唯一 blocker 为 `parent effective production 14646 < 15000`。

## 状态处置

原 review/evidence 文件不改写；本记录追加 remediation 事实。根 execution state 应：

- 将 `completed_through` 回退为 `M1-S07A-01`；
- 从 completed slice/parent 集合移除 `M1-S07A-02` / `M1-07A`；
- next slice 指回 07A-02，并记录父级有效 production 短缺 354；
- 不允许开始 07B；
- 保留原 implementation/evidence hash 作为历史失败边界，并记录 `72732e2` 及本 remediation evidence commit。

门禁第 9 节禁止为固定行数制造代码。当前行为与语言门禁已通过，但没有发现可诚实扩展 354 行的未实现
产品责任；下一步必须先作父级职责/预算裁决，或识别真实 capability finding 后以新的 remediation
implementation commit 修复。不得把本审计脚本、测试或后续无关 patch 用来改写原结论。
