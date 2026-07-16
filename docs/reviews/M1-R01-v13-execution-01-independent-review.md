# M1-R01 Execution-01 V13 completion re-audit

Review date: 2026-07-16  
Taskbook: `docs/remediations/M1-R01-Execution完成后通用独立审查任务书.md`  
Execution: `docs/remediations/M1-R01-claude-source-custody/execution-01-runtime-core-typescript-cutover.md`

## 1. Verdict

**FAIL**.

The V12 completion claim is contradicted by deterministic default-path recovery and concurrency findings. Counts are `P0=0`, `P1=3`, `P2=2`, `P3=0`. E01 must return to `ready_for_fix`; E02 and E03 must remain blocked.

This is an independent-review window. In accordance with the taskbook, no production code, tests, build scripts, manifests, or Execution-02/03 files were modified. The correction must produce a new E01 candidate followed by a fresh independent review.

## 2. Findings

### P1-01: the real stdio path loses in-flight TypeScript custody before `run.result`

- `packages/runtime/claude-runtime/src/e01/coordinator.ts:1424` performs prepare/effect/commit/project/ack inside the in-memory coordinator.
- `packages/workers/zyra_workers/typescript_claude_runtime.py:395` persists incoming runtime events only as event/projection records. The complete TypeScript snapshot is not persisted until the terminal `run.result` path at `typescript_claude_runtime.py:501`.
- `packages/runtime/claude-runtime/src/stdio.ts:314` adds `typescriptCapabilities` only to the terminal result snapshot. `PermissionedCapabilityHost` always constructs fresh enforcement and settlement runtimes at `capability-host.ts:45`; no default-path restore call consumes `restoredState.typescriptCapabilities`.
- Reviewer-owned dynamic probe persisted one completed enforcement batch, rebuilt `PermissionedCapabilityHost` with that snapshot under `restoredState.typescriptCapabilities`, and observed `persisted_batches=1`, `restored_batches=0`, and unchanged `restart_epoch=0`.
- The committed resume probe is not a counterexample. `scripts/remediation/probe_m1_r01_e01.ts:136` directly constructs `E01RuntimeCoordinator`, writes its snapshot before sleeping, and is killed only after the snapshot exists. It does not kill `apps/code-worker --stdio` between a real tool effect and durable checkpoint.
- The lost-ACK probe at `probe_m1_r01_e01.ts:333` only calls an in-memory `Journal` and projects the same outbox item twice. It does not execute a Python ToolExecutor, artifact/provider request, or other external effect.

Impact: a crash after a tool effect but before terminal `run.result` can restart from an older snapshot without the pending/receipt/fence state. Exact-resume and no-repeat-effect claims are therefore unproven and the current host implementation can lose the state required to prevent replay.

Required E01 correction: checkpoint pending/effect/receipt/commit state through the durable host before terminal result, restore it before any new transition, and replace the synthetic probes with default stdio/Python-host kill-point probes around a real effect.

### P1-02: `concurrent_read_only` is a label, not concurrent execution

- `packages/runtime/claude-runtime/src/stdio.ts:136` emits requests, then waits for each `tool.result` in request order.
- `packages/workers/zyra_workers/typescript_claude_runtime.py:415` synchronously executes one `tool.request` and returns its result before reading and executing the next request.
- `packages/runtime/claude-runtime/test/runtime.test.ts:201` asserts batch mode labels only. It does not assert overlapping execution, wall-clock concurrency, cancellation, timeout, deterministic late-result handling, or a late-result fence.

Impact: the mandatory Execution-01 concurrent-tool behavior at execution document line 204 is absent from the real cross-language path. Cancellation and late results cannot be handled as claimed because the host never has multiple in-flight executions.

Required E01 correction: add a batch or multiplexed protocol with bounded host execution, correlation-based demultiplexing, cancellation/timeout/late-result fencing, and deterministic result ordering; test it through the real stdio host.

### P1-03: E01 pre-implements the E02 permission canonical owner

- The Execution-01 scope explicitly excludes E02 permission canonical control and permits only a non-executing typed contract below 3%.
- `packages/runtime/claude-runtime/src/tools/permission-enforcement-runtime.ts:234` is a 703-line executing permission state machine with decision, delegation, settlement, snapshot, restore, and audit behavior.
- `packages/runtime/claude-runtime/src/capability-host.ts:97` evaluates permission, records it in settlement state, and at line 142 enforces the decision before gateway delegation. It repeatedly declares `canonical_permission_owner: "typescript"`.
- The frozen E01 target custody map credits five accepted mappings (`e01-src-0231`, `0233`, `0235`, `0236`, `0237`) to this permission-gated state. The strict gate also counts the runtime in E01 production, subject only to near-clone deductions.

Impact: this is not a passive typed boundary. It executes and persists the next Execution's permission control flow and receives E01 source/LOC credit, violating scope isolation. Because accepted source credit has zero margin, removing any out-of-scope accepted mapping also drops E01 below the exact `10,587` source-coverage floor until the manifests and candidate are legitimately re-frozen.

Required E01 correction: keep only the narrow tool-execution handoff needed by E01, remove E02 canonical permission custody and credit from E01, and re-freeze/review the affected mappings without implementing E02.

### P2-01: an active turn is restored as data but not as the active pointer

- `packages/runtime/claude-runtime/src/session.ts:68` initializes `activeTurn` to null.
- `RuntimeSession.restore` at `session.ts:151` restores active-status turn records but does not restore or reject the unique active turn as `activeTurn`.

Impact: after a crash, `beginTurn` can open another turn instead of resuming or deterministically resolving the unfinished turn. This is incompatible with exact recovery to the unfinished action boundary.

### P2-02: product health trusts editable JSON fields instead of verifying review evidence

- `packages/runtime/claude-runtime/src/stdio.ts:57` reads candidate metadata and strict-gate JSON, then accepts their PASS/identity fields and line count.
- It does not hash or validate the independent-review receipt, verify the review commit's content/ancestry, or bind the evidence file set before returning `complete:true`.

Impact: syntactically valid local metadata and strict-gate edits can self-assert completion without the independent evidence that V7 claims to bind. The health projection is not a fail-closed verifier of I/E/A/R evidence integrity.

## 3. Target identity

- Verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`
- E01 diff baseline: `0cd21bff5e2d160476f2ce3cef766bf53aab1239`
- Implementation candidate I: `567b14acfbbabcf59a82868d1c20b042f056a80c`
- Candidate evidence E: `70c0d97af385dca39927e9a9d4860020ef3886f7`
- Critical target A: `0fd1f452a28b5b74db096fc42da856368a2b2f01`
- Pre-review metadata M: `a9b1a60ed273eecc04bdf4db62d1973034f68502`
- V12 review R: `c1f8c8a3d32e3bc449b64d4f69974b548e92076b`
- V12 final metadata F / reviewed completion head: `2d439de289d071c0893f2c2680073dea20fef530`
- Worktree at freeze: clean
- Reviewer CSPRNG nonce: `4f41b263567a2aebf2d1ada7a1c21a0e4ecbf4fa815a05d38d0b2a0c5fc47483`

## 4. Scope isolation

The implementation diff contains an executing permission owner and E01 custody mappings for it. This is beyond the permitted typed-contract exception because it changes decisions, blocks delegation, owns state, restores snapshots, and receives production/source credit. No E02 or E03 file was opened for implementation and no next-Execution production change was made in this review.

## 5. Independent line lower bounds

The immutable V12 machine report states 32,778 effective changed TypeScript lines, 50,371 final production lines, 11,352 test lines, and 35,151 deleted Python lines. This re-audit does not credit the 703-line out-of-scope permission state machine, yielding conservative ceilings of 32,075 changed and 49,668 final production lines before any additional scope/clone deductions. The numeric production floors remain above threshold, but they cannot offset P1 failures.

Accepted source coverage is **less than 10,587** after excluding at least one of the five out-of-scope permission-custody mappings. With zero source-line margin, the source hard gate fails even though the previous machine artifact reports 10,587.

## 6. Source-language five-hop assessment

The five affected mappings end at an E02 permission-controlled state effect rather than an E01-only tool-execution owner. Their default callsite is real, but scope ownership is invalid. Therefore the previous `276/276` closure cannot be retained unchanged. A corrected candidate must re-freeze the affected source/target roles and rerun full closure; this review did not mutate the frozen manifests.

## 7. Runtime origin

The default product entry loads Zyra's built TypeScript package and no Python query-loop fallback was observed. Runtime origin itself is not the blocker. The blocker is that the real stdio/durable-host path does not persist or restore the newly claimed in-flight custody before terminal `run.result`.

## 8. Write-path census

- Query/session journal: TypeScript coordinator; only terminal snapshot is durably returned.
- Tool external effect: Python host or local TypeScript capability.
- Permission/enforcement/settlement: TypeScript `PermissionedCapabilityHost`; snapshot is returned under `typescriptCapabilities` but not restored.
- Durable event projection: Python receives runtime events, but those events do not carry a restorable coordinator/host checkpoint.

The combination leaves a gap between external effect and durable TypeScript commit/fence restoration. It is not a complete prepare/effect/receipt/commit/ack path.

## 9. Reviewer-owned probes

1. **Capability-host restore probe: FAIL.** One persisted enforcement batch became zero after reconstructing the real host with the persisted snapshot.
2. **Lost-ACK evidence audit: FAIL.** The shipped probe is an in-memory Journal method test and performs no real external effect.
3. **Cross-process resume evidence audit: FAIL.** The shipped worker writes its own snapshot before it is killed and does not exercise the default stdio/Python host.
4. **Concurrent-tool path audit: FAIL.** The stdio and Python loops serialize real tool execution.

The deterministic blockers made rerunning the full frozen install/build/422-test/cleanroom suite non-probative for this verdict. Those previously passed commands do not exercise the failing paths and cannot override them.

## 10. Test credibility sample

The permission mixed-batch tests cover allow/deny ordering, but not host reconstruction from `typescriptCapabilities`. Settlement restore is tested only by directly restoring a standalone settlement object. Runtime batch tests assert `executionMode` labels, not concurrent execution. Resume/lost-ACK scripts test isolated Journal/coordinator objects rather than the real default entry and external-effect boundary.

## 11. Upgrade triggers

Triggered: canonical owner transfer, restore/idempotency semantics, execution review contradicting the real default path, next-Execution scope pollution, and probes that do not cover their claimed boundary. The review expanded the write-path and probe audit to the stdio/Python host and E01 target-custody mappings. It did not run an unrelated full repository suite after deterministic P1 failures were established.

## 12. State transition

- E01: `completed_after_independent_review` -> `ready_for_fix`
- E02: `ready` -> `blocked`
- E03: remains `blocked`
- `verified_zyra_head`: remains `2d439de289d071c0893f2c2680073dea20fef530` under the workflow's FAIL head rule
- Failed implementation candidate: `567b14acfbbabcf59a82868d1c20b042f056a80c`
- Next entry: `docs/remediations/M1-R01-claude-source-custody/execution-01-runtime-core-typescript-cutover.md`

## 13. Review evidence commit

This report and `docs/reviews/evidence/M1-R01-v13/execution-01-independent-review/reviewer-evidence.json` are the only Zyra repository changes. The exact review commit is recorded in the root workspace `docs/milestones/execution-state.yaml`, which is outside the Zyra Git repository.
