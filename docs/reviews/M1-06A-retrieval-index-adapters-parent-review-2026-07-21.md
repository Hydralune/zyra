# M1-06A Retrieval Index Adapters Parent Review

- Review date: 2026-07-21
- Scope: `M1-S06A-01` and `M1-S06A-02`
- Review authority: both slice contracts and `docs/执行单元完成后通用审查任务书.md`
- Pre-review evidence head: `33ea29dfb73d21105a7a17a6222fde2ad8b38034`
- Review-fix commit: `4238dd8b9298d578b873b0322dadb98c56f923b8`
- Verdict: **PASS_AFTER_FIX**

## 1. Outcome

The two slices still satisfy their parent functional and conservative effective-line gates after remediation. The review found real transaction, provenance and reproducibility defects that were not covered by the original evidence. They were fixed on the default path and verified at the exact fix commit in a detached cleanroom.

The review does not claim repository-wide green status. The user explicitly stopped the unrelated long repository regression. Six pre-existing collection blockers were independently reproduced at the pre-06A baseline `733f9a7`; broad repository regression remains assigned to the M1-06 numeric-stage aggregate.

## 2. Findings and remediation

| ID | Severity | Finding | Resolution and sensitivity evidence |
|---|---|---|---|
| 06A-R01 | high | `WorkerRetrievalContextRuntime.prepare` could leave one exact-snapshot delivery claimed when the peer journal or recovery-reference construction failed. | Added snapshot-bound compensation for both journals. `test_prepare_compensates_both_journals_when_reference_build_fails` proves both claims become released. |
| 06A-R02 | high | Delivery finalization stopped at the first journal error, so memory and code journals could diverge without attempting the peer or exposing repair state. | Both journals are now attempted, errors are aggregated, and retry to the same desired state is idempotent. The repair test injects a memory finalize failure, observes code committed/memory claimed, then repairs memory without duplicating code delivery. |
| 06A-R03 | high | A provider result followed by host checkpoint/artifact persistence failure could leave retrieval delivery claimed; a final settlement failure could still return provider success. | Host persistence failure releases both deliveries. Ambiguous final settlement now returns `retrieval_delivery_finalize_failed`, preserves provider evidence, and sets `retrieval_delivery_repair_required=true`. |
| 06A-R04 | medium | Both AgentScope primary ledger rows were labelled `reimplemented_pattern`, contrary to the slices' required cropped same-language migration contract. | Reclassified the fixed-commit AgentScope lifecycle as `direct_port` with `cropped_migration_same_language_module_integration`; added precise module provenance. OMP remains a bounded supplementary cross-language mechanism port. |
| 06A-R05 | medium | The 06A-02 AgentScope source path `_knowledge_base.py` did not exist at the fixed commit. | Corrected it to `_knowledge.py`; all two source commits and 12 declared source objects were verified with `git cat-file`. Synchronization replaces rows by owner/capability so the corrected identity cannot leave a duplicate stale row. |
| 06A-R06 | medium | Original 06A-02 ledger commands depended on the default ledger path, which selected a stale worktree cache but the bundled seed in cleanroom. | This review uses an explicit bundled seed for every sync/readiness/policy command. Both owner filters report 2 total, 2 connected, 2 productized and 0 owner blockers. |
| 06A-R07 | low | The full 06A Ruff scope exposed 13 unused imports omitted by the original changed-file lint selection. | Removed the unused imports; full reviewed 06A package/script/test Ruff scope passes. |

## 3. Source-to-target and internalization decision

| Source role | Fixed source | Zyra target responsibility | Decision |
|---|---|---|---|
| sole primary implementation | AgentScope `b6698c5dbaa1aa916925e27402767f45e2405fa4` | Memory/code durable job admission, claim, heartbeat, sweep, failure sink and fenced publication in `packages/memory`, `packages/code_index` and worker context integration | Cropped same-language direct port. AgentScope bus, knowledge store and vector-store writes are replaced by Zyra schemas, SQLite stores, generation fencing and canonical owner guards. No AgentScope runtime runs. |
| bounded supplementary implementation | oh-my-pi/Mnemopi `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | MMR, intent, temporal and polyphonic retrieval mechanisms | Bounded cross-language mechanism port only. No OMP storage, embedding or agent control flow is copied and no second canonical owner exists. |
| conformance only | LangGraph | Narrow checkpoint identity, pending/committed and exact-resume references | No StateGraph, Pregel, channel, Store or production runtime ownership. |
| reference only | Hermes, opencode and claude-code-best | Typed retrieval/context-consumption comparison | No production migration quota or canonical owner. |
| forward excluded | OpenClaw | None | No new source reading, ledger role, migration, comparison test or runtime dependency. Historical protected facts are unchanged. |

The changed scope contains no parent-repository runtime path, npm link, pip editable source path, new vendor/source-pool path, external MCP/server/port or upstream process dependency.

## 4. State custody and semantic effect

| State domain | Canonical owner | 06A responsibility and fail-closed effect |
|---|---|---|
| canonical memory | `SQLiteStore` and `MemoryRecordStore` | 06A reads current canonical revisions and maintains disposable derived generations; deleting the derived index rebuilds from canonical memory. |
| canonical workspace/patch | `WorkspaceManagerRuntime`, file revisions and committed patch transactions | Code indexing binds the current read-only snapshot and revision; it does not reacquire or rotate workspace custody. |
| derived retrieval state | `MemoryIndexRuntime`, `CodeIndexRuntime` and their durable SQLite job/generation stores | Lease, heartbeat, stale sweep, generation candidate and publication fence change actual query visibility and process recovery. |
| retrieval delivery | `WorkerRetrievalContextRuntime` plus memory/code journals | Exact request/source digest claims are released or committed in both journals; ambiguous settlement fails the CodeWorker result and remains repairable. |
| query loop/session | existing TypeScript QueryEngine/session runtime | Retrieval only supplies bounded messages, constraints and recovery references; it does not take session ownership. |

The four new adversarial tests provide disable/failure sensitivity for the repaired module. Removing the compensation or fail-closed settlement changes durable journal state or returns an incorrect successful worker outcome.

## 5. Verification

### Exact review-fix worktree

```text
pytest <11 reviewed 06A/adjacent files> -q -p no:cacheprovider
32 passed, 8 subtests passed in 61.61s

ruff check <reviewed 06A packages, scripts and tests>
All checks passed

python -m compileall -q <reviewed 06A production/scripts/tests>
passed

sync_retrieval_index_source_ledger.py --check --ledger <bundled-seed>
2 decisions aligned for M1-S06A-01

sync_retrieval_index_integration_source_ledger.py --check --ledger <bundled-seed>
2 decisions aligned for M1-S06A-02
```

Both owner-filtered readiness reports are `ok=true`, with `2` connected/productized entries and no owner blocker. Both policy matrices are `ok=true` with zero blockers. The strict global ledger audit still reports 11 historical blockers outside these two owner filters; this review does not conceal or reclassify them.

### Exact-commit cleanroom

The detached cleanroom was checked at exact commit `4238dd8b9298d578b873b0322dadb98c56f923b8` after Bun 1.2.15 frozen-lockfile installation (`14` packages):

```text
06A focused/adjacent pytest: 32 passed, 8 subtests passed in 51.70s
reviewed Ruff scope: passed
reviewed compileall scope with external pycache prefix: passed
06A-01 source-ledger sync against explicit bundled seed: passed
06A-02 source-ledger sync against explicit bundled seed: passed
06A-01/02 owner readiness and policy matrix: passed
forbidden parent runtime path hits: 0
changed-scope OpenClaw hits: 0
new vendor/source-pool paths: 0
cleanroom Git status after verification: clean
```

Git worktree registration was removed. Windows denied deletion of one test-created ACL-restricted workspace subtree even after scoped escalation; the deregistered residual path is `G:\agent-zoo\.tmp\m1-06a-review-cleanroom` and is not a runtime or evidence dependency.

### Broad regression disposition

- A bare repository `pytest` was invalid because it recursively collected historical cleanrooms, `tmp/uv-cache` and vendored browser-use tests; it stopped at 224 collection errors and is not evidence.
- `pytest tests --collect-only` found six protected export/collection failures. The same six files and 486 collected tests reproduced at the pre-06A baseline `733f9a7`.
- A separate protected CodeWorker/session group had the same 16 failing test identities at the current tree and pre-06A baseline.
- The remaining long repository run was stopped at the user's request and produced no completion result. It is explicitly deferred to the M1-06 numeric-stage aggregate.

## 6. Effective-line buckets

The original accepted parent count remains `8,638 + 6,566 = 15,204` conservative production-effective lines against the `15,000` parent minimum. The review fix does not use ledger data, comments or tests to satisfy that gate.

Mechanical review-fix diff (`33ea29d..4238dd8`):

| Bucket | Added | Deleted | Count treatment |
|---|---:|---:|---|
| production Python | 269 | 88 | Behavior/provenance/lint changes; only executable production behavior is effective |
| adversarial tests | 166 | 2 | Test evidence, excluded from production-effective count |
| ledger synchronization scripts | 23 | 5 | Review/reproducibility support, excluded from parent minimum |
| ledger seed data | 10 | 6 | Data/evidence only, excluded |
| generated, fixture-only, mock-only, vendor/source-pool, thin unconnected adapter | 0 | 0 | Excluded |

## 7. Competition and residual gates

This review strengthens `REQ-COMM-01` and `REQ-TRACE-01` evidence by making context delivery causally durable and fail-closed. It does not close cross-domain live-task, sealed 2,000-transition, dynamic-topology comparison, real local/edge/cloud, multi-model, visual causal-trace or final delivery gates. M1-06B/06C memory curator and skill/procedure memory remain the next planned capabilities; this review does not implement them or change `next_slice`.
