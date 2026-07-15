# M1-R01 v3 Execution-01 Critical Self-Review

## Verdict

`READY_FOR_INDEPENDENT_REVIEW`, not verified complete.

The verified baseline remains `c34535a783e88f9481387ced89cba4fbc333dc74`. The implementation candidate reviewed here is `05bfad86398234035a062aa295f2055dcf06ac26`. The cleanroom evidence targets that exact implementation commit.

## Scope and custody

The default code-worker entry is `apps/code-worker/src/main.ts`. It constructs `ClaudeRuntimeCore`, which bootstraps `E01RuntimeCoordinator` before recording runtime events. TypeScript owns the query loop, journal, restore epoch, context compaction, provider request observation, recovery planning, telemetry, tool protocol, and composite snapshot. No Python canonical-owner path from the frozen E01 baseline remains.

The runtime does not require a root source repository, vendor runtime, legacy inspection sidecar, npm link, editable path, symlink, external process, local port, or Node strip-types behavior. The frozen runtime toolchain is Bun `1.2.15` and TypeScript `5.8.3`.

## Findings discovered and fixed during this review

| Finding | Correction | Behavioral proof |
| --- | --- | --- |
| The first v3 crosswalk still mapped 229 source ranges to only 8 broad targets and attached whole domain test files. | Replaced the domain fallback with per-source-symbol routes, explicit adaptation notes, immediate caller checks, default-entry edges, one or two exact test bodies, state observations, and killed mutation links. | 229/229 complete chains, 32 target symbols, maximum 43 mappings per consolidated target. |
| Exact-text clone filtering could still count renamed templates. | Added identifier/literal-normalized TypeScript AST structural hashes for production and behavior tests. Baseline structural units and repeated structural units are excluded. | 1,665 production lines and 7 test lines are conservatively excluded as structural clones. |
| `simulate_model_error` bypassed model request and recovery custody. | Routed simulated failure through `resolveModelTurns`, provider reports, canonical recovery planning, telemetry, and journal commit. | `runtime commits provider prompt usage and recovery state through default loop`. |
| The HTTP loop retried non-retryable errors without using the recovery owner. | `resolveModelTurns` now consumes `E01RuntimeCoordinator.decideProviderRecovery`; authentication failure stops after one request. | `runtime lets canonical recovery policy stop a non-retryable provider request`; mutation `e01-mut-031-recovery-planner-custody`. |
| A successful retry left its recovery context active. | Added `completeProviderRecovery`, which calls the same owner's `recordSuccess` after a retry succeeds. | `runtime clears canonical recovery state after a retry succeeds`. |
| Restored runs reused a provider request ID and collided with restored usage custody. | Provider request identity now includes the journal restart epoch. | `runtime restores its exact TypeScript snapshot`. |
| A generic stop rule overwrote `max_turns_exceeded`, allowing the loop to continue. | Stop-rule output only overrides a decision that is still accepted. | `runtime stops on max turns, abort and model error`. |

## Effective line buckets

| Bucket | Lines | Counted toward effective production/test threshold |
| --- | ---: | --- |
| Final non-test TypeScript physical SLOC | 38,065 | No, reported separately |
| Exact changed TypeScript AST-unit SLOC | 30,160 | Intermediate only |
| Structural clone exclusion | 1,665 | No |
| Effective changed TypeScript SLOC | 28,495 | Yes |
| Exact behavior-test AST-unit SLOC | 8,106 | Intermediate only |
| Structural test clone exclusion | 7 | No |
| Effective behavior-test SLOC | 8,099 | Yes |
| Adapter-only SLOC | 0 | No |
| Generated/data/docs/source-pool/vendor-like SLOC | 0 | No |

The production threshold is 25,416 effective changed TypeScript lines and 28,000 final non-test TypeScript lines. The behavior-test threshold is 7,000 effective lines. Source manifests, JSONL crosswalks, evidence JSON, docs, deleted generic templates, and source repository files are not counted.

## Source-to-target evidence

The frozen source manifest contains 229 accepted top-level source symbols across 15 `claude-code-best` files and 11,027 physical source-range lines. Each target mapping records:

1. Frozen source repository, commit, file, symbol, line range, and file hash.
2. Existing Zyra target file and TypeScript declaration symbol.
3. Immediate caller plus verified default-entry call edges from `ClaudeRuntimeCore.run`.
4. Composite snapshot owner and an observable durable state path.
5. One or two exact behavior-test bodies with target anchor and state assertions.
6. At least one declared mutation whose latest executable result is `KILLED`.

The crosswalk has 32 distinct target symbols. The largest consolidation is 43 source symbols into one target symbol, below the frozen maximum of 60. Each consolidation includes an explicit cropping or adaptation rationale; SDK-only presentation and credential-refresh wrappers are not claimed as migrated behavior.

## Runtime and recovery evidence

| Gate | Result |
| --- | --- |
| Runtime origin | Default built entry reports TypeScript owner and E01 v5 runtime. |
| Write path | `ClaudeRuntimeCore.run -> recordRuntimeEvent -> Journal.commit` changes revision and state digest. |
| Same-session resume | Revision 2 to 4, restart epoch 0 to 1, two new transition IDs, zero overlap. |
| Lost ACK | Duplicate retry does not change revision, commit count, or delivery timestamp. |
| Disable | `ZYRA_DISABLE_E01_TYPESCRIPT_RUNTIME=1` makes the default entry fail deterministically. |
| Dependency path | 104 runtime files scanned; no forbidden source path, link, or symlink. |
| Mutation | 31 declared, applied, compile-surviving, and killed; zero survived or invalid; restored hashes match. |
| Frozen toolchain | Install, typecheck, build, 364 tests across 10 files, and built-entry health pass. |
| Cleanroom | Fresh `git archive` of `05bfad863982...`; no Git metadata, dependencies, build output, or cache at start; all frozen commands pass. |

## Residual risks and non-claims

No real provider credential or public cloud endpoint was used in this E01 review. Deterministic HTTP responses cover non-retryable stop and retry-then-success control flow; real local/edge/cloud dispatch remains governed by the later milestone evidence gates.

The built health response intentionally remains `candidate_pending_independent_review` and `complete: false`. This self-review does not authorize updating the verified head or unblocking Execution-02/03.

## Self-review conclusion

No blocking implementation finding remains after the corrections above. The candidate may proceed to an independent critical review against the exact candidate/evidence commit. E01 must remain incomplete if that review fails or reviews a different implementation tree.
