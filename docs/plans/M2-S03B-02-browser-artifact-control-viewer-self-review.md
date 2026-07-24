# M2-S03B-02 browser artifact and control viewer critical self-review

## Verdict and immutable interval

- Slice: `M2-S03B-02`
- Baseline:
  `7e9483c00cd422afc537b916ab234c9221b53bda`
- Pre-implementation decision:
  `a9967d69c98215b996e1f7fd5448339adf115386`
- Implementation:
  `25b58af8aca0efd6650b2a391eab08f625117b77`
- Verdict: pass for the ordinary-slice gate.
- Conservative effective TypeScript/React: `8,935`, above the required
  `5,500`.
- Raw implementation interval: `14,351` additions and `1` deletion.

The connected task path is:

`BrowserWorkerRuntime -> BrowserObservabilityApplication/canonical events and
artifacts -> typed TaskApi -> BrowserViewerRuntime -> BrowserWorkbench`.

The control return path is:

`BrowserWorkbench -> BrowserViewerRuntime -> typed TaskApi ->
POST /tasks/{task_id}/workers/browser -> existing BrowserWorkerRuntime,
BrowserActionApplication or BrowserSessionControlRuntime -> canonical
event/checkpoint and typed receipt`.

This is not a static screenshot panel. A real HTTP integration submits controls
through the BrowserWorker endpoint, and the adjacent productized-browser suite
starts one shared BrowserRuntimeRegistry session, executes actions, commits
observability/artifacts, diagnoses it, detects process failure, and stops it.

## Source-role and migration judgment

| Source | Frozen role | Selected mechanism | Zyra-owned result |
|---|---|---|---|
| `browser-use@18484f23ac96bb955259a1c54530a7d265dfffdb` | primary semantic implementation source | browser/action/tool-result views, target/frame state, screenshot metadata and history | strict observability reader plus target/frame, DOM/AX, step/result, artifact, health and aggregate projectors under `apps/web/src/features/browser/projection/**` |
| `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | supplementary TypeScript implementation | browser panel and snapshot composition | task-bound `BrowserWorkbench`, explicit empty/error/loading states, verified screenshot composition and accessible history/control surfaces |
| `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | supplementary TypeScript implementation | typed RPC correlation, abort and settlement | browser-specific request policy, receipt normalization, idempotency, concurrency, timeout/cancel and late-result fencing |
| `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | conformance only | auth/correlation/replay negative behavior | Browser viewer tests only; no Hermes gateway, replay store, process or production owner |

The browser-use primary source is Python while the allocated console owner is
TypeScript/React. The prospective cross-language exception was recorded before
production changes. It is bounded to observable browser models and association
semantics. Browser execution loop, CDP transport, action registry, permission,
screenshot writing, state persistence, checkpoint and replay remain in the
existing Python BrowserWorker modules. Python is therefore still non-zero and
authoritative; this slice did not replace it with a TypeScript runtime.

The OpenHands and OMP same-language obligations were met independently through
cropped TypeScript/React production modules. No upstream entrypoint, store,
package, CLI, process or path is used at runtime. OpenClaw was not restored,
read, referenced or depended on.

## Structural and semantic internalization

The selected mechanisms were decomposed into these Zyra-owned modules:

- `contracts.ts` admits task/run/session-scoped observability and browser-control
  envelopes; malformed schema, identity and cross-task input fails closed.
- `projection/reader.ts` deduplicates durable history, spans and artifact
  lineage, audits digest chains and rejects scope crossover.
- `projection/targets.ts` derives page, popup, frame, opener/parent, attachment,
  focus and crash state.
- `projection/dom.ts` creates bounded DOM/AX summaries, strips secret-bearing
  attributes and labels injection-shaped page text as display-only evidence.
- `projection/steps.ts` correlates action, result, tool receipt and step through
  explicit action/tool/span/correlation/sequence evidence.
- `projection/artifacts.ts` joins BrowserWorker lineage, canonical artifact
  projection and the S03A artifact contract, producing screenshot/download
  associations and hash/size/quarantine findings.
- `projection/health.ts` reconciles crash, watchdog, reconnect generation and
  recovery state.
- `projection/causality.ts` audits action/result/span/tool/mutation/artifact
  completeness instead of silently inventing joins.
- `history/navigator.ts` and `history/virtualizer.ts` provide bounded filtering,
  previous/next/follow-live navigation and a measured variable-height window
  without persisting another history.
- `control/policy.ts`, `control/receipts.ts` and `control/runtime.ts` provide
  strict navigate/stop/retry/inspect requests, bounded retry payloads,
  idempotency, concurrency, abort, timeout, stale/error and sealed-invariant
  settlement.
- `runtime.ts` composes canonical selectors, observability reads, S03A catalog,
  projection, transient view state and typed controls.
- `view/browser-workbench.tsx` is mounted by the production `TaskDetail`.

Deleting or disabling these modules changes real behavior:

- projection tests lose URL/title/target/frame, DOM, tool-result and artifact
  linkage;
- control tests lose BrowserWorker delegation or sealed-denial proof;
- the mounted workbench cannot render or submit typed controls;
- disabled transport/runtime tests fail closed without a fallback.

This is dynamic reachability, not a ledger/import smoke path.

## Canonical custody

| State or decision | Canonical owner |
|---|---|
| Chrome process, CDP connection and reconnect resource | Python `BrowserRuntimeRegistry` |
| browser session lifecycle, target/focus and process epoch | Python `BrowserSessionControlRuntime` and session state store |
| browser action execution and active action cancellation | Python `BrowserActionApplication` / `BrowserActionControlRuntime` |
| durable browser history, trace, health and artifact lineage | Python `BrowserObservabilityApplication` |
| task/run/checkpoint and canonical event persistence | API `SQLiteStore` plus canonical event spine |
| screenshot/download bytes and immutable revision | `LocalArtifactStore` plus canonical task artifact state |
| integrity, MIME, redaction, range, cache and download admission in web | existing S03A `ArtifactWorkbenchRuntime` modules |
| allow/deny/ask and exact permit | existing TypeScript `PermissionCoordinator` |
| task workspace and lease | `WorkspaceManagerRuntime` |
| browser selection/filter/window/inflight reads | transient TypeScript `BrowserViewerRuntime` |

`BrowserViewerRuntime.diagnostics()` declares that it owns no browser,
artifact or canonical-event state. It stores no replay log. Closing the React
viewer aborts only view-local reads and control waits; the lifecycle test
asserts zero BrowserWorker control submissions, and its close message explicitly
states that BrowserWorker ownership is unchanged.

During the adjacent real-session regression, a pre-existing main-path error was
exposed: `get_browser_runtime_services` passed the Chrome/browser session ID to
`WorkspaceBindingStore`, whose binding is intentionally task/query-session
scoped. This caused valid browser controls to fail with
`workspace_not_found`. The implementation now acquires the workspace by task
only. Browser session identity remains exclusively in BrowserRuntimeRegistry,
so the correction removes an accidental second-session coupling rather than
moving an owner.

## Artifact reuse and causality

The browser viewer does not fetch or render screenshot bytes directly.
`BrowserArtifactPreview` uses S03A `ArtifactWorkbenchRuntime`, which performs:

- canonical catalog selection and immutable revision binding;
- metadata and range receipt validation;
- independent redaction and prompt quarantine;
- SHA-256/size verification;
- MIME/family admission;
- bounded range scheduling and cache;
- managed Blob URL acquisition/release;
- download permission and quarantine handling.

Browser projection contributes only action/step/target/frame lineage. It compares
BrowserWorker lineage digest/size with both canonical artifact projection and
S03A artifact contract. Mismatch becomes an error finding and screenshot
integrity `mismatch`; it is never presented as verified.

The projector and tests correlate one browser step across action ID, result ID,
tool call, span, canonical event, mutation and artifact. Missing correlations
remain explicit causality findings and can force a partial projection. Popup,
frame, screenshot, download and failure filters consume those same associations.

## Control, sealed and untrusted-page review

- Navigate accepts only explicit `http`/`https` operator input and forbids URL
  credentials. Page/DOM text is never an input to request creation.
- Retry contains one selected prior action, its bounded arguments, prior action
  ID and argument digest. It cannot expand into a new action plan.
- Stop maps to the existing browser lifecycle stop command.
- Inspect maps to the existing diagnose path and remains non-mutating.
- All commands carry task/run/session/request/command/actor identity,
  idempotency, expected generation and task revision.
- Local repeated command IDs return the same receipt; changed bodies conflict.
  Server replay returns the persisted receipt without a second BrowserWorker
  call.
- Timeout, cancel, stale, concurrency and disabled transport settle visibly.
  No alternate client or static success exists.

Sealed mode accepts the UI submission only to create auditable denial evidence.
The server:

1. increments `operator_intervention_attempt_count` once;
2. appends one canonical `CONTROL_COMMAND` event;
3. returns `403` and a typed denied receipt;
4. applies no manual mutation;
5. enters no approval wait;
6. preserves `human_intervention_count == 0`;
7. records autonomous `fail_closed`;
8. replays the same receipt without incrementing again.

The web control runtime independently verifies those invariants and converts an
invalid sealed success into `sealed_browser_control_invariant_failed`.

DOM/AX summaries strip credential/token/cookie/password attributes. Text such
as “ignore previous instructions” or “reveal secret token” is projected only
inside a `data-browser-prompt-display-only` warning. A behavior test proves it
cannot change the explicit URL, permission, identity, rules or request body.

## Behavior evidence

The implementation and final evidence worktree passed:

| Command | Result |
|---|---|
| `bun test ./apps/web/test/browser-artifact-control-viewer.test.ts` | `14 passed`, `73 assertions` |
| Browser/03A/terminal/timeline adjacent five-file Bun suite | `65 passed`, `445 assertions`; popup/download/hash mismatch/DOM/action/result/virtualization/crash/reconnect/close/sealed/disable included |
| `python -m pytest` browser viewer, observability, productized session, session integration and permission-gate files | `36 passed`, `2 subtests passed` |
| `bun test ./packages/core/typed-api-client/test` | `15 passed`, `35 assertions` |
| `bun run typecheck:web` | pass |
| `bun run build:web` | pass; production bundle contains `229` modules |
| `python -m py_compile apps/api/zyra_api/main.py` | pass |
| browser source-ledger synchronizer `--check` | `4` entries, `0` missing implementation targets |
| internalization ledger contract tests | `6 passed` |

The backend set includes a real productized BrowserRuntimeRegistry/CDP session,
real BrowserWorker action and durable observation/artifact commit, diagnose and
stop, real Chrome-process-kill detection, permission-gate behavior and the new
HTTP sealed/control integration. The new HTTP test verifies exact delegated
`browser_plan`, one BrowserWorker invocation, mutation/event receipt, and
idempotent replay.

The TypeScript set includes:

- strict task/schema/scope admission;
- page, popup and nested frame projection;
- DOM/AX secret-attribute stripping and display-only injection findings;
- action/result/tool/span/mutation/screenshot/download linkage;
- hash mismatch failure;
- failed-step bounded retry;
- crash then reconnect generation and healthy reattachment;
- 3,000-step bounded virtualization;
- viewer detach without stop;
- sealed success invariant rejection;
- disabled and timed-out control with no fallback.

## Effective-code and anti-padding audit

The exact Git-added-line classifier reports:

| Bucket | Lines | Credit |
|---|---:|---|
| executable TypeScript production runtime | 8,433 | yes |
| active React UI behavior | 502 | yes |
| CSS/static JSX/SVG presentation | 1,793 | no |
| TypeScript declarations | 1,249 | no |
| schema/DTO/static data | 42 | no |
| Python API/BrowserWorker owner integration | 456 | real production, excluded from the TypeScript/React floor |
| TypeScript/Python tests and fixtures | 1,639 | no |
| comments/blanks/docs | 237 | no |
| adapter-only | 0 | no |
| vendor/source-pool | 0 | no |
| **conservative effective TypeScript/React** | **8,935** | **pass** |

The generic classifier places the 456 Python lines in its out-of-scope/generated
bucket because this gate accepts only TypeScript/React. They are reviewed,
reachable Python production, not generated or vendored material, and receive
zero floor credit.

Every file above 500 raw additions was reviewed:

| File | Raw/effective | Reachability and exclusion judgment |
|---|---:|---|
| `projection/steps.ts` | 1,443/1,314 | action/result/receipt/step correlation called by every session projection; 119 declaration and 10 comment/blank lines excluded |
| `view/browser-workbench.tsx` | 1,384/502 | production TaskDetail mount; refresh, filter, selection, verified preview and control handlers reachable; 825 presentation and 52 declaration lines excluded |
| `browser-artifact-control-viewer.test.ts` | 1,088/0 | direct behavior evidence only |
| `contracts.ts` | 1,008/514 | every HTTP/projection/control boundary uses strict admission; 448 declaration, 42 schema/DTO and 4 comment/blank lines excluded |
| `styles.css` | 966/0 | presentation only |
| `projection/reader.ts` | 733/655 | all observability views flow through its scoped durable reader |
| `runtime.ts` | 720/639 | mounted controller composing reads, selectors, projection, virtualization and controls |
| `projection/targets.ts` | 652/586 | page/popup/frame tree consumed by workbench and popup filters |
| `projection/causality.ts` | 640/607 | session projector calls it for every projection; error findings affect partial/verified state |
| `projection/dom.ts` | 570/527 | bounded DOM/AX and injection/secret filtering called per session |
| `projection/artifacts.ts` | 561/509 | joins BrowserWorker, canonical and S03A evidence; screenshot/download UI consumes it |
| `test_browser_artifact_control_viewer.py` | 551/0 | HTTP and owner-delegation integration evidence only |
| `history/navigator.ts` | 542/510 | workbench filters, search, previous/next and follow-live use it |
| `control/runtime.ts` | 540/456 | every viewer command uses its concurrency/idempotency/timeout/settlement state |

Files with excluded ratio above 30% received additional judgment:

- `browser-workbench.tsx`: static markup/presentation is excluded; only active
  callbacks, state effects and executable selection/control composition count.
- `contracts.ts`: interfaces and DTO/schema declarations are excluded; only
  validators, parsers, normalization and executable defaults count.
- CSS, tests and Python are zero-credit by rule.

No counted file is ledger-only, adapter-only, fixture-only, generated,
data-as-code or vendor-shaped. There is no single counted file above 20% of the
effective total; `projection/steps.ts` is approximately 14.7%.

## Dependency, path and boundary audit

- New production paths contain no `../browser-use`, `../OpenHands`,
  `../oh-my-pi`, `../hermes-agent`, `../claude-code-best`, `../opencode` or
  OpenClaw runtime dependency.
- No package manifest, lockfile, requirements, `pyproject.toml`, Dockerfile,
  npm link, pip editable path, dynamic import, external port, MCP server or
  subprocess was added.
- BrowserWorker remains a migrated Zyra package; the web viewer talks only to
  existing Zyra API routes.
- Root `G:\agent-zoo` docs were not included in the Zyra implementation or
  evidence commits.

## Ledger and residual risk

The source ledger contains four `M2-S03B-02` entries. The synchronizer reparses
the complete seed with `InternalizationLedgerEntry`, verifies every production
target at the exact implementation commit, and reports zero missing targets.
It records one primary, two supplementary and one conformance-only role, plus
`root_source_runtime_dependency=false` and `second_browser_store=false`.

Residual validation intentionally deferred to the M2-03 numeric-stage aggregate:

- full cleanroom and broad all-web/all-backend regression;
- sustained multi-session history and artifact load;
- cross-view trace navigation owned by M2-S03B-03;
- packaging and milestone live-scenario evidence.

These deferred items do not create an alternate owner, remove a required
failure path or weaken this slice's behavior. No ordinary-slice high-risk
trigger was introduced: there is no new dependency/process/port, incompatible
public schema migration, canonical owner transfer, or global default-policy
change.
