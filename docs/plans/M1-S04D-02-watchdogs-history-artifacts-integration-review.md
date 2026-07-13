# M1-S04D-02 Watchdogs / History / Artifacts Integration 增量批判式自审

## 1. 结论

- Slice：`M1-S04D-02`
- 基线提交：`6a3fdd740eb76e30120c768272cfe037b2be7271`
- 实现提交：`3ce93b1b6562a0e53ad7a1b403a7a9c6c48add8a`
- 父级：`M1-04D`
- 自审结论：**当前 integration slice 完成，父级 M1-04D 累计收口；进入 M1-05 前仍必须完成 M1-04 数字阶段聚合审查。**
- 事件边界：当前 slice 只生成 `worker_health`、`browser_tool_failed`、`browser_recovery_input` 和浏览诊断/artifact 事件；没有生成 `recovery_planned`，其 owner 仍为 `M1-07C`。

## 2. Slice 目标覆盖

| 目标 | 生产落位 | 行为证据 | 结论 |
| --- | --- | --- | --- |
| watchdog 在 action 前挂到真实 04A event bus | `integration/event_bus.py`、`integration/application.py`、`browser_worker.py` | 真实 bus publish/teardown、代际 stale 拒绝、默认 Worker attach-before-action | 完成 |
| history 批次原子可见、崩溃尾部不进入 committed head | `history_store.py`、`history_runtime.py` | 未提交 JSONL tail 对 reader 不可见；下一 writer 截断并从 sequence 2 继续；transaction id 幂等/冲突 | 完成 |
| action failure 进入 health 和 07C input | `integration/application.py`、`failure_projection.py`、`browser_worker.py` | `TOOL_FAILED -> UNHEALTHY -> browser_recovery_input`，保留 retryable/outcome_unknown | 完成 |
| download 中断必须取消并清理 quarantine | `browser_action/download_guard.py`、`download_runtime.py`、`integration/downloads.py` | active GUID cancel、CDP disarm、partial/quarantine 删除、progress 清空 | 完成 |
| screenshot 在 highlight-free 状态捕获并必定 restore | `browser_session/screenshot_capture.py`、04A/04C screenshot handler、`integration/screenshots.py` | 调用顺序 remove/capture/restore；capture 异常仍 restore；不发布未完成 receipt | 完成 |
| storage state 原子保存与 backup restore | `profile_store.py`、`integration/storage.py` | fsync + replace；primary 损坏时从 backup 修复；双损坏 fail closed | 完成 |
| navigation/target 安全后置闭环 | `integration/navigation.py` | 禁止的 `file://` target 调用精确 `Target.closeTarget`；close 失败按 policy fail closed | 完成 |
| provider/MCP/subagent/background/Hashline/worktree evidence 映射 | `contracts.py`、`runtime_evidence.py` | partial 序列、terminal failure、outcome_unknown、worktree conflict list、disabled 不影响 browser 主路径 | 完成，supplementary only |
| restart 后 health/artifact/API 可重建 | `restart_projection.py`、`api_projection.py` | 每次 HTTP query 新建 worker/application，仍从 BrowserHistoryStore 得到 artifact/health；API 不暴露 URI | 完成 |
| EventLog/TaskState 落盘后关闭 delivery fence | `commit_fence.py`、`apps/api/zyra_api/main.py` | 事件集合缺失不能 ACK；checkpoint 不能越级 ACK；真实 HTTP 返回 `checkpoint_committed` | 完成 |
| history trajectory 输入 MemoryFabric 而不接管 owner | `integration/trajectory.py` | committed-head cursor、head 变化冲突、artifact/record causal identity 保留 | 完成 |

## 3. 父级 M1-04D 累计收口

| 父级能力 | 04D-01 foundation | 04D-02 integration | 累计结论 |
| --- | --- | --- | --- |
| history/trace/replay | hash-chain store、tool pair、trace/replay | atomic visibility、tail repair、continuation-safe transaction identity、trajectory | 完成 |
| watchdog/health | watchdog registry、crash detector、typed handoff | pre-action bus attachment、runtime event signal、restart reconstruction | 完成 |
| artifact | trace/history/adopted artifact lineage | atomic LocalArtifactStore、download/screenshot/storage evidence、redacted restart API | 完成 |
| browser lifecycle | post-run evaluator | attach-before-action、before-stop cleanup、finalize、teardown、commit ACK | 完成 |
| 最低有效 production | `9,245` | 保守 `6,520` | **`15,765 / 15,000`，完成** |

## 4. 来源角色、裁剪和正式落位

| 来源 | 角色 | 保留机制 | Zyra-owned 落位与裁剪 |
| --- | --- | --- | --- |
| browser-use | primary implementation | session event/watchdog 生命周期、profile/storage、download/screenshot cleanup | 拆入 `browser_observability/integration/**`、`browser_session/screenshot_capture.py` 和既有 04A/04C handler；不迁入上游 session owner 或未挂载 CrashWatchdog |
| OpenHands | supplementary implementation | event/artifact 可查询投影和 restart projection 模式 | `restart_projection.py`、`api_projection.py`、`commit_fence.py`；不创建第二套 EventLog、artifact store 或 server runtime |
| oh-my-pi | supplementary implementation | tool/runtime correlation、partial/terminal receipt、Hashline/worktree conflict | `contracts.py`、`runtime_evidence.py`、`trajectory.py`；不取得 provider/MCP/subagent/worktree canonical owner |
| browser-use CrashWatchdog | rejected | 无 | 继续由 Zyra `BrowserCrashDetector` 观察真实 process/CDP/timeout |
| claude-code-best、opencode | conformance/reference | lifecycle/tool-result/durable session 对照 | 不产生新的 production owner 或运行依赖 |

正式运行不依赖 `../browser-use`、`../OpenHands`、`../claude-code-best`、`../opencode`，也没有新增 source-pool、vendor runtime、pip/npm dependency、sidecar、MCP server、插件、Docker、本地辅助端口或动态 import。

## 5. 状态 custody

| 状态域 | canonical owner | 04D-02 责任 |
| --- | --- | --- |
| task/checkpoint | `SQLiteStore/TaskState` | API 只在真实 `save_checkpoint` 成功后 ACK delivery fence |
| canonical events | `EventLog` | API 只在整批 event ids 持久化后 ACK；不完整集合拒绝 |
| browser process/session/target/CDP/event bus | M1-04A BrowserRuntime | 订阅、generation fencing、diagnostic evidence；不复制 bus/session state |
| action/permission/download grant | M1-04C + M1-03A | 消费 receipt 和结果；cleanup 调回既有 guard/port |
| canonical artifact bytes | `LocalArtifactStore` | 原子写入、lineage/history 接入和 API redaction |
| browser observability history | `BrowserHistoryStore` | segmented JSONL + atomic head/index/transaction receipt |
| storage state | M1-04A `BrowserProfileStore` | primary/backup 原子替换和修复 evidence |
| process-live health | 04D projection | 非 canonical；restart 时从 history 重建 |
| recovery input | 04D typed handoff | 仅给 `M1-07C` 的输入；不做 recovery plan |
| trajectory memory | `MemoryFabric` | 04D 只提供 immutable committed-head input |

## 6. 主路径、动态可达性和断开即失败

真实入口为 `POST /tasks/{task_id}/workers/browser`：

1. `BrowserWorkerRuntime.run` 从 04A 得到 session/CDP/event bus。
2. 在任一 04C action 前调用 `BrowserObservabilityApplication.attach`；attach disabled/missing CDP/event bus 时 worker fail closed，action 不执行。
3. 04C 执行过程中，04D 订阅 04A bus，观察 timeout/disconnect/navigation/download/screenshot/storage 和 action receipt。
4. stop 前执行 storage/download/screenshot cleanup，随后 finalize history/health/artifact/recovery input。
5. API 持久化 canonical events、ACK `events_committed`，保存 TaskState、ACK `checkpoint_committed`。
6. `GET /tasks/{task_id}/browser-observability?view=...` 从 durable history/commit store 重建，而不是依赖上一个 Worker 进程对象。

断开 `BrowserObservabilityApplication` 会使真实 BrowserWorker 返回 `browser_observability_failed` 和 typed recovery input；关闭 OMP supplementary mapper 只删除 supplementary spans/signals，不改变 action/history/artifact 主路径。

## 7. 失败、恢复和因果语义

- CDP disconnect/request timeout/protocol failure均保留 bus event id、generation、retryable 和 outcome_unknown。
- 04C action receipt 的 `tool_call_id/action_id/receipt_id` 进入同一 history transaction、trace span、health signal和 recovery input。
- tool call/result pair 以一次 head replacement 对 reader 可见；崩溃在 segment append 后、head replacement 前时，只留下可修复 tail。
- EventLog 与 TaskState 不是伪装成跨 store 原子事务；`ObservationCommitFence` 明示 partial delivery，并提供 restart audit 识别损坏 receipt。
- permission continuation 复用相同 worker request/session 时建立新 delivery attempt，不回退或回归上一 attempt 的 commit phase。
- screenshot、download、storage 只有在真实 byte/state cleanup 成功后才生成可发布 evidence。
- API artifact projection只返回 digest、size、media type、causal ids 和 lineage receipt，不返回原始 filesystem URI。
- 所有 failure handoff 都标记 `planner_owner=M1-07C`、`is_recovery_plan=false`。

## 8. 有效行数分桶

基于 `git diff --numstat 6a3fdd7 3ce93b1`：

| 桶 | 新增 | 删除 | 是否计 production |
| --- | ---: | ---: | --- |
| 10 个新增 integration runtime modules（排除 `__init__.py`） | 4,600 | 0 | 是 |
| restart projection + highlight-free screenshot runtime | 560 | 0 | 是 |
| 既有 apps/packages 主路径改造 | 1,360 | 189 | 是 |
| 保守有效 production | **6,520** | **189** | **是，超过 6,000** |
| package export `integration/__init__.py` | 107 | 0 | 否 |
| tests | 611 | 0 | 否 |
| ledger seed data | 330 | 0 | 否 |
| ledger sync script | 124 | 4 | 否 |
| generated/mock-only/fixture-only/data-as-code | 0 | 0 | 否 |
| vendor/vendor-runtimes/source-pool | 0 | 0 | 否 |

父级累计 `9,245 + 6,520 = 15,765`，超过 `15,000`。测试、export、脚本和 ledger data 均未抵扣 production 下限。

## 9. 验证记录

在实现提交 `3ce93b1` 上完成：

1. 04D/04A/04C/API 组合：`59 passed`，52.39s。
2. 04B context + BrowserWorker 邻接回归：`38 passed, 12 subtests passed`，161.39s。
3. 新 integration 单测（独立工作区 basetemp）：`15 passed`，1.81s。
4. source-role/root-import/subprocess audit：`1 passed`。
5. internalization ledger verifier：`ok=true`、`blocker_count=0`、`error_count=0`；552 条是全 seed 的既有 warning，不是当前 slice error。
6. exact-commit clean copy（`git archive 3ce93b1`，`PYTHONDONTWRITEBYTECODE=1`）：04D unit + main path `18 passed`；真实 HTTP lane `1 passed`。
7. `compileall apps packages scripts tests`、`git diff --check`：通过。
8. vendor/vendor-runtimes diff：空。

普通 slice 未重复全仓长期套件；M1-04 数字阶段聚合审查必须在包含本 review 的最终 target commit 上执行全仓测试、完整 cleanroom、全量 ledger/source-to-target audit 和独立批判式复审。

## 10. 实现期间发现并修复

1. 最初 supplementary worktree attributes 用 set 过滤值，list 类型会触发不可哈希异常；改为显式 `None/empty-string` 判断并新增 conflict paths 测试。
2. 首次 API ACK 直接修改 frozen `BrowserWorkerRun`；改用 `dataclasses.replace`，保持 immutable result contract。
3. permission continuation 首次复用稳定 session-start transaction id，但第二 attempt payload 不同；transaction id 现在加入 canonical start event id，同时重复同一 event 仍幂等。
4. active integration 首次把 finalized scope 当成仍活动，导致 commit phase regression；现在同一 request/session 的新 continuation 建立独立 delivery attempt。
5. failure-before-action helper 首次引用未定义 scope；改为使用 typed signal 自带 scope。
6. health restart API 首次遗漏 `rebuilt` 初始化；现在统一从 BrowserHistoryStore 重建。
7. commit/integration API 首次未在每个 scope view 返回 scope；现在显式包装，HTTP restart 测试按 worker request 精确选择。
8. pytest 系统默认 temp root 在重复 Windows 清理后出现 ACL/lock；最终验证使用全新工作区 basetemp，代码和 clean-copy 测试均通过。

## 11. 批判式剩余风险

1. Commit fence 是可审计 outbox，不是跨 EventLog/TaskState/ArtifactStore 的分布式事务。数字阶段聚合应增加“进程在每个 phase 被杀死”的 crash matrix，并验证重放只推进未 ACK owner。
2. storage capture 真实 CDP origin/cookie 组合、OOPIF navigation 和大文件中途断连仍需在 M1-04 live/cleanroom 聚合 lane 扩展；本 slice 已覆盖 typed port、失败路径和 owner integration。
3. process-live health restart reconstruction 依赖 history 完整；损坏 history 会 fail closed 并交给 07C，不会静默使用内存 cache。聚合审查应把 history tamper 和 commit-receipt corruption 联合注入。
4. OMP evidence mapper 是明确的 supplementary input；正式 benchmark 不应通过 constraint 注入预录 evidence 代替真实 provider/MCP/subagent 事件，M3 evidence audit 仍需检查这一点。

## 12. 完成判定

- 当前 slice 行为、失败路径、动态可达性、语义效果、断开即失败：完成。
- 当前 slice production：`6,520 / 6,000`，完成。
- 父级 M1-04D production：`15,765 / 15,000`，完成。
- source role、state owner、event/recovery、artifact/path、clean-copy：完成。
- 根来源仓库和 vendor/source-pool 运行依赖：未引入。
- 状态推进条件：必须先完成 M1-04 数字阶段聚合审查；聚合通过后下一入口为 execution-state 指定的 M1-05 slice。
