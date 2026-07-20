# M1-S05D-02 Backend Failover Dispatch Integration Review

## 1. Review identity

- Slice: `M1-S05D-02`
- Measurement baseline and prior evidence commit: `f0700258840da4514b0090e7851e8abff4879409`
- Final implementation commit: `dc010d102ca4b74d7f13e42952706014e2fc40e2`
- Review type: incremental critical self-review with high-risk exact-commit cleanroom verification
- Result: **PASS for M1-S05D-02**
- Parent result: **M1-05D complete**
- Next slice: `M1-S06A-01`

The verdict is based on real provider HTTP/SSE requests, a real loopback edge backend service,
durable dispatch state, task-graph/API reachability, cancellation and workspace-corruption effects,
and an exact-commit cleanroom. Ledger rows, transport class presence, static health output, or line
volume are not used as substitutes for those behaviors.

## 2. Outcome

This slice turns the 05D-01 provider/backend foundation into the default worker dispatch layer.
The task graph acquires a provider route before selecting a worker backend and binds the same
`session_id`, `turn_id`, provider route checksum, M0 execution reference, and dispatch envelope to
CodeWorker or BrowserWorker. Backend failover can change the execution backend while retaining the
provider route; provider failure can change the provider route while retaining backend custody.
Only an explicit escalation policy may change both.

The integration adds four real execution transports: in-process/local worker invocation, bounded
local subprocess execution, Docker CLI execution, and typed edge/cloud HTTP execution. The current
environment has no Docker daemon, so Docker live execution is not claimed. A loopback edge service
was exercised with actual HTTP requests, cancellation, drain/resume, bounded bodies, and typed
envelope/status contracts. External cloud live dispatch remains a later M1-08 competition gate.

## 3. Goal coverage matrix

| Requirement | Implementation and semantic evidence | Result |
| --- | --- | --- |
| Provider route before worker execution | `ProviderRouteLeasePort` binds a strict immutable route; task graph passes it into the canonical backend router and both workers | PASS |
| Same-turn worker custody | Envelope validates run/task/node/session/turn, provider checksum, M0 execution ref, workspace and artifact roots | PASS |
| Backend-only failover | Real unreachable primary edge endpoint changes to a live loopback alternate; provider route and M0 ref remain equal | PASS |
| Provider/model fallback | 503, 429, and zero-output SSE stall each perform a second real request on a new provider route | PASS |
| Partial-output fence | Text/tool output makes retry reconciliation-only; fallback receives zero requests | PASS |
| Turn timeout and cancellation | Session state machine, process/HTTP abort, remote cancel-by-envelope and durable recovery input interrupt pending dispatch | PASS |
| Workspace corruption | Root identity replacement quarantines the backend/workspace pair and prevents reuse | PASS |
| Control commands | API/runtime cancel, requeue, quarantine, release, drain and resume mutate durable session/backend state | PASS |
| Restart/replay custody | Hash-chained journal, side-effect fences, materialization, outbox, session recovery inputs and replay verification are persisted in SQLite | PASS |
| 07A/07C handoff | Envelope exposes provider route ref, backend lease/attempt refs, M0 execution ref, nullable future physical lease ref, and typed recovery input | PASS |
| Source custody | Exact bundled ledger has 3/3 current entries connected; no parent source repository is loaded | PASS |
| Effective volume | 9,752 conservative production lines; parent cumulative 23,033 | PASS |

## 4. Runtime architecture and state custody

| State domain | Canonical owner | Non-owner boundary |
| --- | --- | --- |
| Provider/model/catalog and credential lifecycle | TypeScript `ProviderControlPlaneStore` and `CredentialManager` | Python lease port and workers carry safe route references only; no plaintext secret is returned |
| Provider route, attempt, stream and fallback | TypeScript route store, transport runtime, stream supervisor and model fallback policy | Backend registry cannot parse or replace provider state |
| Backend definitions, health, circuits and backend leases | Python `BackendRegistryStore`, `BackendRegistry` and `BackendHealthSupervisor` | Provider control plane has no backend concept |
| Dispatch session, attempt, cancellation and recovery input | Python `WorkerDispatchRouter`, `BackendDispatchControl` and SQLite store | API/task graph expose typed projections and cannot bypass the router |
| Idempotency, journal, materialization and outbox | Python `BackendDispatchJournal` and store tables | Transport may execute only after envelope and side-effect fences are pinned |
| Workspace identity and quarantine | Python `WorkspaceAttestationRuntime` plus backend store | Worker receives the attested root; it cannot approve a replaced root |
| Worker result/artifact and canonical event stream | Existing worker/artifact/event owners | Backend router links route/attempt refs and emits causal event records without taking artifact custody |
| Future physical worker lease | Reserved nullable envelope field for 07A | 05D does not synthesize a physical lease |

`BackendDispatchRuntime` now delegates to `WorkerDispatchRouter`; the old callable foundation is a
private compatibility implementation, not an alternate default path. API dispatch/session/control,
task cancellation, replay and hash verification all call the same store/router/control boundary.

## 5. Provider and worker integration

The Provider Control Plane now persists immutable catalog and credential snapshots per route.
Credential rotation preserves an already acquired route's material while the next route receives a
new credential version. Revoked current status remains a zero-byte fence. The stream supervisor
enforces ordered frames, first/chunk/total deadlines, monotonic usage, fragmented tool-argument JSON,
terminal framing, and partial-output replay prohibition.

CodeWorker invokes the Zyra-owned TypeScript provider package directly when `providerControlPlane`
is required; QueryEngine does not silently install its legacy default model stream in that mode.
BrowserWorker no longer constructs a provider SDK from API-key environment variables and fails
closed when the strict route reference required for an agent model call is absent. Static scanning
found zero direct worker reads of common provider API-key variables.

## 6. Backend transports, failover and control

`WorkerDispatchRouter` performs strict preflight, creates the dispatch session, pins the canonical
envelope, acquires a backend lease, records the attempt and causal event, invokes the selected
transport, materializes success once, or records a typed recovery input. Retry requires an
unchanged provider route and M0 execution ref unless the failure is explicitly classified as a
provider-route change. Observable output, side-effect candidates, cancellation and reconciliation
states prevent unsafe replay.

The HTTP transport rejects cross-origin redirects, limits response bytes, redacts endpoint detail,
never sends provider credentials, and uses the remote control protocol for cancellation. The local
subprocess and Docker transports bound command, environment, timeout, output and process cleanup.
The loopback service implements health, dispatch, list/status, cancel-by-envelope, drain and resume
using the same wire contract used by edge/cloud clients.

## 7. API, task-graph and event reachability

The following production paths exercise current state rather than fixed contracts:

- task-graph CodeWorker and BrowserWorker execution acquire a provider route and call
  `dispatch_worker_callable`, which delegates to the router;
- `/backends/dispatch-sessions`, recovery inputs, control requests, probe, replay and hash-verify
  endpoints query or mutate the canonical backend store;
- task cancel invokes backend dispatch cancellation rather than only appending a task event;
- backend started/failed/failover/completed and recovery records are converted into task event
  records and enter the existing event-log path;
- the provider API remains the sole provider catalog/credential/route owner.

Disconnecting the router makes the real task graph lose backend lease/attempt/session behavior and
fail strict dispatch. Disconnecting Provider Control Plane route acquisition makes both worker paths
fail closed instead of selecting an environment-key fallback. Disconnecting workspace attestation
allows no reuse path: the dispatch is quarantined before the worker is invoked.

## 8. Source-to-target decision

| Role | Source mechanism | Zyra target and retained responsibility |
| --- | --- | --- |
| Primary: opencode | catalog/provider/model/credential and session processor mechanisms at `adf178a6b95c61506ddaadaf4dd062badb4a8fda` | TypeScript Provider Control Plane plus strict CodeWorker/BrowserWorker turn binding. No opencode runtime, store or process is loaded. |
| Supplementary: Hermes | bounded resolver precedence from `hermes_cli/runtime_provider.py` at `44ddc552f5e054759a6970af8997ea588a9d81c9` | Python strict route-lease handoff only; no provider execution or state owner. |
| Supplementary: oh-my-pi | provider wire, stream and auth-retry mechanisms at `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | TypeScript stream supervisor, protocol mapper and bounded fallback behind Zyra route/credential custody. |
| Conformance/reference: OpenHands | process/Docker/remote sandbox lifecycle shapes | Used to check adapter behavior only. The backend transport, state machine, journal, health and control owners are Zyra implementations. |
| Conformance: Claude-derived runtime | protected QueryEngine/session/tool/permission/compact loop | Provider binding stays inside the existing model-stream seam; the graph does not split local reasoning into shared micro-nodes. |

This is a forward role correction relative to the protected 05D-01 historical review, which called
OpenHands a primary source for a separate backend domain. Under the current source-role de-dup rule,
05D-02 assigns no external primary to the Zyra-authored backend control plane; OpenHands is
conformance/reference only. The protected 05D-01 record was not rewritten.

The synchronized bundled ledger has three `M1-S05D-02` productized entries: one primary and two
supplementary. Owner readiness reports 3 total, 3 connected, 0 blocked and 0 missing target/runtime/
test; `ok=true`. The owner policy matrix reports 0 errors and 0 warnings. The full legacy seed audit
still exposes protected historical warnings/blockers outside this owner filter and is not presented
as a current-slice failure or silently rewritten.

## 9. Behavioral verification

### Implementation worktree

```text
npm run typecheck
# passed: Claude runtime, Provider Control Plane, Runtime Event Spine

npm test --workspace @zyra/provider-control-plane
# Node: 11/11 passed

node_modules/.bin/bun.exe test packages/runtime/provider-control-plane/test/provider-control-plane.test.ts
# Bun 1.2.15: 11/11 passed

python -m pytest \
  tests/unit/test_backend_registry.py \
  tests/unit/test_provider_control_plane_port.py \
  tests/unit/test_task_graph.py tests/unit/test_scheduler.py \
  tests/integration/test_backend_failover_dispatch.py \
  tests/integration/test_provider_backend_api.py -q
# 18/18 passed in 41.08s

python -m pytest tests/integration/test_scheduler_api.py \
  tests/integration/test_e02_typescript_api_cutover.py -q
# 2/2 passed in 22.16s

bun test packages/runtime/claude-runtime/test/{protocol,runtime,permission,skills,agents,control}.test.ts
# 4 passed across 6 discovered files; 0 failed
```

Provider tests use real loopback servers and verify immutable catalog/credential snapshots, two
credential versions on two real requests, OpenAI and Anthropic bytes/SSE, thinking, fragmented tool
JSON, zero-byte revoke, 503/429/stall route changes, and no replay after observable output.

Backend tests use an unreachable primary HTTP endpoint and a real loopback alternate, assert that
backend identity changes while provider/M0 refs do not, verify journal replay/hash integrity, cancel
a pending HTTP dispatch, mutate drain/resume acceptance, and quarantine a replaced workspace root.

### Exact-commit cleanroom

Detached commit `dc010d102ca4b74d7f13e42952706014e2fc40e2` was checked out at
`.tmp/zyra-cleanroom-05d02-dc010d1`. `bun@1.2.15 install --frozen-lockfile` installed 14 locked
packages. On that exact tree:

- all TypeScript typechecks passed;
- Provider behavior passed 11/11 under Node and 11/11 under Bun;
- Python core provider/backend/task-graph/API passed 18/18 in 44.51s;
- scheduler API passed 1/1 in a fresh process in 18.19s;
- E02 TypeScript API cutover passed 1/1 in a fresh process in 3.61s;
- Claude protocol adjacency passed 4/4;
- ledger sync aligned; readiness was 3/3 connected; policy was 0 errors/0 warnings;
- forbidden root-source runtime paths, worker secret-environment reads and root source repositories
  in the cleanroom were all 0;
- Git status was clean after verification.

One combined 20-test Python process first produced 19 passes and one scheduler API timeout. The
preceding API suite had closed the process-global Runtime Event port, and the scheduler case then
failed with `runtime event port is closed`. The unchanged exact commit passed the core 18, scheduler
1 and E02 1 in separate fresh processes. This is recorded as shared-process lifecycle debt for the
numeric-stage aggregate, not hidden as a clean combined-suite pass.

## 10. Adjacent diagnostics and baseline comparison

Expanded adjacent API/browser suites were run during the incremental review. The control-command
file reported 5 passed and 17 failed; representative failures (`e02_route_not_found` and a false
legacy `query_plan_ok`) reproduced at the protected baseline commit
`f0700258840da4514b0090e7851e8abff4879409`. The expanded browser set reported 116 passed, 17 nested
subtests passed and 7 failed; a representative local-HTML extraction failure also reproduced at the
same baseline. The current slice does not weaken protected E02 permission/provider semantics to
make those stale expectations pass. These suites remain required diagnostic input for the M1-05
numeric-stage aggregate; they are not counted as current-slice passing evidence.

## 11. Effective line buckets

The conservative measurement counts only nonblank, non-comment additions from the protected
baseline and excludes tests, exports/manifests/lock data, ledger data and helper scripts:

- Python backend registry/router/transport/control/journal/health/workspace/service: `7,281`.
- TypeScript Provider/stream/CodeWorker production runtime: `1,914`.
- API/task-graph/provider-lease/BrowserWorker main-path integration: `557`.
- Conservative counted production: **`9,752`** (`10,524` raw additions).
- Slice minimum: `9,000`; shortfall: `0`.
- M1-S05D-01 conservative production: `13,281`.
- Parent cumulative conservative production: **`23,033`**; parent minimum: `18,000`.
- Dedicated tests: `425` effective (`462` raw); excluded.
- Source-ledger sync helper: `86`; excluded as an ordinary evidence script.
- Ledger seed/source records: `368`; excluded as data/evidence.
- Package exports, manifests and lock changes: `344`; excluded.
- Review/evidence documents: excluded.
- Generated, data-as-code, mock-only, fixture-only, vendor-like and source-pool production counted:
  `0`.

The route lease, direct TypeScript provider binding and backend protocol modules are not counted as
thin adapters: they own validation, snapshot identity, cancellation, replay fencing, redaction,
failure classification and state transitions, and their disconnect changes tested behavior.

## 12. Adversarial findings corrected

The implementation/review corrected the following before evidence freeze:

- Provider and backend identity could previously be passed as loose strings; strict route checksum,
  M0 execution and turn correlation are now required.
- BrowserWorker and legacy QueryEngine paths could select environment/default provider behavior;
  required Provider Control Plane mode now fails closed.
- A successful transport result could be replayed or rematerialized; side-effect and
  materialization fences now make success single-commit.
- HTTP cancellation originally closed only the client response; cancel-by-envelope now mutates the
  remote dispatch session and the response is closed.
- Workspace path equality could miss root replacement; attestation identity and quarantine now
  prevent reuse.
- Node/Bun SQLite behavior diverged on result/finalization semantics; one runtime adapter now passes
  the same 11 provider tests in both engines.
- The initial source decision inherited OpenHands primary wording; the current owner-filtered ledger
  and review apply the forward de-dup rule without changing protected history.
- Missing explicit credential-rotation and stream-stall tests were found during self-review and
  added as second real-request assertions.

Blocking findings remaining inside M1-S05D-02 scope: `0`.

## 13. Environment limitation and unclaimed gates

Docker CLI 29.1.3 is present, but the Docker server/Windows named pipe is unavailable in the current
environment, including outside the sandbox. The Docker transport is implemented and fails closed;
no live container dispatch is claimed. The real isolated edge, external cloud and three-way
local/edge/cloud competition evidence remains owned by M1-08. The loopback edge behavior in this
slice proves the wire/control/failover path, not production deployment isolation.

This review also does not claim the M1-05 numeric-stage aggregate, milestone exit, two high-quality
cross-domain live tasks, 2,000 effective sealed-autonomy transitions, dynamic sparse-topology/
low-entropy comparison, full multi-model matrix, visual causal trace, or delivery freeze. Those
requirements remain open and cannot be offset by source volume or unit tests.

## 14. Final conclusion

M1-S05D-02 has a default, durable Provider-Control-Plane-to-worker dispatch path; independently
mutable provider and backend routes; strict envelopes; real provider and edge HTTP requests;
provider stall/rate/unavailable recovery; backend failover; cancellation; workspace quarantine;
replay/idempotency/materialization fences; API/task-graph/event reachability; source custody; and an
exact-commit cleanroom. The slice exceeds its production minimum, and M1-05D exceeds its cumulative
minimum. The parent unit is complete, while competition live-dispatch and numeric-stage aggregate
gates remain explicitly open.
