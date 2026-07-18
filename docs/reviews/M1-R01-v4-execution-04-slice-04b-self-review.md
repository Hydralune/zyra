# M1-R01 E04-B 增量批判式自审

## 结论

`E04-B Runtime Core Source Recovery` 在实现提交
`01ca151bd717e792a2db512e24cd82dab2b4c577` 上通过增量验收。本结论只关闭
04B 的 query、compact/restore 和 tool orchestration 三个能力域，不完成 E04，
不恢复 `M1-S05C-01`，也不替代 E04 专用独立终审。

## 上游源码恢复与 Zyra 接管

- `e04-source-001/002`：从 `claude-code-best` 的 `QueryEngine.ask` 与
  `queryLoop` 尾部裁剪输入接纳、observe 后续行进和终止语义，保留 TypeScript，
  接入 `QueryLifecycleRuntime`、`QueryLifecycleSnapshot` 和
  `CodeWorkerApplication.runTaskRuntime`。React/UI、telemetry 和产品缓存分支被移除。
- `e04-source-003`：把 `autoCompactIfNeeded` 的预算触发、失败计数和熔断语义接入
  `CompactionSourceCustodyRuntime` 与 Zyra checkpoint；三次连续失败后打开持久化
  circuit，默认路径不再静默退回旧 reconstruction。
- `e04-source-004`：把 `processResumedConversation` 裁剪为显式的 Zyra snapshot、
  attachment、metadata 和 content-replacement 输入。fork 保留新 session identity，
  non-fork 接管来源 session；恢复 receipt 可持久化并幂等重放。
- `e04-source-005/006`：将 `runTools` 与连续 `partitionToolCalls` 语义接入
  `ToolExecutionRuntime`。修复旧实现把 `read -> write -> read` 重排为
  `read -> read -> write` 的真实顺序错误；并发/串行只在连续分区内执行。
- `e04-source-007`：将 `enforceToolResultBudget` 接入 `ToolResultRuntime`。
  oversized result 必须先形成 artifact receipt，再对下一轮上下文可见；同 payload
  重入幂等，不同 payload 复用 in-flight identity 失败关闭。

逐 range 的 source fingerprint、候选 target fingerprint、裁剪分支和实现提交见
`docs/reviews/evidence/M1-R01-v4/execution-04/slice-04b/target-provenance-report.jsonl`。
G0 不可变清单未改写。

## 默认路径、状态 owner 与反回退

- 默认闭包为 `CodeWorkerApplication.runTaskRuntime -> ClaudeRuntimeCore.run ->`
  query/compact/tool runtimes；coordinator 不再只造状态而不调用这些 owner。
- query 由 `QueryLifecycleSnapshot` 持久化；compact/restore 由
  `CompactionSourceCustodySnapshot` 与 `CompactRestoreSnapshot` 持久化；tool 执行和
  result replacement 分别由 `ToolExecutionSnapshot` 与 `ToolResultSnapshot` 持久化。
- `budget.ts` 只保留兼容调用，并委托 `ToolResultRuntime`；它不再有第二套结果预算
  reconstruction，也不取得 canonical owner。
- `ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME`、
  `ZYRA_DISABLE_E04_COMPACT_SOURCE_RUNTIME`、
  `ZYRA_DISABLE_E04_TOOL_SOURCE_RUNTIME` 会杀死或可观察地改变真实默认路径，未接入
  Python 逻辑决策回退。
- 零 scripted-turn 的跨语言请求仍需合法完成，因此 query 状态机显式允许
  `queued -> completed`；这是修复真实 Python 默认入口回归，不是伪造 user turn。

## 对抗与变异结果

- 三个 G0 mutation operator 均先修改对应 TypeScript source-custody guard，再通过
  typecheck，随后由精确 killer `e04-query-disable`、`e04-compact-disable`、
  `e04-tool-disable` 杀死，并恢复到原 SHA-256；无 backup 残留。
- 若 tool result 在 artifact receipt 前进入下一轮上下文，durability 测试失败；若
  restore 再做一次外部写入，幂等计数失败；若 conflicting payload 被当成重复请求，
  in-flight fence 测试失败。
- 若工具按全批类型聚类而不是连续分区，`read-write-read` 顺序测试失败。
- `--all` 在剩余 10 个 operator 未由 04C-04E 实现前明确拒绝执行，防止把局部
  mutation 伪报成 E04 全量通过；全 13 项只能由 04F 收口。

## 有效行数分桶

- production TypeScript：新增 757 行，删除 49 行；承担默认 query、compact、
  restore、tool batch、budget/result 状态与副作用语义。
- test：新增 538 行，覆盖正常、失败、恢复、disable 和精确 mutation killer。
- validation tooling：新增 165 行，删除 14 行；只承担可逆 mutation apply/restore，
  不计入产品运行时代码。
- adapter-only：新增 8 行、删除 58 行，均为 `budget.ts` 兼容委托；不计入 canonical
  owner 或有效 production。
- generated、data-as-code、vendor-like/source-pool、mock-only、fixture-only：0 行。

## 验证与未关闭事项

验证命令、通过数量与 G0 复核见
`docs/reviews/evidence/M1-R01-v4/execution-04/slice-04b/verification.json`；mutation
逐项结果见同目录 `mutation-results.json`。两份 Python 默认入口集成测试共 22 项
通过，所选 TypeScript 相邻回归共 115 项通过，E04-B 专项 12 项通过，typecheck
通过。

`default-loop-adversarial.behavior.test.ts` 中 3 个 permission 测试仍使用旧的 fake
capability-host 形状；这是 G0 后已知且分配给 04C 的 permission/capability 默认路径
收束，不在 04B 偷改 fixture 掩盖。八域 E2E、全 13 mutation、双发行构建、cleanroom、
有效行数总审计与 candidate gate 仍由 04F 强制执行；E04 terminal verdict 只能由
专用独立审查任务书在最终 target commit 上给出。
