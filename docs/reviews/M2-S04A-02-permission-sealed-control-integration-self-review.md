# M2-S04A-02 Permission And Sealed Control Integration Self-Review

Date: 2026-07-25

Verdict: `PASS`

## 1. Frozen boundary

- slice baseline:
  `33327d49ef1fab62d314709e169eebc781eb2a9c`
- parent M2-04A baseline:
  `53b002bac1e97eab23b7553d344da068dd8dd3c9`
- prospective source decision:
  `1ef817c2f7f26d2751b6760b15a093d86dee755f`
- implementation:
  `cc09f054ad491877eeed5951574a518d57e157a0`
- evidence:
  `this_commit`
- direct line interval:
  `33327d49ef1fab62d314709e169eebc781eb2a9c..cc09f054ad491877eeed5951574a518d57e157a0`
- direct parent interval:
  `53b002bac1e97eab23b7553d344da068dd8dd3c9..cc09f054ad491877eeed5951574a518d57e157a0`

Production code and directly related tests are frozen in the implementation
commit. The two exact line auditors, source-ledger synchronizer, generated JSON
reports and this review are evidence-only and receive no implementation credit.

## 2. Source and migration decision

The prospective decision is
`docs/plans/M2-S04A-02-permission-sealed-control-preimplementation-decision.md`.
No source role was changed after implementation.

| Role | Pinned source | Language / mode | Bounded responsibility |
| --- | --- | --- | --- |
| primary | `claude-code-best@c57f5a29e88e9a814bea47abeb9a0a6f725dc102` permission prompt/request/dialog/context/interactive/bridge paths | TypeScript/TSX to TypeScript/TSX; retained control flow, cropped and adapted | request/detail separation, one-shot response claiming, active-request race, warning presentation and exact callback/resume proof |
| supplementary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` permission context/dock/reconnect paths | TypeScript/TSX to TypeScript/TSX; cropped same-language integration | dock/list interaction, responding exclusion, bounded reconnect refresh, stable session scoping and accessible action order |
| conformance | `hermes-agent@44ddc552f5e054759a6970af8997ea588a9d81c9` | tests only | timeout/interrupt release, secret redaction and concurrent response negatives |
| conformance | `agentscope@b6698c5dbaa1aa916925e27402767f45e2405fa4` | tests only | mode/rule ordering and HITL event projection negatives |
| reference | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | rejection/reference only | execute-boundary approval and rejection of sealed/yolo authority widening |
| reference | `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | UI composition review only | pending-action/confirmation separation without state or runtime migration |

Claude query/session/tool and process-local React pending ownership were not
copied. OpenCode auto-accept, SDK, store, provider and session owners were not
copied. Hermes, AgentScope, OMP and OpenHands receive no production code quota.
The analytical `claude-reviews-and-fine-tuning` and
`Dive-into-DeepResearch` references remain review-only in the prospective
decision. OpenClaw was not restored, read, compared, migrated or entered in the
ledger.

The six code-repository source decisions are synchronized by
`scripts/sync_m2_permission_console_source_ledger.py`: six entries are aligned
and every target exists in the frozen implementation commit.

## 3. Canonical ownership reconciliation

The parent slice text uses “M1 PermissionStateStore/ToolPermissionRuntime” as
the backend-owner shorthand. The protected runtime had already completed its
source-custody correction before this slice. The concrete current ownership,
frozen prospectively and preserved here, is:

| State domain | Canonical owner | This slice's responsibility |
| --- | --- | --- |
| permission evaluation, exact response acceptance and decision | TypeScript `PermissionCoordinator` | redacted projection, authenticated exact response construction and receipt admission only |
| response challenge/proof and exact resume | TypeScript `PermissionContinuationRuntime` plus E02 API port | Web computes the specified digest; TypeScript verifies every echoed identity before resume |
| one-use physical-call permit | TypeScript permission continuation/execution permit owner | derived issue/claim/consume/revoke/expire/conflict projection only |
| durable permission session custody and HTTP transport record | Python `PermissionStateStore` / `PermissionControlPlane` where the transport is used | in-memory bearer custody only; no browser persistence or token projection |
| rule/mode/policy revision | existing permission runtime/store | read-only rule and policy-diff projection; no browser rule editor or auto-accept owner |
| canonical permission events | existing event log and M2-01B selector state | typed task-scoped reconciliation, dedupe and conflict quarantine |
| browser pending list/detail | backend summaries plus canonical events | disposable external-store snapshot; `ownsPendingState=false` |
| sealed intervention count | existing task control/intervention ledger | rejects the attempted action through real `/steer` or `/retry` control and displays its canonical receipt |

Python only validates the shape of `console_response`, stamps transport
metadata and forwards it. It cannot decide allow or deny. React cannot resolve
a request locally, issue a permit, change a rule or persist a second pending
queue.

## 4. Default path and semantic behavior

The real interactive path is:

`tool/control request -> PermissionCoordinator pending envelope -> E02
redacted projection -> typed custody session -> PermissionConsoleRuntime ->
task-scoped selector/list/detail -> response race claim -> browser SHA-256
proof -> Python forwarding -> TypeScript proof verification -> exact
continuation -> canonical decision/permit receipt -> event/permit timeline`.

The real sealed path is:

`sealed pending/ASK or manual approve/deny/steer/retry/mode-change attempt ->
PermissionConsoleRuntime sealed guard -> no resolve call -> task control
intervention receipt -> deterministic deny/recovery -> canonical intervention
projection with human_intervention_count=0`.

The following effects are directly verified:

1. Pending list/detail includes exact request, envelope, response version and
   nonce, task/run/session/worker/tool-call, canonical owner, session/policy/mode
   revisions, tool namespace/server/operation, arguments digest, request
   fingerprint and expiry.
2. Raw tool arguments and secrets are never retained in the request model,
   audit projection or timeline. Redacted semantic previews remain bounded.
3. Browser, terminal, MCP, remote capability, plugin/skill supply-chain,
   prompt/tool-injection, secret and sealed-policy warnings are deterministic
   projections and cannot rewrite executable arguments.
4. Interactive response requires a bounded named operator and optional bounded
   feedback. The displayed actor is selected from authenticated/operator task
   metadata, with a non-secret service label only as fallback.
5. One race gate admits a single allow or deny. A competing local response,
   duplicate response ID, changed payload, stale revision, wrong owner,
   cross-task/session binding, expired request or forged digest fails before
   resume.
6. An allow receipt must prove the accepted response, challenge, final
   arguments, canonical owner and resume. Only then may the UI project the
   canonical permit. A permit is one-use; replay or changed event identity is
   quarantined.
7. Repeated canonical events are idempotent. An out-of-order consume event is
   retained as an orphan observation and deterministically applied only after
   the verified issue receipt arrives.
8. The session roster keeps multiple custody bindings in memory only. A
   reconnect reopens/resumes canonical sessions with bounded backoff and stops
   permanently on custody denial.
9. The expiry supervisor derives due work but calls the backend expire owner;
   it does not settle the pending request locally. Identity/deadline drift is
   quarantined.
10. Sealed approve/deny never calls resolve. Command-input steer/interrupt,
    explicit retry and permission-mode change are intercepted before command
    transport. Queue retry buttons use the same rejection ledger. Every
    attempted intervention is fail-closed and can trigger deterministic
    recovery without human wait.
11. Task detail, timeline, browser, terminal and command surfaces subscribe to
    the same permission controller/canonical events. No component retains a
    local pending truth.

## 5. Failure, disconnect and anti-fallback proof

| Failure | Observable result |
| --- | --- |
| response proof effect/revision/digest/owner/nonce/expiry mismatch | TypeScript verifier rejects before continuation and no permit is issued |
| simultaneous allow/deny | first exact claim wins; loser is marked `lost_race` and cannot call resolve |
| duplicate/conflicting receipt | receipt ledger rejects identity reuse and cannot project a second permit |
| permit replay or conflicting event identity | lifecycle becomes conflict/replay-rejected and records quarantine |
| expired request or changed deadline | request is non-selectable/default-deny or expiry observation is quarantined |
| reconnect custody denial | reconnect supervisor enters permanent terminal state; it does not rotate to an unauthenticated fallback |
| sealed manual response/control | resolve is not called; a real intervention control receipt is recorded and the action cannot advance the run |
| receipt or permit projection disabled | claimed behavior throws visibly; no local allow or fabricated success exists |
| Web permission controller closed/unbound | response/control methods fail before transport |
| E02 TypeScript coordinator unavailable | existing API-port tests fail closed; Python has no fallback decision path |

The real E02 integration executes an approval through the existing
`PermissionCoordinator`, verifies the proof and issues exactly one retry
permit. The Python integration starts the real in-process HTTP application and
forwards the same proof to TypeScript. BrowserWorker permission-gate and
continuation suites verify the adjacent browser/tool owner, while sealed
control tests verify real control receipt semantics.

## 6. Verification

All passing commands target the frozen implementation:

| Command | Result |
| --- | --- |
| `bun run typecheck` | PASS; all TypeScript projects |
| focused permission/runtime/client/Web tests | PASS; 25 tests, 108 assertions |
| existing E02 API-port runtime suite | PASS; 14 tests |
| `bun test ./packages/core ./apps/web` | PASS; 224 tests, 1,365 assertions |
| permission console, E02 cutover and 04A-01 real HTTP tests | PASS; 5 tests |
| BrowserWorker permission gate plus permission continuation | PASS; 27 tests and 16 subtests |
| `bun run build:web` | PASS; 295 bundled modules |
| Python compile for API/test/ledger sync | PASS |
| direct slice line audit | PASS; 7,739 effective versus 7,500 |
| direct parent line audit | PASS; 16,112 effective versus 15,000 |
| source-ledger sync `--check` | PASS; 6 decisions, 6 entries, 0 missing targets |
| whitespace and runtime dependency/path scans | PASS |

This slice strengthens an additive approval response contract but does not
transfer a canonical owner, change the global default permission mode, add an
external dependency/process/port/MCP server/plugin/Docker context/dynamic
import, or change packaging. The full M2-04 numeric-stage cleanroom and broader
repository audit remain assigned after M2-04B, as required by the slice.

## 7. Direct effective-line audit

The canonical direct report is
`docs/reviews/evidence/M2-S04A-02/effective-lines.json`.

| Bucket | Lines |
| --- | ---: |
| raw additions | 12,618 |
| raw deletions | 8 |
| production runtime | 7,258 |
| UI behavior | 481 |
| UI presentation excluded | 1,002 |
| type/declaration excluded | 1,160 |
| schema/DTO/data excluded | 4 |
| adapter-only excluded | 517 |
| generated wire excluded | 181 |
| test/mock/fixture excluded | 1,666 |
| docs/comments/blank excluded | 349 |
| vendor/source-pool | 0 |
| **effective production** | **7,739** |
| required minimum | 7,500 |
| headroom | 239 |

The report uses exact Git added-line sets and the shared TypeScript compiler
AST scanner. Imports/exports, interfaces, type aliases, declaration-only
signatures, comments/blanks and complete JSX presentation ranges are excluded.
This slice then deducts the complete Web permission HTTP client as
adapter-only, Python forwarding as adapter-only, Workbench/export composition
as adapter-only, and typed constants/protocol/normalizers as generated wire.

No file contributes more than 20% of effective production. The threshold is
1,547.8 lines; the largest contributor is
`apps/web/src/features/permissions/runtime.ts` at 849 lines, or 10.97%.

## 8. Direct parent M2-04A audit

The canonical parent report is
`docs/reviews/evidence/M2-S04A-02/parent-effective-lines.json`.
It scans the exact parent Git interval; it does not add the two slice totals.
It first applies the committed S04A-01 catalog/adapter/wire deductions and then
the S04A-02 deductions.

| Bucket | Lines |
| --- | ---: |
| raw additions | 28,093 |
| raw deletions | 42 |
| production runtime | 15,488 |
| UI behavior | 624 |
| UI presentation excluded | 1,245 |
| type/declaration excluded | 2,406 |
| schema/DTO/data excluded | 2,872 |
| adapter-only excluded | 798 |
| generated wire excluded | 936 |
| test/mock/fixture excluded | 2,859 |
| docs/comments/blank excluded | 865 |
| vendor/source-pool | 0 |
| **effective production** | **16,112** |
| parent minimum | 15,000 |
| headroom | 1,112 |

This directly closes the M2-04A parent code floor. It does not claim the
separate M2-04 numeric-stage aggregate, cleanroom or milestone-exit competition
gates.

## 9. Per-trigger file audit

Every direct file with raw additions over 500, effective contribution over
20%, or an excluded bucket over 30% is explicitly reviewed below. The
raw/effective numbers come from the canonical direct report.

| File | Responsibility and disable/failure evidence | Raw / effective; exclusion reason |
| --- | --- | ---: |
| `apps/api/zyra_api/main.py` | validates and forwards `console_response`; TypeScript tamper test proves Python cannot decide | 7 / 0; adapter |
| `apps/web/src/api/index.ts` | constructs/exports the permission transport | 6 / 0; adapter |
| `apps/web/src/api/permission-api.ts` | custody-authenticated typed list/detail/respond/expire transport; disconnect is visible | 547 / 0; complete adapter exclusion |
| `apps/web/src/app/runtime.ts` | constructs/closes the shared permission controller | 8 / 0; composition adapter |
| `apps/web/src/components/command-input/command-input.tsx` | pre-transport sealed steer/retry/mode-change interception | 52 / 29; 23 JSX/presentation lines excluded |
| `apps/web/src/components/tasks/task-detail.tsx` | mounts the workbench on the real task route | 3 / 0; JSX binding |
| `apps/web/src/features/browser/view/browser-workbench.tsx` | mounts the shared permission selector/status | 7 / 0; JSX binding |
| `apps/web/src/features/commands/queue-panel.tsx` | intercepts sealed retry and displays shared status | 38 / 5; 33 JSX/presentation lines |
| `apps/web/src/features/permissions/contracts.ts` | DTO/type contracts only; owns no state | 408 / 0; declarations |
| `apps/web/src/features/permissions/event-reconciler.ts` | task/run-scoped canonical event dedupe, join and quarantine | 673 / 606; 67 declarations/comments; conflict tests |
| `apps/web/src/features/permissions/index.ts` | export barrel | 22 / 0; declarations |
| `apps/web/src/features/permissions/permit-lifecycle.ts` | verified issue plus canonical consume/revoke/expire/replay projection | 609 / 550; 59 declarations/comments; disable/replay/out-of-order tests |
| `apps/web/src/features/permissions/projection.ts` | strict redacted backend projection and stale selection guard | 839 / 780; 59 declarations/comments; secret/stale tests |
| `apps/web/src/features/permissions/runtime.ts` | custody, refresh, race, proof, receipt, expiry, sealed and event orchestration | 898 / 849; 49 declarations/comments; real path and close/failure tests |
| `apps/web/src/features/permissions/view-model.ts` | queue/timeline/cross-view derived models only | 506 / 485; 21 declarations/comments; selector tests |
| `permission-detail.tsx` | exact identity/warning/feedback/allow-once/deny UI | 261 / 86; 175 JSX presentation |
| `permission-policy-panel.tsx` | read-only policy/rule diff projection | 113 / 18; 95 JSX presentation |
| `permission-queue.tsx` | accessible backend-derived pending list | 102 / 37; 65 JSX presentation |
| `permission-timeline.tsx` | canonical request/receipt/permit/intervention timeline | 106 / 45; 61 JSX presentation |
| `permission-workbench.tsx` | binds task/session/events and named responder to the controller | 311 / 183; 128 JSX/declarations |
| `apps/web/src/features/terminal/view/terminal-workbench.tsx` | mounts shared permission status | 7 / 0; JSX binding |
| `apps/web/src/features/timeline/view/timeline-workbench.tsx` | mounts shared permission status | 6 / 0; JSX binding |
| `apps/web/src/styles.css` | permission layout and visual states | 419 / 0; presentation |
| `apps/web/test/permission-sealed-control-integration.test.ts` | 16 Web behavior tests | 852 / 0; test evidence |
| typed client `constants.ts` | permission route/operation constants | 11 / 0; generated wire |
| typed client `normalizers.ts` | permission response normalizer | 55 / 0; generated wire |
| typed client `protocol.ts` | permission HTTP protocol declarations | 127 / 0; generated wire |
| `permission-custody-headers.test.ts` | custody precedence and no-token diagnostic tests | 58 / 0; test evidence |
| runtime permission `index.ts` | response-proof export | 1 / 0; declaration |
| `permission-console-proof.test.ts` | real E02 exact resume and tamper tests | 232 / 0; test evidence |
| `permission-response-proof.test.ts` | exact proof field/expiry/replay tests | 168 / 0; test evidence |
| `test_permission_console_api.py` | real Python HTTP-to-TypeScript proof tests | 356 / 0; test evidence |

The trigger list includes all presentation-only and test-only files whose
excluded ratio exceeds 30%; they are deliberately visible rather than hidden
from the report.

## 10. Dependency, provenance and clean-path audit

- changed production code contains no `../claude-code-best`,
  `../opencode`, `../browser-use`, `../OpenHands`, source checkout absolute
  path, npm link, pip editable parent path, vendor runtime or source-pool
  dependency;
- changed production code contains no OpenClaw reference;
- no manifest or lockfile changed, and no external package, process, local
  helper port, MCP server, plugin, Docker context or dynamic import was added;
- bearer custody remains in memory and diagnostic projections omit both
  ambient API and permission session tokens;
- raw arguments, secret-bearing payloads and active URLs are not persisted in
  the Web request/timeline/audit models;
- source-ledger production targets are formal Zyra runtime/Web/API modules and
  all exist in the exact implementation commit.

Removing the response verifier breaks exact E02 approval. Removing
`PermissionConsoleRuntime` breaks list/detail/respond/expiry/reconnect and
sealed intervention behavior. Removing event or receipt/permit admission
breaks canonical timeline and one-use enforcement. Removing the shared surface
bindings removes browser/terminal/command permission state. These are
observable behavior changes, not import or health-only evidence.

## 11. Critical conclusion

The slice completes a real permission control console without creating a
second permission owner. Interactive responses are named, redacted, exact and
receipt-backed. Sealed ASK and every manual approval/control path fail closed,
record intervention evidence and do not wait for a human. Request, response,
decision and permit identities remain causally traceable across task,
timeline, browser, terminal and command surfaces.

The direct slice closes at 7,739 conservative effective production lines. The
exact direct parent interval closes M2-04A at 16,112 lines. Both floors pass
after excluding adapters, generated wire, types, presentation, tests, docs and
source-pool material. The M2-04 numeric-stage aggregate and milestone-exit
competition evidence remain correctly open for their assigned later layers.
