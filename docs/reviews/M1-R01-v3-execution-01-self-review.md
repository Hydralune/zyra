# M1-R01 v3 Execution-01 Critical Self-Review

## Verdict

`READY_FOR_INDEPENDENT_REVIEW`, not verified complete.

The verified baseline remains `c34535a783e88f9481387ced89cba4fbc333dc74`. The implementation candidate reviewed here is `5a92745897c1a8737c1294e002e454dcdc996224`. The cleanroom evidence targets that exact implementation commit.

## Scope and custody

The default code-worker entry is `apps/code-worker/src/main.ts`. It constructs `ClaudeRuntimeCore`, which bootstraps `E01RuntimeCoordinator` before recording runtime events. TypeScript owns the query loop, journal, restore epoch, context compaction, provider prompt construction, durable request/attempt/chunk state, normalized response state, route lease/outcome, quota reservation/settlement, recovery planning, telemetry, tool protocol, and composite snapshot. No Python canonical-owner path from the frozen E01 baseline remains.

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
| The first independent v3 reachability pass found that several large provider owners were counted although their default-path use was only `inspect/snapshot/restore`. | Connected prompt, request, response, routing, and rate-limit owners to every real `model_request_prepared/frame/report` lifecycle; added request IDs to frames and real route/quota success/failure settlement. Explicitly excluded model, transport, and credential modules from both production line gates because they remain dormant. | Five complete provider activation contracts, durable success/failure assertions in the default `ClaudeRuntimeCore.run` test, and mutation `e01-mut-032-provider-lifecycle-custody`. |
| Pre-registering local and compatible routes weakened the existing fail-closed default-route test. | Restored the single Anthropic default policy and register local/compatible route and quota policy only when a real request for that provider arrives. | All 355 E01 behavior tests pass before mutation execution; disabling the default route still changes selection behavior. |
| An initial cleanroom location under `zyra/.tmp` could resolve the repository's parent `node_modules`. | Rejected that receipt and reran from a system-temp archive whose ancestors contain no `node_modules`, with empty `NODE_PATH`, isolated empty Bun cache, local dependency installation, and downstream stop-on-install-failure. | Final cleanroom receipt records repository isolation, local dependency creation, six passing commands, and cleanup. |

## Effective line buckets

| Bucket | Lines | Counted toward effective production/test threshold |
| --- | ---: | --- |
| Raw final non-test TypeScript physical SLOC | 38,622 | No, includes explicitly dormant modules |
| Explicitly dormant provider module SLOC | 3,000 | No |
| Semantically counted final non-test TypeScript SLOC | 35,622 | Yes |
| Exact changed TypeScript AST-unit SLOC after dormant exclusion | 28,009 | Intermediate only |
| Structural clone exclusion | 1,518 | No |
| Effective changed TypeScript SLOC | 26,491 | Yes |
| Exact behavior-test AST-unit SLOC | 8,106 | Intermediate only |
| Structural test clone exclusion | 7 | No |
| Effective behavior-test SLOC | 8,099 | Yes |
| Adapter-only SLOC | 0 | No |
| Generated/data/docs/source-pool/vendor-like SLOC | 0 | No |

The production threshold is 25,416 effective changed TypeScript lines and 28,000 final non-test TypeScript lines. The behavior-test threshold is 7,000 effective lines. Source manifests, JSONL crosswalks, evidence JSON, docs, deleted generic templates, source repository files, and `provider/model-runtime.ts`, `provider/transport-runtime.ts`, and `provider/credential-runtime.ts` are not counted. Those three modules remain production-shaped debt but have no E01 completion credit until their decisions affect the default path.

The line verifier now requires five provider activation contracts. For each counted provider owner it proves the default event callback reaches the lifecycle method, that the lifecycle invokes an operational owner method (`build`, `create`, `begin`, `decide`, or `reserve`), that the owner is present in the composite snapshot, and that the default `ClaudeRuntimeCore.run` test asserts the resulting state. Merely importing, inspecting, snapshotting, or restoring a module cannot satisfy this contract.

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
| Mutation | 32 declared, applied, compile-surviving, and killed; zero survived or invalid; restored hashes match. Mutation 32 disconnects the provider lifecycle from the default event path. |
| Frozen toolchain | Install, typecheck, build, 364 tests across 10 files, and built-entry health pass. |
| Cleanroom | Fresh repository-external system-temp `git archive` of `5a92745897c...`; no Git metadata, dependencies, build output, project cache, ancestor `node_modules`, or `NODE_PATH` at start; isolated frozen install creates local dependencies, then all commands pass. |

## Residual risks and non-claims

No real provider credential or public cloud endpoint was used in this E01 review. Deterministic HTTP responses cover non-retryable stop and retry-then-success control flow; real local/edge/cloud dispatch remains governed by the later milestone evidence gates. Provider model registration/execution, transport execution, and credential selection are deliberately not claimed or counted by E01 because `model-stream.ts` still owns fetch execution and runtime configuration still supplies credentials.

The built health response intentionally remains `candidate_pending_independent_review` and `complete: false`. This self-review does not authorize updating the verified head or unblocking Execution-02/03.

## Self-review conclusion

No blocking implementation finding remains after the corrections above. The candidate may proceed to an independent critical review against the exact candidate/evidence commit. E01 must remain incomplete if that review fails or reviews a different implementation tree.
