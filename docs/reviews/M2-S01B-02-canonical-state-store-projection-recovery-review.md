# M2-S01B-02 Canonical State Store Projection Recovery Review

Date: 2026-07-23
Verdict: `PASS`

## 1. Frozen commit boundaries

- slice baseline: `8f6ea956286a6f84f7c20fc46225fa0b9b5ee9d5`
- M2-01B parent baseline: `1f41ab17864dd789948aeebe3723540007ebaf2b`
- M2-01 numeric-stage baseline: `f53bf78287a2b2a6eec74102e7218d664307c137`
- prospective decision: `ff0e791`
- implementation: `7446f4cb57c4a75f416a5d6707ccf47c03a67cc3`
- evidence: `this_commit`

The source/language/owner decision was committed before production changes.
The implementation commit contains production code and directly related
behavior tests. This review, its JSON evidence, line auditor, source-to-target
auditor, ledger synchronizer and ledger changes are evidence-only and are
outside every effective-code interval.

## 2. Source and migration decision

The prospective record is
`docs/plans/M2-S01B-02-canonical-state-store-projection-recovery-preimplementation.md`.
Implementation follows that decision:

| Role | Pinned source | Migration | Result |
| --- | --- | --- | --- |
| primary | OpenCode `adf178a6b95c61506ddaadaf4dd062badb4a8fda`, `server-session.ts`, global event reducer/cache/trim, `server-sync.tsx` and child store | same-language TypeScript retained-control-flow/crop/adapt | immutable transaction reduction, domain projection, reconciliation, causality, selectors, retention, store lifecycle and recovery |
| supplementary | OpenHands `c105a82387898e744423c8831d412e26495b38a9`, event service/router and remote sandbox service | prospectively approved bounded Python-to-TypeScript behavior port | stable history/live fold, accept-only-missing identity and restore behavior; no second store or router |
| existing Zyra | `TaskEventTransport`, typed API client and `RuntimeEventSpine` | direct composition | sole event ingress, backend canonical event/state custody and browser request boundary |
| excluded | OpenClaw | none | forward exclusion remains intact |

OpenCode Solid/TanStack ownership, upstream DTOs, project cache shapes and
original UI state were removed. The retained algorithms were decomposed into
Zyra-owned state, transaction, projector, settlement, causality, selector,
retention, integrity, migration and persistence modules. The OpenHands
exception ports only independently testable history/recovery behavior; it does
not copy its Python control plane or create another canonical owner.

The aggregate source audit also corrected two factual spellings in the
protected M2-S01A-01 ledger entry: OpenCode uses
`middleware/authorization.ts`, not `middleware/auth.ts`, and
`server-session.ts`, not `.tsx`. Source repository, pinned commit, role,
language, target and owner are unchanged.

## 3. State custody and default path

The production path is:

`createZyraApi().events -> TaskEventTransport -> CanonicalProjectionStore ->
typed selectors -> workbench/panel consumers`.

`WorkbenchRuntime` restores the projection before transport binding, pins the
active task, binds the existing event transport and detaches browser state on
close. No panel owns a parallel reducer and no direct API/fetch path was added.

| State | Canonical owner | This slice |
| --- | --- | --- |
| task/run/session/event durability, event sequence, causation, correlation, state mutation and artifacts | existing backend stores and TypeScript `RuntimeEventSpine` | read-only projection |
| browser task/run/node/edge/worker/message/tool/permission/artifact/checkpoint/command/session projection | one `CanonicalProjectionStore` | unique owner |
| revision, compare-and-swap transaction and committed event identities | `CanonicalProjectionStore` | immutable browser transaction state |
| partial/final, optimistic confirmation, tombstone and orphan bookkeeping | projection settlement/orphan modules | deterministic projection-only reconciliation |
| causal reverse indexes and panel derivations | projection causality/selectors | reversible and dependency-keyed |
| snapshot schema, checksum, migration and persistence fallback | projection persistence/migrations | browser recovery only |
| backend cancellation | existing control-command path | never called by projection close |

The store commits a new deeply frozen snapshot per revision. Failed compare and
swap, malformed snapshots, checksum mismatch, stale snapshots, unknown schema,
disabled persistence and duplicate/conflicting events have explicit visible
outcomes; they cannot silently mutate the prior revision.

## 4. Implemented behavior

The slice provides:

- one normalized reducer/store for all required console domains;
- legacy nested payload normalization without replacing canonical event
  identity;
- deterministic task terminal aggregation from all terminal nodes rather than
  last-arrival order;
- immutable revision transactions with compare-and-swap semantics;
- optimistic-to-confirmed reconciliation, partial/final settlement,
  tombstones and bounded orphan release;
- causation/correlation/span/tool/checkpoint/artifact reverse indexes with
  deletion cleanup;
- dependency-keyed base selectors plus topology/cycle, timeline/lane, artifact
  lineage, session tree, operator queue and readiness panel selectors;
- protected bounded retention that preserves pinned/active/unsettled/causal
  records;
- versioned snapshots, checksum verification, migration, IndexedDB preference,
  local-storage fallback and in-memory test persistence;
- restore-before-live-fold and duplicate-free reconnect;
- multi-task isolation, route pin/unpin and one subscription fan-out;
- integrity auditing across identities, references, reverse indexes and
  aggregate task state.

Closing the browser projection releases listeners and browser resources only.
The real API probe proves that a pending backend task survives close and that a
reopened projection resumes from persisted state without duplicate effects.

## 5. Adversarial findings fixed

| Finding | Severity | Fix and evidence |
| --- | --- | --- |
| Legacy runtime envelopes can carry state under nested payload shapes. | high | normalization resolves bounded nested shapes before domain projection; behavior test covers the legacy envelope |
| Last terminal event can incorrectly mark a task terminal while another node remains live. | high | task state is recomputed from all node terminal states; out-of-order terminal tests cover it |
| Freezing only the root leaves maps, arrays and nested objects mutable. | high | each committed state is recursively cloned/frozen; mutation attempts and revision isolation are tested |
| Route changes could retain the old pinned task and hooks could reuse stale selector closures. | medium | runtime unpins before rebinding; hook memoization keys on selector identity/key |
| M2-S01A-02 queue ordering used random UUID lexical order for same-millisecond timestamps. | medium, aggregate | the existing stable array order is now the deterministic tie-break; the protected FIFO test passes in 20 repeated runs |
| Browser close could be conflated with task cancellation. | high | projection close has no control-command/cancel edge; real pending-task integration proves backend survival |

No unclosed P0/P1/P2 finding remains in the current slice or the M2-01
aggregate scope.

## 6. Verification

All commands ran against the frozen implementation content. The detached
cleanroom was
`G:/agent-zoo/.tmp/m2-s01b-02-cleanroom-7446f4c` at exact commit
`7446f4cb57c4a75f416a5d6707ccf47c03a67cc3`.

| Check | Main workspace | Exact-commit cleanroom |
| --- | --- | --- |
| typed-client/Web typecheck | PASS | PASS |
| typed-client and all Web tests | 84 passed, 0 failed, 332 assertions | 84 passed, 0 failed, 332 assertions |
| production Web build | PASS; 106 modules, about 1.75 MB JavaScript | PASS |
| real projection recovery integration | 3 passed across projection recovery and event cursor ingestion | included below |
| applicable M2-01 API/runtime/static/control regression | 16 passed in 68.79 s | 16 passed in 84.03 s |
| same-timestamp command FIFO repeat | 0 failures across 20 reruns | covered by full suite |
| ledger/source-to-target audit | 17 M2-01 entries, 60 source files, 81 targets, 0 blocker | source workspaces are intentionally outside cleanroom runtime |
| dependency/path scan | 0 runtime parent-source dependency | 0 runtime parent-source dependency |
| exact effective-line audit | slice, parent and numeric stage PASS | Git-object-bound audit |

The cleanroom installed eight packages with the frozen lockfile. It introduced
no source-repository link, editable parent path, external Docker context or
runtime cache dependency. Three scan hits are detector literals in existing
evaluation/scheduler code that reject parent-source paths; they are not
dependencies.

An unscoped repository-root `pytest -q` was sampled and is not a valid test
command for this workspace: discovery descends into browser profiles,
historical cleanrooms, package caches and vendored BrowserUse tests, producing
225 collection errors. It also exposes six pre-existing
`zyra_runtime.__all__` declared-but-not-imported findings. Neither issue is
introduced by this diff. The applicable M2-01 Python suite was selected
explicitly and passed in both the main workspace and the exact-commit
cleanroom.

The three general ledger unit files produced 26 passes and one protected
baseline failure. The same failure reproduces at the unmodified implementation
commit: an old M1-02B `LedgerQuery` test returns no result for a planned
Claude-code vendored query. Current M2-01 ledger synchronization, schema/
accounting tests and the strict 17-entry source-to-target audit pass; the
baseline query defect is not used to waive a current ledger error.

## 7. Effective-code audit

`scripts/audit_m2_s01b_02_effective_lines.mjs` uses exact Git added-line sets
and TypeScript/Python AST classification. Imports, declarations, static
schema/DTO data, generated files, tests/fixtures, docs/comments/blanks,
presentation, adapter-only and vendor/source-pool material receive zero
production credit.

| Scope | Interval | Raw additions | Runtime | UI behavior | Excluded | Effective | Minimum | Margin |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| M2-S01B-02 | `8f6ea956..7446f4cb` | 11,652 | 8,338 | 44 | 3,270 | 8,382 | 7,500 | 882 |
| M2-01B parent | `1f41ab17..7446f4cb` | 26,652 | 18,089 | 44 | 8,519 | 18,133 | 15,000 | 3,133 |
| M2-01 A/B stage | `f53bf782..7446f4cb` | 49,356 | 23,862 | 5,642 | 19,852 | 29,504 | 27,000 | 2,504 |

Current-slice excluded buckets are: 43 presentation, 1,183 types, 47
schema/data, 17 adapter-only, 1,718 tests and 262 docs/comments/blanks. There
are zero generated and zero vendor/source-pool additions.

Per-file current-slice production accounting:

| Path | Raw | Effective | Principal excluded bucket |
| --- | ---: | ---: | --- |
| app runtime, queue and task detail | 99 | 44 | 43 presentation, 12 type |
| `state/causality.ts` | 508 | 490 | 17 type |
| `state/contracts.ts` | 651 | 14 | 590 type, 45 schema |
| `state/history-fold.ts` | 344 | 306 | 37 type |
| `state/integrity.ts` | 743 | 704 | 38 type |
| `state/migrations.ts` | 631 | 593 | 33 type, 2 schema |
| `state/orphans.ts` | 172 | 160 | 11 type |
| `state/panel-selectors.ts` | 927 | 777 | 149 type |
| `state/persistence.ts` | 397 | 370 | 11 type, 16 comments/blanks |
| `state/projectors.ts` | 1,151 | 1,071 | 51 type, 29 comments/blanks |
| `state/reducer.ts` | 446 | 385 | 55 type |
| `state/retention.ts` | 329 | 303 | 24 type |
| `state/selectors.ts` | 1,037 | 995 | 40 type |
| `state/settlement.ts` | 346 | 321 | 24 type |
| `state/state.ts` | 492 | 467 | 9 type |
| `state/store.ts` | 678 | 613 | 51 type |
| `state/value.ts` | 809 | 769 | 15 type |
| index, hook adapter, tests, probe, integration and prospective doc | 1,892 | 0 | adapter/type/test/docs only |

No file contributes more than 20% of slice effective production. Production
files over 500 raw lines have distinct state-domain responsibilities. The only
production candidate above 30% excluded is `contracts.ts`: 635 of 651 lines
are type/schema/declaration material and receive no production credit. Pure
test/doc/adapter/index files also receive zero credit by construction.

## 8. M2-01 aggregate source and owner audit

`scripts/audit_m2_01_source_to_target.py` audits the full numeric stage directly
at the final implementation commit:

- 17 decisions: 14 production, 3 conformance;
- 60 resolved source files and 81 existing implementation targets;
- one canonical UI projection owner;
- zero target under vendor/source-pool paths;
- zero blocking source or target finding;
- one explicitly non-blocking AG-UI conformance descriptor, which is prose and
  owns no production code.

M2-01A remains the typed API/workbench shell owner; M2-01B owns event ingress
and the sole UI projection. Backend stores and `RuntimeEventSpine` remain
canonical. The aggregate audit found no duplicated store, panel write owner,
external source runtime, hidden fallback or OpenClaw role.

## 9. Anti-fake-internalization and failure tests

- A real isolated API/SQLite/runtime-event-spine task is ingested, projected,
  persisted, closed, reopened and resumed without duplicate effects.
- Removing the reducer, store, migration, persistence or typed selectors
  breaks directly related behavior tests; disabling persistence has a visible
  non-persistent result rather than a fixture fallback.
- Permission and command events change operator-queue projection; task/node
  events change topology/timeline; artifact events change lineage; session
  events change session trees.
- No source manifest, source scan, replay-only trace or fixed health response is
  counted as completion behavior.
- No dependency, lockfile, subprocess, local port, MCP server, Docker context,
  dynamic import or parent-source runtime path was added.
- Browser projection cleanup cannot terminate backend work.

This slice implements the owner assigned by its execution document and does not
transfer a pre-existing canonical owner or make an incompatible backend schema
or persistence change. Numeric-stage cleanroom and aggregate validation were
still completed because this is the final M2-01 slice.

## 10. Closure

`M2-S01B-02` passes at 8,382 effective production lines. Parent M2-01B closes
at 18,133 effective lines. The complete M2-01A/B numeric stage closes at 29,504
effective lines, with its aggregate source/owner audit, applicable
front/back-end regression and exact-commit cleanroom green.

This does not claim M2 live scenarios, large-graph visualization, artifact
viewers, terminal/browser control or milestone exit. The sole next entry is
`docs/milestones/M2-console-demo/slice-02a-01-topology-route-placement-projection.md`.
