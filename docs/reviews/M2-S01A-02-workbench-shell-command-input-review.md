# M2-S01A-02 Workbench Shell And Command Input Review

Date: 2026-07-23  
Verdict: `PASS`

## 1. Frozen commit boundary

- `baseline_commit`: `3f51da450bd65fa1f20e53613e1b5bc791b811fb`
- `parent_baseline_commit`: `f53bf78287a2b2a6eec74102e7218d664307c137`
- `preimplementation_decision_commit`:
  `e38210aca806d4273cfbfa58c2cbdc1e5cb4d6da`
- `implementation_commit`: `4b7f5f8bd5bc3e2819921bc62d27daf36ed8e682`
- `evidence_commit`: `this_commit`
- slice line-count interval:
  `3f51da450bd65fa1f20e53613e1b5bc791b811fb..4b7f5f8bd5bc3e2819921bc62d27daf36ed8e682`
- parent line-count interval:
  `f53bf78287a2b2a6eec74102e7218d664307c137..4b7f5f8bd5bc3e2819921bc62d27daf36ed8e682`

Production code and directly related tests are frozen in the implementation
commit. This review, its JSON evidence, exact line auditor and ledger
synchronizer are evidence-only and cannot inflate either interval.

## 2. Source, language and migration decision

The prospective source decision is
`docs/plans/M2-S01A-02-workbench-shell-command-input-preimplementation.md`.
It was committed before production changes. No source-role reassignment,
cross-language exception or post-implementation waiver was used.

| Role | Source / pinned commit | Source language | Target language | Migration mode | Slice effective production | Responsibility |
| --- | --- | --- | --- | --- | ---: | --- |
| primary | OpenCode `adf178a6b95c61506ddaadaf4dd062badb4a8fda` bounded layout, session-route and PromptInput modules | TypeScript / TSX | TypeScript / TSX | `same_language_crop_and_adapt` | 1,743 | route/layout/task navigation and the base command surface |
| supplementary | `claude-code-best` `c57f5a29aa7012308252cf739f4f8c3430a1c034` REPL, PromptInput, queue and submission modules | TypeScript / TSX | TypeScript / TSX | `same_language_crop_and_adapt` | 3,666 | command parse/completion/history/draft/queue, overlay, focus and guarded submission |
| supplementary | OpenHands `c105a82387898e744423c8831d412e26495b38a9` bounded frontend route/lifecycle modules | TypeScript / TSX | TypeScript / TSX | `same_language_bounded_integration` | 1,244 | loading/empty/error/reconnect, task lifecycle presentation and route cleanup |
| existing Zyra owner | Zyra web serving boundary | Python | Python | `same_language_extend` | 21 | production bundle serving and SPA deep-route fallback only |
| primary foundation | M2-S01A-01 typed client | TypeScript | unchanged | `same_language_integrate_existing` | 0 new lines attributed here | the sole browser transport and typed lifecycle boundary |
| conformance | prior OMP correlation/cancel contract | TypeScript | tests only | `conformance_only` | 0 | cancel/coalescing/fail-closed oracle; no runtime owner |

The slice prose described the OpenHands lifecycle supplement generically as
Python, while the actual source bound to this browser target is OpenHands'
TypeScript frontend route and conversation-service code. The prospective
decision pinned those real files and the same-language mode before
implementation. No OpenHands Python runtime or second frontend store was
migrated. OpenClaw was not read, restored or assigned a role.

OpenCode Solid contexts/session stores and SDK calls were removed. Claude
query/session/tool state, Ink rendering and source command registry were
removed. OpenHands Axios/query cache and conversation state were removed. The
remaining mechanisms were decomposed into Zyra modules, Zyra request/error
types, the existing typed transport and behavior tests.

## 3. Internalized default path and state custody

The default production path is:

`apps/web/src/main.tsx -> WorkbenchApp -> WorkbenchController /
CommandCoordinator -> createZyraApi -> M2-S01A-01 ZyraApiClient ->
ZyraRequestHandler -> existing M1 canonical owners`.

The implementation provides:

- SPA list/detail/new/settings/recovery routing with normalized untrusted task
  IDs, pop-state handling and deep-link fallback;
- desktop split panes, bounded resizer state and a responsive mobile layout;
- explicit loading, empty, not-found, error, reconnecting, retry and readiness
  states without sample-task fallback;
- create/list/detail/cancel/resume lifecycle actions through the sole typed API;
- slash suggestions, argument hints, quoted parsing, execution policy,
  remote-safe/disabled states and deterministic keyboard behavior;
- bounded history and draft capture/restore, priority-plus-FIFO queued preview,
  editable queued items, busy-input handling and duplicate-submit coalescing;
- local overlay, focus trap/restore, notification and screen-reader live-region
  lifecycles;
- bounded large-list windows, task query/sort, plan hierarchy, task metrics and
  action policy.

| State | Canonical owner | What this slice owns |
| --- | --- | --- |
| task / run / plan | existing `SQLiteStore` / `TaskState` | typed projection and local selection only |
| event history | existing `EventLog` | no event reducer or durable event cache |
| artifacts | existing `LocalArtifactStore` | no new artifact owner |
| create/cancel/resume receipts | existing M2-S01A-01 typed transport and M1 control runtimes | guarded invocation and rendering of returned projection |
| route / pane / focus / overlay | workbench shell | local browser state |
| draft / history / queued preview | command runtime | bounded local editing state; never canonical task state |
| request phase / reconnect timer | `WorkbenchController` | transient request lifecycle only |

`applyMutation` accepts only a typed API-returned task projection. Local overlay
commands cannot mutate a task. An unknown or unavailable remote command is
disabled by the execution policy. Disabling the typed client, workbench
controller or command coordinator fails visibly and has no fabricated-success
fallback.

## 4. Behavior, failure, build and start evidence

All commands below were run against the frozen implementation.

| Command / probe | Result |
| --- | --- |
| `bun run typecheck:web` | PASS |
| `bun run test:web` | PASS; 46 tests, 0 failures, 140 assertions |
| `bun run build:web` | PASS; 74 modules, 1.31 MB JS and 27.79 KB CSS |
| `python -m pytest -q -p no:cacheprovider tests/integration/test_m2_typed_api_client_transport.py tests/unit/test_web_console_static.py` | PASS; 3 tests in 21.88 s |
| hidden `scripts/dev_web.py` smoke on port 5175 | PASS; `/` 200, `/tasks/task_demo_001` 200, JS 200, CSS 200, old landing absent; process stopped |
| `bun scripts/audit_m2_s01a_02_effective_lines.mjs --summary` | PASS; 6,674 effective, minimum 6,000 |
| `bun scripts/audit_m2_s01a_02_effective_lines.mjs --parent --summary` | PASS; 13,625 effective, parent minimum 12,000 |
| `python scripts/sync_m2_workbench_source_ledger.py --check` | PASS; exactly 3 aligned source decisions |

The tests exercise normalized route changes, invalid identities, list/detail
loading and supersession, retryable failure, disabled data source, task
projection application, parser and cursor replacement ranges, command
availability, multiline input, history boundaries, draft restore, priority/FIFO
queue order, editable preview, busy enqueue, double-submit coalescing, error
restore, local-only overlay, real cancel/resume method calls, coordinator
disable, 2,000-item list windows, immutable query/sort, plan hierarchy,
responsive panes, bounded retry, overlay snapshots, focus return and accessible
request states.

The adjacent real transport integration proves the browser foundation still
crosses the single typed client into a real embedded API/store. A source scan
finds zero direct `fetch(` calls outside the existing typed client boundary and
zero parent-repository runtime paths in current Web/build/serve inputs.

## 5. Per-file effective-code audit

The evidence script obtains exact Git added-line sets. TypeScript compiler AST
classification excludes imports, interfaces, type aliases, export-only
declarations and bodyless signatures. Python AST classification excludes
imports, docstrings and class schema fields. Tests, fixtures, manifests,
lockfiles, docs, CSS and static HTML are excluded. Evidence-only ledger
synchronizers and prior audit fixtures are explicitly excluded in the direct
parent recomputation.

`Runtime + UI behavior` is the effective production count.

| File | Raw | Runtime | UI behavior | Presentation | Type/decl | Schema/data | Adapter | Generated | Test/fixture | Docs/comments | Effective |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `apps/web/index.html` | 6 | 0 | 0 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `apps/web/package.json` | 10 | 0 | 0 | 0 | 0 | 10 | 0 | 0 | 0 | 0 | 0 |
| `apps/web/src/app/hooks.ts` | 74 | 0 | 63 | 0 | 3 | 0 | 0 | 0 | 0 | 8 | 63 |
| `apps/web/src/app/runtime.ts` | 138 | 0 | 104 | 0 | 32 | 0 | 0 | 0 | 0 | 2 | 104 |
| `apps/web/src/app/workbench-app.tsx` | 297 | 0 | 277 | 0 | 16 | 0 | 0 | 0 | 0 | 4 | 277 |
| `apps/web/src/command/argument-completion.ts` | 125 | 0 | 111 | 0 | 12 | 0 | 0 | 0 | 0 | 2 | 111 |
| `apps/web/src/command/catalog.ts` | 375 | 0 | 323 | 0 | 47 | 0 | 0 | 0 | 0 | 5 | 323 |
| `apps/web/src/command/coordinator.ts` | 600 | 0 | 536 | 0 | 62 | 0 | 0 | 0 | 0 | 2 | 536 |
| `apps/web/src/command/draft-store.ts` | 307 | 0 | 284 | 0 | 22 | 0 | 0 | 0 | 0 | 1 | 284 |
| `apps/web/src/command/execution-policy.ts` | 147 | 0 | 104 | 0 | 37 | 0 | 0 | 0 | 0 | 6 | 104 |
| `apps/web/src/command/history.ts` | 201 | 0 | 179 | 0 | 20 | 0 | 0 | 0 | 0 | 2 | 179 |
| `apps/web/src/command/keyboard.ts` | 129 | 0 | 88 | 0 | 35 | 0 | 0 | 0 | 0 | 6 | 88 |
| `apps/web/src/command/parser.ts` | 301 | 0 | 259 | 0 | 39 | 0 | 0 | 0 | 0 | 3 | 259 |
| `apps/web/src/command/queue.ts` | 376 | 0 | 335 | 0 | 39 | 0 | 0 | 0 | 0 | 2 | 335 |
| `apps/web/src/components/command-input/command-input.tsx` | 499 | 0 | 475 | 0 | 21 | 0 | 0 | 0 | 0 | 3 | 475 |
| `apps/web/src/components/layout/pane-divider.tsx` | 54 | 0 | 51 | 0 | 2 | 0 | 0 | 0 | 0 | 1 | 51 |
| `apps/web/src/components/overlays/overlay-host.tsx` | 294 | 0 | 284 | 0 | 6 | 0 | 0 | 0 | 0 | 4 | 284 |
| `apps/web/src/components/status/notification-tray.tsx` | 41 | 0 | 38 | 0 | 2 | 0 | 0 | 0 | 0 | 1 | 38 |
| `apps/web/src/components/status/request-state.tsx` | 102 | 0 | 96 | 0 | 2 | 0 | 0 | 0 | 0 | 4 | 96 |
| `apps/web/src/components/tasks/task-detail.tsx` | 290 | 0 | 276 | 0 | 12 | 0 | 0 | 0 | 0 | 2 | 276 |
| `apps/web/src/components/tasks/task-list.tsx` | 273 | 0 | 260 | 0 | 10 | 0 | 0 | 0 | 0 | 3 | 260 |
| `apps/web/src/main.ts` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `apps/web/src/main.tsx` | 11 | 0 | 6 | 0 | 3 | 0 | 0 | 0 | 0 | 2 | 6 |
| `apps/web/src/shell/accessibility-announcer.ts` | 87 | 0 | 69 | 0 | 14 | 0 | 0 | 0 | 0 | 4 | 69 |
| `apps/web/src/shell/collection-window.ts` | 125 | 0 | 99 | 0 | 17 | 0 | 0 | 0 | 0 | 9 | 99 |
| `apps/web/src/shell/focus-manager.ts` | 170 | 0 | 145 | 0 | 21 | 0 | 0 | 0 | 0 | 4 | 145 |
| `apps/web/src/shell/focus-trap.ts` | 101 | 0 | 86 | 0 | 5 | 0 | 0 | 0 | 0 | 10 | 86 |
| `apps/web/src/shell/layout-runtime.ts` | 218 | 0 | 173 | 0 | 23 | 0 | 0 | 0 | 0 | 22 | 173 |
| `apps/web/src/shell/notification-center.ts` | 178 | 0 | 158 | 0 | 19 | 0 | 0 | 0 | 0 | 1 | 158 |
| `apps/web/src/shell/overlay-runtime.ts` | 229 | 0 | 192 | 0 | 36 | 0 | 0 | 0 | 0 | 1 | 192 |
| `apps/web/src/shell/retry-supervisor.ts` | 109 | 0 | 82 | 0 | 18 | 0 | 0 | 0 | 0 | 9 | 82 |
| `apps/web/src/shell/route-loader.ts` | 221 | 0 | 192 | 0 | 22 | 0 | 0 | 0 | 0 | 7 | 192 |
| `apps/web/src/shell/router.ts` | 365 | 0 | 321 | 0 | 42 | 0 | 0 | 0 | 0 | 2 | 321 |
| `apps/web/src/shell/task-action-policy.ts` | 102 | 0 | 89 | 0 | 10 | 0 | 0 | 0 | 0 | 3 | 89 |
| `apps/web/src/shell/task-metrics.ts` | 107 | 0 | 87 | 0 | 16 | 0 | 0 | 0 | 0 | 4 | 87 |
| `apps/web/src/shell/task-query.ts` | 89 | 0 | 73 | 0 | 12 | 0 | 0 | 0 | 0 | 4 | 73 |
| `apps/web/src/shell/task-tree.ts` | 171 | 0 | 140 | 0 | 22 | 0 | 0 | 0 | 0 | 9 | 140 |
| `apps/web/src/shell/workbench-controller.ts` | 666 | 0 | 598 | 0 | 63 | 0 | 0 | 0 | 0 | 5 | 598 |
| `apps/web/src/styles.css` | 1,616 | 0 | 0 | 1,616 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `apps/web/test/workbench-shell.test.tsx` | 784 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 784 | 0 | 0 |
| `apps/web/tsconfig.json` | 3 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 |
| `bun.lock` | 20 | 0 | 0 | 0 | 0 | 20 | 0 | 0 | 0 | 0 | 0 |
| `docs/plans/M2-S01A-02-workbench-shell-command-input-preimplementation.md` | 93 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 93 | 0 |
| `package.json` | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| `scripts/dev_web.py` | 23 | 21 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 21 |
| `tests/unit/test_web_console_static.py` | 24 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 24 | 0 | 0 |
| **Total** | **10,152** | **21** | **6,653** | **1,622** | **762** | **34** | **0** | **0** | **808** | **252** | **6,674** |

## 6. Large-file and high-ratio review

No file contributes more than 20% of 6,674 effective lines; the threshold is
1,334.8.

| File / trigger | Upstream and executable symbols | Default path / state, error and permission responsibility | Disable/mutation proof | Raw/effective delta |
| --- | --- | --- | --- | --- |
| `command/coordinator.ts`, raw >500 | Claude queue/submission path; `CommandCoordinator.submit`, `drain`, `cancelActive`, `disable`, `enable`, `close` | `WorkbenchApp -> CommandInput -> submit -> execution policy -> local overlay or typed task API`; owns transient submissions and fail-closed availability only | double-submit, busy queue, failed draft restore, local overlay, disable and cancel/resume tests | 600 raw / 536 effective; 62 declarations and 2 comment/blank lines excluded |
| `shell/workbench-controller.ts`, raw >500 | OpenHands lifecycle plus OpenCode route handoff; `bootstrap`, `refreshRuntime`, `refreshTasks`, `loadTask`, `applyMutation`, `cancelRequest`, `disable` | `WorkbenchApp -> route loader -> controller -> TaskApi`; owns transient phases, cancellation and projection selection, never canonical task truth | load/supersede/error, disable-no-request and typed projection mutation tests | 666 raw / 598 effective; 63 declarations and 5 comment/blank lines excluded |
| `styles.css`, raw >500 | no upstream executable symbol | presentation only; no state/error/permission owner | production build and DOM/a11y behavior tests; no production credit claimed | 1,616 raw / 0 effective; all presentation |
| `workbench-shell.test.tsx`, raw >500 | behavior test harness only | no production owner | its 31 tests are evidence, not production | 784 raw / 0 effective; all test/fixture |
| `apps/web/package.json`, >30% schema/data | dependency/build metadata | no runtime owner claimed | frozen-lock cleanroom install/build/test | 10 raw / 0 effective; all schema/data |
| `apps/web/tsconfig.json`, >30% schema/data | TypeScript build metadata | no runtime owner claimed | cleanroom typecheck | 3 raw / 0 effective; all schema/data |
| `bun.lock`, >30% schema/data | locked dependency graph | no runtime owner claimed | `bun install --frozen-lockfile` | 20 raw / 0 effective; all schema/data |
| root `package.json`, >30% schema/data | workspace command metadata | no runtime owner claimed | cleanroom root scripts | 1 raw / 0 effective; all schema/data |

## 7. Parent M2-01A cumulative closure

The parent result is a direct audit from the parent baseline, not a sum of child
reports:

| Bucket | Direct parent count |
| --- | ---: |
| raw additions | 21,130 |
| production runtime | 6,217 |
| UI behavior | 7,408 |
| UI presentation excluded | 1,626 |
| type/declaration excluded | 1,816 |
| schema/DTO/data excluded | 985 |
| adapter-only excluded | 0 |
| generated excluded | 272 |
| test/mock/fixture excluded | 1,607 |
| docs/comments/evidence excluded | 1,199 |
| **effective production** | **13,625** |
| required minimum | 12,000 |

Direct parent source/language attribution is:

| Source role / language | Effective production |
| --- | ---: |
| OpenCode primary TypeScript/TSX | 7,546 |
| Claude supplementary TypeScript/TSX | 3,666 |
| OpenHands supplementary TypeScript/TSX | 1,666 |
| existing Zyra Python owner extension | 747 |
| **Total** | **13,625** |

M2-S01A-01 supplies the sole typed transport, request correlation,
auth/version/error and real API lifecycle foundation. This slice replaces its
minimal diagnostic entry with the production workbench while preserving that
transport boundary. The parent now has one default start path, one client,
real lifecycle controls, failure/reconnect behavior, accessibility, responsive
layout, command queue semantics and direct parent budget coverage.

## 8. Ledger, dependency and high-risk cleanroom audit

The bundled ledger contains exactly three `M2-S01A-02` decisions: OpenCode
primary, Claude supplementary and OpenHands supplementary. Direct strict audit
filtering reports zero slice findings and zero slice errors. The full historical
seed still reports three unrelated baseline blockers and 695 warnings; therefore
its global disposition remains `blocked` even though this slice's filtered
finding set is empty. No historical finding is hidden or reclassified here.

React 19 and ReactDOM 19 are new external dependencies on the default Web path,
so this slice triggers the matching high-risk dependency/cleanroom escalation.
A clean tree was exported from the frozen implementation commit to
`.tmp/m2-s01a-02-cleanroom-4b7f5f8`; `bun install --frozen-lockfile` installed
six locked packages, then `typecheck:web`, all 46 Web/transport tests and
`build:web` passed. This proves the implementation does not require the current
`node_modules`, build artifacts, caches or a parent source tree.

The current slice inputs have:

- zero `../claude-code-best`, `../opencode`, `../OpenHands`,
  `../browser-use` or OpenClaw runtime paths;
- zero npm links, `file:..` dependencies or editable parent installs;
- zero direct `fetch` calls outside the prior typed-client owner;
- no new MCP server, Docker context, dynamic import, persistent process or
  canonical store;
- no canonical state-owner transfer, incompatible persistence migration or
  global permission/scheduler/recovery/compact policy change.

The hidden HTTP smoke process was stopped and port 5174/5175 had no retained
listener after verification.

## 9. Critical self-review and residual scope

- Browser refresh/deep-link serving is proved by the SPA HTTP smoke and route
  normalization tests. The in-app browser connector had no available browser
  instance in this session, so screenshot-level visual inspection was not
  claimed. DOM semantics, focus primitives, keyboard behavior, live-region
  states, responsive layout calculations and the production bundle were
  verified programmatically.
- The controller keeps a transient task projection so the shell can render and
  select rows. It cannot commit task/run/event state and is overwritten by
  typed API responses. M2-S01B remains the owner of durable event ingestion,
  reducers and exact cursor recovery.
- Queued preview is local editing state. Dispatch still enters the one command
  coordinator and typed lifecycle path; closing the browser cannot cancel a
  backend task unless the user explicitly invokes cancel.
- Remote-safe is a frontend availability constraint, not a replacement for
  backend permission enforcement.
- A broader scenario suite currently imports the removed legacy Python
  `parse_slash_command` landing-page helper. That protected baseline mismatch is
  unrelated to this React/TypeScript shell and was not repaired by reviving the
  obsolete second command path. The directly affected static Web test and all
  new command behavior tests pass.
- This slice advances `REQ-CLOSE-01`, `REQ-TRACE-01` and `SCORE-UX`; it does not
  by itself close the competition's live multi-domain, 2,000-transition,
  dynamic-topology or M2 exit gates.

Result: `M2-S01A-02` satisfies its source/language, default-path, behavior,
failure, accessibility, cleanroom, evidence and 6,000-line gates.
`M2-01A` closes at 13,625 directly audited effective lines. The next execution
slice is `M2-S01B-01`.
