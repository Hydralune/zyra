# M1-S06C-01 Skill Memory / Compact Restore Foundation Review

Date: 2026-07-21

Baseline: `57a6ac8288c1e999ccbba924cfd85b4abbbdf2d2`

Implementation target: `f4d079ef25f42b05ce3c686c8f6ce6727ce365c7`

Implementation commits:

- `ed4db58dbc2de77c84c1bbc8daf59b31cb82a100` — skill-memory, procedure-memory and compact/restore foundation;
- `f4d079ef25f42b05ce3c686c8f6ce6727ce365c7` — exact dynamic route/event/TypeScript-runtime reachability evidence.

## 1. Result

`M1-S06C-01` is complete. It delivers the foundation owners required by the slice and connects
them to real 03C, 02B/02D, QueryEngine, 06B and API paths. The parent `M1-06C` remains in progress;
`M1-S06C-02` still owns full cross-provider/BrowserWorker integration, revoked-version behavior,
06A retrieval composition, routing/recovery consumption and parent closeout.

The implementation does not add another skill loader, catalog writer, policy evaluator, version
or revoke owner. Historical outcome memory never carries executable skill-body authority. Current
execution must resolve through 03C again. Snapcompact is experimental, off by default and is not
used to claim compact effectiveness.

## 2. Acceptance matrix

| Slice requirement | Runtime evidence | Result |
|---|---|---|
| 03C immutable outcome input | Real `SkillCoordinator` invocation returns version/body/resource, policy, composition, journal and tool evidence refs consumed by `SkillMemoryApplication` | PASS |
| No second skill owner | 06C health reports no loader/invoke/version/revoke ownership; disabling 06C leaves the real 03C invocation path operational | PASS |
| Outcome memory | Admission, idempotency, quarantine, supersession, retention and checksummed snapshot/restore are owned by the TypeScript package | PASS |
| Compact trigger and atomic cut | Threshold/manual/idle/failure trigger policy and circuit breaker preserve tool-call/result atomic groups and a recent suffix | PASS |
| Restore changes next context | `ClaudeRuntimeCore` emits `skill_memory_compact_restored` and injects the restore provider message into the next provider round | PASS |
| Code/browser context contract | Code and browser projections use the 02B context and 02D restore ports, advance context epoch and cannot enlarge the parent tool scope | PASS (foundation) |
| Reusable procedures | Only deterministic, canonical-changing 06B `SKILL_CANDIDATE` outcomes addressed to `SKILL_MEMORY` can create a validated durable procedure | PASS |
| Procedure provenance | Procedure records retain run/task/session, tool call, artifact, skill version, memory event, curator decision/outcome and evidence digests | PASS |
| Routing/recovery/context consumers | Runtime and live API queries return only validated/applicable procedures; replay is idempotent and leases are fenced | PASS |
| Dynamic reachability | Three ledger decisions are materialized and `3/3` reachable with no unit entry reasons | PASS |
| Clean submission boundary | No current production change contains parent-source paths, npm links, editable parent paths, vendor/source-pool runtime dependency or OpenClaw path | PASS |
| Effective production minimum | Conservative production-only count is `8,186`, above the slice minimum of `8,000` | PASS |

## 3. Main paths and semantic effect

### 3.1 Skill outcome path

```text
03C SkillCoordinator.resolve/invoke/journal ACK
  -> immutable outcome_reference in real skill tool result
  -> ClaudeRuntimeCore tool observation
  -> E01RuntimeCoordinator.recordSkillToolOutcome
  -> SkillCoordinatorOutcomeAdapter
  -> SkillInvocationOutcomeRuntime
  -> skill_memory_updated event + E01 composite snapshot
```

The behavior test creates a filesystem-backed Markdown skill, invokes the real 03C coordinator and
admits the returned outcome. Turning off 06C makes outcome admission fail closed while the same 03C
skill invocation still succeeds. This is the required owner-separation/disconnect effect.

### 3.2 Compact/archive/restore path

```text
ClaudeRuntimeCore existing compact decision
  -> 02D ContextCompactionRuntime boundary
  -> CompactTriggerRuntime + SafeCutRuntime
  -> CompactArchiveRuntime / artifact reference
  -> CompactRestoreMemoryBridge
  -> 02D CompactRestoreRuntime + 02B ContextAssemblyRuntime
  -> tool-scope intersection + context epoch
  -> restore provider message in the next QueryEngine provider request
```

The live QueryEngine test forces compaction with oversized runtime context, observes archive and
restore events and verifies that the restore message changes the next provider context. The package
behavior test independently projects `workerKind=code` and `workerKind=browser`, verifies both
context effects and proves that a restored scope cannot add a denied tool. Full BrowserWorker and
cross-provider fidelity are deliberately left to 06C-02, as specified by that integration slice.

### 3.3 Procedure path

```text
06B CuratorIntegrationStore published validated outcome
  + canonical SQLite memory/event/tool/artifact facts
  -> ReusableProcedureMiner deterministic eligibility and provenance checks
  -> ReusableProcedureStore transactional record/audit/signal
  -> routing / recovery / context query
  -> GET/POST /tasks/{task_id}/memory/procedures/**
```

The test uses a real temporary canonical SQLite database and `EventRecord` trajectory, not a
procedure fixture. A forged, non-deterministic or disabled input creates no procedure. A successful
record is replay-idempotent, visible to all three consumers and exports a digest-compatible
TypeScript projection.

## 4. State custody and recovery

- 03C remains sole owner of discovery, body/resource loading, trust, allowed tools, invocation,
  install/publish/version/revoke/rollback and current execution resolution.
- `SkillMemoryApplication` owns immutable invocation-outcome memory, compact archives, restore
  projections, context epoch and low-entropy signals. Its checksummed snapshot is embedded in the
  existing E01 composite snapshot and restored with the session.
- Existing 02D `ContextCompactionRuntime` remains canonical compact-boundary owner; 06C records
  memory/archive/projection facts around that boundary and does not replace it.
- Existing 02B `ContextAssemblyRuntime` remains context assembly owner; 06C contributes bounded
  sections/attachments and an intersected tool scope through its port.
- `ReusableProcedureStore` owns procedure revisions, audit rows, projection receipts and fenced
  consumer claims in the configured canonical SQLite file. `SQLiteStore.memory_records` remains
  canonical memory-fact owner and 06B remains curator outcome owner.
- TypeScript and Python projection digests use the same canonical numeric/JSON representation.
  The procedure export is a cross-runtime projection, not a second procedure database.

Deletion/disconnect consequences are covered: without 06C the skill invocation still completes but
no outcome memory is formed; without a valid compact bridge the restored context is deferred; without
the procedure miner no validated procedure becomes visible to routing/recovery/context; without 03C
there is no skill invocation that 06C can revive from cached body text.

## 5. Productized modules

| Zyra module | Owned responsibility |
|---|---|
| `packages/memory/skill-memory-runtime/src/outcome-runtime.ts` | Immutable outcome admission, evidence validation, retention, quarantine, supersession and snapshots |
| `skill-outcome-adapter.ts` | Strict conversion of committed 03C runtime results; no skill loading or invocation |
| `compact-trigger-runtime.ts` / `safe-cut-runtime.ts` | Trigger/circuit policy and tool-pair-safe compact boundary planning |
| `archive-runtime.ts` / `restore-bridge.ts` | Archive facts, restore projection, context epoch, scope intersection and next-provider message |
| `context-projector.ts` | Bounded skill/procedure/archive projection through 02B/02D ports |
| `signal-emitter.ts` | Ordered, replay-safe outcome/procedure/compact signals for later consumers |
| `snapcompact-experimental.ts` | Compatibility/evaluation contract, hard disabled unless explicitly enabled |
| `procedure_models.py` | Procedure, applicability, provenance, signal, receipt and error contracts |
| `procedure_store.py` | SQLite revision/transaction/audit/projection/lease state |
| `procedure_miner.py` | Deterministic 06B eligibility, trajectory evidence, validation and signal production |
| `procedure_runtime.py` | Mine/status/routing/recovery/context/API projection orchestration |
| Claude runtime and API integration | Real skill-result admission, compact restore, snapshot and dynamic procedure routes |
| `ledger_reachability.py` | Static discovery of task-ID route manifests, TS emit sites and TS workspace exports |

These are cropped Zyra modules with Zyra contracts and state owners. No upstream directory tree,
CLI, sidecar, package, Docker image or source repository owns a core decision at runtime.

## 6. Source-to-target decisions

| Role | Source and pinned revision | Decision and target |
|---|---|---|
| Primary implementation | Claude Code `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`: SkillTool and compact/post-compact context chain | Cropped TypeScript control flow in `@zyra/skill-memory-runtime` plus 03C/E01/QueryEngine integration; current 03C owners are preserved |
| Supplementary implementation | Hermes `44ddc552f5e054759a6970af8997ea588a9d81c9`: memory/procedure concepts | Bounded Python procedure model/store/miner/runtime consuming only validated 06B facts |
| Supplementary implementation | Oh My Pi `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`: compact modes/messages/snapcompact contract | Trigger, atomic cut and archive semantics in TypeScript; OMP session/SQLite/Mnemopi owners are not imported |
| Conformance/reference only | opencode and Agent Framework | Contract comparison only; no production migration quota, state owner or parallel runtime |
| Experimental | Snapcompact frame compression | Contract only, default off, no effectiveness claim |
| Forward excluded | OpenClaw | No read, migration, ledger row, dependency, path, test duty or runtime reference was introduced |

The seed ledger has three productized `M1-S06C-01` decisions. Owner accounting reports three
effective, main-path-tested entries across three sources, 17 distinct target paths, zero findings
and no conflicting/data-only targets. Owner-filtered strict audit has zero findings. The global
strict audit remains false because of protected historical entries (`928` findings, `164` errors,
`11` blockers at the audit run); this slice does not rewrite those historical facts.

## 7. Verification

Main worktree verification included:

```text
bun run typecheck
# all configured TypeScript workspaces passed

bun test skill-memory package + Claude skill-memory integration
# 7 passed, 0 failed, 51 assertions

bun test runtime.test.ts
# 10 passed

bun test e01/coordinator-cutover.behavior.test.ts + skills.test.ts
# 45 passed, 0 failed, 183 assertions

pytest reusable procedure unit/API
# 3 passed

pytest 06B foundation/API/integration adjacency
# 19 passed

pytest reachability/procedure/API/linecount targeted set
# 16 passed

source ledger sync --check
# aligned=true; decisions=3; owner=M1-S06C-01
```

The exact detached cleanroom at
`f4d079ef25f42b05ce3c686c8f6ce6727ce365c7` used `bun install --frozen-lockfile`. Python ran with
`-I`, inserted only detached package paths and asserted:

```text
ZYRA_MEMORY_ORIGIN=G:\agent-zoo\.tmp\cleanroom-M1-S06C-01-f4d079e\packages\memory\zyra_memory\__init__.py
```

Cleanroom results:

- full configured TypeScript typecheck: pass;
- Bun and Node code-worker builds: pass, 236 modules / 4.81 MB each;
- 06C + runtime + E01 tests: 60 passed;
- isolated 03C skills regression: 2 passed;
- isolated Python 06B/06C/reachability set: 25 passed in 78.86 seconds;
- ledger sync: aligned, three decisions;
- reachability: `3/3`, unreachable `0`, reasons `[[], [], []]`;
- forbidden runtime dependency scan: no matches;
- detached target remained clean at the exact SHA.

The temporary worktree and its generated dependencies/build outputs were removed after validation.

An exploratory protected 02D adjacency run remains red outside this diff:
`test_codeworker_context_compact_api_foundation.py` has five old metadata/trace assertions failing,
and `test_browser_message_state_compression_integration.py` has one provider-selection assertion
failing (four tests in those files pass). A diagnostic live request still reported
`canonical_runtime_owner=typescript`, `python_query_engine_fallback=false` and
`compact_restore_ok=true`. The missing `restore_integration_ok` /
`runtime_state_checkpoint_ok` metadata and browser assertion were not introduced by 06C code and
would require altering protected 02D contracts, so they are recorded for the M1-06 numeric-stage
aggregate rather than backfilled here. All current 06C behavior and its 02B/02D TypeScript
adjacency tests pass.

## 8. Effective line buckets

The acceptance count is conservative: additions only, nonblank/non-comment, and only connected
production code in the new skill-memory/procedure modules plus real API/E01/QueryEngine/03C glue.
It deliberately excludes the reachability auditor improvement, export-only glue and all tests.

- production internalization: **`8,186` effective** (`8,781` raw added);
- dedicated behavior/integration/reachability tests: `1,090` nonblank (`1,169` raw), excluded;
- source-ledger sync helper: `317` raw, excluded as ordinary slice bookkeeping;
- ledger seed/data: `469` added / `9` removed, excluded;
- manifests, lockfile, tsconfig and export-only `__init__` glue: excluded;
- review/evidence docs: excluded;
- generated, mock-only, fixture-only, source-pool, vendor-like, thin-adapter-only and unconnected
  sample code counted as production: `0`;
- slice minimum: `8,000`; conservative shortfall: `0`.

The repository linecount tool reports `raw_added=10,919`, `effective_added=10,430`,
`excluded_added=489`, `review_added=20`, `shortfall=0`. Its broader effective surface includes
tests and the audit helper, so it is supporting evidence only; the production-only `8,186` count
is the acceptance number.

## 9. Adversarial review findings corrected

- Safe-cut planning originally rejected a valid contiguous summarized middle span unless it began
  at message zero. It now permits a preserved system prefix while keeping the summarized span
  contiguous and tool-call/result atomic.
- Python emitted integral confidence values as `1.0`, while JavaScript canonical JSON serialized
  them as `1`, causing cross-language digest drift. Export digest normalization now matches the
  TypeScript projection contract.
- Tiny context windows could reserve all tokens for output. The runtime caps reserved output below
  the context window and keeps a positive compact threshold.
- API `outcome_ids` accepted a string and would iterate characters. It now rejects non-list input.
- Initial ledger events were generic labels and dynamic task routes/TypeScript exports were not
  statically discoverable. Declarations now match exact produced events and the reachability
  auditor verifies route manifests, TS emit sites and TS workspace exports.
- The first cleanroom target was superseded after the reachability correction. Only the final
  `f4d079e` detached run is completion evidence.

Blocking findings inside `M1-S06C-01`: `0`.

## 10. Residual scope and handoff

This slice incrementally strengthens the memory/context/trace requirements but does not claim final
competition closure. `M1-S06C-02` is the next entry and must reuse these exact owners to finish:

- full live BrowserWorker and cross-provider restore fidelity;
- 06A outcome/procedure retrieval composition;
- success/failure/revoked-version/allowed-tools tightening across resume;
- routing and recovery planner signal consumption;
- parent cumulative `15,000` production-line and end-to-end closeout.

The parent `M1-06C` therefore remains in progress. The M1-06 aggregate still owns broad sibling
regression and the final handling of protected 02D adjacency failures; milestone exit still owns
sealed live tasks, 2,000 canonical transitions, dynamic topology evaluation, real local/edge/cloud
dispatch and final submission evidence.
