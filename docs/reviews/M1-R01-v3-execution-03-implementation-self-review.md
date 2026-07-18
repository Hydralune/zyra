# M1-R01 Execution 03 Implementation Self-Review

- Execution: `E03 AgentTool / task / team / isolation / control TypeScript final cutover`
- Verified predecessor: `f07fd239dd768f399a36329314da82e90ddce6a4`
- User-accepted E02 baseline and E03 implementation baseline: `454a22d344d7a5413cf8d49c22bf609f85f9d7e4`
- Frozen G0 implementation candidate: `50b248e24d22cd209a58ec0bd017a11339ab76ff`
- Final implementation candidate: `3b52a598194e4ac7c9fcdf506e18fc30ae0353e1`
- Evidence root: `docs/reviews/evidence/M1-R01-v3/execution-03`
- Checkpoint verdict: implementation complete, independent review required

This is an implementation-window self-review, not an independent PASS. It does not advance `verified_zyra_head`, close M1-R01, or authorize M1-S05C-01.

## 1. Scope and source-role custody

The frozen schema-v3 G0 corpus contains 391 accepted ranges and 6,928 executable source lines. The primary source is `claude-code-best` with 180 rows across 20 files. `OpenClaw` contributes 198 supplementary rows across 16 files and `opencode` contributes 13 supplementary rows across 2 files. All rows use bounded `adapted` migration into Zyra-owned modules; no source repository is a runtime dependency.

The final target map covers 391 mappings and 62 unique target symbols. No target receives more than the frozen maximum of 40 mappings, every target file hash is finalized against `3b52a598...`, and the verifier found no missing symbol or target-hash drift.

| Source mechanism | Zyra target boundary | Runtime responsibility |
| --- | --- | --- |
| Claude AgentTool, local/in-process task and teammate control | `packages/runtime/claude-runtime/src/agents/**`, `tasks/**`, `team/**` | Definition loading, context fork, run/resume, durable task lifecycle, mailbox, fanout, delivery and background supervision |
| Claude structured control and task result patterns | `control/**`, `e03/**` | Typed create/steer/cancel/kill/wait/status/result routing and transaction coordination |
| OpenClaw agent/team/session mechanisms | `agents/**`, `tasks/**`, `team/**`, `isolation/**` | Supplementary durable registry, ownership, retention, workspace request and merge mechanisms |
| opencode task/control mechanisms | `tasks/**`, `control/**` | Supplementary identity, routing and structured protocol behavior |

The implementation is not an upstream directory transplant. The mechanisms are split by Zyra state domain, expressed with Zyra E03 identities, revisions, leases, journals, checksums, receipts and errors, and reached through the code-worker entry.

## 2. Canonical state-custody map

| State domain | Canonical TypeScript owner | Durable/physical boundary | Forbidden duplicate owner |
| --- | --- | --- | --- |
| Agent definition and activation | `AgentDefinitionRegistry`, `AgentDefinitionLoader`, definition provenance/dependency runtime | Definition files and typed inventory snapshots | Python agent selection or activation policy |
| Context, scope and memory | `AgentContextFork`, `AgentScopeLattice`, `AgentMemoryRuntime` | Immutable context snapshots, deltas and access journal | Python context fork or scope decision |
| Task identity, lease and lifecycle | `TaskIdentityRuntime`, `DurableTaskRegistry`, `TaskStateMachine`, `TaskExecutor` | Stable task/session/run/lease identity and append-only journal | Python task state machine or resume decision |
| Background execution and resume | `AgentExecutionRuntime`, `AgentBackgroundSupervisor`, run-lease/incident runtimes | Host effect through `E03PhysicalPort`; durable transition receipts | Python subagent execution owner |
| Team, swarm and steering | `TeamMailbox`, `TeamSteeringQueue`, `TeamFanout`, `TeamDelivery`, delivery outbox | Durable message/delivery facts and effect receipts | Python fanout, delivery or routing decision |
| Worktree/remote isolation | `IsolationRequestRuntime`, workspace policy/path custody, `IsolationMergeRuntime`, worktree ledger | Physical workspace/host operation after TypeScript decision | Python isolation or merge policy owner |
| Structured control | `ControlSchema`, `StructuredControlRouter`, `AgentControlHandler`, `StructuredControlStdio` | Typed frames, route leases, session request journal and write queue | Python control command dispatcher |
| Cross-language transaction | `E03AgentControlCoordinator` | `FileE03PhysicalPort` or `HostE03PhysicalPort` only after prepare; receipt persisted before commit/ack | Python logical state advancement or fallback |

The retained Python files are transport, storage, transcript, receipt, budget and data-model boundaries. Seventeen frozen logical-owner files, totaling 6,268 executable lines, are deleted. The frozen retained-owner scan reports zero occurrences of all forbidden E03 logical symbols. Retained physical port code is excluded from production credit.

## 3. Default path and semantic effects

The built path begins at `apps/code-worker/src/main.ts`, enters `CodeWorkerApplication.runTaskRuntime`, and delegates E03 commands to `E03AgentControlCoordinator.execute`. The coordinator uses the exact transaction sequence `prepare -> effect -> receipt -> commit -> ack` and routes through the TypeScript-owned domain runtimes before a physical effect.

The live probe established all five required behaviors:

- Runtime origin: create dispatches exactly once from `typescript.E03AgentControlCoordinator`; Python logical ownership and fallback are both false.
- Write path: create commits revision 1, steer commits revision 2, and the steering message is in canonical task state.
- Restart restore: a new coordinator restores the same task identity and revision without another dispatch.
- Lost ACK: the effect remains single-dispatch, replay returns the committed result, and a stale revision is deterministically rejected.
- Disable: `ZYRA_DISABLE_E03_TYPESCRIPT_RUNTIME=1` fails closed with `typescript_agent_control_disabled`, zero dispatches and no Python fallback.

These are disconnect-to-fail proofs: disabling the TypeScript coordinator, registry revision fence, journal restore, outbox receipt, or route owner changes or fails externally observed behavior. The Python boundary cannot complete the same local-control operation independently.

## 4. Failure, crash and recovery coverage

The 340 behavior cases include 188 explicitly named failure/crash/recovery cases. They cover invalid definitions, scope escalation, budget exhaustion, duplicate identity, stale lease/revision, invalid lifecycle transitions, cancellation and kill cascades, late results, fanout branch failure, mailbox replay/retention, partial delivery, outbox recovery, isolation denial, merge conflict, cleanup failure, malformed frames, route-owner loss and restart restore.

The cross-language crash matrix exercises effect/receipt/commit separation, duplicate suppression, lost-ACK replay and stale-writer rejection. Resume rotates to a new attempt lease while retaining task/session/run lineage; historical receipts remain valid only when a durable resume boundary connects the leases. Delivery reconciliation permits one final delivery per attempt and preserves that same resume boundary.

## 5. Effective line accounting

The frozen verifier applies executable-line filtering and rejects near-clone production/test files. Manifests, scripts, evidence, generated/data content, external source, Python ports and docs receive no production credit.

| Bucket | Actual | E03 requirement | Credit decision |
| --- | ---: | ---: | --- |
| Final non-test TypeScript production | 74,642 | 34,000 | Pass |
| Effective changed TypeScript production | 74,483 | 31,890 | Pass |
| Production files / changed files | 41 / 39 | n/a | Informational |
| Production near-clone pairs | 0 | 0 | Pass |
| Effective E03 TypeScript tests | 7,318 | 7,000 frozen G0 | Pass |
| Behavior cases | 340 | 100 | Pass |
| Failure/crash cases | 188 | 35 | Pass |
| Test near-clone pairs | 0 | 0 | Pass |
| Target-specific mutations | 40 | 35 | Pass; 40/40 killed |
| Frozen Python logical-owner deletion | 6,268 / 17 files | 6,268 | Pass; no production credit |
| Retained Python logical-owner token hits | 0 | 0 | Pass; no production credit |
| Generated/data/vendor/source-pool production credit | 0 | 0 | Pass |
| Adapter-only production credit | 0 | 0 | Pass |
| Mock/fixture-only production credit | 0 | 0 | Pass |

`fixtures.ts` is a 357-physical-line shared executable test harness used by the behavior cases, not production and not an independent capability claim. The frozen verifier includes it in the 7,318 test metric and finds no clone pair. No fixture, manifest, ledger or evidence payload is counted toward the 74,483 production result.

## 6. Validation evidence

| Gate | Result |
| --- | --- |
| `typecheck:e03` | PASS |
| Nine E03 behavior files, run individually | PASS, 340/340 |
| Mutation runner | PASS, 40/40 killed; core ratio 1.0, other ratio 1.0 |
| Live runtime-origin/write-path/resume/lost-ACK/disable probes | PASS, five of five |
| Frozen candidate verifier | PASS on `3b52a598...`; findings `[]` |
| Exact-commit source-free cleanroom | PASS on `3b52a598...`; install, typecheck, build, behavior command, built health and probes all exit 0 |
| Forbidden root dependencies | PASS; `forbidden_exists=[]` |
| Python syntax for retained ports | PASS via `py_compile` |

The frozen cleanroom initially completed all six internal checks but its Windows teardown could not remove a non-empty worktree and returned exit 1. A scoped temporary Git wrapper accepted only the exact `.tmp/e03-cleanroom-<12 hex>` worktree removal, removed residue, pruned Git worktree metadata and returned the underlying Git result for every other command. The next unchanged frozen cleanroom run exited 0, reported `passed=true`, and the wrapper and temporary worktrees were removed. `git worktree list` then contained only the main worktree.

Bun 1.2.15's directory argument executed only the first node:test file in this environment. Therefore the cleanroom's frozen behavior command is not used as the sole 340-case proof: all nine files were separately run and totaled 340/340. A historical Python test, `tests/unit/test_subagent_commands_integration.py`, no longer collects because it imports the deleted logical-owner export `AgentContextMode`; this stale test is disclosed to the reviewer and is not silently represented as passing.

The frozen verifier successfully returned zero findings. Two later attempts made solely to recapture its stdout for an evidence file were stopped by outer command timeouts at 120 and 300 seconds. They produced no contrary finding and are recorded as rerun timeouts, not additional PASS results.

## 7. Self-critical findings and review attacks

The implementation window found and fixed resume-lease, delivery-reconciliation, deterministic-clock and retained-Python-token defects before freezing the candidate. No known E03 gate failure remains. A fresh reviewer should still attack:

- process loss after effect but before receipt and after commit but before ACK;
- replay under a new PID/epoch with forged digest, stale revision, old lease and changed parent lineage;
- one-final-delivery-per-attempt across failed and resumed attempts;
- fanout partial failure, mailbox redelivery and outbox projection after restart;
- worktree cleanup/merge conflict and remote-isolation owner loss;
- TypeScript disable and direct Python port invocation for any hidden local-control fallback;
- the verifier's cumulative accounting and the disclosed Bun directory-discovery limitation.

`verified_zyra_head` must remain `f07fd239dd768f399a36329314da82e90ddce6a4`. The candidate may only become verified after the general independent-review taskbook produces PASS and commits its evidence.
