# M1-06 Retrieval, Curator and Skill-Memory Numeric-Stage Review

- Date: `2026-07-21`
- Review level: numeric-stage aggregate under `docs/执行单元完成后通用审查任务书.md`
- User-requested acceptance scope: `M1-S06B-01` through `M1-S06C-02`
- Adjacent regression scope: completed parent `M1-06A`
- Common baseline: `9f3d04b9fa860980d86ad2aaa446bde1db26ad21`
- Last slice evidence commit: `da1611819588f343231b167ba3dbba9397f87701`
- Final review-fix target: `1406e577281bbdc828ee1a1ba4a99a679228bac3`
- Result: **PASS after remediation**
- Next entry: `M1-S07A-01`

## 1. Conclusion

The four requested slices pass after remediation. The review found and fixed durable recovery,
delivery fencing, SQLite lifecycle, dynamic reachability and evidence-accounting defects. The final
target preserves the declared canonical owners, has no parent-repository runtime dependency, and is
reproducible from a detached cleanroom with a frozen Bun lockfile.

The already completed `M1-06A` parent was not rewritten. Its prior parent review is reused, while
all 11 of its focused/adjacent Python files and both source-ledger synchronizers were rerun against
the final target. This closes the required `M1-06` numeric-stage aggregate without counting 06A
again toward the four requested slices' line minimum.

No unresolved blocker remains inside M1-06A/B/C. This is not a claim that the repository-wide
historical ledger or Python suite is globally green; those results are classified in section 8.

## 2. Commits and reviewed range

| Scope | Implementation/fix commits | Evidence commit |
| --- | --- | --- |
| M1-S06B-01 | `2efb0cb` | `b2cf431` |
| M1-S06B-02 | `16d032f` | `57a6ac8` |
| M1-S06C-01 | `ed4db58`, `f4d079e` | `92185ba` |
| M1-S06C-02 | `dec6086`, `a8d3b80` | `da16118` |
| Aggregate remediation | `cb3b624`, `0f86ab7`, `c69785b`, `1406e57` | this review's evidence commit |

The baseline is the accepted 06A parent-review boundary, not the baseline of the last 06C slice.
The reviewed aggregate diff is therefore `9f3d04b..1406e57`.

## 3. Fixed findings

| ID | Severity | Finding | Remediation and disconnect proof |
| --- | --- | --- | --- |
| M1-06-R01 | high | A retryable curator outcome-event failure persisted the projection but changed the integration run to terminal `FAILED`; automatic recovery could never resume it. Successful replay also failed to repair deterministic event evidence. | Retryable failures now preserve the durable non-terminal state and mark recovery metadata. Outcome events are replayed from both `PUBLISHED` resume and idempotent-success paths. The fail-first sink test proves recovery reaches `SUCCEEDED`; removing this behavior leaves the run stranded. |
| M1-06-R02 | high | The integration run was committed `SUCCEEDED` before its completion event. A transient sink failure could therefore hide a missing terminal causal event behind terminal state. | The deterministic completion event is emitted before terminal CAS. Failure leaves a resumable run; recovery emits exactly one completion event and then commits success. |
| M1-06-R03 | high | Browser skill-memory delivery was keyed only by projection and used `setdefault`: a released attempt could not retry under a new request, another request could seed context before conflict detection, and commit/release did not fully fence request ownership or an in-progress seed. | Added reservation state, pending-seed fencing, rollback on seed failure, worker-request checks on commit/release, and replacement of `RELEASED` attempts. The adversarial test proves conflict causes zero context mutation and release-then-retry applies once. |
| M1-06-R04 | high | `CuratorIntegrationStore.initialize()` used `sqlite3.Connection`'s context manager, which commits but does not close. Windows cleanroom cleanup found the canonical SQLite file still locked. Workspace reset also retained the new curator/procedure runtime after closing its event sidecar. | Initialization now uses the store-owned closing context. Workspace reset drops the bound curator/procedure runtime before sidecar close. Code-index adjacency and the new rebind test prove cleanup and a fresh live bridge. |
| M1-06-R05 | medium | The owner-filtered reachability audit reported every 06B entry unreachable because dynamic curator routes were absent from the literal manifest and the generic event scanner did not inspect curator producers or literal Python `event_type` fields. | Added the four real curator routes, generic Python literal event discovery, curator producer paths, and B01/B02 reachability tests. Final result is `2/2`, `2/2`, `3/3`, `3/3` reachable for B01/B02/C01/C02. |
| M1-06-R06 | low | B01 review evidence recorded `11,437/11,087/350`, while rerunning the strict tool at its exact commit returned `11,434/11,086/348`. | Corrected the Markdown and JSON evidence; the conservative `8,957` production count is unchanged. |
| M1-06-R07 | low | The newly added 06B/06C Python scope contained 31 unused imports. | Removed only those imports. The bounded slice Ruff scope is green and no effective behavior line was used to offset a minimum. |

## 4. Acceptance matrix

| Slice | Required behavior | Final evidence | Status |
| --- | --- | --- | --- |
| M1-S06B-01 | evidence/candidate/job lease, deterministic validation, CAS canonical commit, event/outbox/index path | real unit/API path; curator state machine; schedule/commit/recovery events | PASS |
| M1-S06B-02 | runtime-event ingress, immutable outcome/failure projection, downstream delivery, retrieval-context effect, crash recovery | fail-first event recovery, SQLite lifecycle cleanup, API/retrieval integration | PASS |
| M1-S06C-01 | skill outcome provenance, reusable procedure memory, compact safe-cut/archive/restore, default-off experimental lane | TypeScript runtime behavior and Python procedure/API tests | PASS |
| M1-S06C-02 | current-authority revalidation, 06A/06B bounded composition, CodeWorker/BrowserWorker delivery, fidelity/failure directives | TypeScript integration, real 03C coordinator path, Browser retry/fence tests | PASS |

## 5. State custody and main-path effects

| State domain | Canonical owner | Reviewed effect |
| --- | --- | --- |
| canonical memory | `SQLiteStore.memory_records` | only validated curator commit mutates it; model and projections cannot write it |
| curator evidence/candidate/job/outbox | `CuratorCandidateStore` | separate durable tables, lease/watermark/CAS fenced |
| curator outcome/delivery/context proof | `CuratorIntegrationStore` | resumable derived projection and consumer delivery; not a second memory owner |
| derived memory/code index | `MemoryIndexRuntime` / `CodeIndexRuntime` | rebuildable from canonical memory or committed workspace state |
| reusable procedure memory | `ReusableProcedureStore` | mined only from accepted outcome provenance; cannot activate a skill |
| skill authority/invocation | M1-S03C `SkillCoordinator` | historical outcome context is filtered by current authority |
| compact boundary | M1-S02D `ContextCompactionRuntime` | 06C supplies bounded archive/restore/fidelity mechanisms without taking compact ownership |
| CodeWorker/BrowserWorker context | existing worker context owners plus 06C delivery receipts | accepted projection changes the next provider context; request and checkpoint identity are fenced |
| runtime events | runtime event spine plus canonical event store | deterministic outcome/completion causality survives transient sink failure |

Default-path tests show semantic effects rather than only logs: curator decisions change canonical
memory and retrieval; compact restore changes subsequent context; release/retry and disable paths
change delivery behavior; failure/circuit directives remain explicit downstream contracts.

## 6. Source-to-target and internalization audit

All six M1-06 synchronizers pass against the explicit bundled seed:

| Owner | Decisions | Final reachability | Policy/readiness |
| --- | ---: | ---: | --- |
| M1-S06A-01 | 2 | adjacent regression reused | aligned |
| M1-S06A-02 | 2 | adjacent regression reused | aligned |
| M1-S06B-01 | 2 | 2 reachable, 0 unreachable | ready, 0 owner blocker |
| M1-S06B-02 | 2 | 2 reachable, 0 unreachable | ready, 0 owner blocker |
| M1-S06C-01 | 3 | 3 reachable, 0 unreachable | ready, 0 owner blocker |
| M1-S06C-02 | 3 | 3 reachable, 0 unreachable | ready, 0 owner blocker |

The retained sources are Hermes Agent and Oh My Pi for curator/procedure mechanisms, and
`claude-code-best`, Hermes Agent and Oh My Pi for skill-memory/compact mechanisms. Their selected
control flow is split into Zyra memory, worker, runtime, API, event, permission/authority and test
boundaries. No upstream CLI, package, service, image or parent-relative source path owns a core
decision. OpenClaw remains `excluded_forward_only`; eight changed matches are exclusion metadata in
the seed/synchronizers, not source reading or runtime use.

Disconnecting the curator integration breaks recovery/event and retrieval-context tests;
disconnecting the procedure runtime removes mined procedure context; disconnecting the 06C runtime
removes compact/restore projections while preserving 02D/03C baseline owners; disconnecting Browser
delivery fencing makes the new adversarial test fail.

## 7. Effective-line buckets

| Slice | Conservative production | Minimum | Strict tool raw/effective/excluded |
| --- | ---: | ---: | ---: |
| M1-S06B-01 | 8,957 | 8,500 | 11,434 / 11,086 / 348 |
| M1-S06B-02 | 7,386 | 6,500 | 9,450 / 9,114 / 336 |
| M1-S06C-01 | 8,186 | 8,000 | 10,919 / 10,430 / 489 |
| M1-S06C-02 | 7,209 | 7,000 | 9,663 / 9,116 / 547 |

Conservative B/C production is `31,738` against `30,000`. Parent B is `16,343` against `15,000`;
parent C is `15,395` against `15,000`. On the final aggregate target, the strict tool reports
`raw_added=41,755`, `effective_added=40,060`, `excluded_added=1,695`, `shortfall=0` against the
combined `30,000` gate. Review-fix tests, docs, ledger data, synchronizers and audit tooling are not
used to satisfy the conservative production counts; generated/vendor/mock/fixture content counts
as zero.

## 8. Verification and broad-regression disposition

### Exact-commit cleanroom

Detached target: `1406e577281bbdc828ee1a1ba4a99a679228bac3`.

| Verification | Result |
| --- | --- |
| frozen Bun 1.2.15 install | 16 packages, lockfile unchanged |
| isolated Python origins | `zyra_memory` and `zyra_workers` resolved inside the detached cleanroom |
| requested B/C Python scope | 35 passed, 2 subtests passed |
| adjacent 06A Python scope | 32 passed, 8 subtests passed |
| focused B/C TypeScript | 19 passed, 0 failed |
| full Claude runtime TypeScript | 1,262 passed, 0 failed, 1,489 assertions |
| TypeScript workspace typecheck | passed |
| Bun and Node builds | each bundled 243 modules, 5.0 MB |
| bounded new-slice Ruff scope | passed |
| Python compileall | passed with external pycache root |
| six source-ledger sync checks | passed: 2/2/2/2/3/3 decisions |
| B/C readiness and policy | all ready; zero owner blocker/error |
| B/C entry reachability | 10 reachable, 0 unreachable |
| forbidden parent runtime paths | 0 |
| new vendor/source-pool paths | 0 |
| cleanroom Git status | clean |

### Applicable broad Python regression

- A bare repository `pytest` is invalid because it recursively collects historical cleanrooms,
  `tmp`, cache and vendored tests; it stopped with 224 collection errors and is not evidence.
- `pytest tests --continue-on-collection-errors -q` exceeded the 30-minute aggregate budget and was
  stopped at 1,803.9 seconds without a trustworthy final summary.
- Six protected legacy Python facade files still fail collection. The unit suite with the four unit
  collection blockers excluded produced `308 passed, 52 failed`; the scenario suite produced
  `3 passed, 1 failed`. A selected integration run collected 231 tests and exposed the already
  registered API/ledger/facade failures. None of the B/C target tests failed.
- The seven old 02D/HTTP-SSE/Browser failures in four adjacent files were reproduced with the same
  identities at both current code and baseline `9f3d04b`. They require their protected owner fixes;
  reintroducing a duplicate Python compact/session owner would violate the current custody boundary.

These historical failures prevent a repository-wide Python-green claim, but do not block the
reviewed M1-06 implementation: the requested and adjacent semantic paths are green in cleanroom,
the reproduced failures predate the reviewed range, and no fallback was used to hide a disabled
06B/06C module.

The strict global bundled-seed audit remains historically blocked with `930` findings, `755`
warnings, `164` errors and `11` blockers. All ten B/C entries are nevertheless connected,
productized, reachable and owner-policy-clean. This report preserves the global debt instead of
rewriting it as a pass.

## 9. Competition evidence impact

| Requirement | This review advances | State change |
| --- | --- | --- |
| REQ-MEM-01 | canonical curator memory, retrieval-context effect, compact/restore fidelity and exact failure recovery | remains `partial` |
| REQ-COMM-01 | bounded structured procedure/memory context and artifact/reference preservation | remains `planned` pending quantitative comparison |
| REQ-FAULT-01 | retryable event recovery, delivery release/retry, continuity failure directives | remains `partial`; 07C/08 own execution/live evidence |
| REQ-TRACE-01 | deterministic curator outcome/completion events and provenance receipts | remains `planned` pending M2 UI/live evidence |
| SCORE-ALGO | curator consolidation plus safe-cut/restore/fidelity and default-off ablation mechanism | implementation advanced; no score claimed |
| SCORE-EFF | bounded context budgets and reference fallbacks | no metric gate closed |

No live multi-domain task, 2,000-transition run, sparse-topology comparison, real edge/cloud dispatch,
multi-provider freeze or final competition score is claimed by this numeric-stage review.

## 10. Remaining blockers and risks

### Unresolved blockers for this review

None.

### Non-blocking risks

- Repository-wide historical Python facade/API/ledger debt remains visible as described above.
- The global ledger audit is not green and must not be cited as repository-wide completion evidence.
- In-memory Browser delivery maps are bounded at checkpoint projection but remain process-local; M1-07C
  must consume the durable failure/restore contracts rather than infer recovery from process memory.
- Formal live/benchmark and score evidence remains owned by later M1/M2/M3 units.

## 11. Handoff

After the evidence commit and root execution-state update, the next authorized slice is
`M1-S07A-01 worker lifecycle/resource pool foundation`. The root
`docs/milestones/execution-state.yaml` is outside the Zyra Git repository and must be reported as a
separate workspace-state update.
