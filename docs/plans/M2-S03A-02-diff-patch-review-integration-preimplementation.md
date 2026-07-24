# M2-S03A-02 diff, patch and review integration preimplementation decision

## Frozen identity

- Slice: `M2-S03A-02`
- Parent: `M2-03A`
- Decision status: `approved_before_production_change`
- Baseline commit:
  `5dec6715db9f35913acd1b3a078ce9bcde943a8a`
- Parent effective-code baseline:
  `1833319acdb8a09fac3438b12356da0e9c78d6bb`
- Implementation commit: `pending`
- Evidence commit: `pending`
- Required conservative effective TypeScript/React floor: `7,500`
- Parent cumulative floor: `15,000`
- Protected predecessor: `M2-S03A-01`

The baseline worktree is clean. This decision is committed before any
production or direct-test change. Effective-code accounting for this slice
will use the exact `baseline_commit..implementation_commit` interval. Parent
closure will also be recomputed directly across
`1833319acdb8a09fac3438b12356da0e9c78d6bb..implementation_commit`.

## Exact source-to-target decision

| Capability | Role | Source repository and commit | Exact source paths | Source language | Zyra target | Target language | Migration mode | Canonical owner after integration |
|---|---|---|---|---|---|---|---|---|
| Review file focus, file filtering, collapsed/expanded hunks, split/unified preference, comment focus and stable refresh | `primary_implementation` | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/app/src/pages/session/review-tab.tsx`; `packages/app/src/pages/session/v2/review-panel-v2.tsx`; `packages/app/src/pages/session/v2/review-panel-v2-state.ts`; `packages/app/src/pages/session/v2/review-diff-kinds.ts`; `packages/app/src/utils/diffs.ts` | TypeScript/TSX | `apps/web/src/features/diff-review/**`; typed API protocol and TaskDetail mount | TypeScript/React | cropped, same-language component/runtime integration | Browser `DiffReviewRuntime` owns only transient review state; canonical patch/file state remains backend-owned |
| Lazy per-file old/diff/new views, read-only editor behavior, large-file loading and explicit empty/loading/error states | `supplementary_implementation` | `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | `frontend/src/components/features/diff-viewer/file-diff-viewer.tsx`; `frontend/src/components/features/diff-viewer/editor-container.tsx`; `frontend/src/hooks/query/use-unified-git-diff.ts` | TypeScript/TSX | `apps/web/src/features/diff-review/view/**`; `fetch-runtime.ts`; `budget.ts` | TypeScript/React | cropped, same-language behavior integration without Monaco dependency | Browser view/runtime only; no file or patch truth |
| Snapshot-bound edit anchors, hash mismatch diagnostics, preflight, compact post-edit previews and bounded three-way recovery receipts | `supplementary_implementation` | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | `packages/hashline/src/mismatch.ts`; `packages/hashline/src/diff-preview.ts`; `packages/hashline/src/snapshots.ts`; selected preflight/prepare contracts in `packages/hashline/src/patcher.ts` | TypeScript | `apps/web/src/features/diff-review/hashline.ts`; `merge.ts`; `transactions.ts`; receipt validators | TypeScript | bounded same-language semantic integration; no upstream filesystem or patcher owner | Browser preflight/receipt projection only; backend remains final authority |
| Patch transaction, immutable read evidence, idempotency fence, snapshot, verification, rollback and quarantine | `existing_primary_owner_integration` | Zyra M1 workspace runtime at baseline | `packages/workspace/zyra_workspace/transactions.py`; `integration_models.py`; `git_boundary.py` | Python | `apps/api/zyra_api/diff_review_api.py`; existing workspace runtime | Python | direct owner binding and additive API projection; no reimplementation | `WorkspacePatchTransactionRuntime`, `WorkspaceManagerRuntime`, `WorkspaceIntegrationStore` |
| Interactive permission and sealed deterministic denial | `existing_primary_owner_integration` | Zyra TypeScript E02 permission runtime at baseline | `packages/runtime/claude-runtime/src/permission/**`; Python typed effect port | TypeScript with Python port | diff-review mutation API and browser receipt handling | TypeScript/Python | exact-call enforcement and permit claim; no Python decision fallback | `typescript.PermissionCoordinator` |
| Artifact revisions and task membership | `existing_primary_owner_integration` | Zyra M2-S03A-01 at baseline | `apps/api/zyra_api/artifact_api.py`; `apps/web/src/features/artifacts/**` | Python and TypeScript/TSX | diff-review source resolution and post-commit refresh | Python and TypeScript/React | additive composition | `TaskState.artifacts`, `LocalArtifactStore`, existing artifact read service |

OpenClaw is excluded forward-only. It will not be read, restored, cited as an
implementation source, tested, imported or introduced as a dependency.

## Complete implementation boundary

The slice will implement one connected production path:

1. A task-scoped diff API resolves an immutable patch artifact revision and
   returns bounded file/hunk/line pages with cancellation and continuation
   receipts.
2. The TypeScript runtime strictly validates the wire contract, parses file,
   hunk and line identity, schedules bounded fetches, virtualizes hunks, folds
   unchanged regions, searches visible/full loaded content and rejects
   overlapping or stale pages.
3. The React workbench exposes add/delete/modify/rename/binary,
   encoding/line-ending and large-file states; old/diff/new and split/unified
   modes; file/hunk navigation; selection and review comments.
4. Browser preflight binds base/current/proposed SHA-256, mtime, mode, revision,
   artifact, transaction, causation, permission and idempotency identities.
5. Apply and rollback requests go through the task diff-review API. The API
   enforces TypeScript permission before mutation and delegates the write set
   to `WorkspacePatchTransactionRuntime`; direct file writes are forbidden.
6. Result receipts expose committed, rejected, stale, conflict, rolled-back or
   quarantined phases and correlate artifacts, events, terminal/test evidence
   and timeline refresh to the same transaction.
7. Sealed requests are read-only: human mutation attempts are deterministically
   denied before permission wait or workspace mutation. They increment only a
   denied-attempt diagnostic; `human_intervention_count` remains zero.
8. TaskDetail mounts the workbench beside the existing artifact view, and a
   committed/rolled-back receipt refreshes the existing artifact and event
   projections rather than creating a browser truth store.

## Canonical state custody

| State | Canonical owner | Slice responsibility |
|---|---|---|
| File bytes, mode, mtime and workspace binding | `WorkspaceManagerRuntime` and backend | Read-only projection plus expected-value preconditions |
| Patch journal, idempotency, snapshot, path results, rollback and recovery input | `WorkspacePatchTransactionRuntime` / `WorkspaceIntegrationStore` | API adapter and typed receipt projection |
| Dirty/nested repository facts | `WorkspaceGitBoundary` / workspace dirty-state runtime | Risk projection only |
| Permission decision, ASK continuation and execution permit | `typescript.PermissionCoordinator` | Exact mutation-call enforcement/claim |
| Artifact membership, bytes and immutable revision | `TaskState.artifacts` / `LocalArtifactStore` | Source binding and post-transaction refresh |
| Review selection, folds, filters, search cursor and draft comments | Browser `DiffReviewRuntime` | Bounded transient view state; never filesystem truth |
| Review/apply/rollback audit visibility | Canonical event log plus workspace transaction receipts | Correlated projection; no second receipt database |

No existing schema, event, persistence, lease, idempotency or restore owner is
transferred. All new API schemas are additive. The mutation endpoint cannot
fall back if permission, workspace runtime, artifact binding or receipt
validation is disabled.

## Security and failure policy

- Path values are logical workspace paths only and are validated by the
  existing workspace path policy. Physical roots never cross the API.
- Diff artifacts are read through the task-scoped artifact service and exact
  revision. Arbitrary user paths cannot select server files.
- Patch content is budgeted by files, lines and UTF-8 bytes in both browser and
  server.
- `base_sha256`, `base_mtime_ns`, `base_mode`, workspace owner epoch,
  binding revision and lease are checked before mutation.
- Stale or concurrent changes fail closed. Three-way merge is an explicit
  proposal with conflicts; it is never silently auto-applied by the browser.
- Binary, undecodable, mixed-encoding and oversized content is inspectable as
  metadata/hex summaries but is not text-applied.
- Interactive mutation requires an ALLOW decision or a consumed exact permit.
  ASK returns a pending receipt; DENY returns a recovery/replan receipt.
- Sealed mutation returns a deterministic denial without creating an approval
  request and without increasing human intervention.
- Rollback failure is surfaced as quarantined/recovery-required. UI success is
  impossible without a committed canonical workspace receipt.
- No root source repository path, npm link, editable install, source process,
  source cache or runtime dependency may be introduced.

## Planned production modules

The TypeScript/React feature will be split by real runtime responsibility:

- strict contracts and identity validation;
- unified-diff parsing and classification;
- file/hunk/line indexes and view-model projection;
- byte/request/page budgets and request scheduling;
- page assembly, cancellation and cache fencing;
- hunk virtualization, folding and keyboard navigation;
- loaded-content search with cancellation;
- hashline-style snapshot/preflight diagnostics;
- explicit three-way merge/conflict projection;
- review selection/comment drafts and audit causation;
- apply/rollback transaction request and receipt handling;
- workbench controller and React views;
- typed protocol/client endpoints and TaskDetail integration.

Python changes are limited to the true task-scoped API composition, permission
port binding, workspace owner delegation, event persistence and tests. Python
does not count toward the declared TypeScript/React floor.

## Direct behavior-test plan

Browser tests will cover:

- add/delete/modify/rename/binary and encoding/line-ending classification;
- multiple files, multiple sections and overlapping/invalid hunk rejection;
- stable file focus across refresh, filtering and comment focus;
- 100k-line hunk virtualization, paging, folds and keyboard navigation;
- budget exhaustion, partial page assembly, duplicate/overlap fencing,
  cancellation and disconnected owner failure;
- search cancellation and results restricted to verified loaded ranges;
- snapshot hash mismatch, mtime/mode mismatch and stale receipt rejection;
- clean three-way merge and explicit overlapping conflict;
- interactive allow/ask/deny receipt flow and exact idempotent retry identity;
- sealed mutation denial with zero human intervention;
- committed, stale, conflict, rollback failure, concurrent patch and disabled
  runtime receipts;
- apply/rollback causation across event, artifact and transaction identities;
- workbench mount and post-receipt refresh behavior.

Backend tests will use temporary real workspaces, artifacts and HTTP routes to
prove:

- exact-revision diff paging and no arbitrary path disclosure;
- mutation is blocked before the workspace owner when permission is not ALLOW;
- approved apply reaches `WorkspacePatchTransactionRuntime`;
- stale SHA/mtime/mode and concurrent mutation fail before canonical commit;
- idempotent retry returns the same canonical transaction;
- a verification failure performs write-set rollback;
- rollback failure emits a quarantined recovery receipt;
- sealed mode never creates approval work or human intervention;
- disabling diff owner, permission binding or patch owner fails closed.

## Effective-code gate and parent close

Only executable TypeScript/TSX production logic and active UI behavior in
`baseline..implementation` may count. Imports, comments, blanks, interfaces,
type-only declarations, static schema/data, CSS/static JSX, tests, fixtures,
generated code, Python, docs, source-ledger entries and adapter-only code are
excluded. Per-file buckets and a large-file review will be emitted after the
implementation commit.

The implementation must contain non-zero original-language production from the
opencode primary chain and both OpenHands/OMP supplementary chains. The slice
fails if any claimed same-language source becomes a ledger-only or test-only
entry.

After the implementation commit, the audit must establish at least `7,500`
effective TypeScript/React lines for this slice. Because this is the last
`M2-03A` slice, the same scanner will recompute the parent interval from
`1833319acdb8a09fac3438b12356da0e9c78d6bb` and establish at least `15,000`
parent-effective lines. Arithmetic addition alone is not accepted.

## Upgrade-trigger assessment

No high-risk trigger is planned:

- no incompatible public schema/event/persistence migration;
- no canonical state owner or transaction/lease/idempotency/restore transfer;
- no global default permission, scheduler, recovery, compact or fallback
  change;
- no dependency, MCP server, plugin, source process, dynamic import, Docker
  runtime or new local product port;
- no workspace, source repository, packaging or commit-boundary change.

The slice therefore uses direct behavior tests and adjacent regressions within
the ordinary slice budget. If implementation reveals an owner transfer,
incompatible contract or global policy change, production work stops and this
decision is amended and committed before crossing that boundary.
