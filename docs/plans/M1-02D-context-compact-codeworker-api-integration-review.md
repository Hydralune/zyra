# M1-02D Context Compact CodeWorker API Integration Adversarial Review

## Review Status

结论：`verified_pass`。

原“通过”结论曾因对抗复审证据不足而撤销。本轮已修补并验证真实 provider/SSE 与实际 retry、active compact prompt、跨请求 runtime-state checkpoint、合法 branch resume、task/run fail-closed、untrusted user restore、session durability、projection isolation、pure diagnostic 和 strict HTTP contract。

最终证据包括 targeted 10 tests、父级 sidecar/scenario/submission-boundary 命令、286-test full discovery、独立 clean-copy targeted run 和基于 `935dd93` 的有效行数分桶。代码修改、动态可达、状态托管、断开/负例、clean-copy 和行数门禁共同支持本 review 从 `verification_pending` 提升为 `verified_pass`。

本文件不声称根目录 `docs/milestones/execution-state.yaml` 已在本轮更新，也不回写受保护的历史 slice 文档。

## Adversarial Findings And Remediation

| ID | 对抗复审发现 | 修补 | 验证证据 | 状态 |
| --- | --- | --- | --- | --- |
| 02D-R1 | 模拟 frame 不能证明真实 provider、SSE 与 actual retry | 默认 model path 接入 provider transport；SSE delta/done/error 写入 transcript/event；retry 重新发起 provider request | targeted transport/retry 测试证明首请求失败后出现可区分的新请求，usage/error/retry 因果可追溯 | verified |
| 02D-R2 | compact 可能只生成 report，未证明 active prompt 和下一轮消费 | compact prompt 绑定真实 transcript、tool-result budget、preserved/security state；pending contract 进入下一轮 envelope | targeted tests 覆盖 active compact、pending/applied restore 和 disable 对照 | verified |
| 02D-R3 | restore 仅进程内可见，缺少跨请求 custody | session store 增加 runtime-state checkpoint、per-path lock、锁内 sequence、flush/fsync、atomic request index；CodeWorker 跨实例 hydrate | 两个 CodeWorkerRuntime 实例、同 task/run/session 连续请求恢复 budget 与 pending compact contract | verified |
| 02D-R4 | branch resume 与历史 identity 可能污染当前 audit | source session 只用于 replay/runtime reconstruction；plan/actions/events/custody observations 重绑 current session/request；旧 identity 仅存 provenance | 合法同 task/run branch resume 通过；query event-flow/state-graph/disconnect 无历史 identity mismatch | verified |
| 02D-R5 | 跨 task/run restore 可能越界 | runtime-state load 和 replay 均严格校验 expected task_id/run_id | 同 session 跨 task/run 确定性 fail-closed | verified |
| 02D-R6 | 外部内容在 compact/restore 后可能提升信任 | user/Web/MCP/browser provenance、trust、untrusted 与 redaction marker 在 compact/restore 后保持 | untrusted restore message role 为 `user`，包含 untrusted/redaction/provenance marker | verified |
| 02D-R7 | projection 可能跨 task/session 串线，diagnostic 可能产生状态 | task-scoped projector 强制隔离；顶层 probe 保持 pure diagnostic；生产状态只来自 task runtime | 多 task/session HTTP projection 不串线，malformed JSON 返回 400 | verified |
| 02D-R8 | disable path 旧测试假设每个声明 turn 都 dispatch | restore integration disabled 后第一 turn 完成，第二 turn 在 model dispatch 前 fail-closed | mutation test 证明只有第一 turn model report、第二 turn未 dispatch、所有已有 report restore count 为 0 | verified |

## Target Coverage Matrix

| 执行单元目标 | 状态 | 主路径证据 | 阻断 |
| --- | --- | --- | --- |
| context usage、compact boundary、next-turn restore 进入默认 CodeWorker path | 完成并验证 | QueryEngine preflight、compact prompt、pending/applied restore events、model envelope | 否 |
| 真实 provider/SSE/retry/fallback/budget state | 完成并验证 | provider transport、SSE parser、retry attempts、runtime budget mutations | 否 |
| post-compact restore 跨请求影响后续 query/model context | 完成并验证 | durable session checkpoint -> new CodeWorkerRuntime -> QueryEngine hydrate | 否 |
| file/skill/plan/MCP/tool/budget 与 provenance/trust/redaction 恢复 | 完成并验证 | restore segments、context security runtime、user-role untrusted messages | 否 |
| session durability、multi-request replay 与 branch resume | 完成并验证 | `claude_session_store.py`、`claude_session_replay_runtime.py`、current identity rebound | 否 |
| task-scoped API projection、pure diagnostic、strict HTTP contract | 完成并验证 | task routes、projection isolation、malformed JSON 400 | 否 |
| 断开 compact/restore/retry/state owner 后行为明显改变 | 完成并验证 | disable/mutation contrast 与 fail-closed result | 否 |

## Main Path And State Custody

| 状态 | 唯一 Zyra owner | 持久化/恢复链 | 隔离/因果证据 |
| --- | --- | --- | --- |
| active/compacted context | runtime context + compact restore runtime | active prompt -> compact contract -> runtime checkpoint -> next request envelope | compact/restore events 与 model request 因果关联 |
| pending next-turn restore | restore integration + session checkpoint | resolve-once state 保存于 `session_snapshot.runtime_state` | disable 后第二 turn 在 model dispatch 前 fail-closed |
| runtime budget/retry/usage | `RuntimeBudgetState` + canonical event path | snapshot/from_snapshot 恢复 totals、mutations、findings/current limits | 当前 session/request 强制重绑，source identity 仅 provenance |
| session transcript/checkpoint | `CodeWorkerSessionStore` | append-only JSONL、RLock、锁内 sequence、flush/fsync、atomic index | task/run guard、safe-name digest、跨实例 replay |
| replay/branch resume | `CodeWorkerSessionReplayRuntime` | source session records -> validated plan -> current integration | 默认最后 request；显式 selector mismatch 拒绝；历史 records 不修改 |
| task API projection | task-scoped canonical events | task event stream -> read-only projector -> HTTP contract | task/session projection isolation |
| provider catalog/credential | 05D `ProviderControlPlane` | 02D 只保留 provider compatibility/dispatch projection | 02D 不写 catalog/credential truth，secret 不进入 event/catalog |

## Source-To-Target And Internalization Review

| 能力 | 主要来源 | Zyra 目标路径 | 主路径入口 | 裁决 |
| --- | --- | --- | --- | --- |
| query/context/tool/session aggregate | `claude-code-best` QueryEngine/query lifecycle | `packages/runtime/zyra_runtime/claude_query_engine_runtime.py` | CodeWorker default run | `zyra_module_migrated` |
| compact prompt/boundary/restore | Claude compact lifecycle；Agent Framework atomic compaction | compact/context/restore runtime modules | query preflight -> checkpoint -> next request | `zyra_module_migrated`; Claude stub 不作 active 证据 |
| provider stream/retry/usage | Claude stream/retry；opencode/Hermes provider contract | `model_api_runtime.py`、runtime budget/recovery | CodeWorker model dispatch | `zyra_module_migrated`; 05D 仍是 provider owner |
| context trust/redaction | Claude MCP/context trust；Zyra policy | context security + restore integration | compact input/summary/next-turn envelope | `zyra_module_migrated` |
| durable session/checkpoint/budget | opencode durable session/event；Hermes SessionDB；AF session contract | session store/replay/runtime budget | cross-request CodeWorker session | `zyra_module_migrated`; 无第二套 external truth |
| task API projection/HTTP | opencode projector；OpenHands API/event boundary | task API projector/contract + `apps/api` | `/tasks/{task_id}/workers/code/*` | `adapter_encapsulated` + Zyra-owned state/validation |

本轮没有把 `vendor/**`、`vendor-runtimes/**`、source pool、manifest、ledger、预录 frame 或外部 sidecar 计为深度内化。真实主路径在独立 clean-copy 中不依赖根目录来源仓库。

## Effective Line Count Review

Base commit: `935dd93`。

| Bucket | Command | Final numstat | 结论 |
| --- | --- | --- | --- |
| production | `git diff --numstat 935dd93 HEAD -- apps packages skills scripts` | `+11,088 / -347` | 超过 slice 8,500 行 production 门禁 |
| tests | `git diff --numstat 935dd93 HEAD -- tests` | `+707 / -42` | 仅作行为验证，不抵扣 production |
| vendor/source-pool | `git diff --numstat 935dd93 HEAD -- vendor vendor-runtimes` | `0` | 无 vendor/source-pool 新增 |

generated、data、docs、ledger、fixture、mock、未接入样板和薄 adapter 均不计 production。02D 父级 17,000 行累计口径保持原父级记录。

## Tests And Verification

| Command / verification | Result |
| --- | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.integration.test_codeworker_context_compact_api_integration tests.integration.test_code_worker_query_session_integration` | `OK, 10 tests, 22.057s`; 后续最终复跑同样通过 |
| `.\.venv\Scripts\python.exe scripts\verify_code_worker_sidecar.py` | `passed` |
| `.\.venv\Scripts\python.exe -m unittest tests.scenarios.test_m2_runtime_acceptance` | `OK, 1 test` |
| `.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py` | `passed` |
| clean-copy targeted run in an isolated temporary directory | `OK, 10 tests, 23.609s` |
| `.\.venv\Scripts\python.exe -m unittest discover -s tests` | `OK, 286 tests, 304.400s`, exit code 0 |

Clean-copy 只复制 Zyra 所需正式目录，运行目录为临时目录；无 cache、SQLite、artifact 残留，也不包含 vendor、source repos 或根目录来源仓库。targeted tests 覆盖 real transport/retry、active compact、cross-request/branch restore、task/run guard、untrusted user、projection isolation、HTTP 400 和 disable/mutation。

Windows full discovery 期间出现 browser asyncio `ResourceWarning`。命令 exit code 为 0，286 tests 全部通过；该 warning 未造成行为失败，记录为非阻断环境噪声。

## Requirement Alignment

| Requirement | 02D 实际贡献 | 本轮状态 | 尚未关闭的下游证据 |
| --- | --- | --- | --- |
| `REQ-MEM-01` | compact 前后目标/安全元数据保持、next-turn restore、跨请求 checkpoint | 02D 动态证据 verified；总矩阵仍 `partial` | 06A-C/07C/08 retrieval、skill memory、exact recovery、long-run |
| `SCORE-LOOP` | provider failure/retry/compact 后继续或确定性 fail-closed | 02D 局部证据 verified | 单 run 2,000 个有效 transitions |
| `SCORE-ALGO` | Zyra-owned compact/restore/state custody 与 scope guard | 02D 实现/行为 verified | 完整算法消融与复杂度材料 |
| `SCORE-EFF` | context budget、usage/cost、compact/retry metadata | 02D runtime 指标链 verified | 真实端边云/provider 成本对照 |
| `SCORE-ROBUST` | actual retry、跨实例 checkpoint、branch resume、跨 scope reject | 02D 失败/恢复负例 verified | 故障矩阵、重复 live run、MTTR |

赛题总体状态不会因本 review、行数或单元测试自动提升为 verified；本文件只关闭 02D slice 的工程与行为验收。

## Disable And Mutation Evidence

- 禁用 restore integration 后第一 turn model report 存在，第二 turn 未 dispatch，所有已有 report 的 restore count 为 0，worker fail-closed。
- 同 session 跨 task/run restore 被拒绝；合法同 task/run branch resume 恢复 parent context 并重绑 current identity。
- 多 task/session projection 不串线；malformed JSON 返回 400；diagnostic 不成为 canonical task state。
- 历史 replay/session records 不被篡改；source session/request 只进入 provenance metadata。

## Residual Risks

- 外部 provider credentials、真实 live cloud provider 和端边云 dispatch 属于 M1-05D 及后续 evidence owner，不是 02D provider compatibility projection 的状态所有权；不阻断 02D。
- Windows browser asyncio `ResourceWarning` 未影响 full discovery exit code 或测试结果，当前非阻断；后续 browser owner 可继续收束资源关闭噪声。
- `REQ-MEM-01` 总体仍依赖 06A-C、07C、08；02D 只关闭 compact/restore/CodeWorker API 证据。

## Completion Gate

主路径、state custody、source-to-target、targeted/full/clean-copy、submission boundary、disable/mutation、HTTP isolation 和有效行数分桶均已通过，本 review 结论为 `verified_pass`。根目录 `execution-state.yaml` 未由本轮更新。
