# M2-S01A-01 Typed API Client / Transport Contract Review

Date: 2026-07-23  
Verdict: `PASS`

## 1. Frozen commit boundary

- `baseline_commit`: `f53bf78287a2b2a6eec74102e7218d664307c137`
- `preimplementation_decision_commit`: `5e4d0ae91829aa6315db1ccd8f9e111d39c3e3a6`
- `implementation_commit`: `b4e6b5b554ff6ee8ebd786bddc239d00efa9cbc2`
- `evidence_commit`: `this_commit`
- sole implementation line-count interval:
  `f53bf78287a2b2a6eec74102e7218d664307c137..b4e6b5b554ff6ee8ebd786bddc239d00efa9cbc2`

The implementation commit contains production code and directly related tests.
This review, the audit script and the source-ledger synchronization are evidence
only and are not counted back into the implementation interval.

## 2. Source, language and ownership decision

The prospective decision is
`docs/plans/M2-S01A-01-typed-api-client-transport-preimplementation.md`.
No post-implementation language exception or source-role reassignment was used.

| Role | Source / pinned commit | Source language | Target language | Migration mode | Effective production | Canonical responsibility |
| --- | --- | --- | --- | --- | ---: | --- |
| primary | OpenCode `adf178a6b95c61506ddaadaf4dd062badb4a8fda` | TypeScript | TypeScript | `same_language_adapt` | 5,854 | typed protocol, one transport registry, request identity, retry/cancel/deadline, cursor, normalizer and web request lifecycle |
| supplementary | OpenHands `c105a82387898e744423c8831d412e26495b38a9` | TypeScript | TypeScript | `same_language_adapt` | 422 | bounded create/resume/cancel and lifecycle error mapping; no Axios client or conversation store |
| supplementary / existing owner extension | existing Zyra M1 API and stores, informed by the bounded OpenHands lifecycle source above | Python | Python | `same_language_extend` | 726 | API version/auth/correlation/idempotency receipt enforcement around existing task/event/control owners |
| conformance | OMP `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | TypeScript | test only | `conformance_only` | 0 | request/response correlation, cancellation target and malformed-envelope oracle |
| conformance | Agent Framework `d50698bb797710bfd1ebf34eb621c905a4009b2d` | mixed | test only | `conformance_only` | 0 | AG-UI envelope comparison only |
| conformance | AgentScope `b6698c5dbaa1aa916925e27402767f45e2405fa4` | Python/TypeScript | test only | `conformance_only` | 0 | session-projection comparison only |

OpenCode's Solid stores, generated SDK, provider/session owner and source-tree
dependencies were not copied. OpenHands' Axios client, sandbox owner and
conversation store were not copied. OMP, Agent Framework and AgentScope own no
production state. OpenClaw was neither read nor restored and has no source role.

## 3. Internalized runtime and default path

The default browser path is:

`createZyraApi -> ZyraApiClient -> RequestCoordinator -> TransportRegistry ->
FetchApiTransport -> ZyraRequestHandler -> existing M1 store/runtime owners`.

The internalized TypeScript modules provide:

- one fetch-owning transport and one fail-closed transport registry;
- version negotiation, authentication separation, request correlation and
  structured error normalization;
- typed session/run/task/span/checkpoint/tool/artifact/control/request/receipt/event
  identities and causal binding validation;
- timeout, explicit cancellation, bounded transient retry, circuit breaking,
  request concurrency and duplicate-request suppression;
- opaque task cursors, bounded event polling and freshness fencing without a
  second canonical event store;
- idempotency keys and committed/replayed receipts that fence mutating API
  actions before the canonical side effect.

The Python API guard enforces version, auth and request correlation before route
handling. `TypedReceiptStore.begin` reserves an idempotency key in SQLite before
create/resume/cancel; `commit` stores the resulting receipt only after the
existing canonical mutation succeeds. A replay returns the same receipt without
executing the mutation again.

### Canonical state custody

| State | Canonical owner | What this slice owns |
| --- | --- | --- |
| task / plan / run | existing `SQLiteStore` and `TaskState` | typed projection and request correlation only |
| event history | existing `EventLog` | bounded polling cursor and normalization only |
| artifacts | existing `LocalArtifactStore` | artifact identity/projection only |
| resume / cancel controls | existing M1 control runtimes | authenticated, versioned and idempotent submission boundary |
| transport receipts | `TypedReceiptStore` SQLite table | transport idempotency reservation, commit and replay |
| browser request state | `RequestCoordinator`, `TransportRegistry`, cancellation scopes | transient in-flight state only; no canonical task/event ownership |

No canonical M1 owner, transaction model, lease, restore behavior or global
permission/scheduler/recovery policy was transferred. The event transport is a
transient poller, not a reducer or durable event cache.

## 4. Behavior, failure and disconnect evidence

The following commands were rerun against the frozen implementation:

| Command | Result |
| --- | --- |
| `npm --prefix apps/web run build` | PASS; TypeScript check plus 28-module browser bundle |
| `bun test ./packages/core/typed-api-client/test/transport.test.ts` | PASS; 15 tests, 35 assertions |
| `python -m pytest -q -p no:cacheprovider tests/integration/test_m2_typed_api_client_transport.py tests/unit/test_task_graph.py tests/integration/test_api_control_commands.py::ApiControlCommandTests::test_goal_command_mutates_canonical_task_instead_of_event_only_ack` | PASS; 7 tests in 34.42 s |
| `python scripts/sync_m2_typed_transport_source_ledger.py --check` | PASS; exactly five slice decisions aligned |
| `bun scripts/audit_m2_s01a_01_effective_lines.mjs` | PASS; 7,002 effective production lines, minimum 6,000 |

The TypeScript suite proves duplicate in-flight calls share one response,
transport disable and normalizer disable fail closed, auth/version errors do not
fall back, disconnect and timeout use bounded retry, malformed JSON and
correlation mismatches are rejected, committed/replayed receipts verify, OMP
correlation/cancellation semantics remain explicit, the circuit breaker opens,
the semaphore bounds concurrency and the polling identity window suppresses
duplicates without owning event state.

The embedded integration starts a real `ThreadingHTTPServer`, invokes the Bun
TypeScript client, and writes real SQLite/event-log state. It proves create,
list/get, resume and cancel; exact receipt replay; a disabled client producing no
canonical state mutation; and auth/version rejection. The adjacent tests prove
the task graph and existing control mutation still own state.

Disconnect sensitivity is not fixture-only:

- disabling `TransportRegistry` makes the real request fail before `fetch`;
- disabling the normalizer makes a successful HTTP response unusable;
- replaying one idempotency key leaves one canonical mutation;
- changing the request correlation ID rejects the response;
- removing or bypassing `typed_transport.py` would make auth/version/receipt
  integration assertions fail;
- `rg` finds the only `fetch(` in
  `packages/core/typed-api-client/src/transport.ts`.

## 5. Per-file effective-code audit

Method: the evidence script obtains exact added-line sets from Git. The
TypeScript compiler AST excludes imports, interfaces, type aliases, export-only
declarations and bodyless overloads. Python AST excludes imports, docstrings and
class schema fields. Tests, fixtures, manifests, lockfiles, docs and static HTML
are excluded. Runtime validators count only executable parsing, coercion,
refinement and error behavior. `adapter-only` and generated production are zero.

`Runtime + UI behavior` equals effective production.

| File | Lang | Role | Raw | Runtime | UI behavior | UI presentation | Type/decl | Schema/data | Adapter | Generated | Test/fixture | Docs/comments | Effective |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `apps/api/zyra_api/main.py` | Py | Zyra owner extension | 224 | 203 | 0 | 0 | 14 | 0 | 0 | 0 | 0 | 7 | 203 |
| `apps/api/zyra_api/typed_transport.py` | Py | lifecycle supplement | 606 | 523 | 0 | 0 | 16 | 19 | 0 | 0 | 0 | 48 | 523 |
| `apps/web/index.html` | HTML | presentation | 34 | 0 | 0 | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `apps/web/package.json` | JSON | build data | 11 | 0 | 0 | 0 | 0 | 11 | 0 | 0 | 0 | 0 | 0 |
| `apps/web/src/api/client.ts` | TS | OpenCode primary | 278 | 0 | 214 | 0 | 52 | 0 | 0 | 0 | 0 | 12 | 214 |
| `apps/web/src/api/event-transport.ts` | TS | OpenCode primary | 119 | 0 | 96 | 0 | 15 | 0 | 0 | 0 | 0 | 8 | 96 |
| `apps/web/src/api/index.ts` | TS | primary wiring | 33 | 0 | 23 | 0 | 9 | 0 | 0 | 0 | 0 | 1 | 23 |
| `apps/web/src/api/lifecycle.ts` | TS | OpenHands supplement | 229 | 0 | 195 | 0 | 33 | 0 | 0 | 0 | 0 | 1 | 195 |
| `apps/web/src/api/task-api.ts` | TS | OpenHands supplement | 291 | 0 | 227 | 0 | 57 | 0 | 0 | 0 | 0 | 7 | 227 |
| `apps/web/src/main.ts` | TS | primary wiring | 54 | 0 | 51 | 0 | 1 | 0 | 0 | 0 | 0 | 2 | 51 |
| `apps/web/test/embedded-client-probe.ts` | TS | fixture | 151 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 151 | 0 | 0 |
| `apps/web/tsconfig.json` | JSON | build data | 18 | 0 | 0 | 0 | 0 | 18 | 0 | 0 | 0 | 0 | 0 |
| `bun.lock` | lock | dependency data | 12 | 0 | 0 | 0 | 0 | 12 | 0 | 0 | 0 | 0 | 0 |
| `docs/plans/M2-S01A-01-typed-api-client-transport-preimplementation.md` | MD | decision | 71 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 71 | 0 |
| `package.json` | JSON | workspace data | 6 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 | 0 | 0 |
| `packages/core/typed-api-client/package.json` | JSON | build data | 18 | 0 | 0 | 0 | 0 | 18 | 0 | 0 | 0 | 0 | 0 |
| `packages/core/typed-api-client/src/auth.ts` | TS | OpenCode primary | 196 | 172 | 0 | 0 | 23 | 0 | 0 | 0 | 0 | 1 | 172 |
| `packages/core/typed-api-client/src/cancellation.ts` | TS | OpenCode primary | 308 | 268 | 0 | 0 | 21 | 0 | 0 | 0 | 0 | 19 | 268 |
| `packages/core/typed-api-client/src/constants.ts` | TS | OpenCode primary | 243 | 230 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 230 |
| `packages/core/typed-api-client/src/coordinator.ts` | TS | OpenCode primary | 146 | 121 | 0 | 0 | 22 | 0 | 0 | 0 | 0 | 3 | 121 |
| `packages/core/typed-api-client/src/cursor.ts` | TS | OpenCode primary | 322 | 289 | 0 | 0 | 28 | 0 | 0 | 0 | 0 | 5 | 289 |
| `packages/core/typed-api-client/src/digest.ts` | TS | OpenCode primary | 104 | 99 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 3 | 99 |
| `packages/core/typed-api-client/src/errors.ts` | TS | OpenCode primary | 654 | 578 | 0 | 0 | 74 | 0 | 0 | 0 | 0 | 2 | 578 |
| `packages/core/typed-api-client/src/headers.ts` | TS | OpenCode primary | 233 | 192 | 0 | 0 | 37 | 0 | 0 | 0 | 0 | 4 | 192 |
| `packages/core/typed-api-client/src/identifiers.ts` | TS | OpenCode primary | 501 | 461 | 0 | 0 | 32 | 0 | 0 | 0 | 0 | 8 | 461 |
| `packages/core/typed-api-client/src/index.ts` | TS | export declarations | 21 | 0 | 0 | 0 | 21 | 0 | 0 | 0 | 0 | 0 | 0 |
| `packages/core/typed-api-client/src/normalizers.ts` | TS | OpenCode primary | 521 | 404 | 0 | 0 | 114 | 0 | 0 | 0 | 0 | 3 | 404 |
| `packages/core/typed-api-client/src/polling.ts` | TS | OpenCode primary | 259 | 208 | 0 | 0 | 39 | 0 | 0 | 0 | 0 | 12 | 208 |
| `packages/core/typed-api-client/src/protocol.ts` | TS | OpenCode primary | 364 | 322 | 0 | 0 | 41 | 0 | 0 | 0 | 0 | 1 | 322 |
| `packages/core/typed-api-client/src/receipt.ts` | TS | OpenCode primary | 315 | 281 | 0 | 0 | 33 | 0 | 0 | 0 | 0 | 1 | 281 |
| `packages/core/typed-api-client/src/registry.ts` | TS | OpenCode primary | 157 | 137 | 0 | 0 | 17 | 0 | 0 | 0 | 0 | 3 | 137 |
| `packages/core/typed-api-client/src/request.ts` | TS | OpenCode primary | 331 | 264 | 0 | 0 | 66 | 0 | 0 | 0 | 0 | 1 | 264 |
| `packages/core/typed-api-client/src/resilience.ts` | TS | OpenCode primary | 321 | 259 | 0 | 0 | 39 | 0 | 0 | 0 | 0 | 23 | 259 |
| `packages/core/typed-api-client/src/response.ts` | TS | OpenCode primary | 277 | 228 | 0 | 0 | 48 | 0 | 0 | 0 | 0 | 1 | 228 |
| `packages/core/typed-api-client/src/retry.ts` | TS | OpenCode primary | 363 | 284 | 0 | 0 | 59 | 0 | 0 | 0 | 0 | 20 | 284 |
| `packages/core/typed-api-client/src/telemetry.ts` | TS | OpenCode primary | 247 | 176 | 0 | 0 | 56 | 0 | 0 | 0 | 0 | 15 | 176 |
| `packages/core/typed-api-client/src/testing.ts` | TS | fixture | 83 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 83 | 0 | 0 |
| `packages/core/typed-api-client/src/transport.ts` | TS | OpenCode primary | 409 | 329 | 0 | 0 | 64 | 0 | 0 | 0 | 0 | 16 | 329 |
| `packages/core/typed-api-client/src/version.ts` | TS | OpenCode primary | 193 | 168 | 0 | 0 | 22 | 0 | 0 | 0 | 0 | 3 | 168 |
| `packages/core/typed-api-client/test/transport.test.ts` | TS | tests | 359 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 359 | 0 | 0 |
| `packages/core/typed-api-client/tsconfig.json` | JSON | build data | 17 | 0 | 0 | 0 | 0 | 17 | 0 | 0 | 0 | 0 | 0 |
| `tests/integration/test_m2_typed_api_client_transport.py` | Py | tests | 206 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 206 | 0 | 0 |
| **Total** |  |  | **9,305** | **6,196** | **806** | **34** | **1,055** | **101** | **0** | **0** | **799** | **314** | **7,002** |

The raw total includes 71 lines of the prospectively committed decision because
the sole interval intentionally begins at the pre-decision baseline. Those lines
remain fully excluded from production. The repository's older coarse numstat
tool reports 9,152 effective additions, but that result includes categories
excluded by the 2026-07-22 gate and is not used for the verdict.

## 6. Large-file and high-ratio trigger review

No file contributes more than 20% of the 7,002 effective lines; the threshold is
1,400.4.

| File / trigger | Upstream mechanism and executable symbols | Default path / responsibility | Disable or mutation proof | Raw / effective difference |
| --- | --- | --- | --- | --- |
| `errors.ts`, raw 654 | OpenCode structured errors and schema-error middleware; `ZyraApiError`, typed subclasses, `classifyUnknownError`, `mapHttpError`, `serializeError` | transport/request/response normalization owns typed failure classification, retryability and causal context | auth, version, disconnect, timeout, malformed JSON and correlation tests | 654 raw; 578 effective; 74 declaration and 2 comment lines excluded |
| `typed_transport.py`, raw 606 | bounded OpenHands lifecycle semantics plus existing Zyra API; `typed_request_context`, `TypedReceiptStore`, cursor and readiness functions | request guard runs before routes; receipt reservation runs before create/resume/cancel canonical mutation | embedded API auth/version, exact replay and disabled-client no-mutation tests | 606 raw; 523 effective; 16 imports, 19 schema fields and 48 blank/comment/docstring lines excluded |
| `normalizers.ts`, raw 521 | OpenCode protocol response/error normalization; task/event/artifact/readiness normalizers and `registerCoreNormalizers` | response pipeline converts untrusted JSON into typed projections without becoming state owner | normalizer-disable, malformed JSON, task/event binding and real API tests | 521 raw; 404 effective; 114 declarations and 3 comment lines excluded |
| `identifiers.ts`, raw 501 | OpenCode ordered ID mechanism; identity create/parse/bind, idempotency digest and bounded `IdentityRegistry` | request construction, response binding, receipt and cursor correlation | mismatch rejection, duplicate request, OMP correlation/cancel and bounded identity-window tests | 501 raw; 461 effective; 32 declarations and 8 comment lines excluded |

The following files exceed the 30% schema/data/declaration trigger but are
deliberately zero-credit: `apps/web/package.json`, `apps/web/tsconfig.json`,
`bun.lock`, root `package.json`, `packages/core/typed-api-client/package.json`,
`packages/core/typed-api-client/tsconfig.json`, and the export-only
`packages/core/typed-api-client/src/index.ts`. They contain no claimed runtime
owner or production contribution, so executable-symbol and disconnect evidence
is not asserted for them.

## 7. Ledger, dependency and clean-boundary audit

The bundled seed has exactly five `M2-S01A-01` entries and direct entry auditing
returns zero findings and zero blockers. The full historical seed audit retains
unrelated planned-target findings and three baseline false positives where the
M1 hardening dependency scanner's own literal strings
`../OpenHands`, `../browser-use`, and `../claude-code-best` are detected as if
they were dependencies. That file is unchanged across this slice's frozen
interval. The current implementation paths have zero parent-source relative
paths, npm links, editable parent paths, external Docker contexts, new MCP
servers, new local ports or OpenClaw references.

The slice does not trigger a high-risk escalation: it introduces no external
runtime dependency or process, no canonical-owner transfer, no incompatible
schema/persistence migration and no global permission/scheduler/recovery/compact
policy change. The real embedded API server is test-local and starts no
persistent process. Therefore full-repository regression, complete cleanroom and
the M2-01 aggregate source audit remain at the numeric-stage aggregate, while
all directly affected behavior and adjacent owner paths were run here within
the ordinary-slice budget.

## 8. Critical self-review and residual scope

- The previous HTML contained direct fetch calls and was replaced by a minimal
  typed-transport diagnostic entry. M2-S01A-02 owns the React workbench shell;
  this slice does not claim that UI.
- Polling is intentionally bounded and transient. M2-S01B owns canonical
  event-stream ingestion and frontend projection recovery.
- The transport receipt table owns only HTTP idempotency. It cannot write task,
  event, artifact or control canonical state.
- Retry is limited to classified transient failures and idempotent requests;
  auth, version, validation and correlation failures are terminal.
- The build emits a sandbox access warning from the user's global `npm.ps1`
  probe, but the local TypeScript check and Bun bundle complete with exit code
  zero. The same local Bun binary is used for the direct test evidence.
- This slice advances the typed M2 console/API foundation and traceable control
  boundary. It closes no remaining competition-level live-scenario,
  visualization or M2 exit gate by itself.

Result: `M2-S01A-01` satisfies its production, language, behavior, failure,
owner, evidence and 6,000-line gates. `M2-S01A-02` is the next slice; parent
`M2-01A` remains in progress.
