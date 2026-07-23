# M1-S08-02 main-path hardening integration 批判式自审

日期：2026-07-23

slice baseline：`8065bac109a3bed9ba01e0e92392fec4d05bfca3`

父级 baseline：`44da53ad8ea909147709857e358b7d16e39f6313`

最终 implementation：`068d13358e08967947d13c593d447ee21b72ae51`

## 结论

**M1-S08-02 严格通过。** 六条真实 HTTP 主路径、18 个 canonical owner 的
disable -> material failure/difference -> restore、sealed 长程 run、两个跨领域任务、真实
local/isolated-edge/cloud dispatch、Anthropic/OpenAI 两种 wire/model、动态稀疏低熵对照、故障与需求变化恢复、
exact-target cleanroom、有效行数和 M1 -> M2 handoff 均通过 fail-closed policy。

最终复封：

- decision：`ready_for_m2`
- attestation：`29 passed / 0 failed`
- limitations：`[]`
- exit bundle digest：`5849cef791d32e3c185ae5c020cac51852c6ee890cf0a9fd9f34e1b712d85dcb`
- delta reseal digest：`f318141ddfd85be0a93a9cca935e07c2d32aa56e56f3e326f89343d66e1140ec`
- handoff digest：`1702823771d1c434410d3fcc852718a5cef519d8153ff7757f593cd8eb4eb81c`

早期审查发现的 edge/provider/benchmark/source-pool/catalog blocker 已全部关闭；本文件替代先前的
`implementation_complete_exit_blocked` 判定。

## 实施与内化落位

| 产品责任 | Zyra 落位 | 主路径语义 |
| --- | --- | --- |
| 六场景协议与执行 | `packages/evaluation/zyra_evaluation/m1_hardening/integration_contracts.py`、`integration_scenarios.py` | query/session/tool、permission、MCP、skill-memory-restore、subagent-recovery、stream-provider-failover |
| 集成编排与退出 | `integration_service.py`、`exit_gate.py`、`release_reporting.py` | 聚合行为、owner、live、benchmark、cleanroom、line、handoff；缺证据即阻断 |
| sealed 长程与低熵 | `benchmark.py` 及既有 topology/communication owners | 排除 heartbeat/log/replay/no-op；只承认真实 mutation/route/tool/restore/recovery 等 |
| live tier/provider | `live_evidence.py` 及 runtime owner | endpoint/process/isolation/request/route/lease/artifact/digest 与双 provider request-stream-tool-result |
| owner 断开矩阵 | `owner_matrix.py`、`owner_probes.py` | 18 个唯一 owner 真实断开、稳定失败或物质差异、恢复且无 fallback masking |
| exact commit cleanroom | `cleanroom.py` | Git archive、锁定 Bun、离线 workspace、内部 TEMP/TMP、路径与残留审计 |
| M2 handoff | `handoff.py` | surface/source-chain/state-custody/metrics 的 digest-bound 原子交付 |

本 slice 仍为 `migration_mode=audit_and_hardening_only`，没有引入新的上游黑箱、OpenClaw 或根目录来源仓库运行依赖，
也没有转移 02A–07C 的 canonical state owner。Claude-derived QueryEngine/tool/permission/MCP/compact 控制流继续由
Zyra TypeScript 正式模块承担；Python 只保留已裁决的物理 I/O、持久化、编排和 API 接入边界。

## 六条真实主路径与断开矩阵

完整 runner 在 implementation `0049362ccf0d48e355f1a555449d75b03fcdacb0` 上通过：

- run：`m1-integration-7762b39cd124baf91e7f`
- outcome digest：`187de7fe06a84a5914aecd3ba36cd37c3425110ea6d9c86443287d95bc8335f2`
- 6 scenarios、66 steps、24 assertions、18 disconnects，全部通过
- runner exit code：0；API 进程正常停止

| 场景 | 已证明的语义 |
| --- | --- |
| query/session/context/tool | 新 task/session、context、CodeWorker tool、artifact/revision/event 回读 |
| dangerous permission | 危险写操作 ask/deny/block，未授权副作用不落盘 |
| MCP auth/elicitation | tool/resource/prompt、auth/elicitation 与真实 task mutation 串联 |
| skill -> memory -> compact restore | workspace skill、memory ingest/mine、compact/restore 影响后续 context |
| subagent -> lease -> recovery | fanout、worker lease、typed fault、successor、handoff/reroute |
| provider/event/watchdog failover | loopback HTTP/SSE 连续 3 次 529 后 fallback model 成功，retry、event、watchdog 均绑定 canonical run/task/node |

18 个执行并恢复的 owner 为：query-session、tool-loop、workspace-runtime、code-index、sandbox-gateway、
permission-runtime、mcp-runtime、memory-retrieval、memory-curator、skill-memory-restore、physical-worker、
edge-worker、checkpoint-recovery、layered-route、graph-custody、provider-control-plane、runtime-event-spine、watchdog。
没有用空 payload、静态 catalog、日志或 fallback 代替断开即失败。

## Sealed 长程、live 与低熵证据

`m1-final-068d133-20260723a` 的 sealed receipt 通过：

| 指标 | 结果 |
| --- | ---: |
| effective actions | 1,017 |
| canonical transitions | 2,030 |
| source actions completed | 1,000 / 1,000 |
| human interventions / manual resumes / manual state edits | 0 / 0 / 0 |
| fault recoveries / requirement changes / topology mutations | 5 / 1 / 2 |
| final constraint satisfaction | 1.0 |
| duplicate ratio | 0.0 |

receipt digest：
`9220887742cc5e2ac3a9187f93477e0138db8a3e6ea6a1e5f7b9ae197c13d86a`。
两个各 500 actions 的真实文件任务分别覆盖 `software_runtime_integrity` 与
`verification_and_requirement_traceability`，均产生 500 个 digest-bound artifacts 并完成。

live admission 证明：

- local：真实 terminal/in-process worker；
- isolated edge：非 loopback `tcp+hmac`，独立 worker process、lease、artifact 与 manifest digest；
- cloud：OpenAI provider-owned managed CLI；
- provider A：Anthropic compatible `/v1/messages`，`claude-sonnet-5`；
- provider B：OpenAI compatible `/v1/responses`，`gpt-5.5`；
- 两者均非 simulated，均有 authenticated session、request/response/stream/tool-call/tool-result receipts。

provider/tier 观测来自同日真实 run `m1-final-68fdefd-20260723a`。后续改动没有改变 live-probe 或 tier owner
语义，manifest 以 digest 明确限定了可复用范围；不是把本地模拟标签提升为 live。

## 有效行数与 source-pool 边界

| 范围 | effective production | 最低线 | vendor/source-pool 处理 | 结果 |
| --- | ---: | ---: | --- | --- |
| M1-S08-01 | 9,193 | 9,000 | 0 | pass |
| M1-S08-02 | 12,778 | 7,000 | 0 | pass |
| M1-08 parent | 21,926 | 16,000 | 0 | pass |
| whole M1 | 285,538 | 不以规模替代证据 | 历史 130,176 行受 `aecc688...` 保护、计 0；新增未保护 vendor 为 0 | pass |

08-02 raw additions 为 18,509，其中 production raw 15,693、tests 1,950、docs 866；
generated/data/vendor-like/adapter-only/mock-fixture 均为 0。whole-M1 protected source pool 只保留历史 provenance，
cleanroom 无运行依赖，不能计入有效代码。

## 验证分层与增量复封

完整验收在 `0049362...` 上运行约 34 分钟并通过；之后 `068d133...` 只修改：

1. `integration_scenarios.py`：给已经冻结的 catalog gate 增加缺失 evidence pointer；
2. `test_m1_hardening_integration.py`：增加相应断言。

diff digest：
`4157dbf43c9cdb0f0eb918e14ad596bd8495c2b09479e36b9170d070af1911a5`。
该增量没有改变 runtime、scenario definitions 或 owner probes。依据项目“代码变化后只重跑受影响项”的分层规则，
未重复第二次 34 分钟全量，而在最终 target `068d133...` 上执行 exact-target delta reseal：

- Git archive digest：`412597069bd21ac9265df0b62684b80955bdfee106b8393c840a791bb4eb6621`
- cleanroom receipt digest：`416e8e10dfec8783366d088011ed3d33886eab95f057ce1987d0531df6b6c8b3`
- compile：passed
- affected unit：`25 passed in 5.60s`
- source dirty：false；symlink/residual/outside-link/forbidden-reference：0；cleanup：成功
- catalog gate：passed，evidence count 1
- continuity gate、line gate、cleanroom gate、handoff gate、exit gate：全部 passed

此外，当前实现的 foundation/integration/CodeWorker/watchdog 相邻矩阵为
`76 passed, 3 subtests passed`。旧的 Python CodeWorker integration 文件属于 TypeScript cutover 前契约，
其当前替代 foundation 已通过；没有为兼容淘汰 owner 而恢复旧 metadata。

## 批判式修复摘要

本轮最终阶段发现并关闭：

1. provider failover 原场景没有真实 wire retry；改为真实 HTTP/SSE 3×529 后 fallback 成功。
2. watchdog fault observation 缺 canonical scope；加入 run/task/node binding 和 mismatch rejection。
3. owner prefix probe 曾污染 edge worker 健康；取消 worker-targeted cleanup cancel，仅保留 task-scoped 清理。
4. cross-scenario parent 判定混淆外部 causation ID 与 event ID；明确区分。
5. cross-cutting gate 曾只看静态声明；改为消费实际 supporting gates/evidence 与 code-index material disconnect。
6. foundation/disable/topology 证据指针缺失；补成 admitted evidence。
7. catalog gate 没有 evidence pointer，导致内部 exit bundle 与顶层结果矛盾；`068d133...` 修复并完成精确增量复封。

## 权威状态

本审查与 `M1-08` 聚合退出审查均已形成 Zyra evidence commit 后，才允许更新根目录
`G:/agent-zoo/docs/milestones/execution-state.yaml`：保护 `M1-S08-02`，关闭 `M1-08` 与 M1，并把唯一下一入口
推进到权威 YAML 已声明的 M2 入口。根目录文档不属于 Zyra Git，提交边界必须在最终交付中单独披露。
