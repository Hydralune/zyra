# M1-S04D-01 Watchdogs, History, and Artifacts Foundation Review

## 1. Review identity

- Slice: `M1-S04D-01`
- Implementation commit: `e7b94a84a367316d013c88a7fce6313893a3921d`
- Review scope: only the diff from `46d84fac4804ca6dcf2c880a360f0c8449d8abc3`
- Parent unit: `M1-04D`
- Requirement movement: advances `REQ-FAULT-01` and `REQ-TRACE-01`; closes neither

## 2. Result

The slice establishes a Zyra-owned browser observability foundation on the
default productized `BrowserWorkerRuntime.run` path:

- append-only, per-scope durable browser history with sequence and SHA-256 chain;
- action call/result pairing tied to 04C receipts and canonical event ids;
- active local/security/download/storage/permission/screenshot/popup/about:blank watchdogs;
- a Zyra crash detector for process exit, CDP disconnect, heartbeat timeout, and request timeout;
- canonical artifact publication with 04D lineage receipts;
- deterministic replay and trace projections;
- non-canonical health aggregation reconstructible from durable history;
- versioned `worker_health_signal`, `browser_tool_failed`, and `browser_recovery_input` handoff;
- API views for summary, history, trace, health, downloads, screenshots, artifacts, and replay.

The slice deliberately does not emit `EventType.RECOVERY_PLANNED`. Recovery
planning remains owned by `M1-07C`.

## 3. Source-to-target decisions

| Source | Role | Retained mechanism | Zyra target |
| --- | --- | --- | --- |
| browser-use | primary implementation | attached watchdog lifecycle and action history | `zyra_workers.browser_observability.watchdogs`, `history_store`, `history_runtime` |
| OpenHands | supplementary implementation | event/artifact projection | `artifact_publisher`, `api_projection` |
| oh-my-pi | supplementary implementation | tool call/result pairing and digest receipts | `trace_runtime`, `replay`, `models` |
| browser-use CrashWatchdog | rejected | none | replaced by `BrowserCrashDetector` because upstream does not attach it |
| claude-code-best | conformance only | tool-result pairing comparison | no production owner |
| opencode | conformance only | durable session comparison | no production owner |
| Hermes-Agent | reference only | trajectory evaluation comparison | advisory judge only |

No production import, relative root path, subprocess sidecar, MCP server,
plugin, Docker image, local port service, or dynamic import points at a source
repository.

## 4. State custody

| State | Owner after this slice | Persistence |
| --- | --- | --- |
| canonical task/session identity | existing SQLite/TaskState owner | SQLite checkpoint |
| browser process/session/CDP resource | M1-04A BrowserRuntime | existing 04A state/receipts |
| DOM and next-context projection | M1-04B and M1-02D | existing context checkpoint |
| action/permission receipts | M1-04C and M1-03A | existing action/permission stores |
| canonical events | existing EventLog | existing event persistence |
| canonical artifacts | existing LocalArtifactStore | existing artifact root |
| browser observability history | M1-S04D-01 BrowserHistoryStore | segmented JSONL plus atomic head/index |
| process-live health projection | M1-S04D-01 BrowserHealthRuntime | non-canonical; rebuildable from history |
| recovery plan | M1-07C | not created by this slice |

The history scope always contains `run_id`, `task_id`,
`browser_session_id`, `canonical_session_id`, and `worker_request_id`.
Cross-scope appends, sequence conflicts, stale heads, invalid partial records,
and digest mismatches fail closed.

## 5. Dynamic reachability and semantic effect

The package is not reached only by a smoke import:

1. `POST /tasks/{task_id}/workers/browser` invokes
   `BrowserWorkerRuntime.run`.
2. The productized action/session path invokes
   `BrowserObservabilityApplication.observe`.
3. The application records session/action/state facts, evaluates attached
   watchdogs and the crash detector, publishes trace/history artifacts, and
   returns its projection in `BrowserWorkerRun`.
4. The API persists that projection in task metadata and includes it in normal
   and idempotent responses.
5. `GET /tasks/{task_id}/browser-observability` queries the shared runtime and
   durable history owner.

Disabling `BrowserObservabilityApplication` raises
`BrowserObservabilityDisabled`. `BrowserWorkerRuntime` converts any
observability failure into an unhealthy signal plus a versioned recovery input
and marks the browser request failed. There is no legacy-success fallback.

The semantic effects demonstrated by tests are:

- killing a real local child process emits a terminal process-exit signal;
- killing a real Chrome/Edge headless process emits a terminal process-exit signal;
- CDP disconnect escalates from degraded suspicion to terminal confirmation;
- missing heartbeat and overlong request emit distinct terminal timeout signals;
- security, storage, permission, screenshot, popup, and about:blank observations
  change health output;
- failed actions produce tool-failure and 07C recovery-input events;
- replay reconstructs paired tool facts and rejects corrupted history;
- removing or disabling the observability application changes the real worker result.

## 6. Event and recovery owner audit

Emitted canonical event types:

- `worker_health` for typed watchdog/crash evidence;
- `agent_message` containing `zyra.browser-observability.tool-failed.v1`;
- `agent_message` containing `zyra.browser-observability.recovery-input.v1`;
- `artifact_written` for trace/history and adopted browser artifacts.

Forbidden event:

- `recovery_planned` is not emitted by the 04D package or its BrowserWorker
  integration.

Every recovery input includes `planner_owner=M1-07C` and
`is_recovery_plan=false`. The advisory judge cannot override permission,
worker result, or recovery planning.

## 7. Verification

Commands and results:

- `.venv/Scripts/python.exe -m pytest tests/unit/test_browser_observability_foundation.py -q`
  - `20 passed`
- `.venv/Scripts/python.exe -m pytest tests/integration/test_browser_observability_main_path.py -q`
  - `3 passed`
- `.venv/Scripts/python.exe -m pytest tests/unit tests/integration -k "(browser_worker or browser_session or browser_action or browser_message) and not observability" -q`
  - `81 passed, 630 deselected, 17 subtests passed`
- `.venv/Scripts/python.exe -m pytest tests/unit -k "internalization_ledger" -q`
  - `68 passed, 412 deselected`
- `git diff --check`
  - passed
- root-source dependency scan
  - no forbidden runtime import or root source path
- vendor diff
  - empty

The three ledger collection warnings concern the pre-existing dataclass named
`TestEntry`; they do not represent test failures.

## 8. Effective line-count buckets

| Bucket | Added lines | Counted toward slice minimum |
| --- | ---: | --- |
| browser_observability production package | 9,030 | yes |
| BrowserWorker/API main-path additions | 215 | yes |
| effective production total | 9,245 | yes |
| slice tests | more than 1,000 | no |
| ledger seed | 336 | no, data/traceability |
| ledger sync script | excluded | no, auxiliary script |
| vendor/vendor-runtimes | 0 | no |

The `9,245` effective production total exceeds the slice minimum of
`9,000`. No JSON, ledger, test, generated file, fixture, mock, or auxiliary
script is included in that total.

## 9. Critical review findings and corrections

Issues found during incremental verification:

1. The initial crash-signal dedupe key suppressed the confirmed CDP terminal
   transition after a degraded disconnect signal. The key now includes status
   and terminal state.
2. The first source-role test matched a tuple rather than path suffixes. The
   assertion now checks the explicit rejected CrashWatchdog path.
3. The first LocalArtifactStore adapter assumed unsupported `suffix` and
   `metadata` arguments. It now uses the exact keyword-only
   `run_id/task_id/content/kind/title/producer_node_id` contract.
4. The initial ledger sync omitted mandatory main-path and line-count policy
   fields. Productized entries now declare worker/API/event/artifact surfaces;
   the rejected CrashWatchdog remains inventory-only.
5. The first ledger sync sorted and reformatted the whole seed. The seed was
   restored from the base commit, and the script now preserves existing entry
   order, leaving a 336-line additive data diff.
6. The first implementation total was below the 9,000-line failure threshold.
   The missing behavior was addressed with a reconstructible health transition
   runtime, not padding.

## 10. Residual risks and deferred work

- The direct application/API projection is behavior-tested; a full live HTTP
  server test of every new GET view remains appropriate for M1-04D-02 or the
  M1-04 aggregation review.
- Optional recording and HAR watchdog behavior is not claimed by this
  foundation slice.
- Captcha/cloud watchdogs remain deferred as required by the source-role plan.
- Full cleanroom and all-repository tests are intentionally deferred to the
  M1-04 digital-stage aggregation review; this slice introduced no dependency
  that makes cleanroom execution contingent on a source repository.
- `REQ-FAULT-01` and `REQ-TRACE-01` require later scheduler/recovery and
  live scenario evidence and therefore remain open.

## 11. Completion decision

`M1-S04D-01` satisfies its foundation behavior, source-role, owner,
line-count, dependency, artifact, history, watchdog, real-process failure, and
incremental regression gates. The parent `M1-04D` remains incomplete; the
next authorized entry is `M1-S04D-02`.
