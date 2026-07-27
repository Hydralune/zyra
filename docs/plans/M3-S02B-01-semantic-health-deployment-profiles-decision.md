# M3-S02B-01 semantic health and deployment profiles decision

## Frozen interval

- Slice: `M3-S02B-01`
- Baseline commit:
  `f241d33c011e7f0adabb2f390395034ac064c9d6`
- Required predecessor:
  M3-S02A-02 live benchmark and ablation verdict `PASS`
- Required predecessor evidence commit:
  `f241d33c011e7f0adabb2f390395034ac064c9d6`
- Slice minimum conservative effective production additions: `6,000`
- Parent M3-02B minimum after both slices: `12,000`

This decision is committed before production implementation. The
implementation commit will be frozen before review prose, generated receipts
and execution-state updates are committed.

Tests, fixtures, generated doctor or task receipts, JSON evidence,
documentation, configuration examples, schemas and DTO-only declarations,
thin launchers, adapter-only code, mock probes, repetitive declarations,
vendored content and cached output receive zero effective-production credit.

## Protected starting facts

The implementation consumes, and does not reopen, these protected facts:

1. M3-01 selected one canonical Zyra owner for every protected runtime state
   domain and proved owner loss without fallback masking.
2. M3-S02A-01 admitted the default CLI, API, Web and worker paths together
   with generated-input security, clean-state, causality and deterministic
   control regressions.
3. M3-S02A-02 admitted the two formal cross-domain sealed benchmark campaigns,
   all seven comparison variants, 42 cells, 1,554 raw samples, zero human
   intervention and the protected real local/edge/cloud plus provider facts.
4. `ProductBootstrapRuntime` and `RuntimeOwnerComposition` already own runtime
   configuration migration, lifecycle and the eleven-domain owner readiness
   contract.
5. `WorkerPoolIntegrationRuntime`, backend registry and recovery runtime remain
   the canonical worker lease, placement, failure and recovery owners.
6. `GraphCommitRuntime` and `CheckpointRecoveryRuntime` retain the narrow
   checkpoint lineage, pending/committed write, atomic commit and exact-resume
   semantics derived from LangGraph conformance. No StateGraph, Pregel,
   generic channel/reducer, ToolNode, stream, Store, SDK or server enters the
   deployment path.
7. TypeScript CodeWorker, provider control plane, MCP and command/permission
   owners remain in their original language and process boundaries.
8. OpenClaw remains `excluded_forward_only`; this slice will not read, restore
   or depend on an OpenClaw repository, package, process or path.

The slice packages and exercises these active owners. It does not create a
parallel scheduler, checkpoint store, provider catalog, permission journal,
event reducer, memory store, artifact store, worker runtime or task owner.

## Product responsibility inventory

The current repository has owner readiness and formal benchmark verifiers, but
does not yet have the deployment product required by this slice:

1. there is no one-command process supervisor for API, Web and independently
   identified device, edge and cloud nodes with idempotent start, stop,
   restart, status and doctor behavior;
2. no durable deployment store binds process generation, node identity,
   endpoint, profile digest, resource envelope, credential presence and
   lifecycle observation while rejecting stale PIDs and profile drift;
3. no profile runtime enforces distinct sensitivity, latency, network,
   provider/model and resource policies at admission and execution time;
4. no authenticated node protocol provides liveness, semantic readiness,
   capability handshake, real work execution, checkpoint import/export,
   failure injection and redacted diagnostics from separate processes;
5. no placement engine turns sensitivity, SLA, complexity, resource and
   provider requirements into a deterministic executable route with a
   fail-closed explanation and model split;
6. no deployment recovery coordinator observes network loss, node crash or
   provider failure, selects an admissible successor, transfers deployment
   checkpoint context, fences duplicate dispatch and verifies the successor
   result;
7. the existing `/health` and `/runtime/readiness` endpoints are not composed
   with Web, CodeWorker, BrowserWorker, provider/failover, MCP/skills,
   workspace, scheduler placement, memory/compact/checkpoint, sealed
   permission, recovery and artifact behavior into one semantic verdict;
8. there is no short fresh-task verifier that creates a task through the
   canonical API, observes canonical events, worker/session projections,
   artifact output, memory/checkpoint and permission/recovery facts, and also
   proves that the selected deployment node executed a bound subtask;
9. offline/limited-network, low CPU/memory, port conflict, missing credential,
   schema migration, partial startup, crash/restart, event reconnect and
   checkpoint restore are not normalized into operator-facing fail-closed
   diagnostics;
10. clean-state and submission-boundary checks are not part of the default
    deployment doctor, so a deployment could accidentally rely on repository
    caches, build residue, a root source checkout or an undeclared process;
11. the API has no deployment route through which workbench or automation can
    inspect profiles, invoke doctor, dispatch bound work and inject a bounded
    deployment fault;
12. `scripts/verify_m3.py` cannot yet admit an exact-revision semantic
    deployment receipt.

These are independent product responsibilities rather than report templates
or line-count scaffolding. Removing the new package must break default
start/doctor, profile execution, placement/failover, short-task semantic
verification and M3 deployment admission. They are sufficient for the fixed
6,000 effective-production-line budget, so no planning blocker is declared.

## Migration and language decision

The migration mode is `packaging_and_release_integration_only`.

The slice adds a Zyra-owned Python deployment runtime below
`packages/orchestration/zyra_orchestration/deployment/**`, a narrow API facade
in `apps/api/zyra_api/deployment_api.py`, and a product CLI entrypoint. Python
is retained because the existing product bootstrap, API, orchestration,
scheduler, recovery, memory, workspace and artifact composition boundaries
are Python. Existing TypeScript owners are called through their formal
in-repository process/API contracts and remain TypeScript.

There is no upstream production migration quota, no cross-language owner
rewrite and no language-unification decision. The new isolated node server is
Zyra-owned source in this package, not an upstream CLI, image, package or
black-box sidecar.

## Source-language and target decision table

| Source path or product owner | Source language | Target path | Target language | Migration mode | Runtime owner after slice |
| --- | --- | --- | --- | --- | --- |
| `zyra_runtime.productization` configuration, lifecycle and owner readiness | Python | `zyra_orchestration/deployment/configuration.py`, `doctor.py`, `orchestrator.py` | Python | packaging integration | productization owners retain runtime state; deployment owns only deployment profile/lifecycle state |
| `zyra_scheduler.worker_pool` and backend/recovery runtime | Python | `deployment/placement.py`, `recovery.py`, `handoff.py` | Python | packaging integration | scheduler/recovery owners remain canonical; deployment binds executable profile routes |
| `zyra_scheduler.recovery_runtime` checkpoint contracts | Python, LangGraph narrow semantic provenance | `deployment/handoff.py`, semantic probes | Python | conformance-preserving integration | GraphCommit/CheckpointRecovery remain canonical checkpoint owners |
| API canonical event/task/artifact/memory/workspace owners | Python | `deployment/semantic_health.py`, `short_task.py`, API facade | Python | packaging integration | existing API owners remain canonical; deployment observes and correlates immutable facts |
| CodeWorker, provider, MCP and command/permission owners | TypeScript | authenticated semantic probes and public API/process calls | Python orchestration calling TypeScript owners | packaging integration, no port | TypeScript owners remain canonical |
| BrowserWorker and terminal/workspace worker owners | Python plus retained TypeScript boundaries | semantic probes and short-task verifier | Python orchestration | packaging integration | existing worker/session owners remain canonical |
| M3-S02A protected benchmark/deployment evidence | Python/JSON evidence | `deployment/evidence_gate.py` | Python | exact-revision evidence binding only | benchmark remains owner of formal benchmark facts |
| default CLI/API composition | Python | package CLI, `pyproject.toml`, `apps/api/zyra_api/deployment_api.py` | Python | packaging integration | deployment supervisor owns process/profile lifecycle only |

No source is `mixed` or `unknown`. No language exception is required.

## Canonical ownership and state custody

The deployment runtime may persist only:

- deployment schema revision and configuration digest;
- supervisor and child process identity/generation;
- profile/node identity, endpoint and public capability projection;
- health observations, placement decisions and deployment dispatch receipts;
- deployment checkpoint handoff references and idempotency fences;
- redacted failure, restart and degradation observations.

It must not persist or mutate a second copy of canonical task, event,
permission, memory, scheduler lease, provider credential, MCP, workspace,
artifact or graph/checkpoint state. Receipts carry stable references and
digests to those owners. A deployment handoff is not committed until the
existing checkpoint/recovery owner accepts the reference and the successor
node verifies it.

The event causality path is:

```text
CLI/API deployment request
  -> validated profile and placement constraints
  -> scheduler/profile admission decision
  -> authenticated child-node execution
  -> deployment dispatch/result receipt
  -> canonical API task/event/artifact owner observations
  -> semantic-health assertion and deployment event projection
  -> exact-revision doctor or short-task verdict
```

No LLM output can authorize a profile, change sensitivity, select a route,
approve an action, commit a checkpoint or declare health. Those decisions are
deterministic Zyra rules over signed observations.

## Planned runtime modules

The implementation will provide real behavior for:

1. profile configuration, validation, digesting and environment projection;
2. durable deployment state, schema migration, optimistic revision and stale
   process reconciliation;
3. port reservation and conflict diagnostics;
4. child-process start, graceful stop, crash detection, restart and log
   capture;
5. cross-platform resource controls using existing `psutil` and operating
   system facilities, with explicit unsupported/degraded evidence;
6. authenticated node request/response signing, nonce replay protection and
   secret redaction;
7. separate-process node health, capability handshake, work execution,
   checkpoint exchange and bounded fault injection;
8. deterministic placement, data locality, SLA, provider/model capability and
   resource admission;
9. dispatch idempotency, attempt custody, timeout and result integrity;
10. automatic migration/degradation with successor selection and checkpoint
    handoff verification;
11. semantic probe registry, dependency graph, timeout, blocker aggregation
    and evidence digest;
12. API, Web, event/reducer, CodeWorker, BrowserWorker, provider/failover,
    MCP/skills, workspace, scheduler, memory/compact/checkpoint, sealed
    permission, recovery and artifact probes;
13. fresh short-task execution through the canonical API plus deployment node
    work and event/artifact correlation;
14. clean-state, source-boundary, process, dependency, dynamic import, port
    and undeclared binary diagnostics;
15. one-command start, stop, restart, status and doctor CLI behavior;
16. API routes for deployment status, profiles, doctor, dispatch and fault
    injection;
17. exact-revision M3 deployment admission.

## Profile and failure semantics

The default profiles are behaviorally different:

- `device`: private/local data, lowest network exposure, tight resources,
  local capability set and no remote-provider requirement;
- `edge`: sensitive or latency-bound workloads, bounded network/latency,
  independently identified process and edge capabilities;
- `cloud`: public/approved data, high-complexity and provider/model capability,
  explicit credential presence, larger resources and remote-network policy.

Sensitive work is rejected by cloud. Missing cloud credentials prevent cloud
admission. Latency-bound work is rejected by a profile whose measured or
configured latency exceeds the SLA. Low resource availability changes
admission. Disabling placement binding must make the dispatch path fail
closed, not select a fixed default.

Edge/network loss, cloud/provider failure and process crash create a new
attempt only after the prior attempt is fenced. Recovery chooses another
admissible profile or an explicitly declared degraded local plan, transfers a
checkpoint reference and verifies result continuity. It never reports success
solely because a process is alive.

## Validation and high-risk decision

The slice adds default child processes and local ports. This matches the
high-risk triggers for process graph, local service and packaging boundary
changes. Validation therefore includes:

- focused unit and integration tests for every runtime module;
- real separate-process start/stop/status and authenticated work execution;
- disable/mutation tests for profile isolation, semantic probes and placement
  binding;
- port conflict, missing credential, limited-network, resource pressure,
  partial startup, crash/restart, reconnect and checkpoint restore tests;
- a fresh canonical API task with event, artifact, memory/checkpoint,
  permission/recovery and deployment-node evidence;
- exact-revision clean-state/clean-workspace execution of the affected default
  path;
- TypeScript typecheck/Web build and relevant Python/API/M3 regression suites;
- submission-boundary, dependency/process and root-source isolation audits;
- exact-revision line-bucket and large-file review.

The complete M3-02 numeric-stage aggregate and milestone-exit release
clean-machine verification remain assigned to the next slice and later
documented layers. Current high-risk validation will nevertheless run a
matching cleanroom for the new process/port path.

## Effective-code accounting commitment

The final review will compute the interval
`f241d33c011e7f0adabb2f390395034ac064c9d6..<implementation_commit>`.

Per-file buckets are:

- production deployment/orchestration/node/health behavior;
- test;
- docs/review;
- generated evidence/report;
- configuration/data;
- schema/type-only;
- adapter/launcher-only;
- mock/fixture;
- vendor/source-pool.

Every file over 500 raw additions, over 20 percent of conservative effective
production or over 30 percent excluded content receives an individual
responsibility and exclusion review. DTOs, repeated probe declarations,
static report text and thin CLI/API forwarding code are excluded. The final
review must identify the default entrypoints, unique state owners, event
causality, disable semantics, tests that fail when the module is disconnected,
all external processes and ports, every excluded line bucket and any residual
release debt assigned to `M3-S02B-02`.
