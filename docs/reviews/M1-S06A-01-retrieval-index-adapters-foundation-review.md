# M1-S06A-01 Retrieval and Code Index Adapters Foundation Review

## 1. Review identity

- Slice: `M1-S06A-01`
- Measurement baseline: `733f9a7d05530c241e3dba3b765be6033ba54769`
- Final implementation commit: `b1086e064731ee87baa030e3d3de30405acc6972`
- Review type: ordinary-slice incremental critical self-review
- Result: **PASS for M1-S06A-01**
- Parent result: **M1-06A remains in progress**
- Next slice: `M1-S06A-02`

The verdict rests on real SQLite FTS5 queries, a real killed worker process and lease takeover,
canonical-memory hydration, workspace-bound code reads, patch-transaction dereferencing, real HTTP
API paths and fail-closed disable effects. Ledger rows, file presence and source volume are not used
as substitutes for those behaviors.

## 2. Outcome

This slice adds two rebuildable derived-index domains without moving canonical ownership. The
Memory retrieval index derives documents from `SQLiteStore`/`MemoryRecordStore`, stages a new
generation, and atomically publishes it only while a live generation/token/owner lease still
matches. `MemoryFabric.query` and the task memory API now use this runtime instead of a legacy
substring fallback. Retrieval combines safe FTS5 candidates, optional injected vector candidates,
intent and temporal signals, deterministic RRF/MMR ordering, pre-limit filters and explicit output
budgets. Query receipts retain only a digest and bounded diagnostics, never raw query text.

The code index derives file, content, symbol, reference and call-edge state from a live
`WorkspaceManagerRuntime` binding. It applies VCS/vendor/generated/binary/path budgets, rejects
Windows drive/UNC/device/ADS/traversal and escaping link paths, supports bounded literal/regex/
multiline content search, Python AST symbols and bounded structural fallbacks, and consumes only
canonical committed workspace patch transactions. An explicitly injected LSP client is reachable
through `CodeIndexRuntime`; it neither installs nor starts a server, and invalid/out-of-workspace
results visibly degrade to the local index.

## 3. Goal coverage

| Requirement | Semantic evidence | Result |
| --- | --- | --- |
| Canonical memory remains owner | Derived documents carry canonical record revision/digest; retrieval hydrates from the record store and repairs stale derived state | PASS |
| Durable job lifecycle | Queued/leased/building/publishing/ready plus failed/stale/requeued state is persisted in the retrieval SQLite database | PASS |
| Lease fencing | Generation, random lease token, owner and unexpired deadline are checked on heartbeat, transition and publication | PASS |
| Atomic publication | Workers write generation-scoped staging rows; one fence-validated transaction replaces the active pointer | PASS |
| Crash recovery | A real CLI worker process is killed while building; TTL sweep marks it stale, creates the next generation, and another worker publishes | PASS |
| Late-worker exclusion | The killed worker's old generation/token cannot transition or publish after takeover | PASS |
| Retrieval quality and safety | FTS5 token safety, `cat` versus `category`, multi-filter preselection, deterministic concurrent ranking, temporal/intent/RRF/MMR and budgets are tested | PASS |
| Optional vector behavior | No provider causes explicit `unavailable` diagnostics; no model download, network call or silent pseudo-vector fallback occurs | PASS |
| Workspace-scoped code index | Real WorkspaceManager binding, separate derived database, ignores, byte/file/result budgets and revision checks are exercised | PASS |
| Symbols and LSP | Python AST, TypeScript structural fallback and injected LSP definition lookup work; an escaping LSP URI visibly falls back | PASS |
| Patch invalidation | Event payload paths are ignored; the bridge dereferences the canonical committed transaction and applies it idempotently | PASS |
| Main-path and disable effect | Memory and code-index HTTP routes return real indexed results; disabling CodeIndex returns 503 without a legacy fallback | PASS |
| Source and volume gates | Two owner-filtered ledger entries are connected; conservative production is 8,638 effective lines against 8,500 | PASS |

## 4. State custody

| Domain | Canonical owner | Derived/non-owner boundary |
| --- | --- | --- |
| Memory records and revisions | `SQLiteStore` and `MemoryRecordStore` | `MemoryIndexRuntime` may index, hydrate and rebuild; it cannot author canonical memory |
| Retrieval documents, FTS rows and active generation | `RetrievalIndexStore` as disposable derived state | Rows retain canonical record IDs/revisions/digests and can be dropped and rebuilt |
| Index jobs, leases, staging and publication | `RetrievalIndexStore` plus `IndexJobRuntime`/`IndexWorker` | Lease fencing governs derived publication only; it does not take memory custody |
| Memory query behavior | `MemoryFabric` calling `MemoryIndexRuntime` | No substring fallback masks a disabled or broken derived index |
| Workspace identity and file revisions | `WorkspaceManagerRuntime` and workspace file/patch transaction records | `BoundWorkspaceSource` accepts a live access handle and redacts the physical root from public status |
| Code file/content/symbol state | `CodeIndexRuntime` and `CodeIndexStore` as disposable derived state | Stale binding revisions fail; a rebuild creates the next atomic generation |
| Patch truth | Canonical committed workspace transaction | `WorkspacePatchIndexBridge` dereferences transaction ID and never trusts event-supplied paths |
| Vector/LSP enhancement | Explicitly injected providers | Neither adapter auto-installs, opens ports, starts a process or becomes a canonical state owner |
| Events/artifacts | Existing event-log and artifact owners | Retrieval/code results carry causal source refs; index stores do not replace those owners |

## 5. Lease, staging and crash-recovery invariants

An acquired job lease contains the job ID, monotonically increasing generation, cryptographically
random token, worker owner and expiry. Heartbeat and every state transition use all of those fields.
The worker builds only into generation-scoped staging tables. Publication rechecks the same live
lease inside the database transaction before moving staged rows and changing the active-generation
pointer. Expiry changes the abandoned job to stale, records the reason and queues a successor at
generation plus one. Repeating the sweep is idempotent.

The process recovery integration test starts `zyra_memory.index_worker_cli` as an actual child
process, waits until it has a building lease, kills it, expires/sweeps that lease, and drains the
successor with another worker. It then proves the old lease is fenced, restarts the runtime from the
same databases, retrieves the published record and reads the persisted audit trail. This is not a
fixture replay or an in-process exception simulation.

## 6. Retrieval behavior

`RetrievalIndexStore` uses a separate SQLite FTS5 database and parameterized metadata predicates.
The query builder tokenizes hostile operators instead of admitting user-created FTS syntax.
Task/run/session/scope/kind/time/privacy/confidence filters are applied before the candidate limit.
FTS and optional vector ranks are fused deterministically; intent, temporal relevance, recency and
MMR diversification have stable tie breakers. Candidate, result, character and diagnostic budgets
are explicit.

The default vector adapter reports `configured=false`, `available=false` and the reason. The
injected exact-cosine adapter accepts vectors supplied by the caller and owns no embedding model.
No dependency download or network fallback exists. Query receipts persist query ID/digest, scope,
counts, truncation and provider diagnostics without raw query text.

## 7. Code-index behavior

Discovery walks only the bound workspace root, applies ignore rules and budgets before content is
admitted, and indexes neither `.git`, vendor nor generated/minified files by default. Content
search supports case, whole-word, glob/language, literal/regex, multiline, context, head/offset and
output/scanned-byte budgets. Symbols use Python AST where semantics are available and a bounded,
explicitly labelled structural parser for TypeScript/JavaScript/Go/Rust. Context results retain
path, lines, file hash, source revision and generation; changed files can select adjacent tests.

The API registry derives a per-workspace database path outside the indexed task root and creates a
runtime only from a valid WorkspaceManager access handle. Search, symbol, context, test-selection,
invalidation and reconcile operations share one typed service boundary. `ZYRA_CODE_INDEX_DISABLED`
returns a typed 503 and does not route to direct filesystem grep. LSP is opt-in by object injection;
status exposes configured/available/capabilities and both auto-install and auto-start remain false.

## 8. Source-to-target decision

| Role | Source mechanism | Zyra target and retained responsibility |
| --- | --- | --- |
| Primary: AgentScope | RAG document/chunk/knowledge shapes and index worker/consumer/sweeper lifecycle at `b6698c5dbaa1aa916925e27402767f45e2405fa4` | Typed retrieval documents, durable jobs, lease recovery, staging and publication in `packages/memory/zyra_memory`. Zyra adds fences absent from the source and does not run an AgentScope service/store. |
| Supplementary: oh-my-pi | MMR, query intent, temporal parsing, polyphonic fusion and vector-index patterns at `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | Bounded deterministic ranking and optional vector diagnostics only. OMP storage, embedding ownership, agent loop and process runtime are not copied. |
| Conformance: LangGraph | Narrow checkpoint identity, pending/committed write and exact-resume contracts | Used to check generation/commit/fence behavior only; no StateGraph, Pregel, channel, store or runtime migration. |
| Reference: Hermes | Retrieval request/response and bounded API patterns | Design check only; no production implementation quota or owner. |
| Reference: opencode and Claude-derived runtime | Code navigation, context and tool-loop consumption patterns | Design check only; neither becomes a code-index store or workspace owner. |
| Forward excluded: OpenClaw | None | No source read, ledger entry, migration, adapter, test quota or runtime dependency. Protected historical facts are unchanged. |

The bundled seed contains exactly two `M1-S06A-01` productized rows: AgentScope primary and
oh-my-pi supplementary. Owner-filtered readiness reports 2 total, 2 connected, 0 blocked, 0
missing target/runtime/test and `ok=true`. The owner policy matrix reports 0 errors, 0 warnings and
0 blockers. The global legacy verifier still reports protected earlier-unit target-path debt and an
obsolete global source-repository set; it is recorded as historical aggregate debt, not rewritten
or presented as this owner-filtered slice result.

## 9. Dynamic reachability and disconnect effect

- `MemoryFabric.query` synchronizes and retrieves through `MemoryIndexRuntime`; disconnecting it
  removes ranked/filtered retrieval and does not expose a substring fallback.
- `GET /tasks/{task_id}/memory` uses the same derived database and canonical record hydration used
  by the runtime tests.
- The memory worker CLI executes the real durable job/lease/staging/publication path used by the
  process-kill test.
- Code-index routes construct a runtime from a live WorkspaceManager handle; direct test roots are
  confined to the explicitly named `for_test` constructor.
- Code search/symbol/context/test selection read the active generation, and a stale workspace
  binding stops them.
- Disabling the code-index service changes a real HTTP response to 503; disconnecting canonical
  patch resolution prevents invalidation rather than trusting event payload paths.
- Injecting LSP changes definition provenance/results; an invalid result changes diagnostics and
  invokes the local-index fallback.

## 10. Behavioral verification

```text
python -m pytest \
  tests/unit/test_retrieval_index_adapters_foundation.py \
  tests/integration/test_index_worker_process_recovery.py \
  tests/unit/test_code_index_foundation.py \
  tests/integration/test_retrieval_api_main_path.py \
  tests/integration/test_code_index_api_main_path.py \
  tests/unit/test_memory_fabric.py -q
# 14 passed, 8 subtests passed in 16.09s

python -m compileall -q \
  packages/memory/zyra_memory \
  packages/code_index/zyra_code_index \
  apps/api/zyra_api
# passed

python scripts/sync_retrieval_index_source_ledger.py --check
# aligned=true; decision_count=2; owner=M1-S06A-01

zyra_integration_ledger.py --ledger-path <bundled-seed> readiness \
  --owner-unit M1-S06A-01 --json
# 2 total, 2 connected, 0 blocked, 0 missing target/runtime/test, ok=true

zyra_integration_ledger.py --ledger-path <bundled-seed> policy-matrix \
  --owner-unit M1-S06A-01 --include-decisions --fail-on-error --json
# ok=true; 0 errors; 0 warnings; 0 blockers
```

The focused tests also cover exact-token FTS behavior, hostile query text, multi-dimensional
filters, concurrent deterministic retrieval, vector-unavailable reporting, clean rebuild, raw-query
privacy, revision mismatch repair, path attacks, ignore policy, content and symbol budgets,
canonical patch dereference, API source binding and disable effects. Pytest emitted only an
environmental warning that it could not create `.pytest_cache` under the current Windows sandbox;
test behavior and temporary databases were unaffected.

## 11. Effective line buckets

The conservative count takes only nonblank, non-comment additions from the protected baseline.
It excludes tests, package export surfaces, docs/notices, ledger data and the ledger sync helper:

- Memory retrieval/index/job/worker/context/vector runtime including the `MemoryFabric` integration:
  `4,619` effective (`5,040` raw).
- Code index discovery/store/search/symbol/LSP/invalidation/service/runtime: `3,890` effective
  (`4,225` raw).
- API main-path integration: `129` effective (`137` raw).
- Conservative counted production: **`8,638` effective** (`9,402` raw).
- Slice minimum: `8,500`; shortfall: `0`.
- Dedicated behavior tests: `847` effective (`949` raw), excluded; `tests/__init__.py` also excluded.
- Ledger sync helper: `282` raw, excluded as an evidence-maintenance script.
- Ledger seed: `299` additions, excluded as data/evidence.
- Package exports: `189` additions; README/license notices: `25`; excluded.
- Generated, data-as-code, mock-only, fixture-only, vendor-like, source-pool and thin-unconnected
  adapter production counted: `0`.

The optional vector and LSP modules are not unconnected line volume: vector-unavailable state is
observed by the default retrieval API, the injected vector path changes fused ranking, and the LSP
adapter is called through `CodeIndexRuntime` with both successful and adversarial URI behavior.

## 12. Adversarial findings corrected

- The primary source lifecycle writes into a live vector index before final publication; Zyra now
  stages every generation and atomically changes one active pointer under a live lease fence.
- A numeric generation alone could be replayed by an abandoned worker; publication now also binds
  a random token, owner and unexpired deadline.
- FTS syntax and substring matching could make `cat` match `category` or admit operators; the query
  tokenizer and whole-token tests close both cases.
- A vector fallback could silently pretend semantic coverage; unconfigured vector state is explicit
  and tested through the real API.
- Patch events could smuggle paths independent of canonical workspace state; the bridge now reads
  only a committed transaction selected by ID.
- An injected LSP result outside the workspace was correctly rejected by path policy but originally
  escaped as an exception. The adapter now converts it to `invalid_lsp_result` and the runtime
  performs a visible local-index fallback.
- Query receipt persistence initially risked retaining excessive request detail; receipts now store
  only a digest and bounded operational diagnostics.

Blocking findings remaining inside `M1-S06A-01`: `0`.

## 13. Verification scope and open work

This ordinary slice did not trigger a canonical-owner transfer, incompatible public persistence
migration, new external dependency, default external process/server, port, Docker or dynamic-import
path. Focused behavior plus adjacent MemoryFabric/API tests therefore satisfy the incremental gate;
the M1-06 numeric-stage aggregate remains responsible for cleanroom and broad cross-unit regression.
The real worker subprocess exists only as an explicitly invoked package CLI used by worker/runtime
operations and tests; it is not started by default.

No embedding provider or language-server lifecycle is claimed. Those adapters require explicit
injection and fail/degrade visibly. Large-workspace performance, richer incremental indexing and
the remainder of the parent retrieval/skill-memory integration continue in `M1-S06A-02`. This
review does not claim the M1-06 aggregate, milestone exit, live competition scenarios, 2,000
canonical transitions, dynamic-topology comparison, real local/edge/cloud matrix or final delivery
freeze.

## 14. Final conclusion

`M1-S06A-01` now has rebuildable, fenced retrieval and code-index foundations on real runtime/API
paths while preserving memory, workspace, event and artifact custody. The killed-worker takeover,
late-publish fence, deterministic filtered retrieval, explicit vector/LSP degradation, canonical
patch invalidation, workspace-safe code search and disable effects all have behavioral evidence.
The slice exceeds its 8,500-line conservative production minimum and is complete; parent `M1-06A`
remains open for `M1-S06A-02`.
