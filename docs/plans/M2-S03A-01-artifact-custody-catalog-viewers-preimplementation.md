# M2-S03A-01 artifact custody, catalog and viewers preimplementation decision

## 1. Slice identity and immutable baseline

- Slice: `M2-S03A-01`
- Parent: `M2-03A artifact and diff viewers`
- Numeric stage: `M2-03 artifacts, diff, review and browser`
- Authority entry:
  `../../../docs/milestones/M2-console-demo/slice-03a-01-artifact-custody-catalog-viewers.md`
- Parent authority:
  `../../../docs/milestones/M2-console-demo/unit-03a-artifact-diff-viewers.md`
- Baseline Zyra commit:
  `1833319acdb8a09fac3438b12356da0e9c78d6bb`
- Baseline worktree: clean
- Decision commit:
  `228c1cfa41c3a8c2b5d6f45350576413f258fb29`.
- Implementation commit:
  `9ca93e348731ae2d3dded26b8efa9d87165583cc`; it is later than the
  decision commit and contains production code plus directly related behavior
  tests.
- Evidence commit: `this_commit`; it is later than the implementation commit
  and contains the exact-commit audit, source-ledger update and critical
  self-review.

The authority state protects every slice through `M2-S02B-02`. This decision
does not reopen their event, projection, topology, timeline, command,
permission, recovery, worker or checkpoint owners.

## 2. Completion boundary

The slice is complete only when the real task-detail route supports all of the
following against artifacts referenced by the canonical task/event state:

1. a versioned artifact contract exposes immutable identity, digest, size,
   media type, encoding, producer node/span/tool/worker, security label,
   retention and revision facts;
2. the existing Python `LocalArtifactStore` remains the content and metadata
   custody owner and persists integrity metadata on new writes;
3. artifact catalog reads are task-scoped, deterministic, paginated and
   filterable by task, node, worker, type, time and revision without adding a
   second artifact database or frontend truth store;
4. every content read resolves a canonical artifact reference, remains under
   the configured artifact root, validates immutable size and digest, enforces
   media/security/download policy and emits an auditable read receipt;
5. text, Markdown, JSON, image, audio/video and binary/unknown artifacts use
   explicit safe viewers; unknown media never becomes executable content;
6. large text/JSON uses bounded byte ranges, bounded decoded cache, virtual
   rows, incremental search and cancellation rather than reading or rendering
   the complete payload in the browser;
7. UTF BOMs, line-ending variants, a trailing partial code point and malformed
   JSON remain explicit;
8. server redaction is repeated at the browser boundary, untrusted prompt-like
   content is visibly quarantined, and secret artifacts fail closed;
9. timeline/topology/event artifact references can open the same selected
   immutable revision, while pin/bookmark/final-report references remain view
   preferences rather than artifact truth;
10. missing, corrupt, truncated, binary, oversized and disconnected reads have
    distinguishable recovery states and cannot be hidden by a fixture fallback.

This slice does not implement the `M2-S03A-02` diff/PatchEngine/review receipt
owner. It may reserve typed links for a later diff route, but it will not add a
parallel patch algorithm or mutable review store.

## 3. Frozen source, role, language and owner decision

| Capability chain | Source role | Repository and exact commit | Exact source paths / symbols | Source language | Zyra target | Target language | Migration mode | Canonical owner after integration | Predecision reference |
|---|---|---|---|---|---|---|---|---|---|
| Bounded content LRU, bounded persisted view state, stable tab/selection/scroll identity, explicit loading/error/media states, virtual catalog rows | primary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/app/src/context/file/content-cache.ts`; `context/file/view-cache.ts`; `pages/session/file-tabs.tsx` (`createScrollSync`, `FileTabContent`); `pages/session/v2/session-file-list-v2.tsx` (`SessionFileListV2`) | TypeScript/TSX | `apps/web/src/features/artifacts/{cache,catalog,content,viewers,view}/**` | TypeScript/TSX | `cropped_migration` and `same_language_component_integration` | Python artifact store/API owns bytes and immutable metadata; React owns only bounded presentation state | current document sections 4-9 |
| Atomic file custody, stream-oriented large-object boundary, reusable safe Markdown elements, non-executable external links and file list interaction | supplementary | `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | `openhands/app_server/file_store/files.py` (`write_from_path`); `file_store/local.py` (`write`, `write_from_path`); `frontend/src/components/features/files/{file-list,file-item}.tsx`; `frontend/src/components/features/markdown/{markdown-renderer,anchor,code,table}.tsx` | Python and TypeScript/TSX | `packages/runtime/zyra_runtime/artifacts.py`; `apps/api/zyra_api/artifact_api.py`; safe artifact viewers | Python and TypeScript/TSX | `cropped_migration` and `same_language_component_integration` | `LocalArtifactStore` remains the only byte/metadata owner; artifact workbench is a projection | current document sections 4-9 |
| Event/history ordering and run-lifecycle interoperability | conformance only | `agent-framework@d50698bb797710bfd1ebf34eb621c905a4009b2d` | `python/packages/ag-ui/agent_framework_ag_ui/_agent.py`; `_agent_run.py` event and history contracts | Python | artifact-reference jump and lifecycle tests | tests | `conformance_only` | no production owner | current document section 10 |
| Hash-/version-bound receipt discipline | conformance only for this slice | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | `packages/hashline/src/apply.ts`; exact-file/version validation and applied-edit receipt vocabulary | TypeScript | immutable artifact revision/read-receipt negative tests | tests | `conformance_only` | artifact owner/read gate owns receipts; no PatchEngine before `M2-S03A-02` | current document sections 5 and 10 |
| OpenClaw | excluded forward only | none | none | none | none | none | `excluded_forward_only` | none | project forward boundary |

The production result must contain non-zero primary original-language
TypeScript/TSX migration, non-zero supplementary TypeScript/TSX migration and
non-zero supplementary Python migration. Agent Framework and OMP do not create
production-code quotas in this slice. No runtime, build or test path may
reference a workspace source repository.

## 4. Canonical custody and immutable contract

`LocalArtifactStore` remains the only task-artifact byte owner. New atomic
writes calculate metadata from the bytes that are actually committed and place
the following fields in `ArtifactRef.metadata`:

- contract version;
- SHA-256 digest and content length;
- normalized safe media type and detected content family;
- declared/detected encoding and byte-order mark;
- producer node, span, tool call and worker;
- security label and trust disposition;
- retention policy and expiry, if any;
- immutable revision derived from the digest;
- download disposition;
- line-ending summary for textual content.

The existing `ArtifactRef` schema remains backward compatible. New contract
fields live in metadata so earlier completed slices and event serialization do
not receive an incompatible public schema migration.

Legacy references may be described but are never silently upgraded in a
frontend store. A read computes observed size/digest from the actual file and
compares them with any persisted expectations. Missing expected integrity
fields are surfaced as an explicit legacy/incomplete status. A mismatched
digest or size is corruption and fails before content is returned.

The immutable content revision is not a mutable filename. The API resolves an
artifact id to a canonical `ArtifactRef`, verifies the expected revision, and
then resolves its URI through the existing store root guard. No route accepts
an arbitrary path from the browser.

## 5. Catalog and read path

The real path is:

```text
timeline / topology / event artifact reference
  -> M2-01B artifact selector
  -> ArtifactWorkbench selection
  -> typed TaskApi artifact request
  -> task-scoped artifact route
  -> canonical TaskStore ArtifactRef lookup
  -> LocalArtifactStore root resolution
  -> size + digest + media + security gate
  -> bounded byte range / safe media descriptor
  -> browser receipt verification + second redaction
  -> explicit viewer
```

Catalog ordering uses immutable `(created_at, artifact_id, revision)` keys and
opaque continuation cursors. Filtering is a pure projection over canonical
references and supports task, producer node, worker, content family/media type,
time interval and revision. The catalog cannot discover unrelated files by
walking the artifact root.

Content reads carry artifact id, expected revision, offset and bounded length.
The server:

1. looks up the reference in the requested task;
2. rejects task/revision/URI mismatch;
3. resolves below the artifact root;
4. streams the complete file only through the integrity hasher;
5. returns at most the configured range;
6. aligns textual boundaries so a split code point is recoverable;
7. applies server secret and prompt-content policy;
8. returns an integrity/read receipt tied to artifact, revision, digest, range
   and transformation policy.

The browser verifies receipt identity and range continuity before admitting a
chunk to its bounded cache. Abort closes only the HTTP request; it never
deletes, mutates or cancels the artifact producer.

## 6. Security and content policy

The read gate fails closed:

- `secret` artifacts expose contract metadata but no inline content and no
  direct download;
- untrusted text is server-redacted, marked with explicit findings and rendered
  inside a quarantine boundary;
- the browser runs independent redaction and prompt-injection scanning before
  React receives renderable spans;
- Markdown is parsed to a limited AST and rendered as React nodes; raw HTML,
  scripts, data URLs, event handlers and automatic image fetches are excluded;
- external links allow only explicit `http`/`https`, use
  `noopener noreferrer`, and never receive trusted instructions;
- image/audio/video viewers use only the canonical content URL/receipt route
  and an allowlisted media family;
- unknown, archive, executable, HTML, SVG and ambiguous media types get a
  metadata/hex viewer only;
- downloads require an explicit server policy decision and a second UI
  confirmation affordance; a guessed MIME or filename never grants download.

Prompt-like text remains visible only as untrusted data. It cannot invoke a
tool, command, navigation, permission or Markdown raw-HTML capability.

## 7. Bounded client runtime

The primary opencode mechanisms are cropped and integrated as Zyra-owned
runtime modules:

- a byte-accounted LRU with per-artifact and global limits;
- stable catalog row keys and a measured/estimated virtual range;
- persisted view preferences capped by task and artifact count;
- scroll/selection restoration tied to immutable revision;
- explicit idle/loading/partial/ready/failed/cancelled/disconnected states;
- incremental text line indexing and search over admitted safe chunks;
- stale request suppression and `AbortController` cancellation;
- range coalescing and bounded concurrent reads;
- recovery that can reuse already verified chunks without treating cache as
  canonical content.

Closing the viewer releases listeners, media object URLs, search work and
pending browser reads. It does not alter task/runtime state. Reopening the same
revision may restore bounded view preferences and verified chunks; selecting a
new revision invalidates the old content view.

## 8. Viewer registry

The registry is policy-driven:

| Content family | Viewer behavior |
|---|---|
| plain text / logs / source | virtual lines, encoding/line-ending status, incremental search and safe highlight spans |
| Markdown | limited Markdown AST, no raw HTML, safe links, virtualized code/text fallback for large documents |
| JSON | incremental raw-text safety, bounded structural parse for small payloads, virtual tree rows, path search, malformed/truncated status |
| image | allowlisted bitmap content only, bounded dimensions/zoom/pan and alt metadata |
| audio/video | allowlisted media only, user-initiated controls, no autoplay, safe metadata |
| binary/unknown | metadata, bounded hex/ascii range and integrity receipt; never execute or inline as HTML/SVG |

Every viewer shares the same integrity/security admission result. Viewer
selection cannot downgrade a refusal or bypass the server.

## 9. Target module plan

Backend production targets:

- `packages/runtime/zyra_runtime/artifacts.py`
  - atomic content metadata capture and streaming helpers;
- `apps/api/zyra_api/artifact_api.py`
  - contract, catalog cursor/filter, integrity/security gate, range reads,
    download decisions and read receipts;
- `apps/api/zyra_api/main.py`
  - narrow route integration using the existing TaskStore and artifact store.

Frontend production targets:

- `apps/web/src/features/artifacts/contracts.ts`
  - wire/runtime discriminants; type-only lines are excluded by the gate;
- `security/**`
  - receipt admission, redaction, prompt guard and safe-link policy;
- `cache/**`
  - byte-accounted LRU, verified range map and bounded view preferences;
- `catalog/**`
  - canonical projection merge, filters, indexes, cursor state, virtual rows;
- `content/**`
  - range scheduler, cancellation, decoding, line index and incremental search;
- `viewers/**`
  - text, Markdown, JSON, media and binary viewer models/registry;
- `navigation/**`
  - timeline/topology/event selection, bookmark/pin/final-report view refs;
- `view/**`
  - task-detail ArtifactWorkbench and accessible bounded viewer components;
- existing typed API and task detail files
  - narrow endpoint binding and real-route mount.

No module in this plan becomes a byte store, artifact metadata owner,
permission owner, producer runtime or patch engine.

## 10. Verification plan

Direct tests must use real temporary files and true canonical task references
to prove:

1. atomic writes persist the correct digest, size, media, encoding, revision
   and producer/security metadata;
2. task/revision/path mismatch, missing content, root escape, hash mismatch,
   size mismatch and mid-read replacement fail closed;
3. catalog order/cursor/filter behavior remains deterministic and task-scoped;
4. UTF-8/16 BOMs, CRLF/LF/CR, split multibyte ranges and malformed decoding
   remain explicit;
5. secret content is refused and untrusted content is independently redacted
   and quarantined at server and browser boundaries;
6. unknown/HTML/SVG/executable media does not become executable content;
7. large real text and JSON read through multiple bounded ranges, virtual
   windows and incremental search with a measured memory ceiling;
8. cancellation, timeout, disconnect, reconnect, stale response and retry
   retain correct request/revision identity;
9. image/audio/video descriptors remain allowlisted and release browser
   resources when the viewer closes;
10. event/timeline/topology references select the same immutable revision and
    bookmarks/pins do not mutate canonical metadata;
11. disabling the API artifact read gate makes the real task-detail behavior
    fail rather than falling back to fixture data;
12. existing API, task detail, M2-01B projection, topology and timeline
    regressions remain green.

Focused verification will include Python artifact/API integration tests, web
runtime/view tests, the production web build/typecheck, dependency/path audit
and exact diff-bound source/effective-line audit. This ordinary slice does not
repeat the later M2-03 numeric-stage cleanroom unless a high-risk trigger is
discovered.

## 11. Effective-code gate

The slice floor is `7,500` conservative effective TypeScript/React production
lines. Python custody/API work is required behavior but does not replace the
declared TypeScript/React floor. The parent `M2-03A` floor is `15,000`; the
second slice will close the residual independently.

The exact Git-object-bound auditor will compare baseline commit
`1833319acdb8a09fac3438b12356da0e9c78d6bb` with the final implementation
commit and report, file by file:

- raw additions;
- executable production runtime;
- active UI behavior;
- static UI presentation;
- type/interface/declaration lines;
- schema/DTO repetition;
- thin adapter lines;
- generated/data/vendor/source-pool lines;
- tests/fixtures/probes;
- docs/comments/imports/blank lines;
- conservative effective production.

Only executable production behavior and conservative active UI behavior count.
Tests, docs, comments, imports, declarations, generated/data, static
presentation, fixtures and thin adapters do not. Every production file above
500 additions, every file contributing above 20% of effective production and
every file with above 30% exclusions receives individual audit.

## 12. Blocker rules

The slice fails if any of these remain true:

- the browser or a new database becomes artifact metadata/content truth;
- the catalog scans arbitrary files instead of canonical references;
- integrity is logged but corrupt bytes are still rendered;
- a secret or untrusted artifact bypasses either redaction boundary;
- unknown MIME is executed, rendered as HTML/SVG or downloaded by default;
- large content is fetched or rendered wholly in ordinary viewer flow;
- cancellation mutates or cancels artifact production;
- a timeline/topology reference opens a different or mutable revision;
- source-language primary TypeScript/TSX or supplementary TypeScript/TSX
  production is zero;
- the conservative TypeScript/React effective floor is not met;
- a root source repository, cache, editable/link dependency, external service
  or fixture fallback is required at runtime, build time or test time;
- this slice implements a second diff/PatchEngine before `M2-S03A-02`.
