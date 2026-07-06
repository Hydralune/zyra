# M1-02B Query And Session Lifecycle

## Execution Record

- Base commit: `6ee2c010b0fcae202480cdf0c0e18b2ab2e058d6`
- Unit: `docs/milestones/M1-runtime-memory-scheduler-fault/unit-02b-query-session-lifecycle.md`
- Primary source: `claude-code-best`
- Auxiliary references:
  - `claudecode-related/claude-reviews-claude/architecture/zh-CN/01-query-engine.md`
  - `claudecode-related/claude-reviews-claude/architecture/zh-CN/09-session-persistence.md`
  - `claudecode-related/Dive-into-Claude-Code/docs/architecture_zh.md`

## Source-To-Target Crosswalk

| Auxiliary checklist | Claude Code source evidence | Zyra target | Verification |
| --- | --- | --- | --- |
| QueryEngine session-level wrapper plus per-turn query loop | `src/QueryEngine.ts`, `src/query.ts`, `src/services/api/claude.ts`, `src/services/api/withRetry.ts` | `packages/workers/zyra_workers/code_query_loop.py`, `packages/workers/zyra_workers/code_worker_runtime.py` | `tests/integration/test_code_worker_query_session_lifecycle.py` |
| Stream lifecycle and raw SSE-style deltas | `src/services/api/claude.ts`, `src/services/api/client.ts`, `src/services/api/filesApi.ts`, `src/services/api/withRetry.ts` | `QueryStreamEventType`, `message_delta`, `stream_request_start`, `turn_end` events | `test_runtime_emits_turn_message_snapshot_and_transcript_lifecycle` |
| Append-only JSONL transcript | `src/utils/sessionStorage.ts`, `src/utils/sessionStoragePortable.ts` | `QuerySession.to_jsonl()`, session transcript artifact | `tests/unit/test_query_session_lifecycle.py` |
| Parent-UUID chain and resume reconstruction | `src/utils/sessionRestore.ts`, `src/utils/conversationRecovery.ts`, `src/utils/listSessionsImpl.ts` | `QuerySession.snapshot()`, `QuerySession.restore()`, `replay_from_snapshot()` | `test_normal_turn_builds_parent_uuid_chain_snapshot_and_replay` |
| Interrupted turn / continue handling | `src/utils/conversationRecovery.ts`, `src/services/api/withRetry.ts` | `StopReason`, `continue` event, `continue_on_error` runtime path | `test_runtime_records_error_and_continue_events_when_configured` |
| Resume/session commands | `src/commands/resume/*`, `src/commands/session/*`, `src/commands/clear/conversation.ts`, `src/commands/rename/generateSessionName.ts`, `src/bridge/sessionRunner.ts`, `src/bridge/inboundMessages.ts` | sidecar `session_contract`, WorkerResult metadata, API checkpoint metadata | `scripts/verify_code_worker_sidecar.py` |

## Internalization Ledger

| Source repo | Source module | Target path | Integration mode | Main path evidence |
| --- | --- | --- | --- | --- |
| `claude-code-best` | Query/session API stream, client, prompt dump, files API, and retry runtime | `vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/services/api/*` | `vendored_runtime` plus sidecar contract | `apps/code-worker/src/main.mjs --session-contract` |
| `claude-code-best` | Session storage, portable reader, restore, recovery, listing | `vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/utils/session*.ts` and recovery files | `vendored_runtime` plus source inventory | `metadata/query_session_source_inventory.json` |
| `claude-code-best` | Session bridge, inbound messages, resume/session/clear/rename commands | `vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/bridge/*`, `src/commands/resume`, `src/commands/session`, `src/commands/clear`, `src/commands/rename` | `vendored_runtime` plus sidecar contract | `CodeWorkerSidecarClient.session_contract()` |
| `claude-code-best` | Session history and query profiling | `vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/assistant/sessionHistory.ts`, `src/utils/queryProfiler.ts` | `vendored_runtime` plus sidecar contract | `streamRuntime.hasQueryProfiler` |
| Zyra runtime | QuerySession, TurnState, MessageLifecycle, StopReason | `packages/runtime/zyra_runtime/query_session.py` | `runtime_source` | imported by `CodeQueryLoop` |
| Zyra worker | CodeWorker query/session lifecycle | `packages/workers/zyra_workers/code_query_loop.py`, `code_worker_runtime.py`, `code_worker_bridge.py` | `runtime_source` | emits session events and artifacts |
| Zyra API | checkpoint metadata binding | `apps/api/zyra_api/main.py` | `api_main_path` | `state.metadata.last_code_worker_session` |

## Main Path Behavior

The CodeWorker path now creates a `QuerySession` for every request. Each turn records a user message, assistant stream delta, tool messages, stop reason, and turn end. The runtime writes:

- event log entries with phases `session_started`, `stream_request_start`, `turn_start`, `message_delta`, `error`, `continue`, `turn_end`, `query_session_snapshot`, and `session_completed`;
- a structured session snapshot artifact;
- an append-only JSONL transcript artifact;
- WorkerResult metadata containing session id, resume token, snapshot artifact id, transcript artifact id, consistency status, and parent-chain leaf UUID;
- API checkpoint metadata under `state.metadata.code_worker_sessions` and `state.metadata.last_code_worker_session`.

The M1-02B source extraction now internalizes 39 Claude Code query/session files with 16,912 effective upstream lines before adding Zyra runtime glue. The generated JSON inventory remains audit data, while the copied TypeScript sources are available through the sidecar `session_contract` boundary.

## Validation

Commands run:

```powershell
.\.venv\Scripts\python.exe scripts\verify_code_worker_sidecar.py
.\.venv\Scripts\python.exe -m unittest tests.unit.test_query_session_lifecycle
.\.venv\Scripts\python.exe -m unittest tests.integration.test_code_worker_query_session_lifecycle
.\.venv\Scripts\python.exe -m unittest tests.integration.test_code_worker_sidecar tests.integration.test_claude_code_productized_runtime
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
.\.venv\Scripts\python.exe scripts\verify_internalization_ledger.py --json
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Result: all commands passed. Full unittest ran 166 tests successfully. Windows emitted existing asyncio ResourceWarning messages during browser-use tests, but the suite result was OK.

## Exclusions

`vendor-runtimes/claude-code-runtime/metadata/query_session_source_inventory.json` and this document are audit/crosswalk records. They are not counted as effective implementation code. Effective code comes from productized TypeScript runtime source, Python runtime/session code, sidecar/API integration, scripts, and tests.
