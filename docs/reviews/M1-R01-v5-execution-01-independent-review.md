# M1-R01 V5 Execution-01 Independent Review

## Verdict

**FAIL**

Execution-01 is not eligible to advance to `complete`. Runtime behavior, cleanroom, source blob custody, Python deletion, mutation quality, and identity binding all pass, but the frozen candidate-gate/five-hop evidence and candidate scope have hard failures. Per the review contract, no implementation or evidence fix was attempted.

## Review identity

- Review date: `2026-07-16`
- Reviewer nonce: `9818fe1baa4faa311925e43721367ed99ce572672c4c4612785945daac0ab34acc35cf92c54c5e6fe0628c224914f19f73ee94d296718231ad2b7dee8532ebe5`
- Verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`
- Implementation diff baseline: `0cd21bff5e2d160476f2ce3cef766bf53aab1239`
- Implementation candidate `I`: `7e498446d9c42715f99f3fb9c5e36ffe68bc1a0c`
- Candidate evidence `E`: `7bf8777c6a61c62eee04ab2bb1d907765b737d9d`
- Independent-review target `A`: `c21f1ac0aa13531121f24a766178e9d576165f46`
- Binding metadata `M`: `fbf715833018a8d49a17f22171038c561d46ac9f`
- Claude source snapshot: `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`

## Findings

### 1. Critical: the frozen candidate gate fails its own five-hop contract

The gate profile declares `scripts/verify_m1_r01_manifests.ts verify --enforce-gates`, caps behavior tests at `2` per mapping, and treats five-hop completion as a hard gate. An independent LF checkout at exact `I`, with freshly generated probe/mutation evidence, exits `1` and reports only `242/290` complete mappings.

Failure causes overlap across the 48 incomplete mappings:

- `20` default-callsite invocation failures.
- `23` state-owner reachability failures.
- `19` mappings exceed the behavior-test maximum.
- The same `19` mappings fail body-level anchor/assertion validation.
- Target blob hashes, target symbols, and mutation links pass in the LF run.

The V5 strict verifier reports `290/290` because it checks target/callsite symbol presence and non-empty state fields, but it does not enforce the candidate gate's invocation, state-owner, test-count, or test-body requirements. Therefore the strict PASS cannot close the stronger frozen gate.

References:

- `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-gate-profile.json:130`
- `docs/reviews/evidence/M1-R01-v5/execution-01-independent-review/candidate-gate-lf-result.json`
- `docs/reviews/evidence/M1-R01-v5/execution-01-independent-review/five-hop-failure-summary.json`
- `docs/reviews/evidence/M1-R01-v5/execution-01-independent-review/strict-gate.json`

### 2. Critical: nonce-selected accepted mappings contain behaviorally false equivalences

The nonce-selected accepted sample produced six failures out of twelve. Representative hard mismatches:

- `e01-src-0232` maps Claude `partitionToolCalls` to `ToolExecutionSettlementRuntime.recordGatewayReceipt`. The source classifies calls by concurrency safety and forms serial/concurrent batches. The target only accepts/rejects a receipt for an already planned delegated call; its cited test verifies receipt ordering, not concurrency partitioning.
- `e01-src-0014` maps `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT` to `E01RuntimeCoordinator.decideQuery` and the turn-limit mutant. The target method handles abort, maximum turns, and empty input; it does not demonstrate output-token recovery.
- `e01-src-0032` maps remote `verifyApiKey` behavior to provider configuration. `configureProviderRuntime` registers, rotates, selects, and resolves a credential but performs no verification request.
- `e01-src-0310` maps `logEntryToHistoryEntry` conversion to checksum snapshot restore, while its cited test/mutation concerns provider observation and repeated tool IDs.

These are not naming differences or allowed adaptation. The target behavior/test/mutation does not kill disconnection of the accepted source behavior. The rejected nonce sample also contains three tautological reasons (`outside curated executable source boundary for E01`) that do not explain why in-domain provider behavior is excluded.

References:

- `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-target-custody-map.jsonl:25`
- `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-target-custody-map.jsonl:43`
- `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-target-custody-map.jsonl:241`
- `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-target-custody-map.jsonl:286`
- `packages/runtime/claude-runtime/src/tools/execution-settlement-runtime.ts:595`
- `packages/runtime/claude-runtime/src/e01/coordinator.ts:436`
- `packages/runtime/claude-runtime/src/e01/coordinator.ts:682`
- `packages/runtime/claude-runtime/src/session/history-runtime.ts:291`
- `packages/runtime/claude-runtime/test/e01/default-loop-adversarial.behavior.test.ts:496`
- `docs/reviews/evidence/M1-R01-v5/execution-01-independent-review/nonce-semantic-sample.json`

### 3. High: binding evidence `E` retains a stale candidate-gate PASS

The immutable evidence tree contains `candidate-gate-result.json` with `322/322` mappings and `44/44` mutations. The frozen V5 manifests contain `290` accepted mappings and `46` mutations. The file was not regenerated for the identity bound by `M`, and the fresh V5 command fails rather than reproducing the stale PASS.

References:

- `docs/reviews/evidence/M1-R01-v3/execution-01/candidate-gate-result.json:30`
- `docs/reviews/evidence/M1-R01-v3/execution-01/candidate-gate-result.json:34`
- `docs/reviews/evidence/M1-R01-v5/execution-01-independent-review/candidate-gate-lf-result.json`

### 4. High: `B -> I` includes production changes outside the declared candidate scope

The gate profile allows `apps/code-worker`, `packages/runtime/claude-runtime`, the Python owner paths, remediation scripts, the integration test, and review docs. Nevertheless `B -> I` modifies three production files under `packages/runtime/runtime-event-spine`, including an MCP recipient addition. These paths are neither candidate-scope paths nor checker-source paths.

Out-of-scope files:

- `packages/runtime/runtime-event-spine/src/contracts.ts`
- `packages/runtime/runtime-event-spine/src/event-catalog.ts`
- `packages/runtime/runtime-event-spine/src/normalizers.ts`

The small `agents/*` changes were reviewed separately and are compile/type corrections inside the allowed `packages/runtime/claude-runtime` boundary; they are not counted as this finding.

References:

- `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests/execution-01-gate-profile.json:5`
- `packages/runtime/runtime-event-spine/src/contracts.ts:87`
- `packages/runtime/runtime-event-spine/src/event-catalog.ts:51`
- `packages/runtime/runtime-event-spine/src/normalizers.ts:253`
- `docs/reviews/evidence/M1-R01-v5/execution-01-independent-review/reviewer-audit.json`

## Passing gates

| Gate | Independent result |
|---|---|
| Identity and ancestry | PASS: `V -> B -> I -> E -> A -> M`; `E` and `A` share a tree; `A -> M` is metadata-only. |
| Root manifest hashes | PASS: all five manifest hashes match the baseline receipt. |
| Raw source custody | PASS: 360 rows, 290 accepted, 70 rejected, 21 immutable source blobs, zero SHA/range/role failures. |
| Continuations and known false positives | PASS: 14 supplementary continuations with gaps; all nine known unmigrated symbols are rejected. |
| Accepted source size | PASS: 10,711 unique executable source lines. |
| Python cutover | PASS: 42 owner paths absent; 29,020 executable baseline lines; 35,151 physical Python lines deleted from `B -> I`. |
| Strict V5 verifier | PASS: 290/290 structural rows; effective changed TypeScript 30,841. |
| Exact-I cleanroom | PASS: `git archive`, empty extracted tree, Bun 1.2.15, frozen install, TypeScript 5.8.3, typecheck, build, 386/386 tests. |
| Exact-I health fail-closed | PASS: stale historical metadata yields `candidate_metadata_contract_mismatch`. |
| Binding-M health | PASS: Bun-built and Node v22.17.0 bundles report `ready_for_independent_review`, `I/E/A`, effective line count 30,841, and `complete=false`. |
| Runtime probes | PASS: TypeScript owner, write mutation, three forced owner processes, restart epochs 0/1/2, no replay, lost ACK idempotency, disable fail-closed. |
| Dependencies | PASS: zero forbidden source/vendor links and zero runtime symlinks. |
| Mutation gate | PASS: 46/46 applied, compiled, killed, frozen patch matched, and original bytes restored. |
| Candidate five-hop gate | **FAIL: 242/290 in LF exact-I clone.** |
| Nonce semantic audit | **FAIL: 6/12 accepted mappings are not behaviorally supported; 3/8 rejected reasons are insufficient.** |
| Candidate scope | **FAIL: three runtime-event-spine production paths are outside the declared scope.** |

## Command notes

The first `e01:toolchain` invocation in the temporary clone failed before emitting child receipts. The exact-I archive cleanroom had already passed all six commands; running those six commands directly passed, and a second wrapper invocation then passed with complete receipts. This transient harness event is preserved in `command-results.json` and is not used as a candidate failure.

The normal Windows checkout candidate gate also failed all target hashes because it hashes checkout bytes. A second detached-I clone with `core.autocrlf=false` removed that artifact and still failed intrinsically at `242/290`; the verdict relies on the LF result.

## Reviewer boundary

- No production code or tests were changed.
- No candidate evidence under `docs/reviews/evidence/M1-R01-v3/**` was changed.
- No root manifest or `docs/milestones/execution-state.yaml` was changed.
- E02 and E03 were not started.
- No completion-state transition is authorized by this verdict.
