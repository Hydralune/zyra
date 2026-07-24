# M2-S03A-02 diff, patch and review integration critical self-review

## Verdict and frozen identity

Verdict: **passed**.

- Slice baseline:
  `5dec6715db9f35913acd1b3a078ce9bcde943a8a`
- Parent baseline:
  `1833319acdb8a09fac3438b12356da0e9c78d6bb`
- Preimplementation decision:
  `1ea59fbea021f2eee171b04e69957039c741657e`
- Implementation:
  `ae2a37849cf1099c03e62e7228bfecb478c2cd8a`
- Evidence: `this_commit`
- Exact slice interval:
  `5dec6715db9f35913acd1b3a078ce9bcde943a8a..ae2a37849cf1099c03e62e7228bfecb478c2cd8a`
- Exact parent interval:
  `1833319acdb8a09fac3438b12356da0e9c78d6bb..ae2a37849cf1099c03e62e7228bfecb478c2cd8a`

This is the last `M2-03A` slice. The parent artifact/diff/viewer unit is
complete and its next entry is `M2-S03B-01`.

## What became Zyra-owned

The implementation is one connected product path, not a source pool or a
black-box patch CLI:

- `DiffReviewApiService` resolves only a task-owned immutable patch artifact
  revision and emits bounded file/hunk/line manifests, content observations
  and receipts without exposing a physical path.
- `contracts.ts` performs strict identity, count, hash, revision, receipt,
  permission and redaction validation before browser admission.
- `unified-diff.ts` owns text patch parsing, classification, coordinate
  validation, forward application and inversion for the browser projection.
- `budget.ts` and `fetch-runtime.ts` own reconstructible byte, line, request,
  deadline, cancellation, page and immutable-cache state.
- `virtualization.ts`, `search.ts`, `review-model.ts` and the React workbench
  own only transient presentation state: focus, fold, selection, filters,
  preferences, measured viewport and loaded-content search.
- `hashline.ts` and `merge.ts` own snapshot-bound preflight diagnostics and
  explicit three-way proposals. They cannot commit a file.
- `transactions.ts` binds exact apply/rollback/comment requests to artifact,
  diff, workspace, permission, precondition, idempotency and causation
  identity. It accepts only canonical API receipts.
- TaskDetail mounts the workbench over the existing artifact catalog and
  typed task API. The typed protocol marks every mutation as receipt-required.

The implementation retained the mature control-flow ideas in the frozen
TypeScript sources while decomposing them into Zyra module boundaries. It did
not copy an upstream directory, import an upstream package, launch a source
CLI or create a second file/patch store.

## Canonical custody and semantic effect

| State | Canonical owner | This slice |
|---|---|---|
| File bytes, mode, mtime, binding and lease | `WorkspaceManagerRuntime` | Read evidence and precondition projection |
| Patch journal, write set, snapshot, idempotency, verification and rollback | `WorkspacePatchTransactionRuntime` / `WorkspaceIntegrationStore` | Typed API request and receipt bridge |
| Dirty worktree and nested repository facts | `WorkspaceDirtyStateRuntime` / `WorkspaceGitBoundary` | Read-only risk projection |
| Permission decision, pending approval and exact permit | TypeScript `PermissionCoordinator` | Direct enforce/claim call; no Python fallback |
| Patch artifact membership, bytes and revision | `TaskState.artifacts` / `LocalArtifactStore` | Exact source binding |
| Review/apply/rollback audit | Canonical event log and workspace receipts | Causal event projection |
| File focus, folds, search, selection, draft and page cache | Browser diff runtime | Bounded reconstructible/transient state only |

Apply and rollback never write a path directly. The API builds existing
`WorkspaceMutation` and immutable read-evidence sets, then calls the existing
workspace transaction owner. Verification occurs after the owner commit.
Rollback uses the owner-created snapshot. A stale hash, mtime, binding, lease
or concurrent write fails before a new canonical commit.

Review selections and comments also call the TypeScript permission owner
before entering the canonical event log. An interactive ASK returns a pending
receipt and an exact permit can be claimed on a later exact request. A DENY
does not change the review revision.

Sealed mode is unconditionally read-only for these human controls. This
remains true even when the permission classifier would otherwise return
ALLOW. Selection/comment/apply/rollback denials preserve
`human_intervention_count=0` and record one denied manual-mutation diagnostic.

## Source role and language review

The frozen source decision was followed:

- `opencode@adf178a6...` is the one primary implementation source for review
  state, file focus, hunk interaction, diff utilities and refresh behavior.
  Its production target is TypeScript/TSX under
  `apps/web/src/features/diff-review/**`.
- `OpenHands@c105a823...` is supplementary for lazy per-file loading,
  explicit loading/error/empty states and read-only diff presentation. Its
  production target is TypeScript/TSX `budget.ts`, `fetch-runtime.ts` and the
  workbench view.
- `oh-my-pi@c6b83c1d...` is supplementary for hashline-style mismatch,
  snapshot, preview and receipt semantics. Its bounded TypeScript targets are
  `hashline.ts`, `merge.ts` and `transactions.ts`; its filesystem patcher is
  not a Zyra owner.
- Existing Zyra workspace, permission and artifact owners were integrated,
  not replaced and not credited as migrated original-language code.
- OpenClaw remained `excluded_forward_only`; no source, path, dependency,
  comparison test or deferred role was introduced.

The ledger contains exactly three current-slice source entries with non-zero
TypeScript production targets at the frozen implementation commit. There is
no cross-language waiver.

## Critical findings discovered and fixed

1. The initial sealed branch only converted permission ASK to DENY. If the
   permission owner returned ALLOW, a sealed manual apply or rollback could
   still mutate the workspace. Sealed now unconditionally returns a
   deterministic denial before workspace mutation; allow-sealed apply and
   rollback behavior tests prove it.
2. Review selection/comment initially mutated canonical event state without
   consulting permission. Review actions now use the same exact-call
   TypeScript permission port and expose accepted/pending/denied causal
   receipts.
3. A binary workspace file was decoded as UTF-8 while building the manifest,
   so a valid binary diff could fail before it became inspectable. Binary
   snapshots now preserve byte size/hash and metadata without entering a text
   decoder or text apply path.
4. The initial large-page client default requested 20,000 lines while the
   server page identity is hunk-atomic up to 100,000 lines. A valid large hunk
   could therefore be impossible to fetch. The default now matches the
   server's bounded 100,000-line/8 MiB immutable page contract.
5. Dirty/nested-repository risk was initially a constant all-false projection.
   It now comes from the existing real `WorkspaceDirtyStateRuntime` and
   `WorkspaceGitBoundary`; a temporary real Git repository proves untracked
   dirty detection.
6. Workspace nanosecond mtimes can exceed JavaScript's safe integer range.
   Wire mtimes are deterministically converted to microsecond precision
   before strict TypeScript admission and comparison.
7. `EventRecord` does not own correlation/causation as top-level fields.
   Diff review and patch events now place those identities in the canonical
   payload contract instead of fabricating unsupported record fields.
8. The typed normalizer registry registered the shared
   `zyra.artifact-read.v2` contract twice for metadata and content. The
   embedded client failed during catalog construction. The shared contract
   now has one record normalizer; all 15 transport tests pass.
9. The initial typed diff mutation definitions were marked as not requiring a
   receipt. The catalog correctly rejected the protocol. Comment/apply/
   rollback are now receipt-required, expected status sets reflect canonical
   pending/deny/conflict receipts, and the HTTP route emits
   `X-Zyra-Receipt-Id`.

No blocker remained after these corrections.

## Behavioral and adversarial evidence

Backend tests use actual temporary artifacts, workspaces, Git metadata and a
true `ThreadingHTTPServer`:

- exact revision, digest and redacted task-scoped manifest/page/content;
- real HTTP manifest, hunk page, receipt header, permission-gated apply,
  workspace mutation and persisted event;
- added, deleted, renamed and binary files in a multi-file patch;
- CRLF preservation through a committed workspace transaction;
- cp1252 review projection with fail-closed UTF-8 patch application;
- real Git dirty/untracked risk from `WorkspaceGitBoundary`;
- causal permission-gated review comment and revision;
- interactive ASK, exact permit retry, DENY, and sealed allow/ask denial;
- idempotent apply replay and same transaction identity;
- concurrent stale hash refusal without workspace mutation;
- successful rollback through the owner snapshot;
- injected restore failure and `rollback_failed` recovery receipt;
- diff owner disable behavior.

Browser/runtime tests prove:

- strict manifest/page/content/transaction contracts;
- immutable page digest and cross-binding refusal;
- forward and inverted unified hunk application;
- binary/NUL refusal;
- bounded request reservation and cancellation without budget leakage;
- unified/split stable row identity and keyboard range selection;
- a 100,200-line projection with only a measured viewport window mounted;
- trigram search with explicit incomplete-file state;
- deterministic read/write ranges and clean/overlapping three-way outcomes;
- observed snapshot acceptance and stale hash rejection;
- review model selection/comments plus binary apply blocking;
- sealed zero-human receipt validation, permission rejection receipt, exact
  retry identity and committed transaction submission.

Disconnecting or disabling the diff API changes true behavior. Removing the
workspace transaction binding makes apply impossible. Removing page receipt
admission prevents the workbench from caching or rendering hunks. These are
main-path behavior failures rather than import or existence checks.

## Effective-code audit

The exact conservative slice audit reports:

| Bucket | Lines |
|---|---:|
| Raw additions | 17,593 |
| Raw deletions | 4 |
| Production runtime | 9,537 |
| Active UI behavior | 547 |
| **Conservative effective TypeScript/React** | **10,084** |
| Required minimum | 7,500 |
| Margin | 2,584 |
| Static JSX/CSS presentation excluded | 1,021 |
| Interfaces/type declarations excluded | 1,299 |
| Schema/data excluded | 5 |
| Adapter-only excluded | 0 |
| Python/generated owner integration excluded | 2,824 |
| Tests/fixtures/probes excluded | 2,116 |
| Docs/comments/imports/blanks excluded | 244 |
| Vendor/source-pool excluded | 0 |

The scanner directly recomputed the parent interval rather than adding two
self-reported totals:

| Parent bucket | Lines |
|---|---:|
| Production runtime | 18,118 |
| Active UI behavior | 1,039 |
| **M2-03A effective TypeScript/React** | **19,157** |
| Parent minimum | 15,000 |
| Margin | 4,157 |

Large-file review found no bundle, data-as-code, source-pool tree, generated
runtime or duplicate schema padding. The largest effective contributor is
`contracts.ts` with 1,472 executable validation lines, 14.60% of the slice
effective total. Static TSX/CSS, Python owner integration and tests receive
zero credit. All three claimed upstream TypeScript roles have non-zero
production paths.

## Verification results

- Focused diff/PatchEngine/backend/HTTP behavior: 17 passed.
- Adjacent artifact, patch-event, workspace gateway and Git boundary: 21
  passed.
- Embedded typed-client real HTTP probes: 3 passed.
- Focused Web diff behavior: 20 passed, 62 assertions.
- All Web plus typed-client tests: 159 passed, 935 assertions.
- Web and typed-client TypeScript checks: passed.
- Production Web build: passed, 196 modules bundled.
- Slice effective-line audit: passed, 10,084 / 7,500.
- Direct parent effective-line audit: passed, 19,157 / 15,000.
- Slice ledger sync/check: 3 entries, zero missing targets.
- Source-language custody: three implementation roles, non-zero TypeScript
  additions, no exception.
- Current production diff parent-source dependency scan: zero.
- Package/lockfile changes: zero.
- `git diff --check`: passed.

The broader seed-ledger unit command reported 20 passed and 2 failed. Both
failures are protected-baseline assumptions: the generic auditor already
finds two `M2-S02A` entries whose `source_repo=zyra` conflicts with its
required-external-repository policy, and a legacy `M1-02B` query expects an
older vendored status combination. No failure names a current `M2-S03A-02`
entry. This slice does not rewrite protected prior entries; its dedicated
3-entry sync, frozen-target and language-custody gates all pass.

The PowerShell `npx.ps1` wrapper emits a denied-path diagnostic while still
running the downloaded Bun command and returning success. Test, typecheck and
build exit codes and summaries are successful; no result is inferred from the
warning alone.

## Browser and clean-state review

The local API and production bundle both started on isolated local ports.
Browser control setup reported zero available browser instances, so no
screenshot, click-through or manual visual-pass claim is made. The Browser
skill forbids substituting an unrelated browser surface. The executable
replacement evidence is the production TaskDetail mount, React workbench
runtime suite, strict typed client, typecheck and production bundle.

This ordinary slice did not hit a high-risk escalation trigger:

- all API contracts are additive;
- no canonical owner, transaction, lease, idempotency or restore semantic was
  transferred;
- no global permission/scheduler/recovery/compact/fallback policy changed;
- no dependency, MCP server, plugin, product subprocess, port, Docker,
  dynamic import or packaging boundary was added.

The full cleanroom and `M2-03` numeric-stage aggregate remain mandatory after
the `M2-03B` sibling units. They are not duplicated here.

## M2-03A parent close

The direct parent path is now complete:

`TaskState artifact -> ArtifactCatalogService -> immutable patch revision -> DiffReviewApiService -> typed TaskApi -> DiffReviewWorkbenchRuntime -> PermissionCoordinator -> WorkspacePatchTransactionRuntime -> canonical event/artifact refresh`

`M2-S03A-01` continues to own artifact custody, catalog, safe viewers and
bounded immutable content. This slice composes, rather than duplicates, that
owner and adds diff/review/transaction behavior. Workspace, artifact,
permission and event custody remain consistent across both slices.

Parent source-to-target entries exist for the artifact viewer and the three
diff/review sources. Parent effective code is 19,157 by one direct scan. The
parent has real main-path, failure-path, permission, stale, rollback,
large-input, build and regression evidence and is ready to advance.

## Final blocker check

- Final Git diff only: no; patch artifact revision and per-file pages are
  canonical inputs.
- Mutation bypasses permission or PatchEngine: no.
- Sealed manual mutation possible after ALLOW: no.
- Stale/concurrent change silently applied: no.
- Binary or non-UTF-8 content enters text apply: no.
- Browser owns file/patch truth: no.
- Review receipt bypasses canonical event causation: no.
- Large diff requires full DOM or unbounded memory: no.
- Upstream CLI/package/source path owns runtime behavior: no.
- Original-language production is zero for a claimed role: no.
- Slice or parent effective floor missed: no.
- OpenClaw forward exclusion violated: no.

The slice and `M2-03A` parent are complete.
