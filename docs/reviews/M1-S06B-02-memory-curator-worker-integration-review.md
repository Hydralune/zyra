# M1-S06B-02 Memory Curator Worker Integration Review

Date: 2026-07-21

Implementation target: `16d032f0739e10d75df01cda69eeed2bd0750229`

Baseline: `b2cf43189cd40bcd8fdae2543f5c9e512cb6e610`

## 1. Result

`M1-S06B-02` is complete. The default curator path now consumes canonical 05C runtime-event
envelopes, reuses the 06B-01 candidate/validator/committer, publishes immutable outcomes in the
same canonical SQLite transaction boundary, updates the derived 06A index, proves recall through
the real 02D `WorkerRetrievalContextRuntime`, and exposes lease-fenced versioned deliveries for
06C/07B/07C.

The integration does not add a second memory database, model-owned write path, remote memory
service, MCP server or parent-repository runtime dependency. A model remains proposal-only and the
validator/committer remain fail-closed canonical gates.

## 2. Acceptance matrix

| Slice requirement | Runtime evidence | Result |
|---|---|---|
| Task-end automatic curation | API lifecycle schedules a terminal job and `MemoryCuratorIntegrationApplication.process_job` completes it | PASS |
| Manual curator run | Existing POST/runtime operation executes the same integration application | PASS |
| 05C event/message input | `RuntimeEventCuratorIngress` reads paged `RuntimeEventSpineBridge` envelopes and advances a fenced input cursor | PASS |
| Tool/browser/artifact/code evidence | Rich-trace test observes tool-result, browser-trace and failure evidence plus a canonical artifact | PASS |
| Candidate/accept/reject/commit/compress | Immutable outcome projection covers all phases; secret-bearing evidence is rejected with zero canonical writes | PASS |
| Skill/failure outputs | Versioned skill candidate, fault observer and recovery planner contracts are delivered and acknowledged | PASS |
| Canonical index publication | Only validated commit receipts create canonical-change outcomes; index publication is a separate derived phase | PASS |
| Later worker effect | Real `WorkerRetrievalContextRuntime.prepare` retrieves the committed memory and injects a `task_memory` message | PASS |
| Crash/replay/lease fencing | Integration replay is idempotent; expired delivery leases are reclaimed and stale owners cannot acknowledge | PASS |
| Disable core gate | Disconnecting validator or committer raises before any memory/outcome/event publication | PASS |
| Parent line minimum | 06B-01 `8,957` + this slice `7,386` = `16,343` conservative effective production lines | PASS |

## 3. Main path and state custody

```text
05C RuntimeEventSpineBridge + task artifacts
  -> RuntimeEventCuratorIngress (refs, batch identity, input cursor)
  -> 06B-01 MemoryCuratorWorker
       -> evidence bundle -> candidate -> OMP-bounded consolidation
       -> deterministic validator -> transactional MemoryCommitRuntime
  -> CuratorOutcomeProjector + CuratorProjectionAuditor
  -> CuratorIntegrationStore transaction
       -> immutable outcome + downstream delivery + integration audit
  -> MemoryIndexRuntime (derived publication, canonical hydration)
  -> WorkerRetrievalContextRuntime / compact / audit
  -> deferred 06C skill, 07B fault and 07C recovery deliveries
```

Canonical custody remains:

- memory facts: `SQLiteStore.memory_records`;
- evidence/candidates/decisions/commit receipts and original curator jobs: `CuratorCandidateStore`;
- runtime trace: 05C `RuntimeEventSpineBridge` and canonical event rows;
- derived retrieval index: `MemoryIndexRuntime`, reconstructible and never treated as fact owner;
- integration outcomes, delivery leases/checkpoints and recall proofs: `CuratorIntegrationStore`
  tables in the same configured SQLite file;
- TypeScript supplementary runtime: collision/job-state projection only, with no database path or
  canonical write capability.

The integration store persists identifiers, digests, bounded payloads and checkpoint metadata. It
does not copy an index dump, session transcript, artifact body or a second canonical memory record.

## 4. Productized modules

| Zyra module | Owned responsibility |
|---|---|
| `curator_integration_models.py` | Versioned input/outcome/failure/delivery/context schemas and integration state transitions |
| `curator_integration_store.py` | Transactional outcome fan-out, cursors, leases, checkpoints, proofs and consistency audit |
| `curator_outcomes.py` | Projection from durable candidate/decision/receipt/outbox facts and phase auditing |
| `curator_context.py` | Canonical hydration, 06A synchronization and recall/context-effect proof |
| `curator_delivery.py` | Contract validation, consumer ports, retry/reclaim and stale-owner fencing |
| `memory_curator_ingress.py` | Direct 05C paging, identity/digest validation and compatibility-only legacy bridge |
| `memory_curator_integration.py` | Default application orchestration, replay, recovery, events and automatic consumers |
| `memory_curator.py` / API assembly | Main-path construction and status/recovery exposure |

The compatibility ingress only preserves callers that still write canonical task events without
the 05C facade. The normal API trace test reports `direct_spine=true`, `fallback_used=false` and
`compatibility_only_event_count=0`; the fallback therefore does not mask failure of the new path.

## 5. Source-to-target decision

| Role | Source and bounded mechanism | Target owner and decision |
|---|---|---|
| Primary implementation | Hermes `memory_provider.py`, `memory_manager.py`, `curator.py`, compression/trajectory modules | Existing 06B-01 Python curation control flow remains primary; this slice productizes ingress, transactional outcomes and recall around it. No Hermes session/file/provider runtime is loaded. |
| Supplementary implementation | OMP coding-agent memory job mechanics and Mnemopi extraction/consolidation/veracity graph | Existing retained TypeScript consolidation remains bounded; Python adds Zyra versioned delivery/lease protocol and revalidates durable outcomes. OMP SQLite, memory files, embeddings, MCP and model writes remain excluded. |
| Conformance/reference | AgentScope retrieval/KB, LangGraph exact-resume narrow contract, Claude/opencode event/session patterns | Used only through already selected Zyra owners; no parallel manager/store/runtime or migration quota is created. |
| Forward excluded | OpenClaw | No source read, ledger row, package, runtime dependency, test obligation or path was introduced. |

Two `M1-S06B-02` productized ledger rows record the pinned source commits, source evidence,
target bindings, runtime entry, downstream units and behavior tests. Strict ledger audit filtered to
this owner returns zero findings. The global strict report still contains historical findings from
other owner units; this slice does not rewrite those protected facts.

Cross-document comparison found no mismatch among the M1 README, parent 06B unit, first-stage plan
and analysis index: all keep Hermes as 06B primary, OMP as bounded supplement, a 15,000-line parent
minimum and 06C/07B/07C as downstream consumers. No root planning document needed correction.

## 6. Dynamic reachability and semantic effect

The integration is reachable from the existing memory-curator API/runtime builder, both manual and
terminal-task scheduling, and its state is visible from the existing GET status/recovery surface.
It is not reachable only through an import smoke test or ledger scanner.

The strongest semantic-effect assertion executes:

1. a real 05C trace and artifact;
2. deterministic curation and canonical commit;
3. 06A index synchronization over the same canonical store;
4. a real `WorkerRetrievalContextRuntime.prepare` call with a CodeWorker request;
5. assertion that a committed canonical memory ID appears in the retrieval snapshot and its
   content changes the injected `task_memory` message;
6. committed memory/code delivery journals.

Disconnect tests prove that replacing either validator or committer with a failing implementation
prevents canonical memory, outcome and committed/index-published events. Delivery lease tests prove
that stale workers cannot acknowledge after expiry/reclaim. These are the required disable/fail
and state-transition effects, rather than report-only evidence.

## 7. Verification

Main worktree:

```text
python -m pytest tests/unit/test_memory_curator_worker_foundation.py \
  tests/integration/test_memory_curator_api_main_path.py \
  tests/integration/test_memory_curator_worker_integration.py -q
# 19 passed in 74.41s

bun test ./packages/memory/curator-state-machine/test/state-machine.test.ts
# 4 passed, 0 failed, 22 assertions

bun run typecheck
# all configured TypeScript packages passed

python scripts/sync_memory_curator_integration_source_ledger.py --check
# aligned=true; decisions=2; owner=M1-S06B-02

zyra_integration_ledger.py --ledger-path <seed> audit --strict \
  --owner-unit M1-S06B-02 --json
# filtered_finding_count=0

zyra_integration_ledger.py linecount --base b2cf431 --head 16d032f \
  --owner-unit M1-S06B-02 --minimum-effective-lines 6500 --json
# raw_added=9450; tool_effective_added=9114; excluded_added=336; shortfall=0
```

An exact detached cleanroom at `16d032f0739e10d75df01cda69eeed2bd0750229`
used a frozen Bun install and an explicit cleanroom-only Python package path. It asserted that
`zyra_memory` and `zyra_workers` were imported from the detached tree, then ran the same 19 Python
tests (`19 passed in 62.71s`), four TypeScript tests, full TypeScript typecheck and ledger sync check.
All passed. The temporary worktree and its local dependency/cache directories were removed.

The first cleanroom attempt was invalidated before evidence use because the shared venv's editable
finder loaded the primary worktree and Bun was absent from PATH. The corrected run explicitly
proved import origins and installed the frozen dependencies inside the detached tree; only that run
is counted.

## 8. Effective line buckets

The conservative count includes only nonblank/non-comment lines in the seven new productized core
modules. It excludes API/export glue even though that glue is main-path tested.

- productized curator integration modules: **`7,386` effective** (`7,807` raw);
- modified API/export/foundation glue: excluded from the conservative count;
- dedicated integration tests: `661` effective (`701` raw), excluded;
- ledger synchronization helper: `271` effective (`291` raw), excluded;
- ledger seed/data: excluded;
- review/docs: excluded;
- generated, data-as-code, fixture-only, mock-only, vendor-like/source-pool, thin-adapter-only and
  unconnected sample code counted as production: `0`;
- slice minimum: `6,500`; conservative shortfall: `0`.

Parent closeout uses the prior review's conservative `8,957` plus this slice's `7,386`, for
**`16,343` effective production lines** against the parent minimum of `15,000`.

## 9. Adversarial findings corrected

- A provider-token regex treated ordinary `skill_*` identifiers as `sk` credentials. Short token
  prefixes now require a real `-` or `_` delimiter, while actual secret-bearing evidence remains
  fail-closed and has a dedicated rejection test.
- A heartbeat could observe a lost lease after another thread had already made the job terminal.
  Terminal state now wins that race without turning a successful job into a false failure.
- Index-publication projection initially looked like a second canonical mutation. It is now a
  separate derived phase; only the receipt-backed commit outcome carries canonical-change truth.
- Delivery fan-out initially deduplicated dataclasses containing unhashable payload mappings. It
  now deduplicates by stable delivery identity.
- Recall proof initially used an intent/value not present in the canonical message contract and a
  non-existent retrieval digest property. It now emits a normal observation and derives its digest
  from the versioned result.
- Rich success and sensitive-rejection traces were separated so the secret-bearing bundle cannot
  accidentally invalidate the normal successful path or create flaky expectations.
- Cleanroom validation now asserts import origin before running tests, preventing an editable
  install from producing false cleanroom evidence.

Blocking findings remaining inside `M1-S06B-02`: `0`.

## 10. Competition evidence and residual scope

This slice incrementally strengthens `REQ-MEM-01`, `REQ-COMM-01` and `REQ-TRACE-01`: curated facts
are canonical, auditable, recoverable and demonstrably alter a later worker context. It does not
claim final closure of those competition requirements or substitute for sealed live scenarios.

The parent `M1-06B` is closed by 06B-01 plus 06B-02. Downstream implementation remains deliberately
owned by 06C skill memory/compact, 07B fault observer and 07C recovery planner; this slice supplies
versioned contracts and durable inbox semantics but does not pre-implement those owners. The M1-06
numeric-stage aggregate still owns broad sibling regression, complete source/ledger audit and the
stage-level cleanroom. Milestone exit still owns live cross-domain tasks, 2,000 canonical
transitions, dynamic-topology comparison, real local/edge/cloud dispatch and final delivery freeze.
