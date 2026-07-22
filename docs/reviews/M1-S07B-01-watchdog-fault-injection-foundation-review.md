# M1-S07B-01 Watchdog 与故障注入基础批判式自审

日期：2026-07-22

baseline：`6900e96dcb6dd73c22b803787afc30125fbe947c`

实施前冻结：`a9ab2be2ddf1a4bd317a6dc19c1494aa13a483e9`

中间实现：`db27e57892cbb62eec4e783f73394c0a6a5d54df`

最终 implementation：`242038c842a02b3311e2452dad3cb86a2fa864e6`

## 结论

本 slice 完成。最终实现把 Browser Use 的 attached-watchdog lifecycle 以 Python 原语言裁剪进入
Zyra observer runtime，把 OMP 的 deadline、emission、process/MCP 和 provider error 机制以
TypeScript 原语言接入现有 Claude runtime，并由 `FaultStateStore` 统一承担 observer、observation、
fault signal、injection、source cursor 与 07C handoff delivery 的 durable custody。

保守逐文件有效 production 为 **8,866 行**，高于最低 **8,500 行**，余量 366。Python 有效
production 为 8,299，TypeScript 为 567；测试、类型/interface、重复 DTO/schema、export、manifest、
adapter-only 和文档/注释均未抵扣。机器证据见
`docs/reviews/evidence/M1-S07B-01-watchdog-fault-injection-foundation.json`。

## 三提交与门禁事实

1. `a9ab2be` 在首行生产代码之前冻结 source role、原语言、migration mode、owner 和统计 baseline。
2. `db27e57` 是第一次只含代码/直接测试的实现提交。其后、任何 evidence 之前，逐文件保守审查判定
   扣除 DTO/type/adapter 后仍不足 8,500；因此它没有被伪报为完成，也没有被 evidence 冻结。
3. 审查同时发现 crash 后持久化 `RUNNING` 不能证明新进程 callback 已 attach，以及 observer-local
   revision 重启回退两个真实缺陷。`242038c` 补齐 process epoch/restart supervisor 与 durable source
   revision cursor，重新完成直接和相邻测试，成为唯一最终 implementation target。
4. 本 review、机器 evidence、source ledger sync script 与 ledger seed 只进入后续 evidence commit，
   不参与有效行数统计。

## 运行时模块与默认入口

| 能力 | Zyra 模块 | 默认入口与真实效果 |
|---|---|---|
| observer lifecycle | `observer_registry.py`、`lifecycle_supervisor.py` | `RuntimeWatchdog.start/tick/stop` 显式 attach/start/disable；crash 后用新 process epoch 重挂 callback，bounded backoff 后 restart，generation/sequence heartbeat fail closed |
| browser crash source | `browser_source.py`、`observers.py` | 直接 attach 04D `BrowserCrashDetector`，poll 真实 process handle，接收 CDP disconnect/request/heartbeat signal；Browser Use upstream `CrashWatchdog` 保持 `source_inactive` |
| tool/provider/process/MCP | Python `deadline_runtime.py`、`provider_supervision.py`、`supervision.py` 与 TypeScript `watchdog/runtime.ts` | tool timeout 赢得 terminal-result race；provider attempt/backoff/circuit、worker heartbeat、process exit、MCP reconnect 都产生结构化事实 |
| deterministic classification | `classifier.py` | 只读取 typed category/code/status/numeric fields；worker/provider/tool/session 等关键 identity 只来自 `CorrelationRefs`，不从 summary/exception text 推断 |
| durable fault custody | `state_store.py` | SQLite FULL synchronous transaction、observer/injection CAS、signal fingerprint dedupe、source revision cursor、projection receipt、handoff lease/ACK/dead-letter |
| same-run injection | `injection.py` | worker lost、tool timeout、browser crash、model failure、workspace corrupt 五类 injection 经 requested→armed→triggered→observed→projected→continued/handed_off；不破坏外部资源 |
| canonical effects | `event_writer.py` | 真实 observer 写 `WORKER_HEALTH`，injection 写 `FAILURE_INJECTED`；随后投影现有 TaskState、MemoryFabric 与显式绑定的 05D backend health |
| 07C boundary | `recovery_bridge.py` | 只写 typed recovery intention/outbox，支持 claim/expired lease reclaim/ACK/release/dead-letter；不选择 recovery plan |
| API/command/query | `api.py`、API `main.py`、`query.py` | GET fault view、POST inject、POST observer control 和 `/inject` 均动态触达同一 application/store |

## 状态 owner 与去重

- `FaultStateStore` 是 07B 唯一 durable owner。它不复制 TaskState、MemoryFabric、BackendRegistry、
  browser process/CDP 或 07C recovery plan。
- 04D `BrowserCrashDetector` 继续拥有 browser process/CDP evidence；07B 只消费 typed signal 和持久化
  source cursor。
- TypeScript RuntimeWatchdog 只拥有当前 Claude process 内 observer/emission state；通过现有
  `tool_failure_signal` frame 进入 Python canonical store，不形成第二 journal。
- permission、workspace、provider、tool、MCP 与 worker domain state 仍由既有 owner 负责；observer 只保存
  evidence reference、cursor 和分类结果。
- `RequirementChanged` 保持 03D/05C control fact，不进入 fault counter、pressure、health、injection 或 handoff。
- injection provenance 固定为 `injection_only`，pressure weight 为零，不能替代 disabled real observer。

## 批判式缺陷发现与修复

1. **首次实现的有效生产量不足。** raw line 或 generic bucket 会错误计入 TypeScript test、DTO、exports
   和 adapter。逐文件人工扣除后不足 8,500，故未创建 evidence。补充内容来自真实 restore/cursor 缺陷，
   不是 DTO 或报表填充。
2. **持久化 STOPPED 无法直接在新 runtime start。** registry 现在先重新 attach callback，再 start；相关
   restore 测试通过。
3. **持久化 RUNNING 更危险。** crash 不执行 stop，SQLite 仍为 RUNNING，而新 Python object 没有 callback。
   `ObserverRuntimeSupervisor` 以 process epoch 检查 runtime snapshot，先把 stale RUNNING 转为 STOPPED，
   再 attach/start。测试在不调用 application.close 的情况下关闭旧 DB，证明新 epoch 能收到真实 browser
   signal。
4. **source revision 不能只在 observer 内存递增。** provider/browser observer 重启会从 1 开始，可能让
   不同 evidence 复用同一 revision。`observation_source_cursors` 现在按 observer、injection 和显式 refs
   scope 在写 observation 的同一 transaction 内拒绝 regression/reuse；duplicate fingerprint 仍幂等。
5. **API application 曾持有跨请求 SQLite handle。** Windows 回归暴露生命周期错误；API 改成每请求构造、
   `finally` close，HTTP fault GET/POST 测试通过。
6. **04D detector 的实际 kind 与手工 bridge alias 不完全一致。** `process_exited` 与 `heartbeat_late` 已加入
   typed map，并由真实 process-handle poll 测试覆盖。
7. **provider failure 只有分类、没有 attempt/circuit custody。** 增加 generation-fenced attempt supervisor，
   retry backoff 未到时拒绝，预算耗尽或 breaker open 才把 fault 置 terminal；route 选择仍留给 07C。
8. **tool timeout 后迟到结果可覆盖终态。** `ToolDeadlineRuntime` 用 generation+token fence 保证 timeout 赢得
   race，late/duplicate result 只进 diagnostic snapshot，不改 terminal state。
9. **07C handoff 只有 append、没有消费 fence。** 增加 durable outbox lease；错误 consumer/token ACK 被
   拒绝，过期 lease 可 reclaim，bounded attempts 后进入 dead 状态，但 07B 不执行 recovery plan。
10. **injection crash 中断会永久占用 idempotency key。** restart reconciliation 对 observation 前状态
    fail closed 且不 replay；对已有 durable signal/projection 的状态只补 journal/handoff，不重发 canonical
    event 或破坏外部资源。

## 断开即失败与语义效果

- disable `browser-crash` 后，04D real signal 不再写入 store；同一 task 的 injection 仍可执行且带独立
  provenance。这证明 injection 没有掩盖 real source。
- 删除/断开 TypeScript runtime watchdog 后，Bun 测试不再产生 structured tool/provider frames；Python
  ingress 无 canonical signal。
- 删除 `ToolDeadlineRuntime` 会允许迟到 result 改写 timeout；deadline race 测试失败。
- 删除 `FaultStateStore` cursor 会接受重启后复用 revision 的不同 provider evidence；restart cursor 测试失败。
- 删除 injection state machine 会让 HTTP/command 无法产生同 run TaskState mutation、05C event 与 handoff。
- 删除 handoff lease/ACK 会让两个 07C consumer 同时消费；ownership/reclaim/durability 测试失败。

## 有效行数

统计区间固定为 `6900e96..242038c`。

| 桶 | 行数 | 计入最低线 |
|---|---:|---|
| 全路径 raw additions | 12,244 | 否 |
| counted code paths raw additions | 12,183 | 否 |
| raw production candidate | 11,035 | 候选 |
| nonblank/non-line-comment production candidate | 10,071 | 候选 |
| type/declaration | 836 | 否 |
| schema/DTO/data | 369 | 否 |
| adapter-only | 123 | 否 |
| tests/mock/fixture | 1,147 | 否 |
| docs/comments/non-code | 903 | 否 |
| generated | 0 | 否 |
| **conservative effective production** | **8,866** | **是** |

按语言：Python 8,299，TypeScript 567。逐文件表与所有 raw/effective 差额在机器 evidence 中；大文件
单独审查了 TypeScript runtime、contracts、injection、observers、state store 和直接测试。`state_store.py`
最高贡献 1,432，占 16.15%，未超过 20%。`contracts.py` 的 DTO/schema 超过 30%，因此只计 211 行真实
validation/refinement/fingerprint 行为。

## 测试与增量验证

- direct Python：21 passed in 5.93s；覆盖五类 injection、真实 browser/process/provider/tool/MCP/worker、
  disable、RequirementChanged、free-text identity、restart reconciliation、process epoch、source cursor、
  handoff outbox、HTTP/API/command/query/diagnostics/pressure。
- adjacent Python：31 passed in 198.95s；覆盖 scheduler API、M5 fault recovery、05C event spine、
  worker-pool integration/API 和 browser observability main path。
- TypeScript：3 passed；Bun build 250 modules 成功。bundle 为验证产物，已排除并删除。
- compileall 与 `git diff --check` 通过。
- source ledger sync `--write` 后 `--check`：aligned，恰好 2 个 `M1-S07B-01` decisions。
- dependency scan：0 root-source relative path、0 cache/vendor/source-pool changed path、0 OpenClaw path。
- generic line bucket gate 通过；其会把测试和 type/DTO 高估，故只作辅助，人工逐文件值为权威。

普通 slice 未执行完整 cleanroom。当前没有新增外部依赖、进程/端口/MCP、canonical owner 转移、公共
schema 不兼容迁移或全局默认策略变更；M1-07 sibling aggregate 负责完整 cleanroom、全仓回归和全量
ledger/source-to-target audit。

## 非本 slice blocker 与未伪报项

- 全局 ledger verifier 仍报告 11 个受保护 M1-01B `TARGET_PATH_NOT_FOUND` blocker；当前 07B-01 新增
  blocker 为 0，不越权倒填。
- `test_api_control_commands` 的 memory-frontier case 在到达本 slice injection 之前就因既有 E02
  `artifact_write` route 缺失失败。只更新了 command syntax，不修改受保护 E02 owner，也不把该 case
  伪报为通过。
- Windows pytest 曾留下一个 ACL 拒绝删除的 `.tmp` basetemp。它被 ignore、未跟踪、不在 implementation
  diff 或打包路径，也不是运行依赖。
- 本 slice 不声称关闭跨领域 live task、单 run 2,000 transitions、sealed zero-human、动态图熵对照、
  真实 local/isolated-edge/cloud 全矩阵、多模型或最终交付冻结门禁。

## 提交与下一入口

evidence commit 创建后，根目录 `G:/agent-zoo/docs/milestones/execution-state.yaml` 才可更新；该文件不在
Zyra Git 中，必须单独报告。下一入口为 `M1-S07B-02`，本 slice 不提前执行其 recovery/fault integration。
