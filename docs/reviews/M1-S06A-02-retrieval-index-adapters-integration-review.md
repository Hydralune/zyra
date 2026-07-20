# M1-S06A-02 Retrieval Index Adapters Integration Review

## 1. Review identity

- Slice: `M1-S06A-02`
- Measurement baseline: `6d58fc74819f437f7bdac697403a2b8e96e5b4e5`
- Final implementation commit: `e04317e11a71dbed9336d5c9823500ec985f16f5`
- Review type: ordinary-slice incremental critical self-review plus exact-commit cleanroom
- Result: **PASS for M1-S06A-02**
- Parent result: **M1-06A complete**
- Next slice: `M1-S06B-01`

The verdict is based on real canonical memory and workspace sources, durable SQLite jobs and
leases, actual child processes, a killed code-index worker, canonical patch-event dereferencing,
the real TypeScript QueryEngine preprocessing boundary, exact retry delivery state and disable
effects. Ledger rows, source volume and import success are not treated as substitutes.

## 2. Outcome

This slice closes the gap between the 06A-01 index foundations and current worker behavior.
Canonical `MemoryRecord` changes are admitted to a durable queue, processed through a supervised
Zyra child process, published under the existing generation fence and queried through a versioned
filter/provenance contract. Workspace initial state and committed 05B patch transactions follow the
same queue/claim/heartbeat/build/publish shape in a dedicated code-index worker. Both indexes emit
causal 05C lifecycle and query facts.

`WorkerRetrievalContextRuntime` consumes only current memory and workspace snapshots. It turns
retrieval selections into deterministic `AgentMessage` inputs plus bounded file/test constraints,
then passes those inputs into the existing TypeScript QueryEngine as
`preprocessed_messages`. Delivery is claimed before the loop, released if the TypeScript host
fails, and committed only after a successful loop. Exact retry reclaims the same source digest and
deterministic message identifiers. Disabling either index visibly changes context/file/test
selection; there is no direct grep, substring-memory or cached-checkpoint success fallback.

## 3. Goal coverage

| Requirement | Semantic evidence | Result |
| --- | --- | --- |
| Durable production update path | Canonical source or committed patch transaction enters a persisted queue, is claimed/heartbeated/built by a child process and is fenced before publication | PASS |
| Memory current-snapshot semantics | Query source references contain canonical record revision/digest; stale derived records are not returned as current facts | PASS |
| Code current-revision semantics | Workspace revision/generation are carried into selections; a stale worker generation cannot publish | PASS |
| Worker loss recovery | A real code-index child is killed in `building`; expiry makes the lease stale, the sweeper requeues a new generation and a restarted child publishes | PASS |
| Duplicate/idempotent work | Admission keys and job identities are durable; duplicate canonical source or patch admissions do not create a second logical update | PASS |
| Concurrent generation fence | Publication checks job, generation, token, owner and expiry atomically; an abandoned generation is rejected | PASS |
| Restart and rebuild | Durable jobs and active generations survive runtime restart; deleting derived state and replaying canonical sources rebuilds equivalent selections | PASS |
| Patch integration | `workspace.patch.committed` dereferences the canonical transaction and immediately invokes the real code-index process path | PASS |
| CodeWorker semantic effect | Retrieved memory becomes QueryEngine input; code context changes selected files and adjacent tests | PASS |
| Exact retry | A failed TypeScript host releases both delivery claims; a retry reclaims the same source digest instead of conflicting or duplicating | PASS |
| Versioned downstream contracts | Memory/code queries, source refs, selections and reference-only recovery/checkpoint refs carry explicit schemas | PASS |
| Disable/disconnect effect | Memory or code disable removes the corresponding inputs; worker/sweeper disable leaves observable queued/stale work rather than reporting success | PASS |
| Parent volume gate | This slice counts 6,566 conservative production lines; 06A-01 plus 06A-02 totals 15,204 against 15,000 | PASS |

## 4. Internalized modules

The AgentScope job/consumer/sweeper mechanism is decomposed rather than hosted as an upstream
runtime:

- `packages/memory/zyra_memory/query_contract.py` defines the versioned consumer, filter, source,
  snapshot, checkpoint and recovery contracts.
- `integration_store.py`, `integration_runtime.py` and `process_supervisor.py` own durable
  admission/query/delivery facts, canonical record admission and the supervised Zyra worker
  process.
- `packages/code_index/zyra_code_index/job_models.py`, `jobs.py`, `worker.py`, `worker_cli.py` and
  `process_supervisor.py` own generation-fenced jobs, leases, phase transitions and atomic
  publication.
- `packages/code_index/zyra_code_index/integration.py` owns initial/patch admission, durable query
  and delivery provenance, planner/test selection and 05C projection.
- Both `conformance.py` modules exercise current-revision, deterministic order, empty result,
  lifecycle, disable and delete/rebuild behavior.
- `packages/workers/zyra_workers/retrieval_context_runtime.py` owns delivery into the current
  CodeWorker context boundary; `code_worker_runtime.py` makes it part of the normal TypeScript
  QueryEngine path.
- `apps/api/zyra_api/main.py` binds the canonical memory/workspace owners, patch event and current
  access handle to those runtimes. This is counted as main-path production integration, not as a
  black-box adapter: it performs canonical transaction resolution, process execution, causal event
  persistence and fail-closed recovery signaling itself.

No AgentScope or oh-my-pi service, database, package, CLI or process is invoked at runtime. The
child processes execute Zyra-owned modules from the committed project and use only Zyra schemas,
stores, errors and tests.

## 5. State custody

| Domain | Canonical owner | Derived/integration responsibility |
| --- | --- | --- |
| Memory records and revisions | `SQLiteStore` / `MemoryRecordStore` | Retrieval integration admits and indexes; it never authors canonical memory |
| Workspace identity, files and patches | `WorkspaceManagerRuntime`, file revisions and committed integration transactions | Code index reads a live access or committed snapshot and never acquires a second lease |
| Memory derived index | `MemoryIndexRuntime` and its retrieval SQLite database | Rebuildable jobs, cursors, staging, active generation and query receipts |
| Code derived index | `CodeIndexRuntime` / `CodeIndexStore` | Rebuildable files, symbols, calls, jobs, leases, invalidations and active generation |
| Delivery to CodeWorker | `WorkerRetrievalContextRuntime` delivery journal | Claimed/released/committed delivery state keyed by run/request/source digest |
| Query loop/session | Existing TypeScript QueryEngine and CodeWorker session runtime | Retrieval supplies bounded inputs; it does not take session or loop custody |
| Event causality | Existing event-log/runtime-event owners | Integration emits source/job/lease/query/delivery facts with causation IDs |
| Checkpoint/recovery | Existing checkpoint and later 07C recovery owners | 06A emits reference-only cursor/job/source refs; it cannot restore canonical truth from cache |

An adversarial ownership bug was found during integration: the API registry initially reacquired a
workspace after CodeWorker had already obtained its access handle. That rotated the workspace lease
and invalidated the worker about to consume it. `runtime_for_access` now validates and reuses the
existing access, while `runtime_for_snapshot` opens only the already-committed derived snapshot.
Tests assert that owner epoch, binding revision and lease ID do not rotate.

## 6. Durable worker and recovery invariants

Memory and code jobs retain a logical idempotency key, source revision, generation, owner, random
lease token, expiry and phase audit. Heartbeat and phase changes require the complete live fence.
Workers build a generation candidate first; one transaction rechecks job/lease/generation before
replacing the active derived generation. Expired work is marked stale and requeued at a later
generation, so a killed or late process cannot publish.

The code-index process test starts the actual `zyra_code_index.worker_cli`, waits for the durable
`building` phase, terminates that child, expires the lease, sweeps it and runs the successor in a
new child. The successor publishes and the old lease remains fenced. Separate restart and
delete/rebuild tests reopen the same canonical sources and prove that successful results are not
being served from a fixture or a hidden cache.

The memory path reuses the 06A-01 fenced publication runtime, but production admission and draining
now go through `MemoryIndexWorkerProcessSupervisor`. Worker-disabled and sweeper-disabled probes
leave queued or stale durable state and never synthesize a ready receipt.

## 7. Query, provenance and context behavior

`MemoryFilterQuery` carries consumer, task/run/session/scope, source/artifact, schema kind, temporal,
privacy/confidence, intent and output-budget fields. The integration adapter compares lexical-only,
FTS+MMR and optional vector/polyphonic modes without making optional vectors a success requirement.
Selections retain the canonical source refs, query digest, index snapshot/generation, mode and
bounded diagnostics. Skill, failure and procedure kinds are searchable but remain memory records;
this slice does not take the later 06B/06C owner roles.

Code queries carry workspace ID/revision, generation, query mode and budgets. Context and test
selection use the current `CodeIndexRuntime`, not a direct filesystem fallback. Initial workspace
admission and each committed patch update affect the active generation, selected excerpts and
adjacent tests. Recovery consumers receive versioned refs only; if the canonical workspace cannot
be opened, the integration fails instead of treating index rows or checkpoint refs as canonical.

The combined worker context gives each selected message a deterministic ID derived from the
request and source facts. This fixed a retry defect found during review: random IDs changed the
delivery source digest after a TypeScript-host failure, making an exact retry conflict with its own
released claim. Delivery transitions now support `released -> claimed -> committed` with the same
digest and no duplicate message.

## 8. Source-to-target decision

| Role | Source mechanism | Zyra target and boundary |
| --- | --- | --- |
| Primary: AgentScope | RAG/KB index worker, task consumer, sweeper and knowledge-base lifecycle at `b6698c5dbaa1aa916925e27402767f45e2405fa4` | Cropped into Zyra memory/code admission, jobs, leases, process supervisors and publication. Zyra adds stronger token/generation fencing and QueryEngine delivery. |
| Supplementary: oh-my-pi Mnemopi | MMR, intent, temporal and polyphonic recall at `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | Only typed filter/ranking/comparison and recovery-reference gaps supplement the primary lifecycle; OMP storage, embedding and agent control flow are excluded. |
| Conformance only: LangGraph | Narrow checkpoint identity, pending/committed write and exact-resume correlation | Used to test reference and fence semantics only; no StateGraph, Pregel, channel, store, ToolNode or runtime ownership. |
| Reference only: Hermes/opencode/Claude-derived runtime | Retrieval protocol and code-context consumption patterns | Design checks only; no implementation quota or canonical owner. |
| Forward excluded: OpenClaw | None | No source reading, ledger entry, adapter, migration, comparison test or runtime dependency. |

The seed ledger contains exactly two `M1-S06A-02` productized entries. Owner-filtered readiness is
`ok=true`, 2 total, 2 connected, 0 blocked, 2 ready for internalization and 2 ready for
productization. The policy matrix is `ok=true` with 0 errors, 0 warnings and 0 blockers.

## 9. Dynamic reachability and disconnect evidence

- `POST /tasks/{task_id}/workers/code` and the agent route call `_worker_retrieval_context`, which
  admits canonical memory, processes workspace state and passes the service into `CodeWorkerRuntime`.
- `CodeWorkerRuntime.run` calls `WorkerRetrievalContextRuntime.prepare` before invoking the
  TypeScript host, and the real no-tool QueryEngine test observes the additional preprocessed
  messages plus file/test constraints.
- `persist_workspace_events` consumes an actual `workspace.patch.committed` event, resolves its
  canonical transaction and invokes an actual child process; the next context sees the new file
  content.
- Disconnecting memory retrieval removes its messages; disconnecting CodeIndex removes selected
  files/tests. A broken required process produces a failure/recovery state, not a legacy result.
- Killing the code-index child changes the durable job from building to stale/requeued and prevents
  its generation from becoming active.
- Deleting derived memory/code state forces reconstruction from canonical records/workspace. If
  those canonical sources are disconnected, rebuild fails.
- Removing `retrieval_context_runtime.py` breaks the real CodeWorker retrieval integration test;
  removing either integration runtime breaks the corresponding unit/conformance and process path.

## 10. Behavioral verification

The final implementation commit was checked in detached cleanroom
`G:\agent-zoo\.tmp\m1-06a02-cleanroom` after `bun install --frozen-lockfile`:

```text
python -m pytest \
  tests/unit/test_retrieval_index_adapters_foundation.py \
  tests/unit/test_retrieval_index_adapters_integration.py \
  tests/integration/test_index_worker_process_recovery.py \
  tests/unit/test_code_index_foundation.py \
  tests/unit/test_code_index_adapters_integration.py \
  tests/integration/test_code_index_process_worker.py \
  tests/integration/test_code_index_patch_event_integration.py \
  tests/integration/test_code_worker_retrieval_context_main_path.py \
  tests/integration/test_retrieval_api_main_path.py \
  tests/integration/test_code_index_api_main_path.py \
  tests/unit/test_memory_fabric.py -q
# 28 passed, 8 subtests passed in 35.41s

ruff check <all changed Python except protected baseline API/__init__ files>
# All checks passed

python -m compileall -q apps packages tests \
  scripts/sync_retrieval_index_integration_source_ledger.py
# passed

git diff --check 6d58fc74819f437f7bdac697403a2b8e96e5b4e5..e04317e11a71dbed9336d5c9823500ec985f16f5
# passed

python scripts/sync_retrieval_index_source_ledger.py --check
python scripts/sync_retrieval_index_integration_source_ledger.py --check
# both aligned; M1-S06A-01=2 and M1-S06A-02=2

python scripts/zyra_integration_ledger.py readiness --owner-unit M1-S06A-02 --json
# ok=true; total=2; blocked=0; internalization=2; productization=2

python scripts/zyra_integration_ledger.py policy-matrix --owner-unit M1-S06A-02 --json
# ok=true; errors=0; warnings=0; blockers=0
```

The changed API file inherits protected historical lint debt, including undefined legacy inventory
symbols that also explain an exploratory broad legacy CodeWorker-session run (`16 failed, 8
passed`). Those failures predate this slice and occur when no retrieval service is supplied; their
signals were `permission_suspended`, `trace_reader_missing` and existing undefined inventory
builders. They are not hidden as passing tests and are not changed here because that would rewrite
protected completed-unit behavior. The directly affected API, CodeWorker, real QueryEngine,
memory, code-index and process paths above all pass on the exact commit. Full cross-unit regression
remains mandatory at the M1-06 numeric-stage aggregate review.

## 11. Effective line buckets

The count uses nonblank, non-comment additions from the protected baseline. It excludes tests,
exports, ledger data, ordinary ledger-sync scripts, generated files, docs, fixtures, mocks,
vendor-like/source-pool content and unconnected thin adapters.

- Memory integration production: `2,702` effective (`2,911` raw).
- Code-index integration production: `3,255` effective (`3,505` raw).
- CodeWorker retrieval production: `443` effective (`472` raw).
- API event/process/CodeWorker main-path integration: `166` effective (`175` raw).
- Counted production: **`6,566` effective** (`7,063` raw).
- Slice minimum: `6,500`; shortfall: `0`.
- Package exports: `106` effective/raw; excluded.
- Dedicated tests: `964` effective (`1,034` raw); excluded.
- Ledger synchronization helper: `209` effective (`228` raw); excluded as an ordinary evidence
  maintenance script.
- Ledger seed: `323` additions; excluded as data/evidence.
- Generated, vendor, source-pool, data-as-code, mock-only, fixture-only and thin-unconnected adapter
  production counted: `0`.

The API lines are a reachable production boundary that resolves canonical owners, executes Zyra
processes, records causal state and changes CodeWorker input; they are not an adapter to an opaque
upstream runtime. Parent 06A cumulative production is `8,638 + 6,566 = 15,204`, exceeding the
`15,000` parent minimum.

## 12. Dependency, path and clean-state audit

The exact-commit cleanroom installed only locked project dependencies. A changed-file scan found no
absolute `G:\agent-zoo` dependency and no `../claude-code-best`, `../browser-use`, `../OpenHands`,
`../opencode`, `../openclaw`, `../AgentScope` or `../Mnemopi` runtime path. There is no npm link, pip
editable source-repository path, Docker context, new port, external MCP server, dynamic import or
upstream process. Index databases and pytest caches were created only inside test temporary roots
or the cleanroom and were not committed. The Zyra worktree was clean before evidence authoring.

## 13. Adversarial findings corrected

- API code-index runtime creation initially reacquired the workspace and rotated the canonical
  worker lease. It now reuses the current access or opens a read-only committed snapshot; tests
  assert no owner epoch/binding/lease change.
- Exact retry initially used random retrieval message IDs, changing the source digest after a host
  failure. IDs are now deterministic and the delivery journal explicitly reclaims released facts.
- A patch event could have become a second file-truth channel. The implementation ignores event
  paths and resolves the canonical committed transaction by ID.
- Canonical patch commit followed by index-process failure could be silently lost. The API now
  records a fail-closed recovery notice and reconciles committed transactions on the next worker
  context.
- A late code-index worker could publish after lease takeover if publication checked generation
  alone. Atomic publication now validates job, generation, token, owner and expiry.
- A cached index/checkpoint could mask unavailable canonical state. Delete/rebuild and disabled
  canonical-source tests require the canonical record/workspace owner.

Blocking findings remaining inside `M1-S06A-02`: `0`.

## 14. Scope and next work

This slice completes parent M1-06A but not the M1-06 numeric-stage aggregate or M1 exit. It does not
claim the 06B MemoryCurator/skill-memory owner, 06C procedural memory owner, 07C recovery planner,
physical 07A worker placement, final embedding/LSP provider lifecycle, live competition scenarios,
2,000 canonical transitions, dynamic-topology comparison or local/edge/cloud evidence. Those
remain assigned to their later units.

The next authorized entry is `slice-06b-01-memory-curator-worker-foundation.md`. Before advancing
beyond all M1-06 sibling units, the numeric-stage review must rerun the applicable broad regression,
full cleanroom and cross-unit source/state-owner audit, including the recorded protected legacy API
debt.

## 15. Final conclusion

`M1-S06A-02` productizes the 06A index foundations as durable, supervised and fenced worker paths,
and makes their current-snapshot results change the real CodeWorker/TypeScript QueryEngine context,
file selection and test selection. Canonical memory/workspace custody remains unchanged, failure
and retry are durable and observable, disable and delete probes change behavior, source roles stay
deduplicated, and no source-repository runtime dependency is introduced. The slice and parent
effective-code gates are met, so M1-06A is complete.
