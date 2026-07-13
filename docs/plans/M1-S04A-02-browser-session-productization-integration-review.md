# M1-S04A-02 Browser Session Productization Integration Review

Date: 2026-07-13

Status: `completed`.

Implementation evidence commit: `86937955ce65963e789a6f576d85725261d1e609`.

This review closes `M1-S04A-02` and the `M1-04A` parent engineering unit. It does not close any competition requirement or scoring gate. The authoritative `execution-state.yaml` transition is owned and applied separately by the root agent.

## Final decision

The Browser Use session lifecycle, process custody, runtime registry, session-bound action execution, permission, artifact, API, cleanroom and live owner-loss paths are productized through Zyra-owned modules. The default BrowserWorker/API path uses the productized runtime; legacy static and `browser-use-live` compatibility paths are not counted as 04A completion evidence.

All final engineering gates passed:

- Full repository: `660 tests in 949.619s`, `OK`, `skipped=1`.
- Final required scripts: all exited `0`.
- Cleanroom: `violations=[]`, `source_repositories_present=false`.
- Internalization ledger: `entries=1097`, `errors=0`, `blockers=0`, `warnings=552`.
- Submission-boundary verification: passed.
- Effective Zyra-owned production: `9,424` net lines.

The single skipped test is recorded by the full-suite result and does not replace the mandatory non-skipped live productized lane, which passed separately.

## Objective coverage matrix

| Objective | Final status | Evidence | Blocking |
| --- | --- | --- | --- |
| Productized BrowserWorker default | completed | BrowserWorker routes unspecified backend requests to `zyra-browser-productized`; deterministic tests explicitly request `static` | no |
| Shared runtime registry and session identity | completed | registry/application/control/resume integration and API session list/detail tests | no |
| Session-bound action execution | completed | actions execute only on the started lease/CDP/target generation; lost state fails explicitly | no |
| Permission semantic effect | completed | block-before-CDP, exact one-use approval, replay rejection and sealed policy evidence | no |
| Lifecycle and control | completed | start, ensure, reconnect, stop/cancel, diagnose, list and resume mutate the same session | no |
| Artifact causality | completed | screenshot, download and trace receipts bind to run/task/session/action | no |
| Process custody and owner loss | completed | signed marker adoption, PID custody, termination and marker cleanup | no |
| API and health projection | completed | POST browser worker plus health, registry and session-detail routes | no |
| Failure and disable matrix | completed | invalid endpoint, disabled application/CDP, missing live state, stale registry and no-fallback tests | no |
| Source-free cleanroom | completed | controlled import roots; no source repositories or violations | no |
| Effective production minimum | completed | `9,424` net production lines, excluding tests/scripts/docs/data/vendor | no |
| Final regression and submission | completed | full 660-test suite and submission verification passed | no |

## Main-path evidence

```text
POST /tasks/{task_id}/workers/browser
  -> BrowserWorkerRuntime
  -> BrowserRuntimeRegistry
  -> BrowserSessionApplication
  -> BrowserRuntime / BrowserSessionRuntime
  -> SessionBoundActionRuntime
  -> BrowserActionPermissionGate
  -> CDP / target / profile / Chrome process custody
  -> canonical events, artifacts and checkpoints
```

| Surface | Production entry | Behavioral evidence |
| --- | --- | --- |
| Worker runtime | `packages/workers/zyra_workers/browser_worker.py` | default productized backend, lifecycle/action execution, strict failure projection |
| Session application | `packages/workers/zyra_workers/browser_session/application.py` | same-session lease, action, lifecycle transaction and recovery |
| Runtime registry | `packages/workers/zyra_workers/browser_session/runtime_registry.py` | reuse, diagnostics, projection, stale-root eviction and shutdown |
| CDP and target | `packages/workers/zyra_workers/browser_session/cdp_runtime.py`, `target_runtime.py` | request timeout, target focus, generation and reconnect semantics |
| Process custody | `packages/integrations/zyra_integrations/browser_use/chrome_process.py` | signed marker, adopt/relaunch, owner loss and physical termination |
| Permission | `packages/runtime/zyra_runtime/permission/**` | sealed/interactive decisions and exact action grant consumption |
| API | `apps/api/zyra_api/main.py` | worker invocation, health, sessions and detail projections |
| Artifacts/events | browser artifact/event bridge and canonical adapters | screenshot/download/trace and causal event evidence |
| Cleanroom/live | `scripts/verify_browser_session_cleanroom.py`, `scripts/smoke_browser_session_productization_live.py` | source-free copy and real local Chrome lane |

Disabling or disconnecting the registry, application, CDP runtime, permission gate, artifact bridge or process custody changes real behavior and is covered by failure-path tests. No completion claim depends on import-only smoke, a fixed health response, a source ledger alone, a root source checkout, vendor runtime, sidecar or static fallback.

## Live owner-loss evidence

The mandatory live lane used the productized BrowserWorker and a real local Chrome process under sealed permission policy with an explicit unsafe-sandbox policy opt-in.

| Evidence | Result |
| --- | --- |
| Session continuity | sequential operations retained the same browser session identity |
| Process custody | adopted the same owned Chrome PID rather than creating a hidden second process |
| Owner-loss recovery | a fresh registry/runtime resumed the persisted session custody |
| Physical cleanup | owned PID terminated after cancel/stop |
| Marker cleanup | signed custody marker removed |
| Artifacts | two causal artifacts produced: screenshot and trace |
| Permission | sealed policy; zero implicit approval path |
| Unsafe policy | bypass available only through explicit opt-in, never defaulted silently |

The compatibility live selector test also uses stable DOM `id` contracts for input/click. It does not fall back to an arbitrary available index, retry, skip or static execution.

## Source-to-target disposition

| Browser Use source | Strategy | Zyra-owned target responsibility |
| --- | --- | --- |
| `browser/_cdp_timeout.py` | direct port | CDP request runtime, session-bound actions, application |
| `browser/chrome.py` | adapter | Chrome process custody, discovery, registry and worker |
| `browser/events.py` | adapter | event bus, artifact/event bridge and application |
| `browser/profile.py` | direct port | connection policy, profile store and application validation |
| `browser/session.py` | direct port | session runtime, lifecycle application, lease and worker |
| `browser/session_manager.py` | direct port | target runtime, lease and action runtime |
| `browser/watchdog_base.py` | direct port | task supervisor, recovery and application control |

All seven 04A entries are `productized` and `tested_main_path`, carry the implementation evidence commit, resolve real production/test/cleanroom/live paths, and have zero verification blockers. Their NOTICE status is recorded. DOM/AX depth remains owned by 04B, action/security expansion by 04C, and full watchdog/history work by 04D.

## State ownership

| State | Zyra owner |
| --- | --- |
| canonical task/run identity | `TaskState` and API persistence |
| browser session identity and lifecycle | `BrowserSessionRuntime` / `JsonBrowserStateStore` |
| runtime ownership and reuse | `BrowserRuntimeRegistry` |
| session/action lease | `BrowserSessionLeaseStore` |
| CDP connection and generation | `CdpRequestRuntime` |
| targets and focus | `BrowserTargetRuntime` |
| Chrome PID and custody marker | `ChromeProcessController` |
| permission decisions and grants | `PermissionStateStore` and browser action gate |
| action/control receipts | session application receipt and lifecycle transaction stores |
| artifact bytes and refs | `LocalArtifactStore` and browser artifact bridge |
| canonical events/checkpoints | API event log and task persistence |

Browser state writes recreate deleted owned parent directories; registry snapshots evict entries whose owned roots disappear. Persisted state without a live target/CDP fails explicitly rather than synthesizing a replacement session.

## Effective line buckets

| Bucket | Added | Deleted | Net | Completion accounting |
| --- | ---: | ---: | ---: | --- |
| Production `apps/**` + `packages/**` | 9,561 | 137 | 9,424 | counted |
| `scripts/**` | 533 | 8 | 525 | excluded |
| `tests/**` | 1,141 | 27 | 1,114 | excluded |
| Ledger data and review/docs | not counted | not counted | excluded | excluded |
| `vendor/**` + `vendor-runtimes/**` | 0 | 0 | 0 | exclusion gate satisfied |

The 04A production minimum is satisfied by `9,424` net Zyra-owned production lines. Test volume, scripts, ledger/data, documentation and vendor-like material do not contribute to that total.

## Critical self-review and blocker closure

| Earlier blocker or defect | Closure evidence |
| --- | --- |
| Application could diverge from the registry-owned runtime | registry attaches and returns one shared application/control/resume composition; identity/reuse tests pass |
| CDP/application could act on a second or lost connection | session-bound action runtime requires the started lease and CDP generation; missing state fails closed |
| Lease/resume state could report success without live ownership | resume capsules, lease checks, target/CDP diagnostics and explicit state-loss failures are tested |
| Permission/security could allow pre-decision side effects | sealed block-before-CDP and exact one-use grant/replay tests prove zero pre-decision execution |
| Profile/process state lacked physical custody proof | signed Chrome custody marker, executable/endpoint mutual exclusion, owner-loss adoption and PID termination are verified |
| Registry retained dead temporary roots | snapshot/get/diagnostics evict stale entries, increment stale counters and remove the old runtime |
| JSON state store failed after owned parent deletion | persistence recreates owned roots and journal parents before the first write |
| API health could project stale integration readiness | health combines runtime diagnostics, integration audit and blocking findings |
| Live local launch could hide sandbox behavior | unsafe sandbox bypass is explicit-only and recorded in the live policy |
| Live selector indexes changed after DOM rebuild | input/click resolve strict `id/#id/index` contracts; missing targets fail without arbitrary-index fallback |
| Legacy deterministic scenarios accidentally used the productized default | tests/scenarios explicitly declare `browser_backend=static`; task graph only propagates an explicit hint |
| Productized ledger status conflicted with pending NOTICE | seven 04A entries record NOTICE handling; fresh-seed and final ledger audits report zero errors/blockers |
| Full repository previously had browser regressions | deterministic backend, API, stale registry/store and live selector failures were fixed; final 660-test run passed |

## Residual non-blocking risks

- Browser Use may emit intermittent page-readiness warnings for local pages even when the action, DOM contract and cleanup complete successfully. Explicit navigation/action timeouts and diagnostic metadata bound and expose the condition.
- An inactive editable `.pth` entry remains environment debt. Cleanroom import-origin auditing proves it is not active or required by the productized path; removal belongs to environment hygiene, not this runtime.
- Unsafe Chromium sandbox bypass remains a real security tradeoff. It is available only through an explicit policy opt-in and is not the default production behavior.
- The ledger retains `552` warnings across the broader repository. This unit has zero ledger errors/blockers; unrelated warnings remain owned by their source/unit plans.

These risks do not invalidate the verified 04A behavior, but they must remain visible to later browser, packaging and freeze reviews.

## Requirement calibration

This unit advances infrastructure and dynamic evidence for `REQ-CLOSE-01`, `REQ-FAULT-01`, `REQ-TRACE-01` and `SCORE-ROBUST` through real lifecycle, recovery, permission, artifact and causal trace behavior.

It does not close those requirements. It also does not close cross-domain live-task quality, 2,000 canonical transitions, zero-human autonomous benchmark, dynamic topology/entropy comparison, real heterogeneous dispatch, multi-provider compatibility, UI evidence, deployment rehearsal or submission gates. Those gates remain with their requirement-matrix owners.

## Handoff

`M1-04A` is complete. The next engineering entry is `M1-04B-01` for DOM/AX/selector runtime foundation and integration.

The root agent must update `docs/milestones/execution-state.yaml` as the sole progress source. This review does not independently mutate or supersede execution state.
