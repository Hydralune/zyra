# M1-07B Watchdog / Fault Injection 父级完成审查（2026-07-22）

## 结论

**通过（经审查修复后）。** 原完成状态不能直接通过：CodeWorker 真实帧边界没有把结构化 watchdog 信号送入 Python canonical fault custody，OMP 的 MCP/provider/worker supervisors 未全部进入默认执行路径，另有并发重入、浏览器 observer 清理和来源语言审计边界缺陷。上述问题已分别在 `a234ac0`、`4add6d0`、`383acfb` 修复；最终实现目标冻结为 `383acfbe276420dbceec3f64f09829d38b99816c`。

审查范围为父级 baseline `6900e96dcb6dd73c22b803787afc30125fbe947c` 到最终实现目标。M1-S07B-01 的实现提交为 `db27e57`、`242038c`，M1-S07B-02 原实现提交为 `c6887b0`，其后是本次三个修复提交。审查和账本证据在最终实现之后单独提交，不回写已冻结的实现提交。

父级目标已满足：真实与注入故障统一进入 typed signal contract；真实 tool、provider、MCP、worker、browser、permission 等观察可进入 canonical fault state、event、memory、scheduler health、same-run containment 与带 lease/ack 的 recovery handoff；需求变更仍走 control/replan，而不计为 fault。M1-07C 继续拥有 recovery plan 选择权，因此本审查不把 07B 夸大为完整恢复闭环或赛题硬证据完成。

## 审查中发现并已修复的问题

| 严重度 | 问题与首次可见边界 | 根因 | 修复与证据 |
|---|---|---|---|
| blocker | `c6887b0` 后，TypeScript watchdog 事件只进入通用 runtime event spine，CodeWorker 默认路径没有进入 `FaultStateStore -> event/memory/scheduler -> containment/handoff` | CodeWorker composition root 没有 canonical fault observation sink；API 路由是唯一完整入口 | `a234ac0` 为三个 CodeWorker composition roots 注入 sink，只转发带显式 refs 的 `tool_failure_signal`；CodeWorker 行为测试证明 permission denial 进入 Python custody |
| blocker | OMP MCP/provider/worker supervisors 主要只在直接单测中可达 | supervisor 与真实 capability AbortSignal、默认 model stream 和 CodeWorker heartbeat 未完成接线 | `a234ac0` 接入真实 MCP timeout signal 与 provider stream；`4add6d0` 持久化 supervision state；`383acfb` 用每个 stdio 事件驱动 worker heartbeat 并写入 checkpoint/session snapshot |
| high | TypeScript permission observer 不识别 Python host 的 `permission_effect=deny`，且 observation identity 不稳定 | 两侧 metadata contract 不一致 | `a234ac0` 兼容 canonical host settlement metadata，并补原语言 contract 测试 |
| high | 并发 observation 可能重复产生 handler/publish 副作用并回退 sequence cursor | sequence 检查与 effect/publish 不在同一 guard transaction 内 | `a234ac0` 把 admission、handler、publish 和 cursor commit 收进同一临界区，补并发重复输入测试 |
| high | containment/effect receipt 在 `APPLYING`/`PROJECTING` 状态可重入，造成重复外部 effect 或 projection | in-flight receipt 没有 fail-closed | `a234ac0` 对处理中 receipt 返回既有状态，测试覆盖相同 identity 重入 |
| high | browser observer 部分 attach/unsubscribe 失败时会遗留 callback/source session，FAILED bridge 仍可转发 | cleanup 遇首个异常即停止，source stop 与 callback admission 不幂等 | `a234ac0` 改为穷尽清理、attach 失败禁用 source、非 ACTIVE callback 丢弃、DISABLED stop 幂等，并补失败路径测试 |
| medium | 来源语言 verifier 从 implementation target 读取 ledger，令“实现提交后再提交证据”的三提交协议不可满足；同时只识别 `role` | evidence 与 implementation 的读取边界混淆 | `a234ac0` 从当前已提交 evidence checkout 读取 ledger，production additions 仍冻结到 target；同时接受 `source_role`，6 个单测通过 |
| medium | 07B-02 有效代码审计遗漏原实现提交 blame，ledger 对 OMP 默认路径描述偏强 | remediation-only ownership 集合与实际提交链不一致 | evidence tooling 纳入原实现及三个修复提交；同步脚本改为实际 `CrossRuntimeFaultSupervisor`、真实 callsites 和对应行为测试 |

## 仍未解决的阻断问题

无 M1-07B 范围内阻断问题。

全局 ledger verifier 仍会报告受保护早期单元的缺失旧目标路径，以及历史 remediation/审计脚本中的来源仓库文本引用。它们不由 M1-07B 引入，也不位于本次实现 diff；按 `execution-state.yaml` 的完成保护边界，本次未追溯改写。该全局债务不能被本审查宣称为已解决，但不改变 M1-07B 两条当前 ledger decision 的逐项验证结果。

## 非阻断风险

- TypeScript worker supervisor 的 process-exit/requeue 分支由同语言直接行为测试证明；CodeWorker 默认主路径实际接入 heartbeat、sweep 与 snapshot 持久化。外部进程失联的 canonical durable observation 仍由 Python source-session/fault owner 承担，避免产生第二个跨语言进程 owner。
- 本次属于父级 unit 收口，不是 M1-07 数字阶段聚合，也未命中新增依赖、服务、端口、Docker、动态 import、不兼容公共 schema 或 canonical owner 转移等高风险条件，因此未运行完整 cleanroom/全仓测试。完整 cleanroom 与全量 ledger audit 保留到 M1-07 sibling units 完成后的数字阶段聚合或里程碑退出。
- Node `--experimental-strip-types` 输出实验特性 warning，但测试和 TypeScript typecheck 均通过；这是运行器提示，不改变行为结论。
- Seed ledger 的 test-quality 审计为 2 entries、8 test entries、0 errors、2 warnings。两条 warning 来自审计器把 TypeScript Node 测试交给 Python AST heuristic 后误判为 fixture-only/no assertions；同一测试实际由 Node 执行 10 个行为用例并通过。该审计器语言覆盖缺口不改变本 unit 的行为证据，但应在后续 ledger 工具化阶段修复。

## 目标覆盖矩阵

| 父级目标 | 主路径证据 | 结论 |
|---|---|---|
| 统一 tool timeout、worker unavailable、browser crash、permission denied、model/schema/workspace/MCP 等信号 | `RuntimeWatchdog` / `CrossRuntimeFaultSupervisor`、browser source bridge、`RuntimeFaultObservationPort`、`WatchdogSignalClassifier` | 通过 |
| 至少三类 `active_real` observation | tool deadline、provider transport、MCP timeout、worker heartbeat、browser process/CDP、permission settlement 均有真实入口；schema/workspace 由 Python source observer 覆盖 | 通过 |
| observation 与 injection 同 schema、不同 provenance/maturity | real source session 与 `/faults/inject`/`/inject` 共用 canonical contract；测试断言 provenance 区分 | 通过 |
| 真实改变 event/memory/scheduler/containment | `FaultStateStore` 写入后触发 event writer、MemoryFabric、BackendRegistry health、same-run containment/effect coordinator | 通过 |
| 同一 run 向 M1-07C handoff | typed handoff 具 claim/lease/dispatch/ack/release；07C 只接管 plan choice | 通过 |
| 需求变更不是 fault | `/change` 触发 constraint/topology replan，fault count、pressure、scheduler health 和 failure memory 不变 | 通过 |
| API/command/worker runtime 动态可达 | `/tasks/{id}/faults/*`、`/inject`、`/watchdog`、CodeWorker stdio、browser worker session | 通过 |

## 内化审查

browser-use primary 以 Python 同语言裁剪为 `source_session.py` 与 `browser_integration.py`，保留 explicit attach/start/stop、完整 callback cleanup、process/target/CDP observation 语义；没有迁入 Browser Use agent loop、event bus 或状态 owner。断开 browser bridge 后真实 browser observation 不再进入 fault pipeline，API injection 不能替代该证据。

Oh My Pi supplementary 以 TypeScript 同语言裁剪为 execution/provider/MCP/worker supervisors，并在 `capability-host.ts`、`stdio.ts`、CodeWorker frame boundary 接入真实执行。它只拥有进程内 deadline/AbortController、partial-stream cursor、MCP reconnect/breaker 和 worker generation；Python `FaultStateStore` 保持 durable canonical fault custody。没有依赖 `../oh-my-pi`、外部 OMP CLI/package/进程或黑箱 runtime。

Zyra-owned 模块接管 identity/generation admission、分类、持久化、event causality、memory、scheduler health、containment/effect receipt、handoff lease/ack、API/control command 与测试维护。删除 observation port、containment/effect runtime、CodeWorker sink 或 browser bridge 会分别使并发 admission、实际状态改变、跨 runtime fault 入库或真实 browser capture 测试失败，满足断开即失败。

当前 diff 未修改 package/lockfile，未引入 npm link、pip editable path、外部 Docker context、根目录来源仓库运行依赖、缓存/SQLite 残留依赖或 OpenClaw 前向来源。LLM 不参与 classification、permission settlement、containment、scheduler health 或 handoff lease 决策。

## 有效行数审查

审计命令：

```text
python scripts/audit_m1_s07b02_effective_code_gate.py --target 383acfbe276420dbceec3f64f09829d38b99816c --summary-only --fail-on-gate
```

| 范围 | raw additions | effective production | 最低线 | 结果 |
|---|---:|---:|---:|---|
| M1-S07B-02 (`805d319..383acfb`) | 11,791 | 7,059 | 6,500 | 通过，余量 559 |
| M1-07B (`6900e96..383acfb`) | 25,194 | 15,710 | 15,000 | 通过，余量 710 |

07B-02 分桶：production runtime 7,059；test/mock/fixture 1,521；type declarations 503；schema/DTO/data 1,536；docs/comments/blank 713；non-production tooling 459；adapter-only/generated/vendor-like/UI presentation 均为 0。父级分桶：production runtime 15,710；tests 2,668；types 787；schema/DTO/data 1,610；docs/comments/blank 1,667；unrelated scope 2,740；普通 audit tooling 不计生产线。

来源角色的 07B-02 effective production 为：browser-use primary Python 472、OMP supplementary TypeScript 2,289、Zyra API main path Python 190、control main path Python 272、fault integration Python 3,760、supporting Python/TypeScript 76。语言 custody 的 raw declared-path additions 另行 fail-closed 校验：07B-01 browser-use Python 6,178、OMP TypeScript 750；07B-02 browser-use Python 1,668、OMP TypeScript 2,875，均无 violation。

## 测试与验证

| 验证 | 结果 |
|---|---|
| `pytest test_watchdog_fault_injection_integration.py test_watchdog_fault_injection_foundation.py test_change_command...` | 33 passed；真实 observation、注入、event/memory/scheduler、containment/handoff、并发/重入、browser cleanup、需求变更隔离 |
| `pytest test_code_worker_clean_productized_runtime.py` | 7 passed；真实 CodeWorker frame sink、permission deny、worker heartbeat/snapshot |
| Node watchdog integration + contract | 10 passed；tool AbortSignal、MCP timeout/breaker、provider partial stream、worker requeue、permission metadata |
| `pytest tests/unit/test_source_language_custody.py` | 6 passed |
| TypeScript `tsc --noEmit` | passed |
| Python `compileall` | passed |
| 07B-01 / 07B-02 source language verifier | passed，原语言 production 非零 |
| ledger sync / test quality | aligned，M1-S07B-02 恰有 2 条 source decision、8 个 required test entries；0 errors、2 个 TypeScript 静态解析误报 warnings |
| effective-code gate | passed：7,059/6,500 与 15,710/15,000 |

测试不是 import/schema/health-only：包含真实 API/worker/runtime 行为、非 fixture identities、timeout/abort、concurrency、duplicate/reentry、partial attach cleanup、disabled observer、stale generation、lease fencing 与 cross-runtime persistence。未跑全仓和完整 cleanroom的理由与下一强制执行层级已记录在“非阻断风险”。

## 赛题需求与评分回归矩阵

| ID | M1-07B 本次贡献 | 当前判定 |
|---|---|---|
| `REQ-FAULT-01` | 提供动态观察/注入、canonical fault facts、same-run containment 和 recovery handoff 输入 | `partial` 保持；必须由 M1-07C/M1-08 证明恢复后继续交付 |
| `REQ-TRACE-01` | event/span/tool/source/signal/projection/handoff identities 可因果关联 | 未关闭赛题门禁；M2 UI 和 live trace 仍待完成 |
| `REQ-EDGE-01` | scheduler backend health 可被真实 fault observation 改变 | 未关闭真实 local/edge/cloud dispatch 门禁 |
| `SCORE-ROBUST` | 多类 fault、失败路径、并发与幂等测试形成鲁棒性基础 | 未宣称得分完成；仍需重复 live run、P50/P95、MTTR、恢复成功率和故障矩阵 |

代码量、ledger 数量和测试数量没有被用来替代双跨领域 live 任务、2,000 canonical transitions、动态稀疏拓扑对照、真实端边云、多模型、恢复后交付或 UI 因果轨迹。

## 提交与状态边界

- 父级 baseline：`6900e96dcb6dd73c22b803787afc30125fbe947c`
- 最终实现 target：`383acfbe276420dbceec3f64f09829d38b99816c`
- 审查前 evidence head：`4580dba3d089a8be19a8f578300da065362b3b6f`
- 修复提交：`a234ac0`、`4add6d0`、`383acfb`
- 本文、amended slice evidence、ledger clarification 与 audit tooling 属 evidence commit，不计入实现 target 或有效生产代码。
- `G:\agent-zoo\docs\milestones\execution-state.yaml` 位于 Zyra Git 仓库之外；只在 evidence commit 成功后更新，并在最终交付中明确其不可随 Zyra commit 提交的边界。

## 下一步

保持 `M1-S07C-01` 为下一实现入口。M1-07C 应消费本单元的 durable handoff，完成恢复方案选择、局部重规划/重路由与 exact-resume；M1-07 全部 sibling units 完成后执行数字阶段聚合审查、完整 cleanroom、适用全仓回归与全量 ledger/source-to-target audit。
