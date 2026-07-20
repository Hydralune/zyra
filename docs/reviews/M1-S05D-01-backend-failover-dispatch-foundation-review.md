# M1-S05D-01 Backend Failover Dispatch Foundation Review

## 1. Review identity

- Slice: `M1-S05D-01`
- Measurement baseline: `deb2c5a569f118862be1506a5749d4579a47be11`
- Main implementation commit: `63616b446f3aa01c54fd8922d45d14a964068d22`
- Clean-install toolchain repair: `009c9329e767ba864141dc15ee23160df9af55a0`
- Final implementation/source-custody commit: `ef0ea831e2551429bea1a8029865cd7407eae91c`
- Review type: incremental critical self-review with upgraded exact-commit cleanroom verification
- Result: PASS for this slice only
- Parent status: `M1-05D` remains in progress; `M1-S05D-02` is still required

The review measures all retained changes from the last protected M1-S05C-02 evidence commit to
the final M1-S05D-01 implementation commit. Ledger rows, type presence, API health responses, or
line volume are not treated as substitutes for the real dispatch and failure-path tests described
below.

## 2. Outcome and production boundary

The slice establishes two deliberately separate production state owners:

- `@zyra/provider-control-plane` is the canonical TypeScript owner for provider/model catalog
  revisions, integrations, credential references and status, immutable provider route leases,
  provider attempts/events, protocol transport, streaming, error classification, and provider
  failover.
- `zyra_scheduler.backend_registry` is the canonical Python owner for backend definitions,
  health, immutable backend leases, dispatch attempts/events, actual worker invocation, timeout
  reconciliation, and backend failover.

The Python `zyra_runtime.provider_control_plane` package supervises the long-lived TypeScript JSONL
process and maps schema/errors for API callers. It is not a second provider store or resolver. The
backend layer receives only an opaque `provider_route_id`; it never parses provider, model,
integration, credential, secret, or hidden-default state.

## 3. Provider control plane behavior

The provider control plane implements:

- optimistic catalog revision writes and immutable catalog snapshots;
- provider, model, and integration records with explicit protocol/base URL/capability fields;
- secret-reference credentials whose SQLite record contains a reference, fingerprint, version,
  status, counters, and timestamps, but never plaintext secret material;
- in-memory/environment secret resolution at the last responsible moment before transport;
- immutable route checksums pinning catalog revision, provider, model, integration, credential
  version, protocol, base URL, timeout, retry budget, and routing reason;
- OpenAI Chat, OpenAI Responses, and Anthropic Messages request encoding plus incremental SSE
  decoding;
- structured provider/auth/protocol failure classification, request-byte accounting, partial-output
  fencing, and provider-route failover;
- a read-only V1 compatibility projection that cannot create providers, choose hidden defaults, or
  become a state owner.

Credential revocation, blocking, expiry, version conflict, unresolved references, and fingerprint
mismatch are rejected before HTTP request bytes are sent. A provider transport failure may acquire
a new provider route lease, but it does not select or mutate a backend. Once output is observed, a
malformed or failed stream is reconciliation-only and is never replayed automatically.

## 4. Backend registry and dispatch behavior

`BackendRegistryStore` persists backend definitions, health observations, leases, attempts, and
events independently from provider state. Definitions support local process, container, edge HTTP,
and cloud HTTP kinds without claiming that all four are already live competition dispatches.

`BackendDispatchRuntime` performs the selected worker callable under a backend lease. The default
task graph now wraps both CodeWorkerRuntime and BrowserWorker calls with this real dispatch path.
The callable receives a typed backend envelope, and the resulting lease/attempt/event records are
attached to the node and worker event stream.

Failure separation is explicit:

- retryable backend unavailability creates a new backend lease while retaining the exact opaque
  provider route reference;
- provider-control errors are surfaced as provider failures and never rotate the backend;
- non-interruptible work that returns after its turn deadline is reconciliation-only, because its
  side effects cannot safely be replayed;
- partial output likewise forbids automatic retry;
- workspace and artifact roots are validated before the worker callable runs.

The legacy M5 dispatch envelope now also carries the opaque provider route reference, but remains a
compatibility projection rather than the new backend registry's canonical lease/attempt owner.

## 5. API and task-graph reachability

The API exposes real state-owner routes for provider health/catalog/providers/models/integrations,
credential references, routes, events, read-only V1 compatibility, dispatch, and credential
revoke/block operations under `/providers/**`. Backend definitions, health, and events are exposed
under `/backends/**`.

Task-graph execution calls `dispatch_worker_callable` before invoking the selected CodeWorker or
BrowserWorker. Removing that backend integration makes the task-graph path fail rather than falling
back to a direct unleased worker call. Server shutdown closes the long-lived provider process and
its SQLite handle.

The adjacent `/inject` regression found during API verification was corrected by preserving each
command descriptor's event hint. `/inject` again emits `failure_injected` instead of being
normalized as a generic control command.

## 6. Source-to-target roles and custody

| Role | Source mechanisms | Zyra target and retained responsibility |
| --- | --- | --- |
| Primary: opencode | provider catalog, integration, credential, AISDK/model, provider/auth/config mechanisms at `adf178a6b95c61506ddaadaf4dd062badb4a8fda` | TypeScript catalog, credential, resolver, route, store, transport, and control-plane modules. OpenCode supplies no runtime package, process, or second owner. |
| Supplementary: Hermes | `hermes_cli/runtime_provider.py` at `44ddc552f5e054759a6970af8997ea588a9d81c9` | Bounded environment/config resolution precedence and normalization only. |
| Supplementary: oh-my-pi | OpenAI Chat/Responses, Anthropic, simple Responses, and auth-retry mechanisms at `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | Productized TypeScript wire contracts, SSE/protocol mapping, auth retry, and error taxonomy behind Zyra route/credential/attempt custody. |
| Primary for separate backend domain: OpenHands | sandbox service/process/docker/remote lifecycle mechanisms at `c105a82387898e744423c8831d412e26495b38a9` | Zyra Python backend definition, health, lease, attempt, dispatch, timeout, and failover owner. No OpenHands server or sandbox process is loaded. |

The bundled source ledger has four `M1-S05D-01` productized entries. Exact-seed readiness reports
`4` total, `4` connected, `0` blocked, `0` missing target/runtime/test, and `ok=true`. Its policy
matrix reports `0` errors and `0` warnings. Direct-port notices are recorded in
`packages/runtime/provider-control-plane/THIRD_PARTY_NOTICES.md`.

The repository-wide legacy ledger audit still reports protected historical target drift from
earlier units. This slice does not rewrite those completed units. The owner-filtered exact seed,
policy matrix, line audit, clean-boundary scan, and behavior tests all pass for the current diff.

## 7. State-custody map

| State domain | Canonical owner | Non-owner boundary |
| --- | --- | --- |
| Provider/model/integration catalog | TypeScript `ProviderControlPlaneStore` catalog tables and revision | Python catalog facade and API serialize RPC results only |
| Credential lifecycle | TypeScript credential table and `CredentialManager` | Secret resolver returns ephemeral material; plaintext is not stored or returned |
| Provider route/attempt/event | TypeScript route, attempt, and event tables | Backend envelope carries only opaque `provider_route_id` |
| Provider transport and partial-output fence | TypeScript `ProviderTransportRuntime` and protocol/SSE decoders | Backend runtime classifies provider errors but cannot change the provider route |
| Backend definition/health/lease/attempt/event | Python `BackendRegistryStore` | Task graph/API expose projections and dispatch through the owner |
| Worker result/artifact/event | Existing worker, artifact, and event owners | Backend attempt links to the result; it does not take artifact or event-log custody |

## 8. Behavioral verification

### Implementation worktree

Commands and results:

```text
npm run typecheck
# passed: Claude runtime + provider control plane + runtime event spine

node --experimental-strip-types --test packages/runtime/provider-control-plane/test/provider-control-plane.test.ts
# 6/6 passed

.venv/Scripts/python.exe -m pytest -q \
  tests/unit/test_backend_registry.py \
  tests/unit/test_provider_control_plane_port.py \
  tests/unit/test_task_graph.py tests/unit/test_scheduler.py \
  tests/integration/test_provider_backend_api.py \
  tests/integration/test_scheduler_api.py \
  tests/scenarios/test_m5_scheduler_fault_recovery.py
# 17/17 passed
```

The TypeScript suite uses loopback HTTP servers and verifies actual request headers/body bytes,
OpenAI and Anthropic SSE frames, route snapshot pinning, read-only V1 behavior, zero-byte credential
rejection, provider-route failover, and no replay after partial output. The Python suite verifies
real process RPC, inline-secret rejection, callable dispatch, backend-only failover,
provider-failure/backend separation, workspace preflight, timeout reconciliation, API state-owner
separation, task-graph dispatch, scheduler adjacency, and the existing fault-recovery scenario.

### Exact-commit cleanroom

The final implementation commit was checked out in
`.tmp/cleanroom-M1-S05D-009c932` and advanced in detached mode to exact commit
`ef0ea831e2551429bea1a8029865cd7407eae91c`. Dependencies were installed from `bun.lock`.

- Project-local Bun version: `1.2.15`.
- TypeScript typechecks: passed for Claude runtime, Provider Control Plane, and Runtime Event Spine.
- Provider TypeScript tests: `6/6` passed.
- Python/provider/backend/task-graph/API/scheduler/fault tests: `17/17` passed in `39.19s`.
- Source-ledger sync: aligned; owner readiness `ok=true`.
- Forbidden parent-repository runtime dependency hits: `0`.
- Root source repositories present in the cleanroom: `0`.
- Cleanroom Git status after verification: clean.

The first cleanroom attempt exposed that the repository declared Bun 1.2.15 as its package manager
without locking the executable package used by CodeWorkerRuntime. The final implementation adds
`bun@1.2.15` to dev dependencies and the frozen lock. The final cleanroom therefore discovers the
project-local runtime without an environment override or a parent-worktree binary.

## 9. Disconnect and semantic-effect evidence

- Revoking a pinned credential makes dispatch fail before the loopback server observes a request;
  the persisted attempt records `requestBytes=0`.
- Changing catalog configuration after route acquisition does not mutate the in-flight lease.
- Provider unavailability creates a different provider route lease without a backend concept.
- Backend unavailability changes backend lease/backend ID while preserving `provider_route_id`.
- A provider failure leaves backend attempt count and backend identity unchanged.
- Partial provider output and late non-interruptible backend output both require reconciliation and
  prevent replay.
- Removing BackendRegistry dispatch from the task graph removes lease/attempt events and breaks the
  real worker path; there is no direct-call fallback.
- Removing the TypeScript process breaks provider health/catalog/route/dispatch API behavior; the
  Python facade cannot substitute as a state owner.

## 10. Effective line buckets

The broad repository line audit reports `effective_added=15,527`, but that number includes tests,
scripts, adapters, and export surfaces. This review uses the stricter production-only bucket below:

- TypeScript control-plane runtime excluding package export: `3,447`.
- Hand-maintained, compiled TypeScript provider wire contracts imported by the protocol mapper:
  `7,575`.
- Python BackendRegistry implementation excluding package exports: `1,797`.
- API/task-graph/legacy-envelope production integration: `462`.
- Conservative counted production: **`13,281`**.
- Slice minimum: `9,000`; shortfall: `0`.
- Adjacent command-event regression fix: `6`; excluded from the minimum.
- Python provider process/facade adapter-only code: `801`; excluded from the minimum.
- Package export surfaces: `136`; excluded from the minimum.
- Dedicated tests: `996`; excluded from production.
- Source-ledger sync script: excluded from production.
- Ledger seed and source-to-target records: excluded as data/evidence.
- Manifests, lock entries, notices, and this review: excluded.
- Generated, data-as-code, mock-only, fixture-only, vendor-like, and source-pool lines counted as
  production: `0`.

The wire contracts are hand-maintained TypeScript source, not generated SDK output or JSON disguised
as code. They are compiled and imported by the real protocol request mapper, and their maintenance
and source notices are inside the Zyra package. Runtime dispatch behavior is nevertheless proven by
loopback byte/SSE tests rather than by type volume.

## 11. Adversarial findings corrected

The implementation and review corrected these blocking defects before evidence freeze:

- credential revocation originally surfaced as a version conflict; usability is now checked first,
  yielding the precise revoked error while retaining the zero-byte fence;
- the workspace lock initially omitted the new provider workspace;
- the first clean install could not discover Bun for the canonical TypeScript CodeWorker;
- direct-port ledger rows lacked resolved source notices;
- the repository-local project ledger cache was stale, so verification was changed to the exact
  bundled seed instead of treating cache state as completion evidence;
- `/inject` lost its `failure_injected` event hint during generic command normalization.

Blocking findings remaining within M1-S05D-01 scope: `0`.

## 12. Residual work and unclaimed gates

`M1-S05D-02` must still integrate live backend adapters, cross-backend recovery details, and close
the parent unit's cumulative `18,000` production-line and end-to-end behavior target. The local,
container, edge, and cloud backend *contracts* in this slice do not claim the competition's real
local/isolated-edge/cloud dispatch gate. Browser compatibility still uses the existing local path
and does not claim a real isolated edge deployment.

This slice also does not claim M1-05 numeric-stage aggregate review, milestone exit, two live
cross-domain scenarios, 2,000 effective autonomous transitions, dynamic-topology/low-entropy
comparison closure, multi-model compatibility closure, or the remaining submission evidence.

## 13. Final conclusion

M1-S05D-01 now has a versioned, credential-safe TypeScript Provider Control Plane; a separate
Python BackendRegistry; immutable provider and backend leases; real protocol bytes and SSE; real
worker-callable dispatch; provider/backend failover separation; API and task-graph reachability;
source custody; conservative production volume above the slice minimum; and a clean exact-commit
verification with the runtime toolchain locked locally. The parent M1-05D remains open for its
integration slice.
