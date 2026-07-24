# M2-S02B-02 recovery control and sealed timeline critical self-review

## 1. Review identity and verdict

- Slice: `M2-S02B-02`
- Parent: `M2-02B`
- Numeric stage: `M2-02`
- Baseline commit:
  `46be71eb4ced2da38f5b05fce24907da7ffc8f6c`
- Preimplementation decision commit:
  `482cc5f4b582e1b81eeda48c0163e88838f295e8`
- Main implementation commit:
  `fdf82f2059153b917ca2486cdcbe1b8d68b790b9`
- Aggregate isolation fix and final target commit:
  `f3302a3c5366218068cb8acf9790c973e4b62033`
- Verdict: **passed**

The slice satisfies its production behavior, conservative 6,000-line gate,
M2-02B parent closure and M2-02 numeric-stage aggregate review. No approval,
worker, recovery, graph, checkpoint or event state owner moved into the
timeline.

## 2. What was internalized

### 2.1 Primary TypeScript/TSX migration

OpenCode session-control and timeline mechanisms were cropped into:

- `apps/web/src/features/timeline/control/{commands,contracts,observation,transport,validation}.ts`
- `apps/web/src/features/timeline/scale/{causal-fold,contracts,goal-drift,projector,search-index,virtualizer}.ts`
- `apps/web/src/features/timeline/view/timeline-workbench.tsx`

They now use Zyra `TaskApi`, `CanonicalProjectionStore`,
`WorkerCausalTimelineProjection` and task-detail route state. No OpenCode
store, session, provider, tool, permission or persistence owner was copied.

### 2.2 Supplementary original-language migration

OpenHands stop/resume, rollback/refetch and live-status mechanics were cropped
into:

- `control/ledger.ts`
- `control/runtime.ts`
- `view/recovery-control-panel.tsx`

These modules reconcile receipts and connection state. They cannot mutate a
worker optimistically and do not own approval state.

### 2.3 Bounded cross-language semantic port

Browser-use action error, connection recovery, consecutive failure, bounded
retry and pause/resume vocabulary was semantically ported into:

- `control/receipts.ts`
- `control/sealed.ts`
- `scale/overlays.ts`

The port consumes canonical events only. Browser controller, DOM state, action
executor, retry loop, persistence and watchdogs remain excluded.

### 2.4 Zyra owner integration

- `packages/commands/zyra_commands/runtime/registry.py` registers `/kill`,
  `/steer`, `/retry` and `/reassign`; existing `/resume` remains intact.
- `apps/api/zyra_api/main.py` binds the five actions through the existing
  `RuntimeControlDispatcher`, permission runtime and canonical handlers.
- `/kill` uses `WorkerControlRuntime`.
- `/steer` uses `RecoveryApplication` and `GraphStateCustody`.
- `/retry` uses `RecoveryApplication` and the existing QueryEngine recovery
  continuation.
- `/reassign` uses `RecoveryApplication` and `WorkerPoolStore`.
- `/resume` retains exact checkpoint identity and the existing session/recovery
  owner.

## 3. Canonical custody and semantic effects

| State domain | Canonical owner | Timeline responsibility |
|---|---|---|
| request admission, idempotency, queue lifecycle | `RuntimeControlDispatcher` | construct and observe one durable request |
| permission and sealed denial | `ToolPermissionRuntime` / deployment permission owner | display canonical denial and sealed assessment |
| worker, attempt and lease | `WorkerPoolStore` / `WorkerControlRuntime` | validate expected owner and display resulting events |
| recovery decision and application | `RecoveryApplication` and existing recovery stores | observe recovery plan/application/fail-closed result |
| graph mutation | `GraphStateCustody` | display committed route/revision evidence |
| exact checkpoint resume | `SessionControlRuntime` / `RecoveryApplication` | submit exact identity and reconcile the canonical receipt |
| frontend event facts | `CanonicalProjectionStore` | derive read-only control/timeline views |
| transient control state | `RecoveryControlRuntime` | pending/late/offline/viewer lifecycle only |
| transient scale state | `ScaledTimelineRuntime` | virtual range, folds, search, drift and overlays only |

Real HTTP tests prove:

- kill cancels/fences the real lease;
- steer commits a requirement-change recovery through graph custody;
- retry applies a bounded continuation through recovery owners;
- reassign terminates the old lease and acquires a successor worker;
- resume preserves checkpoint identity and idempotent replay;
- stale owner evidence returns `409` before mutation;
- sealed mode records one operator attempt, keeps human intervention at zero,
  never waits, never applies the human command and deterministically replans or
  fails closed.

## 4. Critical findings found and fixed

### Finding 1: request-scoped state could overwrite canonical owner commits

The command route saved its request-scoped state after handlers returned.
Recovery and worker handlers can commit a newer canonical checkpoint through
their own owner. Without synchronization, the route's final save could restore
the older state.

Fix: after a canonical handler commits, the request context synchronizes the
fresh owner state before the route persists its outcome. The HTTP tests assert
worker replacement, graph recovery and exact-resume facts after the route
returns.

### Finding 2: empty announcement reads could trigger a React revision loop

The first control runtime implementation incremented its revision even when
`takeAnnouncements()` returned no announcements. A component effect that read
the empty queue could therefore trigger itself indefinitely.

Fix: an empty read is a no-op and a focused test asserts that the revision does
not change.

### Finding 3: measured-height cache needed long-lived bounds

The first virtualizer bounded mounted rows but retained every historical
measurement. A long-running viewer could therefore accumulate unbounded
measurement state.

Fix: measurements use active-row LRU pruning while preserving selected/anchor
rows. The 5,000-row test proves bounded mounting, measured positions and far
selection.

### Finding 4: aggregate tests exposed a stale command-source coordinator

The first M2-02 Python aggregate run placed this test after another API test.
The process-global command-source coordinator retained a persistence path
inside a prior test's deleted temporary directory, so `/commands` closed the
connection before responding.

Fix: the API test context resets `reset_control_runtime()` on entry and exit.
The fix is commit `f3302a3c5366218068cb8acf9790c973e4b62033`.
The four-file aggregate then passed 6/6 in the main checkout and 6/6 in the
Git-object cleanroom.

### Non-product environment observations

- A rerun using the shared system pytest directory hit Windows access denial.
  The aggregate uses an explicit repository-local `--basetemp` and disables
  pytest cache; all assertions then pass.
- The in-app browser connector exposed no browser instance. No alternate
  browser controller or static screenshot was used to conceal that fact.
  Browser automation was not an authority-file completion gate; production
  route mounting, interaction contracts, all Web tests and the production
  build were verified instead.

## 5. Effective-code audit

Exact interval:
`46be71eb4ced2da38f5b05fce24907da7ffc8f6c..f3302a3c5366218068cb8acf9790c973e4b62033`

| Bucket | Lines | Counted? |
|---|---:|---|
| production runtime | 5,707 | yes |
| active UI behavior | 330 | yes |
| JSX/CSS/static presentation | 494 | no |
| type/interface declarations | 823 | no |
| schema/DTO/data | 24 | no |
| Python owner integration classified conservatively as generated/non-TS | 694 | no |
| tests/fixtures/probes | 1,230 | no |
| docs/comments/blanks | 506 | no |
| adapter-only | 0 | no |
| vendor/source-pool | 0 | no |
| **effective production** | **6,037** | **yes** |

The floor is 6,000; margin is 37. Python owner integration receives zero
credit, so the pass does not depend on cross-language glue, backend line
volume, tests or presentation.

Large-file review:

- over 500 raw additions:
  `main.py`, `ledger.ts`, `receipts.ts`, `runtime.ts`, `validation.ts`,
  `causal-fold.ts`, `virtualizer.ts`;
- no file contributes more than 20% of effective production;
- maximum contributor is `receipts.ts`, 642 lines / 10.63%;
- high-exclusion production files reviewed:
  `contracts.ts` is intentionally type-heavy,
  `recovery-control-panel.tsx` separates behavior from JSX, and
  `timeline-workbench.tsx` separates behavior from JSX;
- `main.py` contributes zero under this TypeScript-focused conservative
  auditor.

Parent and numeric-stage closure:

- M2-S02B-01: 6,836
- M2-S02B-02: 6,037
- **M2-02B cumulative: 12,873 / 12,000**
- M2-02A cumulative: 14,027
- **four-slice M2-02 cumulative: 26,900 / 25,000**
- direct numeric-stage interval reclassification:
  27,021 effective lines from 48,437 raw additions.

The four-slice sum is the parent accounting authority. The direct interval is
reported separately because later slices can modify lines introduced by
earlier slices and because evidence/ledger commits exist between slice
implementation intervals.

## 6. Verification

### Focused and adjacent

- Web control, scale and existing timeline: 25 passed, 311 assertions.
- Python control plus adjacent topology/timeline/fault chain: 7 passed.
- TypeScript typecheck: passed.
- Production build: passed, 172 modules.

### M2-02 aggregate in main checkout

- all Web tests: 110 passed, 761 assertions;
- topology route/placement, topology interaction, worker causal timeline and
  recovery control Python integration: 6 passed;
- production build/typecheck: passed.

### Exact-commit cleanroom

Cleanroom source:
`git archive f3302a3c5366218068cb8acf9790c973e4b62033`

- frozen lockfile install: passed;
- all Web tests: 110 passed;
- applicable Python integration: 6 passed;
- typecheck/build: passed, 172 modules;
- no root source repository is part of the build or runtime path.

### Source-to-target and dependency audit

- M2-02 source decisions: 21 ledger entries;
- production target bindings: 70;
- conformance/reference bindings: 17;
- missing targets: 0;
- current OpenClaw forward entries: 0;
- package/lock changes in M2-02: 0;
- changed production files containing forbidden parent-source paths: 0;
- new npm links, editable parent paths, external Docker contexts, dynamic
  imports, MCP servers, processes and local ports: 0.

The generic repository-wide verifier continues to report the three protected
historical strings in
`packages/evaluation/zyra_evaluation/m1_hardening/long_horizon_runtime.py`.
That file is unchanged across both this slice and the M2-02 numeric stage.
The slice-specific conservative auditor and aggregate source-to-target audit
pass; this review does not rewrite protected M1 history.

## 7. Anti-fake-internalization review

- Dynamic reachability: task detail mounts the real control panel and scaled
  timeline; controls reach the real HTTP command route.
- Disconnect-if-removed: disabling the control binding fails before transport;
  stale owner validation fails before mutation; disabling the prior causal
  projector remains covered by M2-S02B-01 regression.
- Semantic effect: controls change worker lease, graph/recovery/checkpoint state
  rather than only React state.
- Sealed effect: denial changes the intervention ledger and recovery path but
  never opens approval or changes human intervention count.
- Scale effect: 5,000 effective rows are searched/folded/virtualized while
  heartbeat, replay and no-op facts remain excluded from effective-step
  overlays.
- Clean reproducibility: final tests pass from an exact Git-object export with
  a frozen dependency install.
- Source boundary: no runtime/build dependency uses a workspace source
  repository; OpenClaw remains forward-excluded.

## 8. High-risk trigger assessment

No high-risk trigger remains:

- no incompatible public schema/event/persistence migration;
- no canonical state-owner transfer;
- no change to global default permission, scheduler, compact or fallback
  policy;
- new sealed handling is scoped to the four new mutable control commands and
  reuses existing permission/recovery owners;
- no external dependency, subprocess, port, MCP server, plugin or dynamic
  package import;
- no workspace, source-repository, packaging or commit-boundary change.

The numeric-stage cleanroom and aggregate regression were executed because
this slice is the final sibling of M2-02, not because a high-risk owner
transfer occurred.

## 9. Final disposition

`M2-S02B-02`, parent `M2-02B`, and numeric stage `M2-02` are complete. The
next entry is determined only by the root
`docs/milestones/execution-state.yaml`.
