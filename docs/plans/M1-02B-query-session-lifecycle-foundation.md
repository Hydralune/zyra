# M1-02B Slice 01 Query Session Lifecycle Foundation

Base commit: `719c0755d62cd4bd1ef60d105a6866f0dd8bf363`

## Status

Accepted for `slice-02b-01-query-session-lifecycle-foundation.md`.

This slice now internalizes the CodeWorker query/session foundation into Zyra-owned runtime modules. The default CodeWorker path creates a session seed, classifies inputs, assembles context, writes append-only session records, builds replay and turn lifecycle projections, audits foundation evidence, maps transcript events, evaluates the session acceptance gate, and emits a lifecycle state report. `GET /workers/code/session-foundation` now builds the same live reports and returns an API projection instead of a static contract-only payload.

## Target Coverage

| Slice target | Status | Evidence |
| --- | --- | --- |
| `QueryInputProcessor` for text/slash/bash/structured input | complete | `packages/runtime/zyra_runtime/claude_input_processor.py`; `tests.unit.test_query_session_foundation` |
| `ContextAssemblyRuntime` with source metadata and context snapshot | complete | `packages/runtime/zyra_runtime/claude_context_assembly_foundation.py`; CodeWorker metadata `context_assembly_ok=true` |
| `CodeWorkerSessionStore` append-only seed/input/context records | complete | `packages/runtime/zyra_runtime/claude_session_store.py`; store disable test blocks before QueryEngine |
| Replay/resume foundation from session store records | complete | `packages/runtime/zyra_runtime/claude_session_replay_runtime.py`; `tests.unit.test_query_session_replay_lifecycle_projection` |
| Turn lifecycle handoff before QueryEngine | complete | `packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py`; phase `turn_lifecycle_projection` on worker events |
| Transcript/event mapping and acceptance gate | complete | `claude_transcript_event_mapper.py`, `claude_session_acceptance_runtime.py`; worker metadata `transcript_mapping_ok=true`, `session_acceptance_ok=true` |
| Event-driven lifecycle state report | complete | `claude_session_lifecycle_state.py`; phase `session_lifecycle_state` on default path |
| API projection and inventory lineage | complete | `apps/api/zyra_api/main.py`; `tests.integration.test_code_worker_session_foundation_api` |
| Disable/semantic failure behavior | complete | input processor/context/store disable tests block before `stream_request_start`; continue-on-error remains recoverable |

## Main Path Evidence

| Module | Source signal | Zyra target | Runtime entry | Event/API evidence | Tests |
| --- | --- | --- | --- | --- | --- |
| input processor | `processUserInput`, slash/bash/text prompt paths | `packages/runtime/zyra_runtime/claude_input_processor.py` | `CodeWorkerRuntime.run` | `query_input_processed` | `tests.unit.test_query_session_foundation` |
| context assembly | `queryContext`, system/user context | `claude_context_assembly_foundation.py` | `ContextAssemblyRuntime.assemble` | `context_snapshot_ready` | `test_code_worker_query_session_foundation` |
| session store | `sessionStorage`, append transcript/session metadata | `claude_session_store.py` | `CodeWorkerSessionFoundationRuntime.build_seed` | `session_store_append` | disable store blocks QueryEngine |
| replay runtime | `sessionRestore`, replay records | `claude_session_replay_runtime.py` | `CodeWorkerSessionReplayRuntime.build_plan` | `session_replay_plan` | replay unit test with real store records |
| turn lifecycle | `QueryEngine.submitMessage` handoff boundary | `claude_turn_lifecycle_runtime.py` | `TurnLifecycleRuntime.project` | `turn_lifecycle_projection` | CodeWorker integration tests |
| audit/acceptance | source-to-target and foundation evidence | `claude_session_foundation_audit.py`, `claude_session_acceptance_runtime.py` | `SessionAcceptanceRuntime.evaluate` | `session_foundation_audit`, `session_acceptance` | API and worker tests |
| transcript mapping | transcript/event stream lifecycle | `claude_transcript_event_mapper.py` | `TranscriptEventMapper.map_snapshot` | `transcript_event_mapping` | lifecycle integration tests |
| lifecycle state | event-driven lifecycle state machine | `claude_session_lifecycle_state.py` | `SessionLifecycleRuntime.build_report` | `session_lifecycle_state` | full CodeWorker default path |
| API projection | session foundation API contract | `claude_session_api_projection.py`, `apps/api/zyra_api/main.py` | `GET /workers/code/session-foundation` | live projection payload | `test_code_worker_session_foundation_api` |

## Source-To-Target Decision

- `src/QueryEngine.ts`: `zyra_module_migrated` into `claude_query_engine_runtime.py`, `claude_turn_lifecycle_runtime.py`, `claude_session_lifecycle_state.py`, and `CodeWorkerRuntime`.
- `src/utils/processUserInput/*`: `zyra_module_migrated` into `claude_input_processor.py`; slash/bash/text/structured behavior covered by unit tests.
- `src/utils/queryContext.ts` and `src/context.ts`: `zyra_module_migrated` into `claude_context_assembly_foundation.py`.
- `src/utils/sessionStorage.ts`: `zyra_module_migrated` into `query_session.py`, `claude_session_store.py`, and transcript mapping.
- `src/utils/sessionRestore.ts`: `zyra_module_migrated` into `claude_session_replay_runtime.py`; full restoration remains a slice-02b-02 integration target.
- `src/query.ts` pre/post loop boundary: `zyra_module_migrated` into `claude_session_lifecycle_state.py`, `claude_session_acceptance_runtime.py`, and CodeWorker event ordering.
- `src/services/api/*` streaming/retry client: `reference-only` for this foundation slice; no external API client or sidecar performs core session lifecycle decisions.

## Verification

Passed:

- `.\.venv\Scripts\python.exe scripts\verify_code_worker_sidecar.py`
- `.\.venv\Scripts\python.exe -m unittest discover -s tests`
- `.\.venv\Scripts\python.exe -m compileall -q packages\runtime\zyra_runtime packages\workers\zyra_workers apps\api\zyra_api tests\unit tests\integration`
- `.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py`

Additional focused suites run during implementation:

- `tests.unit.test_query_session_foundation`
- `tests.unit.test_query_session_replay_lifecycle_projection`
- `tests.integration.test_code_worker_session_foundation_api`
- `tests.integration.test_code_worker_query_session_foundation`
- `tests.integration.test_code_worker_query_session_lifecycle`
- `tests.integration.test_code_worker_sidecar`
- `tests.integration.test_code_worker_clean_productized_runtime`
- `tests.integration.test_api_control_commands`

Full discover result: `Ran 241 tests in 559.388s OK`.

## Line Bucket Review

Diff bucket from `719c0755d62cd4bd1ef60d105a6866f0dd8bf363..HEAD`:

- production `apps packages skills scripts`: `9,558` additions / `8` deletions
- tests: `709` additions / `3` deletions
- vendor/vendor-runtimes: `0`
- generated/data/docs/vendor-like/source-pool/mock-only: `0` counted as effective production

Effective production additions counted for this slice: `9,558`, above the slice minimum of `9,000`.

## Anti-Fake Internalization Review

- No implementation code was added under `vendor/**` or `vendor-runtimes/**`.
- No runtime path depends on `../claude-code-best`, `../browser-use`, `../OpenHands`, npm link, Docker build context, or an external sidecar for core query/session decisions.
- The default CodeWorker path imports and calls the new modules directly from `zyra_runtime` and fails before QueryEngine when the session store, input processor, or context assembly is disabled.
- API reachability is covered by `GET /workers/code/session-foundation`, which builds live seed/audit/turn/acceptance/lifecycle/lineage reports.
- The lifecycle state machine treats recovered `continue_on_error` tool failures as warnings only when followed by `continue` and `session_completed`; unrecovered failures still block.

## Residual Debt

- This is the foundation half of M1-02B, not the parent unit completion. Full interrupt/cancel, richer resume restore, command-scoped allowed tools, skill/plugin cache-only loading, and deeper API streaming/retry semantics remain for `slice-02b-02-query-session-lifecycle-integration.md`.
- Parent M1-02B requires `18,000` effective production lines across both slices; this slice contributes `9,558`.
