# M2-S03A-01 artifact custody, catalog and viewers critical self-review

## Verdict and commit identity

Verdict: **passed**.

- Baseline:
  `1833319acdb8a09fac3438b12356da0e9c78d6bb`
- Preimplementation decision:
  `228c1cfa41c3a8c2b5d6f45350576413f258fb29`
- Implementation:
  `9ca93e348731ae2d3dded26b8efa9d87165583cc`
- Evidence: `this_commit`
- Exact implementation interval:
  `1833319acdb8a09fac3438b12356da0e9c78d6bb..9ca93e348731ae2d3dded26b8efa9d87165583cc`

The parent `M2-03A` is not complete: this is its first slice. The next entry is
`M2-S03A-02`, which owns diff/PatchEngine/review receipts.

## What became Zyra-owned

The result is not a source pool, sidecar or renamed upstream directory.

- `LocalArtifactStore` now commits bytes atomically and derives the immutable
  revision, SHA-256, size, media family, encoding, BOM, line-ending,
  provenance, security, retention and download metadata from those bytes.
- `ArtifactCatalogService` is a task-scoped projection over canonical
  `TaskState.artifacts`; it does not walk the artifact directory or create a
  second artifact database.
- The API resolves an artifact id only from the selected task, applies the
  existing artifact-root guard, verifies the complete file before returning a
  bounded range and emits a revision/range/transformation receipt.
- The typed client now owns catalog, metadata, content and receipt endpoint
  contracts and normalizers.
- `ArtifactWorkbenchRuntime` owns only transient browser state: selection,
  bounded verified chunks, request scheduling, search, viewer models,
  bookmarks and view preferences.
- Text/Markdown/JSON/binary/image/audio/video rendering is policy-selected.
  Raw HTML/SVG/executable content cannot enter a viewer.
- Timeline and topology artifact controls dispatch the same immutable
  selection event used by the task-detail artifact workbench.

Deleting or disabling the API read gate makes the true HTTP catalog fail with
503 in the integration test. Disabling browser receipt admission throws before
content is cached. Disconnecting the range reader drives the workbench to an
explicit disconnected state. These are behavior failures, not import checks.

## Canonical state custody

| State | Canonical owner | Browser responsibility |
|---|---|---|
| Artifact references and task membership | `SQLiteStore` / `TaskState.artifacts` | Read-only projection |
| Artifact bytes and committed integrity metadata | `LocalArtifactStore` | None |
| Catalog/read/download policy and receipts | `ArtifactCatalogService` and bounded API audit | Receipt verification only |
| Event, topology and timeline artifact references | Existing canonical event/projection owners | Dispatch immutable selection |
| Range cache | None outside browser; reconstructible | Bounded LRU, never truth |
| Bookmark, pin, scroll, zoom and expanded JSON paths | Browser preference stores | Bounded transient preference |
| Diff, patch mutation and review receipts | Not implemented in this slice | Reserved for `M2-S03A-02` |

No public schema or persistence migration moved an existing canonical owner.
New `ArtifactRef.metadata` keys are additive and old references are verified
from their canonical bytes instead of being silently rewritten.

## Source-role and language review

The frozen decision was followed:

- `opencode@adf178a6...` is the single primary implementation source for the
  TypeScript bounded cache, stable selection, lifecycle, virtualization and
  search chain.
- `OpenHands@c105a823...` supplements safe file/Markdown interaction in
  TypeScript/TSX and atomic/streaming file-store mechanics in Python.
- Agent Framework is conformance only for event/history lifecycle.
- OMP hashline is conformance only for hash/version-bound negative receipts.
- No OpenClaw source, path, test, dependency or runtime was read or introduced.

The source ledger contains five entries: one primary TypeScript, one
supplementary TypeScript, one supplementary Python and two conformance entries.
All production target bindings exist at the implementation commit. The Python
custody/API work is required implementation but receives zero credit toward
the TypeScript/React line floor.

## Critical findings discovered and fixed

1. Legacy references initially produced empty revision/digest fields in a
   catalog projection. The API now verifies canonical bytes when integrity
   metadata is missing and projects an explicit legacy status.
2. The first browser catalog parser returned a hard-coded non-disclosure flag
   without rejecting a malicious `artifact_root_disclosed=true` response. It
   now validates and rejects disclosure.
3. The first browser read parser did not correlate response task id to receipt
   task id. The parser now fails on cross-task receipts before admission.
4. Active HTML/SVG/executable content was refused for media/download but could
   still be requested as preview. Server policy now refuses preview, search,
   media and download, while the browser independently rejects active,
   executable or `allow_inline=false` responses.
5. The browser audit digest originally sorted only top-level JSON keys. It now
   recursively canonicalizes nested range/detail objects before hashing.
6. Cached search coverage was counted as newly admitted bytes. Audit accounting
   now counts only non-cache range responses.
7. Windows path segments allowed a colon through the generic token grammar.
   The custody boundary now rejects Windows-reserved path characters.
8. New typed endpoints had no response normalizers in the shared registry.
   Catalog, metadata, content and receipt responses now have explicit
   record-level normalizers before the stricter artifact parser.

No blocker remained after these fixes.

## Semantic and adversarial evidence

Backend tests use actual temporary files and a true `ThreadingHTTPServer`:

- atomic digest/size/revision/provenance/security metadata;
- UTF-16 BOM, CRLF/LF/CR and partial-code-point range decoding;
- root escape, missing file, digest mutation, revision mismatch and range
  refusal;
- signed cursor/filter mismatch and legacy-reference verification;
- server secret redaction and prompt-injection quarantine;
- secret, HTML, SVG and executable refusal;
- bounded large binary range plus receipt;
- task-scoped catalog, metadata, content, partial download and receipt routes;
- an owner-disable probe that turns the real catalog route into 503.

Browser tests prove:

- strict wire identity, count, root-disclosure, revision, task and receipt
  checks;
- independent secret redaction and prompt quarantine;
- stale/conflicting/secret/active/disabled receipt refusal;
- byte/account bounded cache behavior and immutable-range conflicts;
- 10,000-artifact filtering with a bounded virtual window;
- a 20,000-line trigram index with cancellation;
- safe Markdown tokens and refused active links;
- valid/malformed JSON tree behavior and bounded binary hex rows;
- complete SHA-256 media assembly before object URL creation and URL revocation;
- real workbench catalog → metadata → range → viewer flow, bookmarks, search,
  audit causality and disconnected failure.

## Effective-code audit

The exact AST/scanner audit reports:

| Bucket | Lines |
|---|---:|
| Raw additions | 15,640 |
| Raw deletions | 118 |
| Production runtime | 8,585 |
| Active UI behavior | 492 |
| **Conservative effective TypeScript/React** | **9,077** |
| Required minimum | 7,500 |
| Margin | 1,577 |
| Static JSX/CSS presentation excluded | 1,366 |
| Interfaces/type declarations excluded | 1,322 |
| Schema/data excluded | 57 |
| Python/generated owner integration excluded | 1,975 |
| Tests/fixtures/probes excluded | 1,394 |
| Docs/comments/imports/blanks excluded | 449 |
| Adapter-only excluded | 0 |
| Vendor/source-pool excluded | 0 |

Large-file inspection found no opaque bundle, generated behavior disguised as
source, second store, fixture fallback or source-pool code. The largest
effective contributor is `contracts.ts` at 1,090 executable validation lines,
12.01% of the total; no file exceeds 20%. TSX/CSS high-exclusion files contain
presentation, while Python owner integration and tests remain excluded from the
declared floor.

## Verification results

- Focused backend artifact/API: 11 passed.
- Existing runtime protocol adjacency: 18 passed.
- Focused browser artifact behavior: 13 passed, 70 assertions.
- All Web tests: 124 passed, 837 assertions.
- Web TypeScript check: passed.
- Production Web build: passed, 184 modules bundled.
- Exact effective-line audit: passed, 9,077 / 7,500.
- Slice ledger sync/check: 5 entries, zero missing targets.
- Production relative-source dependency scan: zero matches.
- Package/lockfile dependency changes: zero.
- `git diff --check`: passed before both implementation and evidence commits.

The broad historical `test_api_control_commands.py` file did not complete
inside a five-minute diagnostic cap and is not claimed as passed. Its relevant
artifact route behavior is covered by the new true-HTTP integration test; the
unrelated long suite remains assigned to the `M2-03` numeric-stage aggregate.
This is not used to waive a current behavior or failure-path test.

The generic repository ledger verifier still reports the same three
repository-wide relative-source tokens in the unchanged protected
`packages/evaluation/zyra_evaluation/m1_hardening/long_horizon_runtime.py`.
They predate the baseline and were not introduced or edited by this slice. The
slice-specific ledger and dependency audits pass.

## Browser and clean-state review

The local API and production bundle started successfully and both returned
HTTP 200 from isolated temporary state. Browser control setup found no
available browser instance, so no screenshot or manual visual-pass claim is
made. The replacement evidence is the production TaskDetail mount, complete
Web interaction suite, typecheck and production bundle. The isolated browser
processes and temporary state were removed afterward.

This ordinary first slice did not trigger a numeric-stage cleanroom:

- no incompatible public schema/event/persistence change;
- no canonical owner, lease, transaction, idempotency or restore transfer;
- no global permission/scheduler/recovery/compact/fallback change;
- no new dependency, subprocess runtime, port, MCP server, plugin or dynamic
  package import in the product;
- no cross-slice regression was observed.

The full cleanroom and cross-unit audit remains mandatory after all `M2-03`
sibling units, as specified by the project policy.

## Final blocker check

- No browser artifact truth store: passed.
- No root scanning or arbitrary browser path: passed.
- Corrupt/missing/revision mismatch fails before render: passed.
- Server and browser security boundaries both active: passed.
- Unknown/active media is non-executable: passed.
- Ordinary large content remains range- and memory-bounded: passed.
- Cancellation does not cancel artifact production: passed.
- Timeline/topology links keep immutable revision identity: passed.
- Non-zero primary TS/TSX, supplementary TS/TSX and supplementary Python:
  passed.
- Conservative effective floor: passed.
- No root-source runtime/build/test dependency: passed.
- No premature diff/PatchEngine/review owner: passed.

The slice is ready to advance to `M2-S03A-02`.
