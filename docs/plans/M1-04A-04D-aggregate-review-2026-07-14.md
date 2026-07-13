# M1-04A–04D 数字阶段聚合审查（2026-07-14）

## 1. 结论

`M1-04A`、`M1-04B`、`M1-04C`、`M1-04D` 在同一最终代码目标
`66c883c64808f8a8e17a9baae5fa20a1207964f4` 上通过数字阶段聚合审查。

审查范围从聚合基线 `476bc372134b06c12e63b5f25e902a8cc10e882d`
到上述目标提交。结论只表示浏览器 session、context/state compression、action/permission、
watchdog/history/artifact 四个父级单元已经在 Zyra 默认主路径上形成可运行、可恢复、可审计的
后端链路；不关闭跨领域 live 场景、2,000 canonical transitions、动态稀疏拓扑、真实端边云、
多模型、正式 benchmark、可视化或最终交付等赛题门禁。

聚合审查发现的门禁缺陷已在 `66c883c` 修复：cleanroom 从仅覆盖 04A 扩展为 04A–04D，
非 live smoke 不再伪造 memory transport action 成功，04B/04C/04D source-ledger 同步器达到
互相幂等，commit fence 增加逐相位重启恢复矩阵，完整 ledger verifier 暴露 blocking findings，
并修复一个与当前复合 resume-token 合同不一致的历史 API 断言。

## 2. 目标、提交与文档证据

| 项目 | 值 |
| --- | --- |
| 聚合基线 | `476bc372134b06c12e63b5f25e902a8cc10e882d` |
| 04D-02 实现提交 | `3ce93b1b6562a0e53ad7a1b403a7a9c6c48add8a` |
| 04D-02 slice 证据提交 | `0360542b0b4827aae9efd0cf13d07e1178f1a553` |
| 聚合修复和最终代码目标 | `66c883c64808f8a8e17a9baae5fa20a1207964f4` |
| 04D-02 自审 | `docs/plans/M1-S04D-02-watchdogs-history-artifacts-integration-review.md` |
| 本聚合审查 | `docs/plans/M1-04A-04D-aggregate-review-2026-07-14.md` |

逐 slice 事实仍以八份 `M1-S04A-01` 至 `M1-S04D-02` 自审为证据；本文件不建立第二份
执行状态源。聚合完成状态只回写根工作区 `docs/milestones/execution-state.yaml`。

## 3. 父级覆盖与累计有效实现

| 父级 | 保守 production 新增 | 父级下限 | 聚合裁决 |
| --- | ---: | ---: | --- |
| M1-04A Browser session productization | 17,276 | 13,500 | 通过 |
| M1-04B Message/state compression | 15,921 | 15,000 | 通过 |
| M1-04C Action registry/permission | 16,503 | 15,000 | 通过 |
| M1-04D Watchdogs/history/artifacts | 15,765 | 15,000 | 通过 |
| 合计 | **65,465** | **58,500** | **通过** |

这些数值来自各 slice 的严格分桶，已排除 tests、docs、generated、ledger/source map、data、
fixture/mock-only、experimental 默认关闭实现、普通辅助脚本、adapter-only 和 vendor-like 内容。
聚合 raw numstat 仅用于复核，不替代保守计数：

| raw bucket | 文件 | additions | deletions | 是否直接计入 production 下限 |
| --- | ---: | ---: | ---: | --- |
| `apps packages skills scripts` | 173 | 78,464 | 5,169 | 否，按 slice 保守分桶 |
| `tests` | 19 | 5,844 | 32 | 否 |
| `docs` | 8 | 1,296 | 0 | 否 |
| `vendor vendor-runtimes` | 0 | 0 | 0 | 否，且没有新增 |

聚合范围没有 `pyproject.toml`、requirements 或 lockfile 变化，也没有新增 pip/npm 依赖、
MCP server、插件、sidecar、Docker、本地辅助服务或动态 import owner。

## 4. 默认主路径与语义效果

```text
POST /tasks/{task_id}/workers/browser
  -> 04A BrowserRuntimeRegistry / BrowserSessionApplication
  -> exact session, target, CDP generation and Chrome process custody
  -> 04B DOM/frame/selector capture and bounded context disclosure
  -> 03A canonical permission owner
  -> 04C plan-wide admission, action execution and exact continuation
  -> 04D attached observation, history, artifact and recovery-input handoff
  -> EventLog append acknowledgement
  -> SQLiteStore/TaskState checkpoint acknowledgement
  -> GET browser session/context/observability projections
```

聚合行为证据确认：

- 04A 的真实本地 Chrome session、target、CDP generation、process custody 和 owner-loss cleanup
  在默认 productized backend 上运行；静态 backend 仅由明确测试请求。
- 04B 的 DOM/AX/Snapshot、same-origin frame、OOPIF、selector revision、stale-before-CDP、
  action result externalization 和 read-once context disclosure 进入真实 Worker/API/CodeWorker 路径。
- 04C 在执行前完成全计划 schema/security/permission 准入；ASK 返回 202 continuation，sealed
  高风险/未知动作确定性拒绝，批准只消费一次，任何失败都不回退 legacy action owner。
- 04D 在 action 前挂载 event bus，持久 history/artifact/health/recovery input；事件全集和 checkpoint
  未真实落盘时不能关闭 delivery fence；restart projection 从 durable store 重建。
- 禁用各父级 application、transport、selector/context integration、permission continuation、event
  writer 或 observability application 会使对应真实行为失败或显著改变，而不是仅改变日志。

## 5. 状态 custody 与恢复边界

| 状态域 | canonical owner | M1-04 使用方式 | 聚合恢复证据 |
| --- | --- | --- | --- |
| run/task/checkpoint | `SQLiteStore/TaskState` | browser 模块只写既有 task checkpoint/metadata port | HTTP 返回前真实 checkpoint ACK；重启后可查询 |
| canonical event | `EventLog` | browser event 被映射后由 API 持久化 | 事件 id 集合不完整不能 ACK |
| browser session/target/CDP/process | 04A Browser runtime | 唯一 lifecycle/target/generation/process custody | reuse/reconnect/owner-loss/cancel/cleanup 测试 |
| browser DOM/selector revision | 04B browser state runtime | 基于 04A identity 捕获和 CAS revision | stale identity 在任何 CDP side effect 前失败 |
| context budget/compact/provider message | 02D context owner | 04B 只提交 bounded external block/read-once disclosure | claim/consume/release/indeterminate 可恢复 |
| permission grant/decision | 03A `PermissionStateStore` | 04C 保存 continuation WAL 和 exact binding，不建第二 store | ASK/approve/replay/foreign binding 行为测试 |
| action execution/download control | 04C action runtime | plan barrier、CDP fence、deadline/cancel、download quarantine | pending/terminal 分离，失败路径零副作用 |
| artifact bytes | `LocalArtifactStore` | 04A–04D 写 lineage/receipt，不另建 artifact truth | atomic replace、无临时文件、restart redaction |
| observability history | 04D `BrowserHistoryStore` | hash-chain/transaction/head cursor 的父级 owner | uncommitted tail 隐藏并修复、幂等 transaction |
| delivery fence | 04D `ObservationCommitFence` | history/artifact/event/checkpoint 跨 owner outbox receipt | prepared 至 checkpoint 六相位逐次重启恢复 |
| recovery plan | 后续 M1-07C | 04D 只生成 typed recovery input | 无 `recovery_planned` 事件 |
| canonical memory | 后续 06B/06C MemoryFabric | 04B/04D 只提供 candidate/trajectory input | 不提前提交 memory truth |

没有转移既有 canonical state owner，也没有用 LLM 替代 permission、compact、scheduler 或 recovery
约束。commit fence 明确是跨 owner outbox/idempotency fence，不宣称跨数据库原子事务。

## 6. 来源角色去重与严格内化

| 能力域 | primary implementation | supplementary | conformance/reference/deferred |
| --- | --- | --- | --- |
| browser session/target/CDP/profile | browser-use selected mechanisms | 无第二 session owner | OpenHands/opencode/Hermes 仅投影或后续参考 |
| message/DOM/selector/compression | browser-use message manager 与 DOM mechanisms | claude-code-best context contract、oh-my-pi tool-pair/read-once | Agent Framework/opencode conformance；Hermes reference；bitmap experimental default-off |
| action/permission execution | browser-use action/security/download mechanisms | claude-code-best continuation/result budget、oh-my-pi preflight/control | opencode/OpenClaw/AgentScope 仅 conformance/reference |
| observability/history/artifact | browser-use attached lifecycle/watchdog mechanisms | OpenHands restart projection、oh-my-pi evidence correlation | browser-use unattached CrashWatchdog rejected；claude-code-best/opencode conformance/reference |

上游机制已经拆入 Zyra 的 `browser_session`、`browser_context`、`browser_state`、`browser_action`、
`browser_observability`、API、permission、artifact 和 event 边界；没有保持上游目录、入口、状态模型
或核心控制流，也没有运行期 `../browser-use`、`../claude-code-best`、`../OpenHands`、`../opencode`
相对路径。聚合 cleanroom 在移除根来源仓库、vendor、缓存、SQLite/artifact 残留后仍通过。

04B/04C/04D 三个同步器连续执行后不再相互制造 drift，且都 `--check` 对齐：04B 25
decisions，04C 10 decisions，04D 7 decisions。ledger 是来源追溯证据，不替代上述行为测试
和动态可达性。

## 7. 聚合发现、修复与批判性裁决

| 发现 | 风险 | 修复/裁决 | 结果 |
| --- | --- | --- | --- |
| 原 cleanroom 只执行 04A | 04B–04D 可依赖工作区残留而不被发现 | 扩展 04B/04C/04D 测试、真实 Chrome、ledger、导入和提交边界 | 通过 |
| cleanroom 初版引用不存在的 04C integration 路径 | 聚合脚本不能运行 | 改为真实 unit 文件路径 | 通过 |
| 非 live productization smoke 用 memory transport 执行动作 | 04C 后默认 gateway 会真实等待 CDP；旧 smoke 语义失真 | smoke 只验证 lifecycle start/cancel；真实 action 由紧随其后的 live Chrome smoke 负责 | 两条 lane 均通过 |
| 04B/04C/04D ledger sync 顺序不幂等，04D 不更新 typed summary | source-to-target check 会相互制造 drift | 04C 原位替换 owner rows；04D 加 typed summary、canonical JSON、`--write/--check` | 连续三次 check 全对齐 |
| cleanroom 未复制 `third_party` | 提交边界/NOTICE 检查不等价于正式打包 | 将 `third_party` 纳入 source-free copy；仍排除 vendor/source repos | submission boundary 通过 |
| commit fence 仅测终态与损坏 receipt | 进程在中间 phase 退出的恢复证据不足 | 新增六相位重新实例化、audit、pending/terminal 矩阵 | 16 项 04D integration unit 通过；cleanroom 04D 39 项通过 |
| ledger verifier JSON 只给计数 | cleanroom 失败时不能定位 blocker | 增加 `blocking_findings` 字段 | 完整仓库 0 blocker / 0 error |
| source-free copy 内运行全局 ledger verifier 会报 33 个历史 M1-01B vendor target 缺失 | 若把 vendor 复制进 cleanroom 会违反本阶段 source-free 边界 | 分层裁决：cleanroom 检查当前 04B–04D owner、导入和行为；完整仓库单独运行全局 verifier | 完整仓库 `ok=true`；历史 01B pilot 仍为 `required_for_main_path=false` source-pool 债务 |
| 根目录执行裸 `pytest` 递归收集 `tmp/**` 与 vendor 上游 tests | Windows ACL 和上游可选依赖导致 218 collection errors，非正式项目测试失败 | 正式全量范围固定为 `pytest tests`；保留根命令失败记录 | 正式套件全绿 |
| CodeWorker API 测试仍断言旧 `codesession_` resume-token 前缀 | 当前合同为 `session_id:leaf_uuid:sequence`，稳定单测失败 | 断言改为用三个公开 metadata 字段验证完整 token；不改 runtime owner | 相关 4 项与全量套件通过 |

首次正式 `pytest tests` 在最后一项缺陷上得到 `731 passed, 1 failed, 1 skipped, 523 subtests`
后才修复；最终结果不是掩盖该失败的重跑。

## 8. 最终验证

| 命令/场景 | 最终结果 |
| --- | --- |
| `python -m pytest tests -q -p no:cacheprovider --basetemp G:\agent-zoo\tmp\pytest-m1-04-project-tests-final` | **732 passed, 523 subtests passed, 1 skipped, 3 warnings**；936.70s |
| `python scripts/verify_browser_session_cleanroom.py` | `ok=true`；204.1s；source repositories absent |
| cleanroom 04A unittest | 25 passed |
| cleanroom 04B pytest | 11 passed, 10 subtests passed |
| cleanroom 04C pytest | 21 passed, 5 subtests passed |
| cleanroom 04D pytest | 39 passed |
| cleanroom real productized Chrome smoke | 2 actions、2 allow、0 deny、0 human、3 artifacts；通过 |
| 三个 source-ledger `--check` | 25/10/7 decisions；全部 aligned |
| `python scripts/verify_internalization_ledger.py --json` | `ok=true`，0 blocker，0 error，552 historical warnings |
| `python -m compileall -q apps packages scripts tests` | 通过 |
| `python scripts/verify_submission_boundary.py` | 通过 |
| `git diff --check` | 通过 |
| `git status --short`（最终代码目标） | clean |

单个 skip 和三条 warning 属于既有全仓测试状态：skip 不替代 mandatory live Chrome lane；warning
是 ledger dataclass `TestEntry` 的 pytest collection warning，不是本聚合功能或 cleanroom 失败。

## 9. 残余风险与后续 owner

1. M1-01B 历史 `vendor-runtimes/claude-code-runtime/pilot` 仍是 source-pool evidence，
   `required_for_main_path=false`；它不计入本阶段有效实现，也不应为了让 source-free cleanroom 的
   全局 ledger audit 通过而被复制。M3 全局打包/账本工具化应明确表达这一分层。
2. 04B structured coordinate fidelity 的既有样本结果仍是 0.875；更大规模网页、复杂 OOPIF、
   动态 layout 与 bitmap 消融由后续 benchmark/里程碑退出验证，不在本聚合伪装为满分。
3. Windows Chrome 启停、网络和跨进程 owner-loss 仍有平台时序风险；本次真实 lane 已通过，
   但不能替代里程碑退出的多轮鲁棒性与性能测试。
4. 外部 provider/MCP/subagent/worktree evidence mapper 只承担 supplementary projection；其各自
   canonical owner 与真实故障恢复仍由对应后续单元负责。
5. 04D 只提交 recovery input，M1-07B/07C 仍需实现 fault injection、recovery planner、
   reroute/retry/restore 的 canonical state 和正式异常场景。
6. 正式两类跨领域 live 场景、单 run 2,000 有效 transitions、sealed autonomous benchmark、
   动态稀疏拓扑/低熵对照、真实 local/edge/cloud、多模型和提交材料仍全部开放。

## 10. 状态推进裁决

在本审查证据提交后，可把 `M1-S04D-02` 与 `M1-04A–04D aggregate review` 标为完成，并把
唯一下一入口推进到 `execution-state.yaml` 已规划的 M1-05 首个 slice。不得跳过状态文件中的
下一入口，也不得把本聚合结论解释为 M1、第一阶段或任何赛题满分证据已经完成。
