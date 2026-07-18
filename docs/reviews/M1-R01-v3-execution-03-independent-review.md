# M1-R01 Execution 03 Independent Review

- Review date: 2026-07-18
- Authority: `docs/remediations/M1-R01-Execution完成后通用独立审查任务书.md`
- Verified predecessor read from the state source: `f07fd239dd768f399a36329314da82e90ddce6a4`
- User-accepted E02 / effective E03 implementation baseline: `454a22d344d7a5413cf8d49c22bf609f85f9d7e4`
- Submitted evidence candidate: `da3b372c29a838be9816ffce5878d02c525e014b`
- Repaired implementation candidate: `62c514d14b720060479ae5f673c45a9b273bae52`
- Evidence root: `docs/reviews/evidence/M1-R01-v3/execution-03`
- Verdict: **PASS after repair and complete re-verification**

## 1. Review decision

The submitted candidate did not satisfy the independent-review contract. The first audit found material defects in behavior-test discovery, G0 source-to-target semantics, crash receipt reconciliation, lost-ACK correlation, built-health candidate binding and Python owner removal. Under the taskbook, those findings invalidate the submitted candidate instead of becoming non-blocking notes.

The review therefore returned E03 to implementation, repaired the defects, froze a new clean G0 candidate and repeated all candidate-bound gates. The final candidate passes the static gate with zero findings, kills all 40 frozen mutations, executes all 346 E03 cases, passes a source-free cleanroom, and passes an independent 32-byte CSPRNG probe against the built Node entry. No finding from the failed candidate is waived.

## 2. Findings and repairs

| ID | Finding on submitted candidate | Severity | Repair and retest |
| --- | --- | --- | --- |
| E03-R01 | The Windows Bun directory-form invocation executed one of eight E03 files, so the claimed full suite was not a full suite. | Blocker | Added an explicit discovery/individual-file runner. Candidate and cleanroom now execute eight of eight files, 346 passed, zero failed. |
| E03-R02 | The G0 target map assigned source symbols mechanically within a domain and used synthetic behavior IDs. It could not prove the required source symbol -> target symbol -> default callsite -> state effect -> success/failure chain. | Blocker | Replaced round-robin assignment with explicit symbol/path semantic routing, positive scoring, real behavior IDs, exact symbol/hash/range validation and 4 rejected conformance-only ranges. A reviewer-owned 24-row five-hop sample covers all five domains. |
| E03-R03 | Control and stdio rows were attributed to the task runtime entry instead of the built control port. | High | Control rows now bind to `CodeWorkerApplication.runAgentControlPort`; agent/task/team/isolation rows bind to `runTaskRuntime`. Both symbols are statically verified and exercised. |
| E03-R04 | The first physical-effect receipt was not durably journaled before CAS, so a kill after receipt and before commit could redispatch the effect. Existing state could also contaminate a new canonical snapshot. | Blocker | Added an independent effect journal, receipt-first persistence and semantic reconciliation by idempotency key/request digest across new process/lease/effect identities. Actual kill/restart probes preserve receipt identity and `completedAt` with dispatch count 1. |
| E03-R05 | Lost-ACK recovery did not prove exact request correlation/checksum restoration; ACK handling could mutate task state without the registry CAS path. | Blocker | Recovery now returns the exact request identity and committed checksum; ACK advances only request-journal state. Random and deterministic probes show replayed ACK, stable checksum and dispatch count 1. |
| E03-R06 | Built health did not expose E03 readiness or bind its result to the implementation candidate. | High | Built health now reports E03 default/task and control entrypoints, TypeScript owner, protocol readiness and the candidate environment binding. Cleanroom runs it against the exact candidate. |
| E03-R07 | The submitted Python cutover removed only 6,268 executable lines and retained additional logical-owner modules. | Blocker | Deleted 27 logical-owner files / 10,309 executable lines. Five narrow Python physical/transport files remain; the declaration-aware census reports zero logical-owner hits and the targeted Python port tests pass 3/3. |
| E03-R08 | Cleanroom cleanup left Windows worktree residue and did not consistently bind all health/probe commands to the candidate. | High | Cleanup validates the resolved `.tmp` target, removes detached worktree residue, and supplies `E03_IMPLEMENTATION_CANDIDATE`. Final cleanroom removed vendor roots and passed every required step. |
| E03-R09 | Worktree custody and `AgentControlHandler` had no direct end-to-end success/failure tests. | High | Added worktree prepare/receipt/commit/merge/conflict/cleanup integration and real status/wait/result/cancel/kill control-handler integration, including stale/unknown/tamper failures. |

## 3. Candidate and G0 integrity

The repaired G0 receipt was captured from a clean worktree. It contains 395 rows: 391 accepted adapted ranges totaling 6,928 executable source lines and 4 explicitly rejected conformance-only ranges. All accepted source blobs match their pinned upstream commit hash, declared line range and symbol. All target paths and symbols exist at the final candidate, hashes are finalized, semantic route score is positive, and the referenced success/failure tests exist.

The 391 accepted rows map to 60 distinct target symbols. No fallback route remains. The four rejected rows claim no production migration credit. Direct-transplant count is intentionally zero: every accepted mechanism is adapted to Zyra identity, revision, lease, journal, receipt, error and restore semantics.

The manual sample in `five-hop-sample.json` contains 24 accepted mappings: 5 agents, 5 tasks, 5 team/delivery, 4 isolation/worktree and 5 control mappings. It includes primary and supplementary sources, semantic scores from 3 through 11, dense source files, both default entrypoints, and actual success/failure cases. All 24 chains are valid; all four rejected samples remain outside production credit.

## 4. Runtime and state custody

The canonical logical writer is `typescript.E03AgentControlCoordinator`. Its protocol is `prepare -> effect -> receipt -> commit -> ack`. TypeScript owns agent definition/context/scope/memory, task identity/state/registry/execution, team mailbox/fanout/delivery, isolation request/merge/cleanup and structured control. Python is limited to typed physical/CAS receipts, workspace effects and event/artifact projection; it cannot advance E03 logical state or act as a fallback.

Two real built entrypoints are proven:

- `CodeWorkerApplication.runTaskRuntime` for task-runtime operations.
- `CodeWorkerApplication.runAgentControlPort` for NDJSON control and stdio routing.

Disabling the TypeScript owner returns `typescript_agent_control_disabled`, performs zero dispatches and does not attempt Python fallback. Stale writers return `stale_revision`. Disconnecting frozen target methods is not survivable: all 40 target-specific mutations are killed.

## 5. Crash, replay and recovery

The deterministic implementation probe and the reviewer-owned random probe both execute `dist/code-worker-node/main.js --e03-control`, not TypeScript source. The independent probe uses `randomBytes(32)` and a 64-hex-character nonce unknown to the implementation fixtures.

The random path creates and steers a task, restarts the process, compares exact checksums, rejects a stale writer, kills a process after the physical receipt and before CAS, reconciles the same receipt without re-dispatch, simulates commit-before-ACK loss and recovers the exact ACK, then disables the TypeScript owner. All assertions pass. The crash receipt and lost-ACK paths each report dispatch count 1.

## 6. Effective code and anti-inflation audit

| Bucket | Result | Decision |
| --- | ---: | --- |
| Final non-test TypeScript production | 74,677 SLOC | Pass |
| Changed TypeScript production | 74,518 SLOC | Pass |
| Effective E03 TypeScript tests | 7,655 SLOC | Pass |
| Behavior cases / explicit failure cases | 346 / 192 | Pass |
| Production / changed production files | 41 / 39 | Informational |
| Production and test near-clone pairs | 0 / 0 | Pass |
| Frozen Python logical-owner deletion | 10,309 SLOC / 27 files | Pass |
| Cumulative target-file union | 100,302 SLOC | Pass |
| Generated, data-as-code, docs, vendor/source-pool, mock/fixture, adapter-only credit | 0 | Pass |

The E03 production roots and `package.json`/`bun.lock` contain no root-source relative dependency, local `file:`/`link:` dependency, vendor runtime, source-pool or runtime-sources reference. Historical repository code outside the E03 candidate scope still contains source-ledger and legacy productization strings; they are not E03 dependencies and the cleanroom proves they are unnecessary for this path.

## 7. Verification commands

All commands below ran on candidate `62c514d14b720060479ae5f673c45a9b273bae52`:

| Command | Result |
| --- | --- |
| `bun scripts/remediation/verify_m1_r01_e03.ts --candidate 62c514d --output .../candidate-gate-result.json` | PASS, zero findings |
| `bun run typecheck:e03` | PASS |
| `bun scripts/remediation/run_m1_r01_e03_behavior.ts` | 346 passed, 0 failed, 8/8 files |
| `bun scripts/remediation/run_m1_r01_e03_mutations.ts --candidate 62c514d` | 40/40 killed |
| `bun scripts/remediation/cleanroom_m1_r01_e03.ts --candidate 62c514d` | PASS; frozen install, typecheck, build, behavior, built health, probes and forbidden-path checks |
| `bun scripts/remediation/probe_m1_r01_e03.ts all` | 6/6 probe modes passed |
| `node .tmp/e03-independent-review-random.mjs` | PASS with 32-byte CSPRNG nonce |
| `.venv/Scripts/python.exe -m pytest -q tests/unit/test_typescript_agent_durable_port.py` | 3 passed |

The unscoped repository-root `pytest -q` is not used as an E03 gate: it recursively discovers temporary/vendor trees and fails during unrelated collection. A separate E01/E02 adjacent TypeScript run produced 1,209 passes and 3 failures in the unchanged E01 default-loop adversarial path because `capabilities.e02.runtime` is absent; neither the failing test nor `capability-host.ts` changed in the E03 effective diff. Those observations are preserved as pre-existing aggregation risks and do not weaken any E03 gate. They must be handled at their protected owner or the scheduled aggregate review, not by rewriting completed E01/E02 history in this review.

## 8. Final verdict and next boundary

E03 satisfies G0 integrity, source custody, default reachability, state ownership, crash recovery, anti-inflation, Python cutover, mutation and cleanroom requirements. The final verdict is **PASS**.

After the review evidence commit is recorded, `verified_zyra_head` may advance to that evidence commit, `candidate_zyra_head` must be cleared, M1-R01 becomes `completed_after_independent_review`, and the next allowed slice is `docs/milestones/M1-runtime-memory-scheduler-fault/slice-05c-01-runtime-event-message-bus-foundation.md` (`M1-S05C-01`).

