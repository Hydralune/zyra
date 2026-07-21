# M1-S06B-01 Memory Curator Worker Foundation Review

## 1. Review identity

- Slice: `M1-S06B-01`
- Measurement baseline: `9f3d04b9fa860980d86ad2aaa446bde1db26ad21`
- Final implementation commit: `2efb0cbc1f2938fc97d96b4fa5028ac8c39671fe`
- Review type: ordinary-slice incremental critical self-review with high-risk cleanroom upgrade
- Result: **PASS for M1-S06B-01**
- Parent result: **M1-06B remains in progress**
- Next slice: `M1-S06B-02`

The verdict is based on a real event/artifact trace, proposal generation, deterministic validation,
canonical SQLite memory writes, event/index outbox delivery, retrieval, lease takeover, restart and
HTTP behavior. Ledger rows, file presence, model output and source volume do not substitute for
those behaviors.

## 2. Outcome

The slice introduces a Zyra-owned `MemoryCuratorWorker` lifecycle:

```text
queued -> claimed -> extracting -> deciding -> validating -> committing -> succeeded
```

Expired ownership is fenced and moves through `stale/retry_wait`; a retry keeps the same input
watermark. Manual requests enqueue by default and may explicitly request immediate execution.
Task-end hooks only enqueue and cannot make the primary task fail. `run-next`, exact `run-job`,
recovery and health operations share the worker request/result boundary rather than duplicating the
curator in an API handler.

`EventArtifactTraceExtractor` binds evidence to run, task, sequence/range, producer, artifact and
content digests. The decision runtime emits the five required candidate kinds: `promote`, `discard`,
`compress`, `skill_candidate` and `failure_pattern`. An optional model can only propose candidates;
model absence deterministically degrades to rule-based projection and never grants write authority.

`MemoryDecisionValidator` independently enforces schema, scope, provenance, evidence hashes,
trust, secret redaction, rule-mutation prohibition, TTL/retention, revision expectations,
duplicates and contradictions. Accepted writes go through one `MemoryCommitRuntime` transaction
which changes canonical memory, advances revision CAS, creates event/index outbox intents, records
the receipt and settles candidate state. No last-write-wins path exists.

## 3. Goal coverage

| Requirement | Behavioral evidence | Result |
| --- | --- | --- |
| Standalone/background worker | Durable jobs, explicit worker operations, enqueue-by-default manual API and asynchronous task-end hook | PASS |
| Five candidate decisions | Typed schemas and real deterministic projectors for promote/discard/compress/skill/failure | PASS |
| Verifiable evidence | Run/task/range/content digests are resolved from stored evidence before validation and rechecked immediately before commit | PASS |
| Secret and rule safety | Secret-bearing trace candidates and untrusted permission/policy mutation are rejected; canonical memory stays empty | PASS |
| Model cannot write | Model output is schema-bounded proposal data; deterministic validation and commit remain the only admission path | PASS |
| Model unavailable degradation | Real main-path tests run with no model, report `unavailable`, and still produce verified deterministic candidates | PASS |
| Explicit collisions | Duplicate, contradiction, merge-required and supersede relations are persisted; contradictions cannot overwrite memory | PASS |
| Exactly-once commit | Stable candidate/decision/receipt IDs, immutable decision replay and revision CAS prevent duplicate memory writes | PASS |
| Atomic event/index intent | Canonical memory, revision and both outbox messages commit in one SQLite transaction | PASS |
| Crash/outbox recovery | Restart drains pending event/index messages without replaying or duplicating canonical memory | PASS |
| Lease and retry safety | Random ownership token, epoch, owner, expiry, heartbeat and watermark checks fence an expired worker | PASS |
| TypeScript retained source | OMP-derived lease/watermark/consolidation state machine runs as a formal TypeScript package and has four direct tests | PASS |
| Main-path reachability | HTTP manual/recover/status routes create candidates and canonical memory/event/index results; retrieval returns the committed record | PASS |
| Source and volume gates | Two owner-filtered source rows are connected; conservative production is 8,957 lines against 8,500 | PASS |

## 4. State custody and transaction boundary

| State domain | Canonical owner | Non-owner boundary |
| --- | --- | --- |
| Task/run checkpoints and event source | Existing `SQLiteStore` task/event tables | Extractor reads bounded snapshots only |
| Evidence bundles/documents | `CuratorCandidateStore` evidence tables | Immutable inputs for validation; not canonical memory |
| Candidates, relations and decisions | `CuratorCandidateStore` | Model and TypeScript process may propose/project but cannot settle memory |
| Curator jobs, attempts, leases and watermarks | `CuratorCandidateStore` | Worker must present live token/epoch/owner/expiry for transitions |
| Canonical long-term memory | Existing `SQLiteStore.memory_records` | Only `MemoryCommitRuntime` writes curator records |
| Curated memory revision | `memory_record_revisions` under commit transaction | Expected revision and CAS reject stale writers |
| Event/index delivery intent | `memory_curator_outbox` | Dispatch is replayable; delivery state is not canonical memory |
| Retrieval index | Existing `MemoryIndexRuntime` | Derived and rebuildable from canonical memory |
| TypeScript supplementary process | `@zyra/memory-curator-state-machine` code under `packages/memory` | Receives JSON only; no database path, secret, event sink or canonical writer |

Candidate and canonical tables deliberately share one SQLite database file but remain separate
tables and owners. Sharing the transaction engine is what makes memory/revision/outbox admission
atomic; it does not turn candidates into memory. `MemoryCommitRuntime` first verifies the durable
candidate and decision, then re-resolves every evidence document. Any post-validation evidence
mutation aborts before the canonical write.

## 5. Source-to-target decision

| Role | Source mechanism | Zyra target and retained responsibility |
| --- | --- | --- |
| Primary: Hermes Agent | memory provider/manager, curator serialization, protected trajectory extraction and failure isolation at `44ddc552f5e054759a6970af8997ea588a9d81c9` | Cropped same-language mechanisms are integrated into Zyra evidence, decision, validation, commit, runtime and worker modules. Hermes files, providers and runtime paths are not loaded. |
| Supplementary: oh-my-pi | memory storage/index ownership, Mnemopi extraction/consolidation/veracity/orchestration at `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | Bounded ownership-token, lease, heartbeat, retry/watermark and explicit collision logic is retained in TypeScript. OMP database/file memory, embeddings, model-to-file writes and agent loop are excluded. |
| Conformance/reference sources | Previously adjudicated exact-resume, retrieval and worker patterns | No additional production owner, parallel memory manager or migration quota is created. |
| Forward excluded source | None | No source read, ledger row, package, runtime path or test obligation was introduced. |

The bundled source ledger has exactly two `M1-S06B-01` rows. Owner-filtered readiness reports two
connected/productized entries, zero blocked entries, zero missing target/runtime/test bindings and
`ok=true`. The policy matrix reports zero errors, warnings and blockers. The global legacy audit
still reports protected earlier-unit debt and is not rewritten by this slice.

## 6. Lease, watermark and exactly-once invariants

- A claim creates a cryptographically random ownership token and increments the lease epoch and
  attempt number.
- Heartbeat and every transition require job ID, worker, token, epoch, input watermark and a live
  expiry. An expired owner is fenced even when it still holds the old Python object.
- Recovery requeues at the unchanged input watermark; `last_success_watermark` advances only after
  the worker has committed candidates and settled the job.
- Curator operational events do not inflate the evidence watermark, so repeated task-end scheduling
  is idempotent for an unchanged terminal checkpoint.
- `process_job(job_id)` now claims that exact job, not an older queued job for the same task.
- A stored immutable decision is reused for identical candidate/evidence/policy inputs after a
  crash; changed canonical observations cannot retroactively convert the decision into a duplicate.
- Candidate ID, decision ID and receipt ID are stable. A second commit returns the existing receipt
  and leaves one canonical record.
- Contradiction produces `merge_required`; no update occurs without an explicit merge/supersede
  candidate carrying target memory and expected revision.

## 7. Dynamic reachability and disconnect effects

- `POST /tasks/{task_id}/memory/curator` with explicit immediate processing executes the same worker
  used by queued work, then produces candidate rows, a canonical `MemoryRecord`, a
  `memory_curator_committed` event and a searchable retrieval-index document.
- Omitting `process_immediately` leaves a real queued job, proving the API is a scheduler rather
  than the sole implementation.
- Task-end curation is enqueue-only and returns a degraded diagnostic if its scheduler is
  unavailable; the already-settled primary task lifecycle remains successful.
- Removing the deterministic validator or commit module prevents canonical memory admission;
  disabling the TypeScript supplement changes consolidation diagnostics/relations but cannot grant
  Python-side write authority.
- Corrupting or deleting evidence rejects validation. Corrupting evidence after validation causes
  commit-time failure. Removing outbox delivery leaves pending durable intents; restart drains them
  without duplicating memory.
- Removing `MemoryIndexRuntime` leaves canonical memory intact but eliminates the verified derived
  retrieval result.

The generic ledger reachability scanner does not recognize the API's dynamic `parts` routing,
Enum-valued event producers or npm package execution. Its static route/event findings are therefore
not used as behavioral evidence; the runtime entries were nevertheless corrected to importable
Zyra classes, and the HTTP/SQLite/TypeScript tests above exercise the actual surfaces.

## 8. Verification

```text
python -m pytest tests/unit/test_memory_curator_worker_foundation.py \
  tests/integration/test_memory_curator_api_main_path.py -q
# 11 passed in 26.83s

bun test ./packages/memory/curator-state-machine/test/state-machine.test.ts
# 4 passed, 0 failed, 22 assertions

bun run typecheck
# all existing TypeScript packages plus memory curator passed

python -m pytest tests/unit/test_memory_fabric.py \
  tests/unit/test_retrieval_index_adapters_foundation.py \
  tests/unit/test_retrieval_index_adapters_integration.py -q
# 8 passed

python -m pytest tests/integration/test_retrieval_api_main_path.py \
  tests/integration/test_index_worker_process_recovery.py \
  tests/integration/test_code_worker_retrieval_context_main_path.py \
  tests/integration/test_scheduler_api.py -q
# 10 passed

python scripts/sync_memory_curator_source_ledger.py --check
# aligned=true; decisions=2; owner=M1-S06B-01

zyra_integration_ledger.py linecount --base <baseline> --cached \
  --owner-unit M1-S06B-01 --minimum-effective-lines 8500 --fail-on-shortfall
# effective_added=11086; raw_added=11434; excluded_added=348; ok=true
```

The cleanroom used detached commit `2efb0cbc1f2938fc97d96b4fa5028ac8c39671fe`, installed from
the frozen Bun lock, ran the full repository TypeScript typecheck, four TypeScript tests and eleven
Python/HTTP tests, and found no parent-source runtime path. All passed. The temporary worktree and
its dependency cache were removed afterward.

An attempted broad run of the old `test_api_control_commands.py` produced 19 passes and 16 failures
because unrelated tool routes returned the existing `e02_route_not_found` for `artifact_write`,
`file_write` and other capability owners. An isolated memory test failed at that pre-curator tool
call, before reaching memory endpoints. This slice does not alter that E02 route and does not add a
curator fallback to hide it; targeted adjacent memory/retrieval/scheduler behavior passed.

## 9. Effective line buckets

The strict staged tool reports 11,086 effective additions and no bucket findings. A more
conservative manual count excludes API glue, exports, tests, configs, docs, notices, ledger seed and
ledger-sync script, and counts only nonblank/non-comment lines in the curator runtime modules:

- Python memory curator models/store/evidence/decision/validation/commit/runtime/TypeScript port:
  `7,902` effective (`8,436` raw).
- Python worker request/result and runtime assembly: `227` effective (`247` raw).
- TypeScript ownership/watermark/consolidation/protocol implementation: `828` effective (`901` raw).
- Conservative production total: **`8,957` effective** (`9,584` raw).
- Slice minimum: `8,500`; shortfall: `0`.
- Dedicated Python and TypeScript behavior tests: `887` effective (`943` raw), excluded.
- Tool-reported production bucket before conservative exclusions: `9,992` additions.
- Generated, data-as-code, mock-only, fixture-only, vendor-like and source-pool counted production:
  `0`.

The Python port is not a thin black-box adapter: it enforces protocol schemas, input/output budgets,
timeouts and diagnostics, while the Python validator rechecks every TypeScript relation and retains
all state/write custody. The TypeScript package itself contains the retained source-language
control logic and is directly typechecked and tested.

## 10. Adversarial findings corrected

- Initial decision timestamps and TTL evaluation depended on wall-clock timing; both now derive
  deterministically from the candidate/evidence inputs.
- Curator-generated events initially advanced the task-end watermark; operational events are now
  excluded, making repeated terminal scheduling idempotent.
- A crash after memory commit could cause revalidation against the newly written record and change
  the result to duplicate; identical durable decisions are now replayed.
- Exact `run-job` could claim an older job for the same task; the store now supports job-ID-bound
  claims and has a regression test.
- A validator-only evidence check left a validation-to-commit tamper window; commit now re-resolves
  evidence and aborts before the transaction writes memory.
- Task-end originally executed curation synchronously; it now only schedules and isolates curator
  failure from the primary task response.
- Bun's first test invocation treated a path as a filter and ran zero tests; scripts now use an
  explicit `./` path, and four tests are observed in normal and cleanroom runs.

Blocking findings remaining inside `M1-S06B-01`: `0`.

## 11. Scope and unclaimed work

This slice establishes the curator worker foundation only. `M1-S06B-02` still owns broader
integration with retrieval/skill memory, additional lifecycle consumption and parent closeout.
The parent minimum and cross-slice matrix are not claimed here. The M1-06 numeric-stage aggregate
still owns broad cleanroom/regression and source-to-target review across 06A/06B siblings.

No model provider, embedding owner, global memory-policy replacement, live external service, port,
Docker dependency or parent-repository runtime dependency is claimed. This review also does not
claim milestone exit, live competition scenarios, 2,000 canonical transitions, dynamic-topology
comparison, real local/edge/cloud dispatch or final delivery freeze.

## 12. Conclusion

`M1-S06B-01` now has a real, durable and independently runnable memory curator foundation. Evidence
is provenance-bound and rechecked, model output is proposal-only, collisions are explicit, leases
and watermarks are fenced, canonical memory/event/index intent is atomic, and restart recovery is
exactly once. The retained TypeScript state machine is a bounded supplementary implementation with
no database authority. The slice exceeds its conservative 8,500-line production minimum and is
complete; parent `M1-06B` remains open for `M1-S06B-02`.
