# M1-02B Slice 01 Query Session Lifecycle Foundation

Base commit: `719c0755d62cd4bd1ef60d105a6866f0dd8bf363`

## Status

Implemented but not accepted as complete under the slice line-count gate.

The runtime behavior requested by `slice-02b-01-query-session-lifecycle-foundation.md` is now present on the CodeWorker default path:

- `QueryInputProcessor` classifies text, slash command, bash and structured tool-plan input before QueryEngine dispatch.
- `ContextAssemblyRuntime` builds a pre-query context snapshot with source metadata, tool inventory, permission/workspace/session state and fingerprint.
- `CodeWorkerSessionStore` persists append-only pre-query seed records under the artifact root.
- `CodeWorkerSessionFoundationRuntime` creates a shared session seed used by context snapshot, store and QuerySession.
- `SessionFoundationAuditor` projects input/context/store/event evidence and emits `session_foundation_audit` metadata/events.
- `CodeWorkerRuntime` blocks before QueryEngine when input processor, context assembly or session store is disabled.
- `ZyraClaudeQueryEngine` uses the session seed id and emits seed/context attachment events.
- `GET /workers/code/session-foundation` exposes a read-only API view of the foundation contract and snapshot shape.

## Verification

Passed:

- `.\.venv\Scripts\python.exe -m unittest tests.unit.test_query_session_foundation tests.unit.test_query_session_lifecycle tests.integration.test_code_worker_query_session_foundation tests.integration.test_code_worker_query_session_lifecycle tests.integration.test_code_worker_clean_productized_runtime tests.integration.test_claude_productization_integration tests.integration.test_claude_code_productized_runtime tests.unit.test_runtime_protocol`
- `.\.venv\Scripts\python.exe -m unittest tests.integration.test_api_control_commands`
- `python scripts\verify_submission_boundary.py`
- `.\.venv\Scripts\python.exe -m compileall -q packages\runtime\zyra_runtime packages\workers\zyra_workers apps\api\zyra_api`

`pytest` was not available in the local virtual environment, so validation used `unittest`.

## Line Bucket Review

Committed production bucket from `BASE_COMMIT..HEAD`:

- production diff: `3,939` additions / `6` deletions across `apps/**` and `packages/**`
- tests diff: `355` additions / `0` deletions
- vendor/vendor-runtimes diff: `0`

Effective production additions: `3,939` lines. This is below the slice requirement of `9,000` effective production lines.

## Completion Decision

Do not mark this slice complete yet.

The implemented behavior is real and tested, and no vendor/source-pool code was added. However, the strict 9,000-line production minimum is not met. Accepting this as complete would violate the execution-unit review rules. The next work item should either broaden this slice with additional real query/session lifecycle internals, or explicitly revise the slice line target before acceptance.
