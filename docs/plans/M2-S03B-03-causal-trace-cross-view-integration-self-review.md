# M2-S03B-03 causal trace cross-view integration critical self-review

## Verdict and frozen interval

- Slice: `M2-S03B-03`
- Baseline: `ca1d1b1eee0ef04451cc619ddc2b3f944f30f99e`
- Pre-implementation decision:
  `8984aa80596dc29097b13b8dad4723464bd1af57`
- Main implementation:
  `d9c74dc78dd2338541d24625fbccf56fbbb88b1e`
- Numeric-stage regression repair and final implementation target:
  `2bc9604685cd7024825cf94a34c3fc507921d661`
- Verdict: pass after the bounded numeric-stage regression repair.
- Conservative effective TypeScript/React: `5,904`, above the required
  `5,500`.
- Raw slice interval: `9,389` additions and `4` deletions.
- Parent M2-03B direct recompute: `21,152 / 17,000`.
- Numeric stage M2-03 direct recompute: `40,309 / 32,000`.

The production read path is:

`canonical API/event ingress -> CanonicalProjectionStore ->
CausalTraceProjectionEngine -> CausalTraceController -> CausalTraceWorkbench`.

The cross-view path is:

`selected typed trace entity/span -> CrossViewNavigationRuntime -> exact typed
terminal/browser/artifact/diff/timeline/topology focus destination -> focused
view row -> Alt+Enter reverse focus -> exact trace entity/span`.

The trace feature has no task-control write path. Its pins, filters, folds,
selection, viewport measurements and navigation history are disposable UI
state. Closing it does not cancel, stop, retry or mutate a task or worker.

## Source-role and migration judgment

| Source | Frozen role | Selected mechanism | Zyra-owned result |
| --- | --- | --- | --- |
| `zyra@ca1d1b1eee0ef04451cc619ddc2b3f944f30f99e` | primary semantic implementation | canonical projection contracts/selectors and timeline event-reader, causal graph, critical path and drilldown | `features/trace/**` admission, identity, index, analysis, navigation, virtualization, controller and mounted workbench |
| `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | supplementary implementation | typed RPC identity, partial/final settlement, reconnect epochs, subagent/yield and PTY correlation | bounded exact-ID settlement and reconciliation in `identity.ts`, `builder.ts`, `reconciliation.ts` and `navigation/runtime.ts` |
| opencode, browser-use and OpenHands mechanisms already accepted in M2-S03A/B01/B02 | inherited, no new quota | terminal/browser/artifact/diff panel focus contracts | typed destination attributes and focus consumers only; no prior viewer/runtime code is recounted |
| `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | conformance only | auth/correlation/reconnect/replay negative behavior | tests only; no gateway, replay store, session owner or production code |
| OpenClaw | `excluded_forward_only` | none | no source read, migration, comparison, dependency, ledger entry or code quota |

The primary and active supplementary sources are TypeScript and the allocated
console runtime is TypeScript/TSX. Both therefore have non-zero same-language
production paths. No new cross-language semantic port is claimed. Browser
facts originating in Python are consumed only through already productized Zyra
canonical/browser projections; Python BrowserWorker execution and artifact
custody are not reimplemented or recounted.

## Structural internalization and reachable modules

The selected mechanisms were decomposed into Zyra-owned modules:

- `contracts.ts` defines the bounded typed trace vocabulary and immutable
  node/edge/index/diagnostic/view contracts. Its declarations are almost
  entirely excluded from effective-line credit.
- `identity.ts` normalizes exact task/run/session/worker/event/span/tool/action,
  artifact/mutation/checkpoint/provider/MCP/skill/subagent/PTY identities and
  rejects malformed or cross-scope material.
- `index/builder.ts` admits canonical projection records and creates forward,
  reverse, hierarchy, entity, mechanism and boundary indexes. Joins require
  explicit typed IDs, predecessor/cause IDs or canonical bindings.
- `index/reconciliation.ts` keeps only bounded settlement fingerprints for
  partial/final, late artifact, reconnect generation and orphan resolution. It
  never retains a second canonical payload history.
- `projector.ts` reads the existing `CanonicalProjectionStore` and emits one
  derived immutable trace projection keyed by canonical revision.
- `analysis/critical-path.ts` collapses strongly connected components before
  deterministic DAG analysis, retains equal alternatives, and attributes wait,
  permission, retry, placement and recovery latency.
- `analysis/query.ts` provides indexed free text and typed qualifiers for
  identity, domain, mechanism, status, time and diagnostic fields.
- `analysis/fold.ts` folds repeated causal regions while preserving critical,
  fault/recovery, permission and boundary anchors and exposing hidden-edge
  summaries.
- `scale/virtualizer.ts` uses a Fenwick tree for variable-height rows, measured
  updates, overscan and far-index reveal without mounting the full trace.
- `navigation/runtime.ts` resolves only typed destinations, checks view
  availability, scrolls/focuses a concrete row, reports visible failure, and
  implements reverse navigation from typed `data-*` bindings.
- `report/pins.ts` records bounded local references, emits stable Markdown
  report references and marks stale/rebased pins without becoming canonical
  run state.
- `view/controller.ts` composes derived projection, query, fold, critical path,
  virtual window, selection, navigation and pins. `close()` releases only local
  structures and subscriptions.
- `view/trace-workbench.tsx` is mounted by the production `TaskDetail`; it
  exposes search, filters, completeness, critical path, virtualization,
  diagnostics, typed jumps, reverse-jump hints and report-reference actions.

The existing terminal, browser, artifact, diff and topology workbenches gained
typed `data-*` bindings only. Those bindings do not infer causality from text;
they expose identities already carried by the canonical projection/view model.

Deleting or disabling the projector, builder, navigation runtime or controller
causes direct behavior tests to fail explicitly. Removing the TaskDetail mount
removes the production path. This is dynamic reachability, not an import,
ledger, manifest or static-page claim.

## Canonical custody and no-second-store proof

| State or decision | Canonical owner |
| --- | --- |
| task/run/checkpoint and committed event facts | API `SQLiteStore` plus canonical event spine |
| browser-side committed projection | TypeScript `CanonicalProjectionStore` |
| worker attempt, lease and route | existing Python worker-pool/scheduler owners |
| permission decision and exact permit | existing permission runtime / `PermissionCoordinator` boundary |
| terminal PTY process/session/ticket authority | Python `TerminalSessionRegistry` and terminal runtime |
| browser process/session/action/history | Python `BrowserRuntimeRegistry` and BrowserWorker observability owners |
| artifact bytes and immutable revision | `LocalArtifactStore` plus canonical task state |
| diff/workspace bytes and patch transaction | workspace and patch transaction runtimes |
| trace filter/fold/window/selection/pins | transient TypeScript `CausalTraceController` |

The trace selector consumes a canonical snapshot and revision. The projection
engine caches only the result for that same immutable input. Reconciliation
retains identity/digest/phase fingerprints, not event payloads. Tests assert
that the selector and engine share the canonical state, and closing the viewer
does not change task/worker/session state. There is no second replay store,
IndexedDB database, API persistence route or checkpoint writer.

## Typed causality, completeness and critical path

The index covers task, run, session, worker, tool, terminal/browser action,
artifact and mutation domains plus permission, compact/checkpoint,
route/placement/provider retry, fault/recovery, MCP reconnect, skill execution
and subagent spawn/yield/finalization mechanisms.

Causal edges are produced from exact canonical predecessor, causation, span,
tool call, action/result, worker attempt, checkpoint, artifact, mutation,
provider request, MCP request and subagent parent/yield bindings. Equal labels,
summaries, URLs, terminal text or filenames do not join. A test gives distinct
records identical labels and proves that no edge is created.

Missing spans/causes, incomplete partial chains, late facts, orphan parents,
identity conflicts and quarantined records remain explicit diagnostics. Late
finals and artifacts reconcile by event/correlation identity and do not create
duplicate semantic nodes. Reconnect epochs remain distinct.

The critical-path algorithm first computes strongly connected components, then
uses a deterministic condensed DAG. It exposes alternative equal-cost paths,
bottlenecks, retry/permission/recovery contribution and latency attribution;
cycles therefore cannot make the result completion-order dependent.

## Cross-view navigation and local reporting

- Terminal focus requires task, terminal and frame/cursor identity.
- Browser focus requires task, browser session and action/target identity.
- Artifact and diff focus require task, artifact/revision and optional
  file/hunk identity.
- Timeline focus requires canonical event/span/tool identity.
- Topology focus requires graph revision and node/edge/worker identity.
- Unavailable view, missing target, timeout, stale binding and rejected focus
  settle as visible navigation failures; no guessed selector or alternate
  transport exists.
- `Alt+Enter` on a focused typed view row returns to the exact trace entity or
  span. The event delegation reads typed attributes, not rendered labels.
- Pins are bounded local references. Markdown report output names exact entity,
  event/span and artifact/mutation references and reports stale rebasing.

## Numeric-stage regression finding and repair

The first exact cleanroom aggregate found a protected M2-S03B-01 regression:
`task.terminals.ticket` was declared as a mutation with `receipt: none`.
`ProtocolCatalog.assertComplete()` consequently rejected construction of the
production `ZyraApiClient`. This was not caused by the trace implementation,
but M2-03 could not close while its default API client was unusable.

The bounded repair in `2bc9604`:

1. retains `TerminalSessionRegistry` and `TerminalTicketAuthority` as canonical
   owners;
2. declares the ticket endpoint as the existing terminal receipt contract;
3. returns a typed issuance receipt containing scope, cursor, expiry,
   correlation/causation and SHA-256 of the ticket, never the secret ticket;
4. adds unit and real-platform assertions;
5. reruns the typed client, terminal runtime, full Web and M2-03 cleanroom
   suites.

No permission, process, session or ticket owner moved. No fallback was added.

## Behavior and cleanroom evidence

Main implementation checkout:

| Command or suite | Result |
| --- | --- |
| causal trace Web behavior | `12 passed`, `118 assertions` |
| causal trace real scheduler/fault/recovery bridge | `1 passed` |
| 11-file adjacent Web suite | `145 passed`, `1,006 assertions` |
| related Python API/runtime suite | `8 passed` |
| typed client + terminal after receipt repair | `32 passed`, `152 assertions` |
| Python typed-client/terminal repair suite | `32 passed` |
| `bun run typecheck:web` | passed |
| `bun run build:web` | passed |

Exact `git archive` cleanroom for `2bc9604` outside the Zyra workspace root:

| Check | Result |
| --- | --- |
| frozen `bun@1.2.15 install --frozen-lockfile` | local cleanroom `node_modules/.bin/bun.exe` exists; locked install passed |
| all `apps/web/test` | `187 passed`, `0 failed`, `1,208 assertions` |
| production typecheck/build | passed; `243` bundled modules |
| 15 applicable Python files, process-isolated | `78 passed`, `0 failed` |
| manifest/link/editable/absolute parent-source scan | `0` external path dependencies |
| slice runtime source-name scan | `0` source-repository runtime references |
| slice OpenClaw scan | `0` matches |

The first Python cleanroom command used pytest's inaccessible user temp root;
moving `--basetemp` into the cleanroom changed `1 failed / 10 passed / 45 setup
errors` into `1 failed / 55 passed`, exposing the real terminal contract issue.
After repair, a cleanroom nested under `zyra/.tmp` was deliberately rejected
because Bun treated it as a parent workspace. The final archive was expanded to
`G:/agent-zoo/.tmp`, installed locally, and verified there. Python files were
then run in separate processes because several pre-existing suites mutate
process environment/singletons and fail when arbitrarily concatenated; every
same-file suite passed in isolation.

## Effective-code and anti-padding audit

The frozen TypeScript AST/scanner classifier reports:

| Bucket | Lines | Credit |
| --- | ---: | --- |
| executable TypeScript production runtime | 5,743 | yes |
| active React UI behavior | 161 | yes |
| UI/CSS/static JSX presentation | 663 | no |
| type/interface/declaration only | 1,034 | no |
| schema/DTO/static data | 104 | no |
| Python canonical-owner receipt repair | 17 | real production, excluded from TS/React floor |
| tests, fixtures and integration probe | 1,471 | no |
| docs/comments/blanks | 196 | no |
| adapter only | 0 | no |
| vendor/source pool | 0 | no |
| **conservative effective TypeScript/React** | **5,904** | **pass** |

Raw interval totals reconcile to `9,389` additions. The audit command is:

`bun scripts/audit_m2_s03b_03_effective_lines.mjs
--baseline=ca1d1b1eee0ef04451cc619ddc2b3f944f30f99e
--target=2bc9604685cd7024825cf94a34c3fc507921d661
--minimum=5500 --summary`.

Large/concentrated files received explicit review:

| File | Raw/effective | Judgment |
| --- | ---: | --- |
| `index/builder.ts` | 1,340 / 1,228 | mounted projection builder; exact admission and all forward/reverse mechanism indexes; 100 declaration and 12 blank/comment lines excluded; 20.8% concentration reviewed and accepted because splitting would sever one atomic index invariant |
| `navigation/runtime.ts` | 854 / 780 | all typed focus ports, availability/failure settlement and reverse navigation; 70 declarations and 4 blanks excluded |
| `analysis/query.ts` | 695 / 618 | indexed qualifiers/search and diagnostics consumed by controller; 69 declarations and 8 blanks excluded |
| `contracts.ts` | 564 / 14 | 444 declarations and 104 schema/DTO lines excluded; only executable frozen helpers count |
| `analysis/critical-path.ts` | 546 / 497 | SCC condensation, deterministic DAG/alternatives/contribution analysis; 42 declarations and 7 blanks excluded |
| `identity.ts` | 529 / 503 | exact identity normalization, scope checks and typed reference construction; 20 declarations and 6 blanks excluded |
| `view/trace-workbench.tsx` | 541 / 161 | only handlers/effects/composition count; 356 presentation, 23 declarations and 1 blank excluded |
| causal trace Web test | 1,018 / 0 | direct behavior evidence only |

CSS, barrel exports, typed destination attributes, Python owner repair, tests,
probe and docs receive zero TS/React floor credit. No counted file is ledger,
generated data, adapter-only, fixture-only, vendor-shaped or source-pool code.

## Parent and numeric-stage closure

Direct frozen classifier runs, not arithmetic carry-forward:

| Scope | Interval | Raw + / - | Effective | Minimum | Result |
| --- | --- | ---: | ---: | ---: | --- |
| current slice | `ca1d1b1..2bc9604` | 9,389 / 4 | 5,904 | 5,500 | pass |
| M2-03B | `cc92c129..2bc9604` | 43,825 / 24 | 21,152 | 17,000 | pass |
| M2-03 | `1833319a..2bc9604` | 80,786 / 142 | 40,309 | 32,000 | pass |

M2-03 source-to-target audit selected only the five executable slice owners,
not the legacy parent-plan inventory rows:

- 20 unique ledger entries;
- 14 production entries and 70 production target bindings;
- 6 conformance/reference entries and 12 non-production bindings;
- 0 missing target at `2bc9604`;
- 0 OpenClaw entries;
- source-role caps respected in every slice.

Current slice synchronizer reports 3 entries and 0 missing targets. The focused
ledger contract suite passes `6/6`. The generic legacy verifier remains
non-green because it reads protected `tmp/internalization_ledger.json` and
reports three pre-existing negative-audit string literals in
`long_horizon_runtime.py`; the broader ledger unit suite also retains two
protected baseline assumptions. Neither finding is introduced by or points at
an M2-S03B-03 row or target, so no green generic-ledger claim is made.

## Dependency, packaging and forward-exclusion audit

- No package manifest or lockfile changed in the slice.
- No new dependency, npm link, pip editable path, Docker context, dynamic
  import, external CLI/process, local port, MCP server or plugin was added.
- Final cleanroom manifests contain no `file:`, `link:`, editable or absolute
  parent-source dependency.
- Production trace and receipt paths contain no source-repository name or
  `../source-repo` lookup.
- The trace uses existing typed API and canonical projection paths only.
- OpenClaw was neither restored nor referenced; the source graph and runtime
  dependency count remain zero.

## Competition evidence and remaining gates

This slice advances, but does not by itself close, the requirement matrix:

| Requirement | Evidence advanced | Still required later |
| --- | --- | --- |
| `REQ-TRACE-01` | one typed cross-domain causal index, critical path, diagnostics and six bidirectional view ports | formal live-task capture and submission material |
| `REQ-FAULT-01` | real scheduler fault/recovery chain reaches the cross-view projection | full fault matrix and repeated live MTTR evidence |
| `REQ-TOPO-01` / `REQ-EDGE-01` | route/placement/provider facts are inspectable and focus topology | formal sparse/full/static ablation and real local/edge/cloud dispatch |
| `REQ-CLOSE-01` | existing sealed denial/recovery facts remain typed and searchable; viewer adds no human wait | one sealed run with at least 2,000 effective canonical transitions |
| `SCORE-UX` | task-mounted searchable/foldable/virtualized trace and typed jumps | milestone visual capture/live demo |
| `SCORE-ROBUST` | missing/late/orphan/quarantine/reconnect/disable behavior | repeated live recovery runs |

There are no remaining blockers for this slice, M2-03B or the M2-03 numeric
stage. Milestone-level live tasks, 2,000-transition sealed run, real three-tier
dispatch, multi-model/provider evidence, ablations, visual captures and final
packaging remain owned by later M2/M3 exit gates and are not falsely closed.

## Commit and repository boundary

The evidence commit contains this review, machine evidence, effective-line
auditor, source-ledger synchronizer and synchronized seed. Root
`G:/agent-zoo/docs/milestones/execution-state.yaml` is updated only after that
commit exists. `G:/agent-zoo` is not the Zyra Git repository, so the root state
update cannot be included in the Zyra evidence commit and must be reported
separately.
