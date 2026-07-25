# M2-S04A-01 Command Input And Queue Control Self-Review

Date: 2026-07-25
Verdict: `PASS`

## 1. Frozen boundary

- baseline: `53b002bac1e97eab23b7553d344da068dd8dd3c9`
- prospective source decision:
  `3264cc479dd30bb3b0829c7f1f803c06a5d1f87e`
- implementation:
  `6e365ea1e5a28619a052d5d0f8d6a5ccd880f976`
- evidence: `this_commit`
- line interval:
  `53b002bac1e97eab23b7553d344da068dd8dd3c9..6e365ea1e5a28619a052d5d0f8d6a5ccd880f976`

Production code and directly related tests are frozen in the implementation
commit. The source-ledger synchronizer, exact line audit, generated audit JSON
and this review are evidence-only and receive no implementation credit.

## 2. Source and migration decision

The committed prospective decision is
`docs/plans/M2-S04A-01-command-input-queue-control-preimplementation-decision.md`.
No source role was reassigned after implementation.

| Role | Pinned source | Language / mode | Bounded responsibility |
| --- | --- | --- | --- |
| primary | `claude-code-best@c57f5a29e88e9a814bea47abeb9a0a6f725dc102` | TypeScript to TypeScript; retained control flow, cropped and adapted | PromptInput parsing/submission, guarded busy-run enqueue, queue lifecycle, result/history lifecycle and tool-disabled side question |
| supplementary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | TypeScript to TypeScript; cropped palette/settlement integration | stable command registry, palette ranking, typed receipt/result projection and diagnostics |
| conformance | `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | tests only | reconnect/auth/replay negative cases |
| conformance | `agentscope@b6698c5dbaa1aa916925e27402767f45e2405fa4` | tests only | session interaction, isolation and queue wakeup expectations |
| conformance | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | tests only | typed receipt, correlation and cancellation expectations |

Claude query/session/tool owners and Ink rendering were not copied. OpenCode
stores, provider/session runtime and SDK calls were not copied. The retained
mechanisms were decomposed into the formal `@zyra/commands` package, the Web
command surface, typed client/API routes and Zyra behavior tests. There is one
primary and one supplementary implementation source, so the per-domain source
cap is respected. OpenClaw was not restored, read or assigned any role.

## 3. Default path and state custody

The real path is:

`PromptInput -> CommandSurfaceRuntime -> @zyra/commands parser/policy/
coordinator -> TaskApi -> typed Zyra client -> M1 RuntimeControlDispatcher /
PromptQueueRuntime -> event log and receipt -> M2-01B
CanonicalProjectionStore -> CommandProjectionIndex -> queue panel / result
overlay`.

The exact command surface is `/status`, `/graph`, `/trace`, `/artifacts`,
`/permissions`, `/btw`, `/inject`, `/change`, `/verify`, `/eval` and `/doctor`.
These names are intercepted before ordinary prompt/task submission. An unknown,
malformed, disabled or unavailable control is rejected and cannot fall through
to normal chat.

| State | Canonical owner | This slice's custody |
| --- | --- | --- |
| command request, idempotency and terminal receipt | M1 `RuntimeControlDispatcher` plus `ControlRequestStore` | typed construction, validation and projection only |
| busy-run queue, priority/FIFO order, cancellation and retry source | M1 `PromptQueueRuntime` | backend snapshot merge, preview and user actions |
| committed command/event state | M1 event log and state owners | reversible command/event/span/mutation/checkpoint/artifact lookup |
| browser committed projection | M2-01B `CanonicalProjectionStore` | external-store subscription and command-specific derived index |
| draft, completion selection and command history | `CommandInputEngine`, `CommandPalette`, bounded history | transient browser state only |
| result overlay | `CommandResultStore` and existing overlay host | local lifecycle only; no durable receipt or queue truth |
| `/btw` answer | `SideQuestionRuntime` | single tool-free turn, usage/error/close record; no main session/context mutation |

The GET queue route reads `PromptQueueRuntime` entries. Cancel delegates to the
existing runtime dispatcher. Retry reconstructs a typed request from canonical
arguments and records `retryOf`; it does not fabricate or mutate a queue entry
in the browser. Reconnect first restores the backend snapshot and then
reconciles M2-01B projections. Disabling the dispatcher returns HTTP 503 with
`fallback:false`; disabling the Web coordinator fails before transport.

The older M2-S01A local prompt queue continues to serve ordinary task prompts
only. It is not consulted for the eleven control commands and is not a second
control-command queue.

## 4. Required behavior

| Behavior | Evidence |
| --- | --- |
| exact commands, stable identities, descriptions, scopes and modes | registry test asserts exactly eleven commands |
| typed arguments, quoted/escaped input, JSON/identity/duration/boolean/integer validation | tokenizer/binder/parser tests |
| malformed or unknown controls never become normal chat | parser negative test and real HTTP malformed-delivery 400 |
| incremental palette, command/flag/value suggestions and disabled entries | registry/palette/completion tests |
| Shift+Enter newline, Alt+Enter steer, Ctrl/Meta+Alt+Enter interrupt, Escape cancel/close | input-engine and Web key-path tests |
| queued/running/applied/rejected/expired/cancelled receipts with scope, mode, priority, idempotency and error | receipt ledger and result tests |
| busy-run enqueue, priority/FIFO, reconnect restore and projection reconciliation | queue, recovery and real HTTP integration |
| preview cancel and retry with duplicate suppression and retry linkage | action and integration tests |
| command-to-event/state/span/checkpoint/artifact reverse correlation | correlation and projection-index tests |
| command-specific graph/trace/artifact/permission/check result presentation | result-model tests and semantic overlay rendering |
| `/btw` one tool-free turn, usage/error/close and main-session isolation | side-question unit test and real API isolation test |
| disabled command runtime fails closed | coordinator unit test and dispatcher HTTP 503 test |

The disconnect proof is direct: disabling `CommandCoordinator` prevents
transport; disabling `RuntimeControlDispatcher` prevents API settlement; a
queue response without canonical backend/projection/receipt evidence produces
no queue item; and a missing projection subscription prevents 01B causal
updates from appearing in the command surface. These are semantic failures,
not health-only probes.

## 5. Verification

All passing commands target the frozen implementation:

| Command | Result |
| --- | --- |
| `npx --yes bun@1.2.15 run typecheck:web` | PASS |
| `npx --yes bun@1.2.15 test ./packages/commands/test/commands.test.ts` | PASS; 26 tests, 108 assertions |
| `python -m pytest -q tests/integration/test_command_input_queue_control.py` | PASS; 2 real in-process HTTP tests |
| adjacent command/client/workbench/projection Bun suites | PASS; 84 tests, 392 assertions |
| `npx --yes bun@1.2.15 run test:web` | PASS; 205 tests, 1,257 assertions |
| `npx --yes bun@1.2.15 run build:web` | PASS; 268 modules |
| Python compile for API, integration test and ledger synchronizer | PASS |
| line audit | PASS; 8,374 effective versus 7,500 required |
| ledger synchronizer `--check` | PASS; 5 decisions, 5 entries, 0 missing targets |
| `git diff --check` and runtime source-path scans | PASS |

One broader API/symbolic command timed out after 184 seconds. A focused rerun
passed the directly affected `/change` case and all six symbolic-control cases;
three unrelated cases failed on the existing E02 MCP configuration snapshot
digest (`/skills`, legacy `/help`, `artifact_write`).

The existing ledger suites were also probed. The control-plane suite passed 13
and failed its pre-existing delegated MCP connect-route discovery assertion.
The core/contracts selection passed 16 and failed two historical expectations:
three prior `source_repo=zyra` policy errors already make the loose audit
non-OK, and the old `M1-02B` query fixture no longer matches. Filtering the
audit shows no error for any of this slice's five entries. These observations
are recorded rather than hidden or expanded into an unauthorized M1/E02/MCP
repair.

This is an ordinary slice: it did not transfer a canonical owner, change a
global default permission/scheduler/recovery/compact policy, add an external
package/process/MCP server/port, or change packaging boundaries. Full cleanroom
and repository-wide Python validation therefore remain at the M2-04
numeric-stage aggregate review.

## 6. Effective-line audit

The canonical per-file report is
`docs/reviews/evidence/M2-S04A-01/effective-lines.json`. It uses exact added
lines from the frozen interval and the shared TypeScript compiler AST scanner.
Imports/exports, interfaces, type aliases, declaration-only signatures,
comments/blanks and complete JSX presentation ranges are excluded. This slice
then conservatively deducts command catalog data, typed wire declarations,
Python/TaskApi route adapters, Workbench route binding, direct transport/payload
maps and static title/name tables.

| Bucket | Lines |
| --- | ---: |
| raw additions | 12,344 |
| raw deletions | 23 |
| production runtime | 8,230 |
| UI behavior | 144 |
| UI presentation excluded | 246 |
| type/declaration excluded | 1,246 |
| schema/DTO/data excluded | 704 |
| adapter-only excluded | 281 |
| generated excluded | 30 |
| test/mock/fixture excluded | 1,193 |
| docs/comments/blank excluded | 270 |
| vendor/source-pool | 0 |
| **effective production** | **8,374** |
| required minimum | 7,500 |
| headroom | 874 |

No file exceeds 20% of effective production; the threshold is 1,674.8 and the
largest contributor is `parser.ts` at 852. The 15,000-line parent closure is
not claimed here and remains mandatory in `M2-S04A-02`.

## 7. Per-trigger large-file audit

Every raw-`>500`, effective-`>20%`, or excluded-`>30%` trigger emitted by the
canonical report is reviewed below. No effective-`>20%` trigger exists.

| File / trigger | Executable symbols and default-path responsibility | Failure/disable proof | Raw / effective and excluded reason |
| --- | --- | --- | --- |
| `packages/commands/src/result-model.ts`, raw >500 | `buildCommandResultModel`, `filterCommandResult`, `windowCommandResult`; command-specific bounded projection only | result redaction/window/specialized-section test | 963 / 848; 94 declarations, 15 static data, 6 comments |
| `packages/commands/src/parser.ts`, raw >500 | tokenizer, binder, parser, completion and suggestion functions; pre-transport fail-closed boundary | malformed/no-fallback and typed-argument tests | 868 / 852; 11 declarations, 5 comments |
| `packages/commands/test/commands.test.ts`, raw >500 and excluded >30% | no production symbol or owner | 26 behavior tests | 832 / 0; all test evidence |
| `packages/commands/src/registry.ts`, raw >500 and excluded >30% | `CommandRegistry` lookup/rank/availability; eleven-command policy surface | exact-set, disabled-entry and palette tests | 753 / 360; 383-line static catalog, 7 declarations, 3 comments |
| `packages/commands/src/projection-index.ts`, raw >500 | `CommandProjectionIndex`; derives reversible lookup from M2-01B snapshots | external-store/correlation tests | 629 / 506; 92 declarations, 31 comments |
| `packages/commands/src/input-engine.ts`, raw >500 | `CommandInputEngine`; transient draft/selection/keyboard/capture only | key delivery, settle/history and disabled tests | 604 / 495; 97 declarations, 12 comments |
| `apps/web/src/features/commands/runtime.ts`, raw >500 | `CommandSurfaceRuntime`; binds PromptInput to typed transport, queue restore and projection | disabled runtime and real API integration | 589 / 443; 48 declarations, 18 data, 74 transport/payload adapter, 6 comments |
| `packages/commands/src/coordinator.ts`, raw >500 | `CommandCoordinator`; parse/policy/request/transport/receipt settlement | busy admission, immediate BTW, disable-before-transport | 576 / 548; 27 declarations, 1 comment |
| `packages/commands/src/diagnostics.ts`, raw >500 | `auditCommandRuntime`; cross-owner invariant diagnostics, not canonical truth | healthy/degraded diagnostic tests | 566 / 489; 54 declarations, 13 static names, 10 comments |
| `packages/commands/src/queue-recovery.ts`, raw >500 | `CommandQueueRecovery`; backend restore/reconcile/backoff lifecycle | restore, retry and projection reconciliation test | 523 / 446; 69 declarations, 8 comments |
| `packages/commands/src/queue.ts`, raw >500 | admission/merge/sort/build functions and `CommandQueueProjection`; derived queue only | backend/projection/receipt-only and identity-mismatch tests | 519 / 482; 28 declarations, 9 comments |
| `apps/api/zyra_api/main.py`, excluded >30% | GET queue and POST cancel route glue into existing canonical owners | real HTTP queue/cancel/disable tests | 127 / 0; all route adapter |
| `apps/web/src/api/task-api.ts`, excluded >30% | typed TaskApi command/queue/cancel methods | real TaskApi transport path tests | 75 / 0; 18 declarations and 57 adapter |
| `apps/web/src/app/runtime.ts`, excluded >30% | Workbench construction binding only | full Web tests/build | 25 / 0; 2 declarations and 23 adapter |
| `apps/web/src/command/catalog.ts`, excluded >30% | pre-existing Web catalog compatibility mapping | exact registry test prevents drift | 231 / 0; all static data |
| `apps/web/src/features/commands/index.ts`, excluded >30% | export barrel only | typecheck/build | 2 / 0; declarations only |
| `apps/web/src/shell/overlay-runtime.ts`, excluded >30% | one overlay-kind declaration | result overlay lifecycle test | 1 / 0; declaration only |
| `bun.lock`, excluded >30% | locked workspace metadata; no new external runtime package | frozen build/test | 10 / 0; data |
| root `package.json`, excluded >30% | workspace registration only | typecheck/test/build | 3 / 0; data |
| `packages/commands/package.json`, excluded >30% | formal package/build boundary | typecheck/test/build | 17 / 0; data |
| `packages/commands/src/contracts.ts`, excluded >30% | DTO/type contract only; no state owner | compile plus receipt/queue tests | 381 / 0; 379 declarations and 2 data |
| `packages/commands/src/index.ts`, excluded >30% | package export barrel only | typecheck/build | 21 / 0; declarations only |
| `packages/commands/tsconfig.json`, excluded >30% | compiler metadata | typecheck | 12 / 0; data |
| typed client `constants.ts`, excluded >30% | generated route constant only | typed client and integration tests | 4 / 0; generated |
| typed client `normalizers.ts`, excluded >30% | generated wire normalizer registration | typed client and integration tests | 2 / 0; generated |
| typed client `protocol.ts`, excluded >30% | generated protocol declarations | typecheck and real integration | 24 / 0; generated |

The audit includes all 43 files in the frozen implementation interval. Tests,
docs, manifests, lock data, generated declarations and adapter-only code
receive zero effective production credit.

## 8. Dependency, fallback and provenance audit

- no changed runtime input contains `../claude-code-best`, `../opencode`,
  `../hermes-agent`, `../agentscope`, `../oh-my-pi`, `../openclaw` or an
  absolute parent-repository path;
- no changed runtime input references OpenClaw;
- the formal package adds no npm runtime dependency and introduces no
  subprocess, local port, MCP server or dynamic import;
- browser truth continues to flow through the existing typed client; direct
  payload maps and route adapters are excluded from effective credit;
- a disabled coordinator/dispatcher has no fabricated-success, local queue or
  ordinary-chat fallback;
- all five source decisions resolve to files present in the frozen
  implementation commit, and production targets sit under formal Zyra package,
  Web or API boundaries.

The new implementation is dynamically reachable through the production
Workbench PromptInput and API routes. Removing `@zyra/commands` breaks parsing,
policy, request/receipt settlement, recovery and result behavior; removing the
Web command surface restores ordinary prompt handling but makes every exact
control command unavailable; removing the M1 owner causes fail-closed API
errors. This is therefore not source-pool, manifest-only or adapter-only
completion.

## 9. Critical conclusion

The slice closes its own command-input and queue-control contract with one
default path, explicit owner boundaries, real backend queue/cancel/retry
behavior, 01B projection correlation, tool-disabled BTW isolation, negative
fallback proof and 8,374 conservatively counted effective lines. Evidence-only
material is separated from implementation and no parent source repository is a
runtime dependency.

The parent `M2-04A` is deliberately still open. `M2-S04A-02` must supply the
remaining plan/history/approval/command-orchestration behavior, run the direct
parent cumulative audit and close the 15,000 effective-line parent threshold.
