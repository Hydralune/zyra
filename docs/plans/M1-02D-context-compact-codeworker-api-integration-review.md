# M1-02D Context Compact CodeWorker API Integration Review

## Conclusion

结论：通过，阻断问题已在本次复审中修复。

本 slice 已把 compact/restore、model stream/retry、runtime budget、CodeWorker task API projection 接入 CodeWorker 默认主路径和 task API 路由。复审发现 `restore_files` 生成的文件恢复段原先主要携带路径，不能在 restore contract 层证明 workspace 文件状态；已修复为基于 `workspace_root` 的受限读取、size/hash/preview 元数据和安全层 redaction 后注入下一轮模型 envelope。当前未发现运行期依赖 `../claude-code-best`、`../opencode` 或 vendor/source-pool 黑箱的证据。

## Blocking Issues

未发现仍未修复的阻断问题。

已修复问题：

| 问题 | 证据 | 修复 | 验证 |
| --- | --- | --- | --- |
| `restore_files` 恢复段只证明路径，不证明 workspace 文件状态，弱化“恢复文件状态进入下一轮上下文”的语义效果 | `packages/runtime/zyra_runtime/compact_restore_runtime.py` 原 `FILE_ATTACHMENT` content 为 `Restore file attachment path: ...` | `CompactRestoreRuntime.build_report(..., workspace_root=...)` 通过 QueryEngine 注入 workspace root；`_workspace_file_restore_payload` 在 workspace 内解析文件，生成 `sha256`、`size_bytes`、`preview`、状态元数据，并保留越界/缺失/不可读状态 | `tests/integration/test_codeworker_context_compact_api_integration.py` 新增断言：workspace file restored message 包含 `sha256:`、`preview:`、`[REDACTED_SECRET]`、`restore_file_status=available` |

## Non-Blocking Risks

| 风险 | 状态 |
| --- | --- |
| CodeWorker API 的 `/workers/code/compact-state` 顶层 route 仍是诊断型 sample/probe；真实 task 范围投影在 `/tasks/{task_id}/workers/code/{session,tool-trace,compact-state}` | 非阻断；测试覆盖 task-scoped API，并且 contract runtime 校验 task_id scope |
| 本 slice 生产新增行数距离 8,500 下限余量不大，后续不应把审计/DTO 膨胀当成能力完成 | 非阻断；当前 8,696 行生产新增包含主路径 runtime、API projection、contract、restore/security/causality/disable semantics，并被 QueryEngine/API 调用 |

## Target Coverage Matrix

| 执行单元目标 | 状态 | 证据 | 阻断 |
| --- | --- | --- | --- |
| 上下文窗口、compact boundary、next-turn restore contract 接入 CodeWorker 主路径 | 完成 | `packages/runtime/zyra_runtime/claude_query_engine_runtime.py`; `compact_restore_contract_pending`; `next_turn_restore_contract`; `codeworker_restore_context_applied` events | 否 |
| post-compact restore 真实影响下一轮 query/model context | 完成 | 第二轮 `model_stream_report.envelope.messages` 包含 restore messages；测试断言 first turn restore count 为 0、second turn 大于 0 | 否 |
| 文件/skill/plan/MCP/tool/budget 状态进入恢复契约 | 完成 | `packages/runtime/zyra_runtime/compact_restore_runtime.py` 的 `FILE_ATTACHMENT`、`INVOKED_SKILL`、`ACTIVE_PLAN`、`MCP_INSTRUCTION_DELTA`、`TOOL_RESULT`、`BUDGET_STATE` segments | 否 |
| provenance/trust/secret-redaction/untrusted marker 保留 | 完成 | `codeworker_context_security_runtime.py`; `codeworker_restore_integration.py`; 测试断言 external MCP `[UNTRUSTED_CONTEXT]` 和 `[REDACTED_SECRET]`; workspace file restore 也 redacted | 否 |
| model stream/retry/fallback/budget state 可被 recovery/API 消费 | 完成 | `model_api_runtime.py`; `codeworker_model_recovery_matrix.py`; `codeworker_context_compact_recovery_audit.py`; `runtime_budget_state.py`; QueryEngine event sequence | 否 |
| CodeWorker task API 暴露 session/tool-trace/compact-state | 完成 | `apps/api/zyra_api/main.py`; `codeworker_task_api_projection.py`; `codeworker_task_api_contract.py`; 集成测试 POST/GET task routes | 否 |
| 断开 Zyra restore integration 后行为明显改变 | 完成 | `test_disabling_restore_integration_changes_next_turn_behavior` 断言 restore counts 为 `[0, 0]` 且 worker result blocked/degraded | 否 |

## Internalization Review

| 能力 | 来源 | Zyra 目标路径 | 主路径入口 | 语义效果 | 计入有效代码 |
| --- | --- | --- | --- | --- | --- |
| compact boundary / restore contract | `claude-code-best/src/services/compact/*` | `packages/runtime/zyra_runtime/compact_restore_runtime.py` | `ClaudeQueryEngineRuntime.run` | 生成 compact boundary、preserved/restore segments、budget custody | 是 |
| next-turn restore injection | `claude-code-best/QueryEngine` context restore pattern | `packages/runtime/zyra_runtime/codeworker_restore_integration.py` | turn loop 开始应用 pending contract | restore message 注入模型 envelope 和 context window | 是 |
| context security/redaction | `claude-code-best` MCP/context trust model, Zyra-owned policy | `packages/runtime/zyra_runtime/codeworker_context_security_runtime.py` | restore integration security snapshot | 外部 MCP 标 untrusted；secret redaction；prompt-injection warning | 是 |
| durable budget/session state | `opencode` durable session/event/budget pattern | `packages/runtime/zyra_runtime/runtime_budget_state.py` | QueryEngine runtime budget state | compact/model retry/tool result state 写入 event/budget snapshot | 是 |
| task-scoped API projection/contract | OpenHands/opencode event stream/API projection pattern | `packages/runtime/zyra_runtime/codeworker_task_api_projection.py`; `codeworker_task_api_contract.py`; `apps/api/zyra_api/main.py` | `/tasks/{task_id}/workers/code/*` | 从真实 task event log 投影 session/tool trace/compact state 并校验 route contract | 是 |

## State Custody

| 状态 | Zyra owner | 恢复路径 | 测试证据 | 外部依赖 |
| --- | --- | --- | --- | --- |
| context budget / compact boundary | `RuntimeBudgetState`, `CompactRestoreRuntime`, event log | compact report -> restore contract -> runtime budget snapshot | integration test phases and metadata | 无 |
| next-turn restore messages | `CodeWorkerRestoreIntegrationRuntime`, `ClaudeContextWindowManager` | pending contract -> restore application -> model envelope messages | restore count 0 then >0; restored message metadata | 无 |
| workspace file restore | `CompactRestoreRuntime` with QueryEngine `workspace_root` | restore_files -> file hash/preview segment -> security redaction -> next-turn model message | workspace file message assertions | 无 |
| API projection | `CodeWorkerTaskApiProjectionRuntime`, task store event log | task events -> projection -> route contract report | task route integration test | 无 |
| disable semantics | `CodeWorkerDisableSemanticsRuntime` | runtime disable flags -> event/report -> worker result | restore integration disabled test | 无 |

## Effective Line Count Review

Base commit: `935dd93`. Reviewed current working tree after this fix.

| Bucket | Command | Added | Notes |
| --- | --- | ---: | --- |
| production internalized code | `git diff --numstat 935dd93 -- apps packages skills scripts` | 8,696 | Counts only `apps/**`, `packages/**`; excludes docs/tests/vendor |
| tests | `git diff --numstat 935dd93 -- tests` | 267 | Integration behavior tests only; not counted toward minimum |
| vendor/source-pool | `git diff --numstat 935dd93 -- vendor vendor-runtimes third_party runtime-sources source-pool` | 0 | No vendor/source-pool additions |

Slice minimum production internalized code: 8,500 lines. Current production bucket meets the line floor. Docs, tests, vendor, data, fixtures, manifests, and this review file are excluded.

## Tests And Verification

| Command | Result |
| --- | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.integration.test_codeworker_context_compact_api_integration` | OK, 3 tests |
| `.\.venv\Scripts\python.exe scripts\verify_code_worker_sidecar.py` | passed |
| `.\.venv\Scripts\python.exe -m unittest tests.scenarios.test_m2_runtime_acceptance` | OK, 1 test |
| `.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py` | passed |
| clean-copy targeted run under `$env:TEMP\zyra-clean-*` with only `apps/packages/tests/scripts/docs` copied | OK, 3 tests |
| `.\.venv\Scripts\python.exe -m unittest discover -s tests` | OK, 283 tests |

默认主路径验证：已执行，CodeWorkerRuntime 默认 route/run path 触发 compact restore、model stream、API projection。

干净目录/干净缓存验证：已执行轻量 clean-copy targeted run；测试自身使用临时 workspace、artifact root、SQLite、event log。

断开即失败验证：已执行 restore integration disable 测试；禁用 Zyra restore integration 后下一轮 restore message count 归零，worker result 不再通过。

失败路径验证：已覆盖 compact-needed/compact boundary/post-compact restore、typed API contract、disabled restore integration；全量测试覆盖既有 worker/API/browser paths。

## Next Step

可以进入下一 slice 前，保留本复审修复的 workspace file restore 断言，后续若扩展 SkillTool/AgentTool restore，应复用同一 provenance/trust/redaction 字段和 disable-semantics 对照测试。
