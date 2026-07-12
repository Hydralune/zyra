# M1-S04A-01 Browser Session Productization Foundation Review

Date: 2026-07-12

Base commit: `476bc372134b06c12e63b5f25e902a8cc10e882d`

## Decision

`M1-S04A-01` is complete. The parent `M1-04A` remains in progress; `M1-S04A-02` must integrate the productized session owner with the complete live action path and close the parent acceptance matrix.

The slice adds a Zyra-owned browser session, profile, target/focus, CDP request, reconnect, process, event-bus, permission, artifact/event, and worker bridge foundation. It does not claim DOM/AX/selector ownership (04B), action/security ownership (04C), watchdog/history ownership (04D), workspace lease ownership (05A), canonical event-store ownership (05C), or physical worker lease ownership (07A).

## Target coverage matrix

| Slice requirement | Status | Evidence | Blocking |
| --- | --- | --- | --- |
| Source decisions for the seven Browser Use files | complete | source mapping below; production modules under `packages/integrations/zyra_integrations/browser_use` and `packages/workers/zyra_workers/browser_session` | no |
| Zyra-owned schema/state/ports | complete | `models.py`, `store.py`, `connection_policy.py`, `profile_store.py`, `cdp_runtime.py`, `target_runtime.py` | no |
| Session lifecycle and resilient event bus | complete | `session_runtime.py`, `runtime.py`, `event_bus.py`, `task_supervisor.py`, `recovery.py` | no |
| BrowserWorker/API reachability | complete | existing `POST /tasks/{task_id}/workers/browser` constructs `BrowserWorkerRuntime`; lifecycle commands and explicit `productized` backend use `BrowserRuntime` | no |
| 03A permission integration | complete for session foundation | `permission_bridge.py`; existing BrowserActionPermissionGate regression suite remains green | no |
| Artifact/event handoff | complete for foundation | `artifact_event_bridge.py`, stable session/target refs and lifecycle EventRecord projection | no |
| start idempotency, timeout, reconnect, focus and blank fallback | complete | targeted unit/integration tests and smoke | no |
| clean-copy operation without root source repositories | complete | `scripts/verify_browser_session_cleanroom.py` | no |
| 7,500 production line minimum | complete | 7,852 added lines in `apps/packages/skills`; tests/scripts/docs excluded | no |
| Parent 04A integration | not in this slice | owner `M1-S04A-02` | does not block this foundation slice |

## Source-to-target decisions

| Source | Decision | Zyra target | Behavior evidence |
| --- | --- | --- | --- |
| `browser_use/browser/session.py` | active semantic port | `browser_session/session_runtime.py`, `runtime.py`, `recovery.py`, `task_supervisor.py` | idempotent start/reuse/stop/reconnect and stale-generation handling |
| `browser_use/browser/session_manager.py` | active semantic port | `browser_session/target_runtime.py` | attach/detach maps, single-owner focus recovery, exactly-one blank fallback |
| `browser_use/browser/profile.py` | active semantic port | `browser_session/connection_policy.py`, `profile_store.py` | isolated dirs, corruption quarantine, argument conflict and secret handling |
| `browser_use/browser/events.py` | selected active adapter | `browser_use/event_bus.py`, `artifact_event_bridge.py`, core Browser EventType values | restartable lifecycle/focus/reconnect events with causal refs |
| `browser_use/browser/watchdog_base.py` | selected semantic port | `task_supervisor.py`, `recovery.py` | disconnect breaker, cancellation/drain and late result quarantine; full watchdog deferred to 04D |
| `browser_use/browser/_cdp_timeout.py` | active semantic port | `browser_session/cdp_runtime.py` | per-request silent timeout, pending cancellation, request receipts and generation fencing |
| `browser_use/browser/chrome.py` | adapter | `browser_use/chrome_process.py`, `discovery.py` | executable/endpoint discovery and process ownership diagnostics |
| Browser Use DOM/serializer | contract-only to 04B | target/CDP handoff only | no DOM/selector completion claim |
| Browser Use action/security | deferred to 04C | existing explicit legacy compatibility remains behind 03A | no action registry completion claim |
| Browser Use watchdog/history/artifacts | deferred to 04D | lifecycle signals and artifact ports only | no CrashWatchdog completion claim |
| OpenHands browser panel | contract-only to M2 | public session/target/artifact refs | backend truth remains Zyra |
| opencode session status projection | contract-only to 05C/M2 | lifecycle event envelope | no second session/event store |
| Hermes gateway/session signal | reference-only for later gateway | recovery classification only | no Hermes process/runtime dependency |

Excluded black boxes: Browser Use Agent as owner, Browser Use Cloud, Browser Use MCP/CLI, bubus history as truth, runtime extension download, root source paths, source-pool launchers, and vendor validation as a runtime precondition.

## State custody

| State | Owner |
| --- | --- |
| canonical run/task/conversation identity | existing TaskState/02B session owner; BrowserWorker bridge stores refs only |
| browser resource lifecycle | `BrowserRuntime` / `BrowserSessionRuntime` |
| browser persisted aggregate | `JsonBrowserStateStore` with atomic replace, revision CAS, backup recovery and identity index; 04A-02 may bind the same port to API SQLite without changing semantics |
| profile/cache/download/temp/runtime dirs | `BrowserProfileStore` |
| target/session maps and focus | `BrowserTargetRuntime` |
| CDP pending requests | `CdpRequestRuntime`, non-durable; receipts and failures are projected |
| permission decision/grants | existing 03A permission state owner; bridge never writes a second decision store |
| artifact bytes | existing `LocalArtifactStore` |
| canonical task events | existing API TaskState/event path; browser bus is transient and restartable |
| physical worker/process lease | deferred to 07A; Chrome process ref is diagnostic, not a worker lease |

## Main-path evidence

`POST /tasks/{task_id}/workers/browser` -> `BrowserWorkerRuntime` -> lifecycle command or explicit `productized` backend -> `BrowserRuntime` -> connection policy -> profile store -> permission bridge -> Chrome/CDP discovery -> target/focus -> lifecycle/artifact event bridge -> existing API event/artifact/checkpoint persistence.

The old static and explicit `browser-use-live` action paths remain compatibility paths so completed 03A behavior is not regressed. They are not counted as the new foundation implementation. `M1-S04A-02` owns full live action migration onto the new session lease.

Disconnecting `BrowserRuntime`, profile, CDP, target, permission, or artifact bridge produces typed failure/no side effect in the new integration suite. Cleanroom removes vendor, root source repositories, caches, SQLite residue, artifacts, and build output and still runs unit, integration, smoke, and submission-boundary checks.

## Verification

| Command | Result |
| --- | --- |
| `python -m unittest tests.unit.test_browser_session_runtime_foundation tests.integration.test_browser_session_productization_foundation` | 7 passed |
| `python -m unittest tests.integration.test_browser_worker tests.integration.test_browser_worker_permission_gate` | 27 passed, including local live Chrome paths; Windows Proactor destructor warning after success |
| `python scripts/smoke_browser_session_foundation.py` | passed; start/reuse/diagnose/stop and no source repository dependency |
| `python scripts/verify_browser_session_cleanroom.py` | passed; 5 unit + 2 integration + smoke + submission boundary |
| `python scripts/smoke_browser_use_runtime.py` | passed; now reports productized runtime health without importing Browser Use |
| `python -m compileall ...` | passed |
| `python -m unittest discover -s tests` | 644 discovered: 641 passed, 1 skipped, 2 failed in 1027.193s |
| isolated rerun of the two full-suite failures | descriptor owner test passed after intentional expectation update; Browser live readiness/action test passed |

The full-suite live failure is not reported as a green aggregate run. It is the known Windows/browser readiness timing risk already present in earlier state records. The exact live test passed in the adjacent suite and again in isolated rerun.

## Defects found and fixed during review

- CDP deterministic response was queued behind a short timeout; transport/runtime now supports direct synchronous ingestion.
- CDP allocator tuple was encoded as a request id; runtime now explicitly unpacks allocator generation and id.
- focus recovery recursively reacquired its owner path during reconcile; internal detach now suppresses nested recovery.
- BrowserWorker wrapper initially started productized CDP before legacy 03A permission; backend routing now preserves completed permission semantics.
- optional provenance snapshot was passed as `None` to legacy trace projection; source metadata is optional and never validated as a runtime prerequisite.
- smoke scripts omitted transitive package roots; both now bootstrap all formal Zyra packages.

## Effective line buckets

| Bucket | Added lines | Counted toward 7,500 |
| --- | ---: | --- |
| production in `apps/packages/skills` | 7,852 | yes |
| tests | 454 | no |
| product verification scripts | 180 | no, conservatively excluded |
| docs/review | not counted | no |
| generated/data/runtime assets | 0 | no |
| vendor/vendor-runtimes | 0 | no |
| mock/fixture-only | 0 production | no |
| adapter-only | transport adapters are counted only where they implement active CDP/process behavior; provenance/compat lines are not relied on for the minimum | conservative |

No source pool, manifest, inventory, ledger data, generated code, vendor dump, test body, or documentation is counted. The production minimum remains satisfied without scripts.

## Competition evidence

Advances: `REQ-CLOSE-01` foundation (BrowserWorker can own a persistent executable session), `REQ-FAULT-01` foundation (disconnect/timeout/reconnect/focus recovery signals), and `REQ-TRACE-01` foundation (causal lifecycle/session/target/artifact refs). No requirement status is promoted to verified and no competition gate is closed. `SCORE-ROBUST`, `SCORE-EFF`, and `SCORE-COMPAT` receive backend infrastructure only; live multi-provider, 2,000-transition, zero-human, cross-domain and UI evidence remain downstream owners.

## Residual risks and next owner

- `M1-S04A-02`: bind all live action execution to the productized session lease, API state adapter, concurrent multi-tab/screenshot live behavior, and parent 15,000-line closure.
- `M1-04B`: DOM/AX/selector and message/state compression.
- `M1-04C`: action registry, navigation/form/upload/download/evaluate policy and exact permission effects.
- `M1-04D`: active watchdogs, browser process/CDP disconnect observation, history and complete artifacts.
- `M1-05A/05C/07A`: workspace lease, canonical event projector and physical worker lease takeover.
- Windows live browser readiness remains timing-sensitive in aggregate discovery; it passed isolated and adjacent runs but remains a non-blocking foundation risk.

