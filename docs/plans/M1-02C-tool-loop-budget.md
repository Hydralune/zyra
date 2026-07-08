# M1-02C Tool Loop Budget

## Execution Record

- Base commit: `f9ccca0259651d36eee2b49cc5638db7d837dae8`
- Unit: `docs/milestones/M1-runtime-memory-scheduler-fault/unit-02c-tool-loop-budget.md`
- Primary source: `claude-code-best`
- Auxiliary references:
  - `claudecode-related/claude-reviews-claude/architecture/zh-CN/02-tool-system.md`
  - `claudecode-related/claude-reviews-claude/architecture/zh-CN/06-bash-engine.md`
  - `claudecode-related/claude-reviews-claude/architecture/zh-CN/07-permission-pipeline.md`
  - `claudecode-related/Dive-into-Claude-Code/docs/architecture_zh.md`
  - `claudecode-related/Dive-into-Claude-Code/docs/build-your-own-agent_zh.md`

## Source-To-Target Crosswalk

| Auxiliary checklist | Claude Code source evidence | Zyra target | Verification |
| --- | --- | --- | --- |
| Tool request/result contract and schema guard | `src/Tool.ts`, `src/tools.ts`, `src/services/tools/toolExecution.ts` | `packages/runtime/zyra_runtime/tool_loop.py`, `packages/runtime/zyra_runtime/tools.py` | `tests/unit/test_tool_loop_budget_runtime.py` |
| Read-only concurrent batching and write serial execution | `src/services/tools/toolOrchestration.ts`, `src/services/tools/StreamingToolExecutor.ts`, `src/utils/groupToolUses.ts` | `ToolLoopScheduler`, `CodeQueryLoop._execute_batch()` | `test_runtime_batches_read_only_tools_and_serializes_conflicting_writes` |
| Result budget and large output externalization | `src/utils/toolResultStorage.ts`, `src/utils/truncate.ts`, `src/services/tools/toolExecution.ts` | `ToolResultBudgeter`, artifact store, `tool_result_budget_exceeded` events | `test_runtime_externalizes_large_tool_result_and_emits_budget_watchdog_signal` |
| Shell lifecycle and read-only command policy source | `src/utils/Shell.ts`, `src/utils/ShellCommand.ts`, `src/utils/bash/*`, `src/utils/shell/readOnlyCommandValidation.ts` | sidecar `tool_loop_contract`, tool metadata, executor timeout handling | `scripts/verify_code_worker_sidecar.py` |
| Permission/schema/runtime failures to watchdog signal | `src/hooks/toolPermission`, `src/utils/permissions/denialTracking.ts`, `src/utils/toolErrors.ts` | `ToolFailureSignal`, `watchdog_signal` session events | `test_runtime_converts_schema_error_to_failure_and_watchdog_events`, `test_runtime_converts_permission_denial_to_watchdog_signal` |

## Internalization Ledger

| Source repo | Source module | Target path | Integration mode | Main path evidence |
| --- | --- | --- | --- | --- |
| `claude-code-best` | Shell runtime, shell command process lifecycle, bash parser, shell validation, sandbox adapter, tool errors, truncation and result storage | `vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/utils/*` | `vendored_runtime` plus sidecar contract | `apps/code-worker/src/main.mjs --tool-loop-contract` |
| `claude-code-best` | Tool execution, orchestration, streaming executor, base tool declarations | `vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/services/tools/*`, `src/Tool.ts`, `src/tools.ts` | `vendored_runtime` plus source inventory | `metadata/tool_loop_budget_source_inventory.json` |
| Zyra runtime | Tool loop request, batch, plan, schema violations, budget decisions, failure signals | `packages/runtime/zyra_runtime/tool_loop.py` | `runtime_source` | imported by `CodeQueryLoop` and exported by `zyra_runtime` |
| Zyra runtime | Tool metadata and timeout mapping | `packages/runtime/zyra_runtime/tools.py`, `packages/runtime/zyra_runtime/executor.py` | `runtime_source` | `ToolExecutor.execute()` and `ToolLoopScheduler` |
| Zyra worker | Contract-backed scheduling, event log, artifact externalization, watchdog propagation | `packages/workers/zyra_workers/code_query_loop.py`, `code_worker_runtime.py`, `code_worker_bridge.py` | `worker_main_path` | `CodeWorkerRuntime.run()` |
| Zyra sidecar | Tool loop contract and runtime inventory boundary | `apps/code-worker/src/main.mjs` | `sidecar_protocol` | `CodeWorkerSidecarClient.tool_loop_contract()` |

## Main Path Behavior

The CodeWorker path now converts planned tool steps into a typed `ToolLoopPlan` before execution. Consecutive read-only and concurrency-safe tools run in one concurrent batch, while workspace writes, shell commands, and artifact writes are executed as serial non-read-only batches. Repeated mutating conflict keys are marked as conflict-protected and surfaced in event metadata.

Tool results are passed through `ToolResultBudgeter`. Oversized outputs are written to structured artifacts, inline output is replaced by a preview plus artifact id, and the query session records `tool_result_budget_exceeded`, `tool_failure_signal`, and `watchdog_signal` events. Schema errors are produced before executor dispatch, permission denials and timeouts are normalized by the executor, and all failure classes enter the same watchdog signal route.

The M1-02C source extraction tracks 49 Claude Code tool-loop budget files with 18,218 upstream source-pool lines. Those files under `vendor-runtimes/**` and the generated JSON inventory are audit/source-custody evidence only and are excluded from effective implementation counts. Effective implementation is limited to Zyra-owned runtime code under `packages/**`, main-path worker integration, productized sidecar protocol code outside the vendor/source-pool bucket, repeatable verification scripts, and behavior tests; tests remain verification evidence and do not count toward the production line minimum.

## Self-Check

- Read/write scheduling: read-only `file_read`, `browser`, `web_search`, `checkpoint`, and `trace` tools are grouped up to `max_read_only_concurrency`; mutating tools execute serially with conflict-key metadata.
- Request/result pairing: each `ToolLoopRequest` owns a stable `ToolCall`, and results are zipped back to requests in batch order before event emission and transcript persistence.
- Externalization: large `ToolResult.output` payloads become structured artifacts with inline preview, original size, budget size, and `full_output_artifact_id`.
- Failure signal path: schema, permission, timeout, runtime, non-zero exit, and budget signals become `tool_failure_signal` and `watchdog_signal` query-session events, then remain in snapshots and WorkerResult metadata.
- Boundary discipline: `zyra` reads productized sources under `vendor-runtimes/claude-code-runtime`; no runtime path depends on `../claude-code-best` or `../claudecode-related`.

## Post-Review Fixes

- Review base: `e1fbe5bb245acc432bcfceee5c51f3a68e470a9f`.
- Fixed stable result pairing for assistant `tool_use` blocks that do not provide an upstream id. `ToolSessionBridgeRuntime` now uses its bridge id as the executable `tool_call_id`, and `ToolSemanticEffectRuntime` requires the same id to appear in real `ToolExecutionRuntime` receipts.
- Fixed `ToolExecutionTimelineRuntime` to carry `read_only`, `access_mode`, `concurrency_safe`, `schema_valid`, `conflict_key`, and `conflict_protected` from streaming frames into timeline events. Without this, concurrent read-only batches could be misclassified as mutating batches.
- Added a runtime gate in `ZyraClaudeQueryEngine`: blocking reports from semantic effects, result context, budget chain, permission checkpoint, replay/source effects, readiness, integration audit, or contract gate now change the final `WorkerResult` to `ok=false` with `tool_runtime_gate_failed` instead of only writing metadata.
- Added regression tests proving no-id assistant tool_use/result pairing and proving a forced semantic blocker changes the worker result, not just event metadata.

## Validation

Commands run during the unit:

```powershell
node apps\code-worker\src\main.mjs --tool-loop-contract
python -m unittest tests.unit.test_tool_loop_budget_runtime
python -m unittest tests.integration.test_code_worker_tool_loop_budget
python -m unittest tests.integration.test_code_worker_sidecar tests.integration.test_code_worker_query_session_lifecycle
python scripts\verify_code_worker_sidecar.py
```

Post-review commands run:

```powershell
python -m unittest tests.integration.test_code_worker_tool_loop_budget
python -m unittest tests.unit.test_tool_loop_budget_runtime
```

Additional full-suite and boundary validation should remain part of final commit review:

```powershell
python scripts\verify_submission_boundary.py
python scripts\verify_internalization_ledger.py --json
python -m unittest discover -s tests
git diff --numstat f9ccca0259651d36eee2b49cc5638db7d837dae8 HEAD -- apps packages tests scripts vendor-runtimes skills
```

## Exclusions

`vendor-runtimes/claude-code-runtime/metadata/tool_loop_budget_source_inventory.json` and this document are audit/crosswalk records. They are not counted as effective implementation code. Large generated inventories, source-to-target ledgers, and documentation remain outside the effective code total.
